"""Remembering manual corrections ("this is the right track" / "it's not this one").

Stored as JSON, keyed by the Spotify track (URI), so a correction applies to
every playlist containing that track and survives library reloads (library
tracks are identified by their file path).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .matcher import Matcher
from .models import MatchResult, Track

DEFAULT_PATH = Path.home() / ".compare2mix" / "decisions.json"


class DecisionStore:
    def __init__(self, path: str | os.PathLike = DEFAULT_PATH):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except ValueError:
                self._data = {}

    def get(self, spotify_key: str) -> dict:
        return self._data.get(spotify_key, {})

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def confirm(self, spotify_key: str, library_key: str) -> None:
        """The user says: this Spotify track is this library track."""
        with self._lock:
            d = self._data.setdefault(spotify_key, {})
            d["confirmed"] = library_key
            if library_key in d.get("rejected", []):
                d["rejected"].remove(library_key)
            self._save()

    def reject(self, spotify_key: str, library_key: str) -> None:
        """The user says: it is NOT this library track."""
        with self._lock:
            d = self._data.setdefault(spotify_key, {})
            if d.get("confirmed") == library_key:
                del d["confirmed"]
            rejected = d.setdefault("rejected", [])
            if library_key not in rejected:
                rejected.append(library_key)
            self._save()

    def reset(self, spotify_key: str) -> None:
        with self._lock:
            if self._data.pop(spotify_key, None) is not None:
                self._save()

    def resolve(self, matcher: Matcher, track: Track) -> MatchResult:
        """Match a track, taking the user's past decisions into account."""
        d = self.get(track.key)
        confirmed = d.get("confirmed")
        if confirmed:
            rb = matcher.get(confirmed)
            if rb is not None:
                return matcher.manual(track, rb)
        res = matcher.match(track, exclude=set(d.get("rejected", [])))
        if d.get("rejected"):
            n = len(d["rejected"])
            res.notes.append(f"{n} candidat{'s' if n > 1 else ''} écarté{'s' if n > 1 else ''} par toi")
        return res
