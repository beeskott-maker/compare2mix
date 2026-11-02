"""Watching the Downloads folder for purchased tracks and preparing their
import into Rekordbox.

Flow:
  1. ``scan_folder`` lists finished audio files in the Downloads folder
     (top level and one sub-folder deep, e.g. an unzipped Bandcamp album).
  2. ``read_audio_track`` turns each file into a Track (tags, else file name).
  3. ``propose`` matches the files against the missing Spotify tracks.
  4. ``ImportLog.import_file`` moves a file into the music folder and
     remembers it; ``write_import_xml`` (rekordbox.py) then produces the XML
     that Rekordbox imports as a playlist.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .matcher import Matcher
from .models import MatchResult, Status, Track
from .normalize import split_artists

# Formats Rekordbox can play.
AUDIO_EXTENSIONS = {".mp3", ".wav", ".aif", ".aiff", ".flac", ".m4a", ".mp4", ".aac"}
# Files still being written by the browser are ignored.
PARTIAL_EXTENSIONS = {".crdownload", ".part", ".download", ".tmp", ".partial"}
MIN_AGE_S = 3.0

DEFAULT_DOWNLOADS = Path.home() / "Downloads"
DEFAULT_MUSIC_DIR = Path.home() / "Music" / "compare2mix"
LOG_FILE = Path.home() / ".compare2mix" / "imports.json"


# --------------------------------------------------------------------------
# Reading files
# --------------------------------------------------------------------------

def scan_folder(folder: str | os.PathLike, min_age_s: float = MIN_AGE_S) -> list[Path]:
    """Finished audio files in ``folder`` (and its direct sub-folders)."""
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        return []
    now = time.time()
    found: list[Path] = []

    def consider(p: Path) -> None:
        if p.name.startswith(".") or p.suffix.lower() not in AUDIO_EXTENSIONS:
            return
        try:
            st = p.stat()
        except OSError:
            return
        if st.st_size == 0 or now - st.st_mtime < min_age_s:
            return  # empty or still being written
        found.append(p)

    for entry in sorted(folder.iterdir()):
        if entry.suffix.lower() in PARTIAL_EXTENSIONS or entry.name.startswith("."):
            continue
        if entry.is_dir():
            try:
                for sub in sorted(entry.iterdir()):
                    if sub.is_file():
                        consider(sub)
            except OSError:
                continue
        elif entry.is_file():
            consider(entry)
    return found


# "01 - Title", "01. Title", "01_Title" (explicit separator)...
_TRACK_NUMBER_RE = re.compile(r"^\s*\d{1,3}\s*[-._)]\s*(?=\S)")
# ...and Bandcamp's "03 Title" (two digits + space), only used on the last
# part of "Artist - Album - 03 Title" so that "5 Mins Of Acid" stays intact.
_BANDCAMP_NUMBER_RE = re.compile(r"^\s*\d{2}\s+(?=\S)")


def track_from_filename(path: Path) -> Track:
    """Guess artist/title from a name like "Artist - Title (Extended Mix).mp3".

    Bandcamp names files "Artist - Album - 01 Title.mp3"; leading track
    numbers are removed.
    """
    stem = _TRACK_NUMBER_RE.sub("", path.stem).replace("_", " ").strip()
    parts = [p.strip() for p in stem.split(" - ") if p.strip()]
    if len(parts) >= 3:  # Bandcamp: artist - album - nn title
        artist = parts[0]
        title = _BANDCAMP_NUMBER_RE.sub("", _TRACK_NUMBER_RE.sub("", parts[-1]))
    elif len(parts) == 2:
        artist, title = parts
    else:
        artist, title = "", stem
    return Track(title=title, artists=split_artists(artist), location=str(path))


# Tag names per format: ID3 (MP3, WAV, AIFF), Vorbis comments (FLAC), MP4 (M4A).
_TAG_KEYS = {
    "title": ("TIT2", "title", "\xa9nam"),
    "artist": ("TPE1", "artist", "\xa9ART", "TPE2", "albumartist", "aART"),
    "album": ("TALB", "album", "\xa9alb"),
    "isrc": ("TSRC", "isrc", "----:com.apple.iTunes:ISRC"),
    "mix": ("TIT3", "version", "subtitle", "----:com.apple.iTunes:SUBTITLE"),
}


def _tag_value(tags, field_name: str) -> str | None:
    for key in _TAG_KEYS[field_name]:
        try:
            v = tags.get(key)
        except Exception:
            v = None
        if v is None:
            continue
        if hasattr(v, "text"):  # ID3 frame
            v = v.text
        if isinstance(v, (list, tuple)):
            v = v[0] if v else None
        if isinstance(v, (bytes, bytearray)):
            v = bytes(v).decode("utf-8", "replace")
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def read_audio_track(path: str | os.PathLike) -> Track:
    """Track from the file's tags (needs mutagen), falling back to its name."""
    path = Path(path)
    fallback = track_from_filename(path)
    try:
        import mutagen  # type: ignore
    except ImportError:  # pragma: no cover
        return fallback
    try:
        audio = mutagen.File(path)
    except Exception:
        audio = None
    if audio is None:
        return fallback
    tags = audio.tags or {}
    title = _tag_value(tags, "title")
    artist = _tag_value(tags, "artist")
    isrc = _tag_value(tags, "isrc")
    duration = getattr(getattr(audio, "info", None), "length", None)
    return Track(
        title=title or fallback.title,
        artists=split_artists(artist) if artist else fallback.artists,
        duration_s=float(duration) if duration else None,
        isrc=isrc.replace("-", "").upper() if isrc else None,
        album=_tag_value(tags, "album"),
        mix=_tag_value(tags, "mix"),
        location=str(path),
    )


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

