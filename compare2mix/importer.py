"""Finding purchased tracks in the Downloads folder and filing them on the
music folder (USB key), ready to be dragged into Rekordbox.

Flow:
  1. ``scan_folder`` lists finished audio files in the Downloads folder
     (top level and one sub-folder deep, e.g. an unzipped Bandcamp album).
  2. ``read_audio_track`` turns each file into a Track (tags, else file name).
  3. ``propose`` matches the files against the missing Spotify tracks.
  4. ``ImportLog.import_file`` renames it "ARTIST - TITLE", moves it into the
     music folder and remembers it.
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

from .matcher import FOUND_THRESHOLD, Matcher
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

# External drives (USB keys) are mounted under /Volumes on macOS.
VOLUMES_DIR = "/Volumes"
_is_mount = os.path.ismount  # replaced in tests


class VolumeUnavailable(OSError):
    """The USB key / external drive holding a folder is not plugged in."""


def volume_root(path: str | os.PathLike) -> str | None:
    """'/Volumes/MA CLE/Musique/a.mp3' -> '/Volumes/MA CLE' (None if not external)."""
    p = os.path.abspath(os.path.expanduser(str(path)))
    prefix = VOLUMES_DIR.rstrip("/") + "/"
    if not p.startswith(prefix):
        return None
    name = p[len(prefix):].split("/", 1)[0]
    return prefix + name if name else None


def is_available(path: str | os.PathLike) -> bool:
    """False when ``path`` lives on an external drive that is not mounted.

    Writing there anyway would create a fake folder on the internal disk and
    the real key would then be mounted as "NAME 1".
    """
    root = volume_root(path)
    return root is None or _is_mount(root)


def list_volumes() -> list[str]:
    """Mounted external drives (USB keys…), e.g. ['/Volumes/DJ KEY']."""
    try:
        names = sorted(os.listdir(VOLUMES_DIR))
    except OSError:
        return []
    out = []
    for n in names:
        p = os.path.join(VOLUMES_DIR, n)
        # Skip the startup disk, which macOS also lists here (as a symlink to /).
        if n.startswith(".") or os.path.islink(p) or not _is_mount(p):
            continue
        out.append(p)
    return out


def guess_music_dir(locations: list[str]) -> str | None:
    """Folder holding most of the collection (where new tracks probably belong)."""
    counts: dict[str, int] = {}
    for loc in locations:
        if loc:
            d = os.path.dirname(loc)
            counts[d] = counts.get(d, 0) + 1
    return max(counts, key=counts.get) if counts else None


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
def is_rekordbox_export(path: str | os.PathLike) -> bool:
    """True for a collection exported by Rekordbox (not our own import file)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(65536).decode("utf-8", "ignore")
    except OSError:
        return False
    if "<DJ_PLAYLISTS" not in head:
        return False
    m = re.search(r'<PRODUCT\b[^>]*\bName="([^"]*)"', head)
    return bool(m) and m.group(1).strip().lower() == "rekordbox"


PROCESSED_MARK = "(traité)"


def is_processed(path: str | os.PathLike) -> bool:
    """An export compare2mix already used (renamed "… (traité).xml")."""
    return PROCESSED_MARK in Path(path).stem


def mark_processed(path: str | os.PathLike) -> Path:
    """Rename an export to "<name> (traité).xml" so it is not read again."""
    path = Path(path)
    dest = _unique_destination(path.parent, f"{path.stem} {PROCESSED_MARK}{path.suffix}")
    path.rename(dest)
    return dest


def find_rekordbox_export(folder: str | os.PathLike,
                          min_age_s: float = MIN_AGE_S) -> Path | None:
    """Most recent finished Rekordbox XML export directly in ``folder``."""
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        return None
    now = time.time()
    candidates = []
    for p in folder.iterdir():
        if (p.suffix.lower() != ".xml" or p.name.startswith(".") or is_processed(p)
                or not p.is_file()):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_size and now - st.st_mtime >= min_age_s and is_rekordbox_export(p):
            candidates.append((st.st_mtime, p))
    return max(candidates)[1] if candidates else None


