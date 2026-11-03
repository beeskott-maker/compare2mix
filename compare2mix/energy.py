"""Energy (1-10) and mood of an audio file, analysed locally with Essentia.

Optional: needs ``pip install essentia-tensorflow`` (Python ≤ 3.14 on macOS 15+).
The models (~24 MB, from essentia.upf.edu, CC BY-NC-SA) are downloaded once
into ~/.compare2mix/models. Nothing leaves the computer.

Features, on the middle two minutes of the track:
  - arousal (calm → excited, 1-9) from the DEAM model on MusiCNN embeddings;
  - "party" and moods (aggressive, happy, relaxed, sad) from Discogs-EffNet;
  - loudness (RMS level).
They are combined into a 1-10 score, roughly like the energy levels DJs use
(warm-up ≈ 4-5, peak time ≈ 7-8).
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import threading
import urllib.request
from pathlib import Path

from .spotify import ssl_context

MODEL_DIR = Path.home() / ".compare2mix" / "models"
CACHE_FILE = Path.home() / ".compare2mix" / "energy.json"
_BASE = "https://essentia.upf.edu/models/"
MODELS = {
    "discogs-effnet-bs64-1.pb": "feature-extractors/discogs-effnet/",
    "msd-musicnn-1.pb": "feature-extractors/musicnn/",
    "deam-msd-musicnn-2.pb": "classification-heads/deam/",
    "mood_aggressive-discogs-effnet-1.pb": "classification-heads/mood_aggressive/",
    "mood_happy-discogs-effnet-1.pb": "classification-heads/mood_happy/",
    "mood_relaxed-discogs-effnet-1.pb": "classification-heads/mood_relaxed/",
    "mood_sad-discogs-effnet-1.pb": "classification-heads/mood_sad/",
    "mood_party-discogs-effnet-1.pb": "classification-heads/mood_party/",
}
# Index of the positive class in each head's output ("aggressive" comes first,
# the others list "non_…" first).
_POSITIVE = {"aggressive": 0, "happy": 0, "relaxed": 1, "sad": 1, "party": 1}
MOOD_WORDS = {"aggressive": "agressif", "happy": "joyeux", "relaxed": "planant", "sad": "sombre"}
SEGMENT_S = 120
SAMPLE_RATE = 16000


class EnergyUnavailable(Exception):
    pass


def available() -> bool:
    """Essentia installed? (checked without importing it: that loads TensorFlow)."""
    return importlib.util.find_spec("essentia") is not None


def _norm(x: float, lo: float, hi: float) -> float:
    return min(1.0, max(0.0, (x - lo) / (hi - lo)))


def energy_score(arousal: float, party: float, level_db: float) -> int:
    """1-10 from arousal (1-9 scale), party probability and RMS level (dBFS)."""
    raw = 0.5 * _norm(arousal, 3.0, 7.0) + 0.3 * party + 0.2 * _norm(level_db, -24.0, -8.0)
    return max(1, min(10, round(1 + 9 * raw)))


def mood_word(moods: dict[str, float], threshold: float = 0.6) -> str | None:
    """Dominant mood in French ("sombre", "planant"…), if clear enough."""
    name, p = max(((k, v) for k, v in moods.items() if k in MOOD_WORDS),
                  key=lambda kv: kv[1], default=(None, 0.0))
    return MOOD_WORDS[name] if name and p >= threshold else None


def comment_text(energy: int | None, mood: str | None) -> str | None:
    """What goes into the Comment tag, e.g. "E7 · sombre"."""
    parts = ([f"E{energy}"] if energy else []) + ([mood] if mood else [])
    return " · ".join(parts) or None


class Analyzer:
    def __init__(self, model_dir: str | os.PathLike = MODEL_DIR,
                 cache_path: str | os.PathLike = CACHE_FILE):
        self.model_dir = Path(model_dir)
        self.cache_path = Path(cache_path)
        self._lock = threading.Lock()
        self._algos = None
        try:
            self._cache: dict[str, dict] = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._cache = {}

    def _ensure_models(self) -> None:
        self.model_dir.mkdir(parents=True, exist_ok=True)
        for name, folder in MODELS.items():
            dest = self.model_dir / name
            if dest.exists():
                continue
            req = urllib.request.Request(_BASE + folder + name,
                                         headers={"User-Agent": "compare2mix/0.1"})
            try:
                with urllib.request.urlopen(req, timeout=120, context=ssl_context()) as r:
                    data = r.read()
            except OSError as e:
                raise EnergyUnavailable(f"Téléchargement des modèles d'analyse impossible : {e}") from e
            tmp = dest.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(dest)

    def _load(self):
        if self._algos is not None:
            return self._algos
        if not available():
            raise EnergyUnavailable("Analyse d'énergie indisponible : lance "
                                    "« pip install essentia-tensorflow » (Python 3.14 au plus).")
        self._ensure_models()
        os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
        import essentia  # type: ignore
        import essentia.standard as es  # type: ignore

        essentia.log.warningActive = False
        essentia.log.infoActive = False
        m = lambda name: str(self.model_dir / name)  # noqa: E731
        self._algos = {
            "es": es,
            "effnet": es.TensorflowPredictEffnetDiscogs(
                graphFilename=m("discogs-effnet-bs64-1.pb"), output="PartitionedCall:1"),
            "musicnn": es.TensorflowPredictMusiCNN(
                graphFilename=m("msd-musicnn-1.pb"), output="model/dense/BiasAdd"),
            "deam": es.TensorflowPredict2D(
                graphFilename=m("deam-msd-musicnn-2.pb"), output="model/Identity"),
            "heads": {k: es.TensorflowPredict2D(
                graphFilename=m(f"mood_{k}-discogs-effnet-1.pb"), output="model/Softmax")
                for k in _POSITIVE},
        }
        return self._algos

    def analyze(self, path: str | os.PathLike) -> dict:
        """{"energy": 7, "mood": "sombre", "arousal": 5.9, "party": 0.9, "level_db": -11.2}."""
        path = Path(path)
        st = path.stat()
        key = f"{path}|{st.st_size}|{int(st.st_mtime)}"
        if key in self._cache:
            return self._cache[key]
        with self._lock:  # one analysis at a time (CPU-heavy)
            a = self._load()
            import numpy as np  # comes with essentia

            audio = a["es"].MonoLoader(filename=str(path), sampleRate=SAMPLE_RATE,
                                       resampleQuality=4)()
            seg = SAMPLE_RATE * SEGMENT_S
            if len(audio) > seg:  # the middle: skips DJ-friendly intros and outros
                start = (len(audio) - seg) // 2
                audio = audio[start:start + seg]
            if len(audio) < SAMPLE_RATE * 5:
                raise EnergyUnavailable("Morceau trop court pour être analysé.")
            emb = a["effnet"](audio)
            moods = {k: float(np.mean(h(emb)[:, _POSITIVE[k]])) for k, h in a["heads"].items()}
            valence, arousal = (float(x) for x in np.mean(a["deam"](a["musicnn"](audio)), axis=0))
            level_db = 20 * math.log10(float(np.sqrt(np.mean(audio ** 2))) + 1e-9)
            info = {
                "energy": energy_score(arousal, moods["party"], level_db),
                "mood": mood_word(moods),
                "arousal": round(arousal, 2), "valence": round(valence, 2),
                "party": round(moods["party"], 2), "level_db": round(level_db, 1),
                "moods": {k: round(v, 2) for k, v in moods.items()},
            }
            self._cache[key] = info
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._cache, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.cache_path)
        return info