@dataclass
class Proposal:
    file: Track
    spotify: Track
    result: MatchResult

    @property
    def status(self) -> Status:
        return self.result.status


def propose(files: list[Track], missing: list[Track]) -> list[Proposal]:
    """Pair downloaded files with missing Spotify tracks.

    Each file and each Spotify track is used at most once, best scores first.
    Only found/probable pairs are returned.
    """
    if not files or not missing:
        return []
    matcher = Matcher(files)
    candidates: list[Proposal] = []
    for sp in missing:
        r = matcher.match(sp)
        if r.match is not None and r.status != Status.MISSING:
            candidates.append(Proposal(r.match, sp, r))
    candidates.sort(key=lambda p: p.result.score, reverse=True)
    used_files: set[str] = set()
    used_tracks: set[str] = set()
    out = []
    for p in candidates:
        if p.file.key in used_files or p.spotify.key in used_tracks:
            continue
        used_files.add(p.file.key)
        used_tracks.add(p.spotify.key)
        out.append(p)
    return out


# --------------------------------------------------------------------------
# Import log
# --------------------------------------------------------------------------

@dataclass
class ImportedTrack:
    spotify_key: str
    spotify_display: str
    source: str  # original path in Downloads
    file: str  # path in the music folder
    title: str
    artists: list[str]
    duration_s: float | None = None
    isrc: str | None = None
    album: str | None = None
    mix: str | None = None
    imported_at: float = field(default_factory=time.time)

    def to_track(self) -> Track:
        return Track(title=self.title, artists=list(self.artists), duration_s=self.duration_s,
                     isrc=self.isrc, album=self.album, mix=self.mix, location=self.file)


def _unique_destination(dest_dir: Path, name: str) -> Path:
    dest = dest_dir / name
    stem, suffix = dest.stem, dest.suffix
    n = 2
    while dest.exists():
        dest = dest_dir / f"{stem} ({n}){suffix}"
        n += 1
    return dest


class ImportLog:
    """Files imported by compare2mix, and files the user chose to ignore."""

    def __init__(self, path: str | os.PathLike = LOG_FILE):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.imported: list[ImportedTrack] = []
        self.ignored: set[str] = set()
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.imported = [ImportedTrack(**d) for d in data.get("imported", [])]
                self.ignored = set(data.get("ignored", []))
            except (ValueError, TypeError):
                pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "imported": [asdict(i) for i in self.imported],
            "ignored": sorted(self.ignored),
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def ignore(self, source: str) -> None:
        with self._lock:
            self.ignored.add(source)
            self._save()

    def is_known(self, source: str) -> bool:
        return source in self.ignored or any(i.source == source for i in self.imported)

    def import_file(self, file: Track, spotify: Track, dest_dir: str | os.PathLike,
                    move: bool = True) -> ImportedTrack:
        """Move (or copy) a downloaded file into the music folder and log it."""
        src = Path(file.location or "")
        if not src.is_file():
            raise FileNotFoundError(f"Fichier introuvable : {src}")
        dest_dir = Path(dest_dir).expanduser()
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = _unique_destination(dest_dir, src.name)
        if move:
            shutil.move(str(src), dest)
        else:
            shutil.copy2(src, dest)
        entry = ImportedTrack(
            spotify_key=spotify.key, spotify_display=spotify.display, source=str(src),
            file=str(dest), title=file.title, artists=list(file.artists),
            duration_s=file.duration_s, isrc=file.isrc, album=file.album, mix=file.mix,
        )
        with self._lock:
            self.imported.append(entry)
            self._save()
        return entry

    def pending(self, collection_locations: set[str]) -> list[ImportedTrack]:
        """Imported files not yet present in the Rekordbox collection."""
        return [i for i in self.imported
                if i.file not in collection_locations and Path(i.file).exists()]