_TRACK_NUMBER_RE = re.compile(r"^\s*\d{1,3}\s*[-._)]\s*(?=\S)")
# ...and Bandcamp's "03 Title" (two digits + space), only used on the last
# part of "Artist - Album - 03 Title" so that "5 Mins Of Acid" stays intact.
_BANDCAMP_NUMBER_RE = re.compile(r"^\s*\d{2}\s+(?=\S)")


def track_from_filename(path: Path) -> Track:
    """Track from a file name, when the file has no usable tags.

    Stores name their files differently: "Artist - Title (Extended Mix)",
    "Title - Mix - Artist", Bandcamp's "Artist - Album - 03 Title"… Except for
    the unambiguous Bandcamp case, the whole name is kept as the title with no
    artist: the matcher then tries the artist at the start *and* at the end of
    the name (see matcher._library_variants) and keeps the best reading.
    """
    stem = _TRACK_NUMBER_RE.sub("", path.stem).replace("_", " ").strip()
    parts = [p.strip() for p in stem.split(" - ") if p.strip()]
    if len(parts) >= 3 and _BANDCAMP_NUMBER_RE.match(parts[-1]):
        title = _BANDCAMP_NUMBER_RE.sub("", parts[-1])
        return Track(title=title, artists=split_artists(parts[0]), location=str(path))
    return Track(title=" - ".join(parts) or stem, artists=[], location=str(path))


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


_FORMAT_NAMES = {".aif": "AIFF", ".aiff": "AIFF", ".m4a": "M4A", ".mp4": "M4A"}


def audio_info(path: str | os.PathLike) -> dict:
    """Technical details shown as tags in step 4:
    {"format": "WAV", "kbps": 2117, "khz": 44.1, "bits": 24} (missing ones are None).
    The bit depth only exists for uncompressed / lossless files (not MP3)."""
    path = Path(path)
    info = {"format": _FORMAT_NAMES.get(path.suffix.lower(), path.suffix.lstrip(".").upper()),
            "kbps": None, "khz": None, "bits": None}
    try:
        import mutagen  # type: ignore

        audio = mutagen.File(path)
    except Exception:
        return info
    i = getattr(audio, "info", None)
    if i is None:
        return info
    bitrate = getattr(i, "bitrate", None)
    rate = getattr(i, "sample_rate", None)
    bits = getattr(i, "bits_per_sample", None) or getattr(i, "sample_size", None)
    if bitrate:
        info["kbps"] = round(bitrate / 1000)
    if rate:
        info["khz"] = round(rate / 1000, 1)
    if bits and info["format"] not in ("MP3", "M4A", "AAC", "OGG"):
        info["bits"] = int(bits)
    return info


def write_tags(path: str | os.PathLike, genre: str | None = None,
               title: str | None = None, artists: list[str] | None = None,
               comment: str | None = None) -> None:
    """Write the genre and comment (replacing existing ones) and, when given,
    the title and artists into the file's tags, so Rekordbox shows them."""
    import mutagen  # type: ignore
    from mutagen.id3 import COMM, ID3, TCON, TIT2, TPE1  # type: ignore
    from mutagen.mp4 import MP4Tags  # type: ignore

    audio = mutagen.File(path)
    if audio is None:
        raise ValueError("format de fichier non reconnu")
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    artist = ", ".join(artists) if artists else None
    if isinstance(tags, ID3):  # MP3, WAV, AIFF
        for frame, value in ((TCON, genre), (TIT2, title), (TPE1, artist)):
            if value:
                tags.setall(frame.__name__, [frame(encoding=3, text=[value])])
        if comment:  # the comment Rekordbox shows: no description
            tags.setall("COMM", [COMM(encoding=3, lang="eng", desc="", text=[comment])])
    elif isinstance(tags, MP4Tags):  # M4A / AAC
        for k, value in (("\xa9gen", genre), ("\xa9nam", title), ("\xa9ART", artist),
                         ("\xa9cmt", comment)):
            if value:
                tags[k] = [value]
    else:  # FLAC / Ogg (Vorbis comments)
        for k, value in (("genre", genre), ("title", title), ("artist", artist),
                         ("comment", comment)):
            if value:
                tags[k] = [value]
    audio.save()


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


DURATION_TOLERANCE_S = 30


