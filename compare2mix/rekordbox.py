"""Reading the Rekordbox library.

Two sources are supported:

1. **XML export** (recommended, no dependency):
   Rekordbox > File > Export Collection in xml format.
2. **master.db** (Rekordbox 6/7 encrypted SQLite database), through the
   optional ``pyrekordbox`` package: ``pip install "compare2mix[db]"``.
   Rekordbox should be closed while reading it.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from .models import Track
from .normalize import split_artists


def _location_to_path(location: str | None) -> str | None:
    """'file://localhost/Users/me/Music/a%20b.mp3' -> '/Users/me/Music/a b.mp3'."""
    if not location:
        return None
    if location.startswith("file://"):
        parsed = urlparse(location)
        path = unquote(parsed.path)
        # Windows: file://localhost/C:/Music/...
        if len(path) > 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return path
    return unquote(location)


def _float(value: str | None) -> float | None:
    try:
        f = float(value) if value not in (None, "") else None
    except ValueError:
        return None
    return f if f else None


def load_xml(path: str | os.PathLike) -> list[Track]:
    """Load every track of the COLLECTION of a Rekordbox XML export."""
    root = ET.parse(path).getroot()
    collection = root.find("COLLECTION")
    if collection is None:
        raise ValueError(
            f"{path} ne ressemble pas à un export XML Rekordbox (pas de <COLLECTION>)."
        )
    tracks: list[Track] = []
    for el in collection.findall("TRACK"):
        a = el.attrib
        tracks.append(
            Track(
                title=a.get("Name", "").strip(),
                artists=split_artists(a.get("Artist")),
                duration_s=_float(a.get("TotalTime")),
                album=a.get("Album") or None,
                mix=a.get("Mix") or None,
                remixer=a.get("Remixer") or None,
                source_id=a.get("TrackID"),
                location=_location_to_path(a.get("Location")),
            )
        )
    return tracks


def load_xml_playlists(path: str | os.PathLike) -> dict[str, list[Track]]:
    """Playlists of the XML export: name -> tracks (in playlist order).

    Playlist entries reference the collection by TrackID (KeyType="0") or by
    Location (KeyType="1"). Nested folders are flattened; when two playlists
    share a name, the first one wins.
    """
    tracks = load_xml(path)
    by_id = {t.source_id: t for t in tracks}
    by_loc = {t.location: t for t in tracks if t.location}
    root = ET.parse(path).getroot()
    out: dict[str, list[Track]] = {}
    for node in root.iter("NODE"):
        if node.attrib.get("Type") != "1":
            continue
        name = node.attrib.get("Name", "")
        if name in out:
            continue
        by_location = node.attrib.get("KeyType") == "1"
        items = []
        for t in node.findall("TRACK"):
            key = t.attrib.get("Key", "")
            track = by_loc.get(_location_to_path(key)) if by_location else by_id.get(key)
            if track is not None:
                items.append(track)
        out[name] = items
    return out


def _path_to_location(path: str) -> str:
    """'/Users/me/a b.mp3' -> 'file://localhost/Users/me/a%20b.mp3' (Rekordbox style)."""
    return "file://localhost" + quote(os.path.abspath(path), safe="/:")


def write_import_xml(path: str | os.PathLike, playlist_name: str,
                     tracks: list[Track]) -> None:
    """Write a Rekordbox XML holding one playlist with ``tracks``.

    Meant to be set in Rekordbox as the "rekordbox xml" library, then imported
    with "Importer la liste de lecture". Tracks already in the collection are
    recognised by their file path and not duplicated.
    """
    root = ET.Element("DJ_PLAYLISTS", Version="1.0.0")
    ET.SubElement(root, "PRODUCT", Name="compare2mix", Version="0.1.0", Company="")
    collection = ET.SubElement(root, "COLLECTION", Entries=str(len(tracks)))
    playlists = ET.SubElement(root, "PLAYLISTS")
    root_node = ET.SubElement(playlists, "NODE", Type="0", Name="ROOT", Count="1")
    node = ET.SubElement(root_node, "NODE", Name=playlist_name, Type="1", KeyType="0",
                         Entries=str(len(tracks)))
    for i, t in enumerate(tracks, start=1):
        attrs = {
            "TrackID": str(i),
            "Name": t.title,
            "Artist": t.artist_str,
            "Location": _path_to_location(t.location or ""),
        }
        if t.album:
            attrs["Album"] = t.album
        if t.mix:
            attrs["Mix"] = t.mix
        if t.remixer:
            attrs["Remixer"] = t.remixer
        if t.duration_s:
            attrs["TotalTime"] = str(int(round(t.duration_s)))
        ET.SubElement(collection, "TRACK", **attrs)
        ET.SubElement(node, "TRACK", Key=str(i))
    ET.indent(root)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    ET.ElementTree(root).write(tmp, encoding="UTF-8", xml_declaration=True)
    tmp.replace(path)


def load_xml_playlist_names(path: str | os.PathLike) -> list[str]:
    """Names of the playlists contained in the XML (useful to filter later)."""
    root = ET.parse(path).getroot()
    return [
        n.attrib.get("Name", "")
        for n in root.iter("NODE")
        if n.attrib.get("Type") == "1"
    ]


def load_master_db(db_dir: str | None = None) -> list[Track]:
    """Load the library directly from master.db (requires pyrekordbox).

    Not testable without a real Rekordbox install: the XML export remains the
    reference source.
    """
    try:
        from pyrekordbox import Rekordbox6Database  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "La lecture de master.db nécessite pyrekordbox : "
            'pip install "compare2mix[db]"'
        ) from exc

    db = Rekordbox6Database(db_dir) if db_dir else Rekordbox6Database()
    tracks: list[Track] = []
    try:
        for c in db.get_content():
            tracks.append(
                Track(
                    title=(getattr(c, "Title", None) or "").strip(),
                    artists=split_artists(getattr(c, "ArtistName", None)),
                    duration_s=_float(str(getattr(c, "Length", "") or "")),
                    isrc=(getattr(c, "ISRC", None) or None),
                    album=getattr(c, "AlbumName", None) or None,
                    mix=getattr(c, "Subtitle", None) or None,
                    remixer=getattr(c, "RemixerName", None) or None,
                    source_id=str(getattr(c, "ID", "")),
                    location=getattr(c, "FolderPath", None) or None,
                )
            )
    finally:
        try:
            db.close()
        except Exception:  # pragma: no cover
            pass
    return tracks


def enrich_isrc_from_tags(tracks: list[Track]) -> int:
    """Read the ISRC from the audio files' tags (requires mutagen).

    Only works on the machine where the files are. Returns the number of
    tracks for which an ISRC was found.
    """
    try:
        import mutagen  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            'La lecture des tags nécessite mutagen : pip install "compare2mix[tags]"'
        ) from exc

    found = 0
    for t in tracks:
        if t.isrc or not t.location or not Path(t.location).is_file():
            continue
        try:
            audio = mutagen.File(t.location, easy=True)
        except Exception:
            continue
        if not audio or not audio.tags:
            continue
        isrc = None
        try:
            values = audio.tags.get("isrc")
            if values:
                isrc = values[0] if isinstance(values, list) else str(values)
        except Exception:
            pass
        if not isrc:  # MP4/AIFF: look for raw keys
            try:
                raw = mutagen.File(t.location)
                for key in ("TSRC", "----:com.apple.iTunes:ISRC"):
                    if raw is not None and raw.tags and key in raw.tags:
                        v = raw.tags[key]
                        v = v[0] if isinstance(v, list) else v
                        isrc = v.decode() if isinstance(v, bytes) else str(v)
                        break
            except Exception:
                pass
        if isrc:
            t.isrc = isrc.strip().upper().replace("-", "")
            found += 1
    return found