def durations_differ(a: float | None, b: float | None) -> bool:
    return bool(a and b and abs(a - b) > DURATION_TOLERANCE_S)


MIN_FULL_TRACK_S = 120


def _downgrade_if_duration_differs(r) -> None:
    """A downloaded file is never "sure" when its length differs from Spotify's
    (another version: extended, radio edit… or another track), nor when it
    lasts under 2 minutes (often a preview, not the full track)."""
    short = bool(r.match.duration_s) and r.match.duration_s < MIN_FULL_TRACK_S
    if short:
        r.notes.append("fichier de moins de 2 minutes : extrait ?")
    if short or durations_differ(r.match.duration_s, r.spotify.duration_s):
        r.score = min(r.score, FOUND_THRESHOLD - 10)
        if r.status == Status.FOUND:
            r.status = Status.PROBABLE


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
            _downgrade_if_duration_differs(r)
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


_BAD_CHARS = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def target_name(file: Track, spotify: Track, suffix: str) -> str:
    """"ARTIST - TRACKNAME.ext" from the file's tags, or from the Spotify
    track it was matched to when the file has no usable tags."""
    tagged = bool(file.artists)
    artists = list(file.artists if tagged else spotify.artists)
    title = (file.title if tagged else spotify.title).strip()
    if tagged and file.mix and file.mix.lower() not in title.lower():
        title = f"{title} ({file.mix})"
    name = f"{', '.join(artists)} - {title}" if artists else title
    name = re.sub(r"\s+", " ", _BAD_CHARS.sub(" ", name)).strip(" .")[:200]
    original = Path(file.location or "")
    # Already named that way (give or take case and spaces): keep it as is.
    if not name or " ".join(original.stem.split()).casefold() == name.casefold():
        return original.name or name + suffix
    return name + suffix


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
        """True when this very file in Downloads was already put in the music
        folder. A file re-downloaded under the same name (newer than the
        import), or whose copy is no longer there, is proposed again. The old
        "ignored" list is no longer used: the wizard has no ignore button."""
        try:
            mtime = os.path.getmtime(source)
        except OSError:
            return False
        for i in self.imported:
            if i.source != source or mtime > i.imported_at + 1:
                continue
            if not is_available(i.file) or os.path.exists(i.file):
                return True
        return False

    def import_file(self, file: Track, spotify: Track, dest_dir: str | os.PathLike,
                    move: bool = True) -> ImportedTrack:
        """Move (or copy) a downloaded file into the music folder and log it."""
        src = Path(file.location or "")
        if not src.is_file():
            raise FileNotFoundError(f"Fichier introuvable : {src}")
        dest_dir = Path(dest_dir).expanduser()
        if not is_available(dest_dir):
            raise VolumeUnavailable(
                f"La clé « {os.path.basename(volume_root(dest_dir) or '')} » n'est pas branchée."
            )
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = _unique_destination(dest_dir, target_name(file, spotify, src.suffix.lower()))
        size = src.stat().st_size
        if move:
            shutil.move(str(src), dest)
        else:
            shutil.copy2(src, dest)
        # Never report success without the file really being there, complete.
        if not dest.is_file() or dest.stat().st_size != size:
            raise OSError(f"Le fichier n'a pas été écrit correctement dans {dest}.")
        # A file without usable tags only has its file name: describe it with
        # the Spotify track it was matched to, so Rekordbox shows a clean title.
        tagged = bool(file.artists)
        entry = ImportedTrack(
            spotify_key=spotify.key, spotify_display=spotify.display, source=str(src),
            file=str(dest),
            title=file.title if tagged else spotify.title,
            artists=list(file.artists if tagged else spotify.artists),
            duration_s=file.duration_s or spotify.duration_s,
            isrc=file.isrc or spotify.isrc,
            album=file.album or (None if tagged else spotify.album),
            mix=file.mix,
        )
        with self._lock:
            self.imported.append(entry)
            self._save()
        return entry

    def pending(self, collection_locations: set[str]) -> list[ImportedTrack]:
        """Imported files not yet present in the Rekordbox collection.

        Files on an unplugged USB key are kept (we cannot check them).
        """
        return [i for i in self.imported
                if i.file not in collection_locations
                and (not is_available(i.file) or Path(i.file).exists())]
