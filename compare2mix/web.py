"""Local web interface: ``compare2mix web``.

A small HTTP server (standard library only) bound to 127.0.0.1, serving a
single page plus a JSON API. Nothing leaves the Mac except calls to Spotify.

Security: the server only answers requests addressed to 127.0.0.1/localhost
(protects against DNS rebinding) and every POST must carry the
``X-Compare2mix`` header, which another website cannot send without a CORS
preflight we never accept.
"""

from __future__ import annotations

import io
import json
from collections import Counter
import os
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from . import config, discogs, energy, importer, rekordbox, spotify
from .decisions import DecisionStore
from .matcher import Matcher
from .models import MatchResult, Status, Track
from .report import search_links, write_csv_to

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_PORT = 8888  # matches the Redirect URI registered in the Spotify app
DEFAULT_LIBRARY = Path("data") / "rekordbox.xml"


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "error"):
        super().__init__(message)
        self.status = status
        self.code = code


# --------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------

def _spotify_url(uri: str | None) -> str | None:
    if uri and uri.startswith("spotify:track:"):
        return f"https://open.spotify.com/track/{uri.rsplit(':', 1)[1]}"
    return None


def track_json(t: Track) -> dict:
    return {
        "key": t.key,
        "title": t.title,
        "artists": t.artist_str,
        "mix": t.mix or "",
        "display": t.display,
        "duration": round(t.duration_s) if t.duration_s else None,
        "location": t.location,
    }


def result_json(r: MatchResult, decision: dict) -> dict:
    sp = track_json(r.spotify)
    sp["url"] = _spotify_url(r.spotify.source_id)
    return {
        "key": r.spotify.key,
        "status": r.status.name.lower(),  # found / probable / missing
        "score": r.score,
        "method": r.method,
        "notes": r.notes,
        "spotify": sp,
        "match": track_json(r.match) if r.match else None,
        "alternatives": [dict(track_json(t), score=s) for t, s in r.alternatives],
        "links": search_links(r),
        "decided": bool(decision),
    }


# --------------------------------------------------------------------------
# Application state
# --------------------------------------------------------------------------

class App:
    def __init__(self, port: int = DEFAULT_PORT, config_path: Path | None = None,
                 decisions: DecisionStore | None = None,
                 spotify_factory: Callable[[str, str], spotify.SpotifyClient] | None = None,
                 imports: importer.ImportLog | None = None,
                 discogs_factory: Callable[[str | None], discogs.Discogs] | None = None,
                 analyzer: energy.Analyzer | None = None):
        self.port = port
        self.config_path = config_path or config.CONFIG_FILE
        self.decisions = decisions or DecisionStore()
        self._spotify_factory = spotify_factory or (
            lambda cid, redirect: spotify.SpotifyClient(cid, redirect, interactive=False)
        )
        self.imports = imports or importer.ImportLog()
        self._discogs_factory = discogs_factory or (lambda token: discogs.Discogs(token))
        self._discogs: discogs.Discogs | None = None
        self._discogs_token: str | None = None
        self.analyzer = analyzer or energy.Analyzer()
        self.on_quit: Callable[[], None] | None = None  # set by serve()
        self.lock = threading.RLock()
        self.scan_lock = threading.Lock()
        self.library_lock = threading.Lock()
        self.library_path: str | None = None
        # Tracks of the Rekordbox export, and its playlists (name -> tracks).
        self.collection: list[Track] = []
        # What we match against: the collection + files imported since the export.
        self.library: list[Track] = []
        self._file_cache: dict[tuple[str, float, int], Track] = {}
        self.isrc_count = 0
        self.read_tags = False
        self.loaded_at: float | None = None
        self.matcher = Matcher([])
        # playlist id -> {"name": str, "tracks": [Track]}
        self.playlists: dict[str, dict] = {}
        self._pending_logins: dict[str, str] = {}  # state -> PKCE verifier
        self._me: dict | None = None

    # ---- settings ----------------------------------------------------------
    @property
    def settings(self) -> dict:
        return config.load(self.config_path)

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}/callback"

    def client(self) -> spotify.SpotifyClient:
        cid = self.settings.get("client_id") or os.environ.get("SPOTIFY_CLIENT_ID", "")
        if not cid:
            raise ApiError("Client ID Spotify manquant.", 400, "no_client_id")
        return self._spotify_factory(cid, self.redirect_uri)

    # ---- library -------------------------------------------------------------
    def load_library(self, path: str, read_tags: bool = False) -> dict:
        path = os.path.expanduser(path.strip())
        if not os.path.isfile(path):
            raise ApiError(f"Fichier introuvable : {path}", 404)
        tracks = rekordbox.load_xml(path)
        isrc = rekordbox.enrich_isrc_from_tags(tracks) if read_tags else 0
        with self.lock:
            self.library_path, self.collection = path, tracks
            self.isrc_count, self.read_tags, self.loaded_at = isrc, read_tags, time.time()
            self._rebuild_library()
        config.update(self.config_path, library_path=os.path.abspath(path), read_tags=read_tags)
        return self.library_status()

    MAX_UPLOAD = 300 * 1024 * 1024

    def upload_library(self, data: bytes, filename: str, read_tags: bool = False) -> dict:
        """Library XML sent from the browser: keep a copy and load it."""
        if not data:
            raise ApiError("Fichier vide.")
        if "<DJ_PLAYLISTS" not in data[:65536].decode("utf-8", "ignore"):
            raise ApiError("Ce fichier n'est pas un export XML de Rekordbox "
                           "(Fichier → Exporter la collection au format xml).")
        dest = self.config_path.parent / "rekordbox-library.xml"
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)
        self.load_library(str(dest), read_tags)
        config.update(self.config_path, library_name=filename or "rekordbox.xml",
                      library_source_mtime=time.time(), library_origin="upload")
        return self.library_status()

    def check_downloads_for_library(self) -> str | None:
        """Load a Rekordbox export dropped in the Downloads folder.

        Each file (path + date + size) is loaded once; only an export newer
        than the current library is taken. Returns its name when loaded.
        """
        folder = self.settings.get("downloads_dir") or str(importer.DEFAULT_DOWNLOADS)
        found = importer.find_rekordbox_export(folder)
        if found is None:
            return None
        st = found.stat()
        signature = f"{found}|{st.st_mtime}|{st.st_size}"
        with self.library_lock:
            s = self.settings
            if s.get("library_source") == signature:
                return None  # already loaded
            if self.collection:
                baseline = s.get("library_source_mtime")
                if baseline is None and self.library_path and os.path.isfile(self.library_path):
                    baseline = os.path.getmtime(self.library_path)
                if st.st_mtime <= (baseline or 0):
                    return None  # not newer than the library already loaded
            self.upload_library(found.read_bytes(), found.name, bool(s.get("read_tags")))
            config.update(self.config_path, library_source=signature,
                          library_source_mtime=st.st_mtime, library_origin="downloads")
        return found.name

    def library_from_downloads(self) -> dict:
        """Step 2 → 3: read the Rekordbox export the user just put in Downloads.

        An export already loaded (or older than the current library) is not
        reloaded; with no library at all and nothing found, it is an error.
        """
        loaded = self.check_downloads_for_library()
        if not self.collection:
            folder = self.import_settings()["downloads_dir"]
            raise ApiError(f"Aucun export Rekordbox trouvé dans {folder}. Exporte ta collection "
                           "(Fichier → Exporter la collection au format xml) ou dépose le fichier.",
                           404, "no_export")
        return dict(self.library_status(), just_loaded=loaded)

    def _key_root(self) -> str | None:
        """The USB key: volume of the music folder, else where most of the collection is."""
        root = importer.volume_root(self.import_settings()["music_dir"])
        if root:
            return root
        roots = Counter(importer.volume_root(t.location) for t in self.collection if t.location)
        roots.pop(None, None)
        return roots.most_common(1)[0][0] if roots else None

    def key_stats(self) -> dict | None:
        """Tracks of the Rekordbox collection that are not on the USB key: files
        elsewhere (Mac, Apple Music links…) and, when the key is plugged in,
        files that should be on it but are not."""
        root = self._key_root()
        if not root:
            return None
        mounted = importer.is_available(root)
        absent = 0
        for t in self.collection:
            loc = t.location
            if not loc or importer.volume_root(loc) != root:
                absent += 1
            elif mounted and not os.path.exists(loc):
                absent += 1
        return {"name": os.path.basename(root), "path": root, "mounted": mounted,
                "absent": absent, "on_key": len(self.collection) - absent}

    def _collection_locations(self) -> set[str]:
        return {t.location for t in self.collection if t.location}

    def _pending_imports(self) -> list[importer.ImportedTrack]:
        return self.imports.pending(self._collection_locations())

    def _rebuild_library(self) -> None:
        """Collection + imported files not yet in the export."""
        with self.lock:
            self.library = self.collection + [i.to_track() for i in self._pending_imports()]
            self.matcher = Matcher(self.library)

    def library_status(self) -> dict:
        path = self.library_path or self.settings.get("library_path")
        return {
            "path": path,
            "name": self.settings.get("library_name") or (os.path.basename(path) if path else None),
            "available": bool(path) and importer.is_available(path),
            "count": len(self.collection),
            "pending_import": len(self._pending_imports()),
            "origin": self.settings.get("library_origin"),
            "exported_at": self.settings.get("library_source_mtime"),
            "key": self.key_stats() if self.collection else None,
            "isrc": self.isrc_count,
            "read_tags": self.read_tags,
            "loaded_at": self.loaded_at,
        }

    def autoload(self) -> None:
        """Reload the last library (or data/rekordbox.xml) at startup.

        When the last library lives on a USB key that is not plugged in, wait
        for the key instead of falling back to another
        file, which would also replace the remembered path.
        """
        s = self.settings
        path = s.get("library_path")
        if path and not importer.is_available(path):
            print(f"Bibliothèque sur une clé non branchée ({path}) : chargée dès que tu la brancheras.")
            return
        if not path or not os.path.isfile(path):
            path = str(DEFAULT_LIBRARY) if DEFAULT_LIBRARY.is_file() else None
        if path:
            try:
                self.load_library(path, bool(s.get("read_tags")))
            except Exception as e:  # pragma: no cover - reported in the UI
                print(f"Bibliothèque non chargée : {e}")

    # ---- spotify -------------------------------------------------------------
    def spotify_status(self) -> dict:
        s = self.settings
        cid = s.get("client_id") or os.environ.get("SPOTIFY_CLIENT_ID", "")
        out = {"client_id": bool(cid), "logged_in": False, "user": None,
               "redirect_uri": self.redirect_uri}
        if cid:
            client = self.client()
            out["logged_in"] = client.is_logged_in()
            if out["logged_in"]:
                try:
                    if self._me is None:
                        self._me = client.me()
                    out["user"] = self._me.get("display_name") or self._me.get("id")
                except spotify.NotLoggedIn:
                    out["logged_in"] = False
                except spotify.SpotifyError:
                    pass
        return out

    def set_default_playlist(self, playlist_id: str) -> None:
        config.update(self.config_path, default_playlist=spotify.parse_playlist_id(playlist_id))

    def set_client_id(self, client_id: str) -> None:
        client_id = client_id.strip()
        if not client_id:
            raise ApiError("Client ID vide.")
        config.update(self.config_path, client_id=client_id)

    def start_login(self) -> str:
        url, verifier, state = self.client().authorization_request()
        self._pending_logins[state] = verifier
        return url

    def finish_login(self, query: dict) -> None:
        state = query.get("state", "")
        verifier = self._pending_logins.pop(state, None)
        if "error" in query:
            raise ApiError(f"Spotify a refusé la connexion : {query['error']}")
        if not verifier or "code" not in query:
            raise ApiError("Réponse de connexion inattendue, recommence.")
        self.client().exchange_code(query["code"], verifier)
        self._me = None

    def logout(self) -> None:
        self.client().logout()
        self._me = None
        self.playlists.clear()

    def list_playlists(self) -> list[dict]:
        client = self.client()
        me = self._me or client.me()
        self._me = me
        out = []
        for p in client.my_playlists():
            p["readable"] = p.get("owner_id") == me.get("id") or p.get("collaborative")
            out.append(p)
        out.sort(key=lambda p: (not p["readable"], (p.get("name") or "").lower()))
        return out

    # ---- comparison ----------------------------------------------------------
    def _playlist(self, playlist_id: str, refresh: bool = False) -> dict:
        with self.lock:
            cached = self.playlists.get(playlist_id)
        if cached and not refresh:
            return cached
        client = self.client()
        entry = {
            "name": client.playlist_name(playlist_id),
            "tracks": client.playlist_tracks(playlist_id),
        }
        with self.lock:
            self.playlists[playlist_id] = entry
        return entry

    def compare(self, playlist_id: str, refresh: bool = False) -> dict:
        if not self.library:
            raise ApiError("Charge d'abord ta bibliothèque Rekordbox.", 400, "no_library")
        pid = spotify.parse_playlist_id(playlist_id)
        entry = self._playlist(pid, refresh)
        results = [self._resolve(t) for t in entry["tracks"]]
        counts = {s.name.lower(): 0 for s in Status}
        for r in results:
            counts[r.status.name.lower()] += 1
        return {
            "id": pid,
            "name": entry["name"],
            "counts": counts,
            "results": [result_json(r, self.decisions.get(r.spotify.key)) for r in results],
        }

    def _resolve(self, track: Track) -> MatchResult:
        r = self.decisions.resolve(self.matcher, track)
        if r.match is not None and r.match.location in self._imported_files():
            r.method = "import"
        return r

    def _imported_files(self) -> set[str]:
        return {i.file for i in self.imports.imported}

    def _find_spotify_track(self, key: str) -> Track:
        with self.lock:
            for entry in self.playlists.values():
                for t in entry["tracks"]:
                    if t.key == key:
                        return t
        raise ApiError("Morceau inconnu, relance la comparaison.", 404)

    def decide(self, spotify_key: str, action: str, library_key: str | None) -> dict:
        track = self._find_spotify_track(spotify_key)
        if action in ("confirm", "reject"):
            if not library_key or self.matcher.get(library_key) is None:
                raise ApiError("Morceau de bibliothèque inconnu.", 404)
            getattr(self.decisions, action)(spotify_key, library_key)
        elif action == "reset":
            self.decisions.reset(spotify_key)
        else:
            raise ApiError(f"Action inconnue : {action}")
        return result_json(self._resolve(track), self.decisions.get(spotify_key))

    # ---- downloads → Rekordbox ------------------------------------------------
    def import_settings(self) -> dict:
        s = self.settings
        music_dir = s.get("music_dir") or str(importer.DEFAULT_MUSIC_DIR)
        return {
            "downloads_dir": s.get("downloads_dir") or str(importer.DEFAULT_DOWNLOADS),
            "music_dir": music_dir,
            "music_dir_available": importer.is_available(music_dir),
            "music_volume": importer.volume_root(music_dir),
            "keep_original": bool(s.get("keep_original", False)),
            # The token itself stays on disk: only say whether there is one.
            "discogs_token_set": bool(s.get("discogs_token")),
            "suggested_music_dirs": self._suggested_music_dirs(),
        }

    def _suggested_music_dirs(self) -> list[str]:
        """Where the collection already lives (e.g. the USB key), then mounted keys."""
        out = []
        guess = importer.guess_music_dir([t.location for t in self.collection if t.location])
        if guess:
            out.append(guess)
        for v in importer.list_volumes():
            if v not in out:
                out.append(v)
        return out

    def set_import_settings(self, body: dict) -> dict:
        values = {}
        for key in ("downloads_dir", "music_dir"):
            if body.get(key) is not None:
                v = str(body[key]).strip()
                if not v:
                    raise ApiError("Valeur vide.")
                values[key] = os.path.expanduser(v)
        for key in ("keep_original",):
            if key in body:
                values[key] = bool(body[key])
        if body.get("discogs_token") is not None:  # "" = forget it
            values["discogs_token"] = str(body["discogs_token"]).strip()
        config.update(self.config_path, **values)
        return self.import_settings()

    def missing_tracks(self) -> list[Track]:
        """Missing tracks of every playlist compared during this session."""
        out: dict[str, Track] = {}
        with self.lock:
            entries = list(self.playlists.values())
        for entry in entries:
            for t in entry["tracks"]:
                if t.key not in out and self._resolve(t).status == Status.MISSING:
                    out[t.key] = t
        return list(out.values())

    def _read_file(self, path: Path) -> Track:
        st = path.stat()
        key = (str(path), st.st_mtime, st.st_size)
        if key not in self._file_cache:
            self._file_cache[key] = importer.read_audio_track(path)
        return self._file_cache[key]

    def _import(self, file: Track, sp: Track) -> importer.ImportedTrack:
        settings = self.import_settings()
        try:
            entry = self.imports.import_file(file, sp, settings["music_dir"],
                                             move=not settings["keep_original"])
        except importer.VolumeUnavailable as e:
            raise ApiError(f"{e} Branche-la puis réessaie.", 409, "volume") from e
        self.decisions.confirm(sp.key, Track(entry.title, entry.artists, location=entry.file).key)
        self._rebuild_library()
        return entry

    def recap(self, playlist_id: str) -> dict:
        """Step 4: audio files in Downloads vs. the playlist's missing tracks.

        Nothing is moved here: the user picks the files, then step 5 transfers
        them one by one (import_download), then calls transfer_done.
        """
        if not self.collection:
            raise ApiError("Charge d'abord ta bibliothèque Rekordbox.", 400, "no_library")
        pid = spotify.parse_playlist_id(playlist_id)
        entry = self._playlist(pid)
        missing = [t for t in entry["tracks"] if self._resolve(t).status == Status.MISSING]
        settings = self.import_settings()
        with self.scan_lock:
            paths = [p for p in importer.scan_folder(settings["downloads_dir"])
                     if not self.imports.is_known(str(p))]
            files = [self._read_file(p) for p in paths]
        proposals = importer.propose(files, missing)
        matched_files = {p.file.location for p in proposals}
        matched_tracks = {p.spotify.key for p in proposals}
        week_ago = time.time() - 7 * 86400
        unmatched = sorted(
            ({"source": str(p), "name": p.name} for p in paths
             if str(p) not in matched_files and p.exists() and p.stat().st_mtime > week_ago),
            key=lambda u: u["name"].lower())
        return {
            "playlist": entry["name"],
            "downloads_dir": settings["downloads_dir"],
            "missing": len(missing),
            "matches": [{
                "source": p.file.location,
                "name": Path(p.file.location).name,
                "audio": importer.audio_info(p.file.location),
                "new_name": importer.target_name(p.file, p.spotify,
                                                 Path(p.file.location).suffix.lower()),
                "spotify_key": p.spotify.key,
                "spotify": p.spotify.display,
                "spotify_artists": p.spotify.artist_str,
                "spotify_title": p.spotify.title,
                "status": p.status.name.lower(),  # found / probable
                "score": p.result.score,
                # The durations are shown under each name, only when they differ.
                "notes": [n for n in p.result.notes if not n.startswith("durée")],
                "durations": _durations_if_different(p.file.duration_s, p.spotify.duration_s),
            } for p in sorted(proposals, key=lambda p: -p.result.score)],
            "unmatched": unmatched[:30],
            "still_missing": [t.display for t in missing if t.key not in matched_tracks],
            "destination": {
                "music_dir": settings["music_dir"],
                "available": settings["music_dir_available"],
                "volume": settings["music_volume"],
                "keep_original": settings["keep_original"],
            },
        }

    def transfer_done(self) -> dict:
        """End of step 5: the Rekordbox export in Downloads has been used."""
        return {"export_archived": self.archive_downloads_export()}

    def archive_downloads_export(self) -> str | None:
        """Rename the Rekordbox export read from Downloads ("… (traité).xml") so
        it is not taken again; the library itself stays loaded (saved copy)."""
        with self.library_lock:
            s = self.settings
            if s.get("library_origin") != "downloads" or not s.get("library_source"):
                return None
            src = Path(s["library_source"].split("|")[0])
            if not src.is_file() or importer.is_processed(src):
                return None
            try:
                dest = importer.mark_processed(src)
            except OSError:
                return None
            config.update(self.config_path, library_source=f"{dest}|archived")
        return dest.name

    def _discogs_client(self) -> discogs.Discogs:
        token = self.settings.get("discogs_token") or None
        if self._discogs is None or token != self._discogs_token:
            self._discogs, self._discogs_token = self._discogs_factory(token), token
        return self._discogs

    def genre(self, spotify_key: str) -> dict:
        """Step 4: detailed genre of a missing track, from Discogs."""
        track = self._find_spotify_track(spotify_key)
        try:
            return self._discogs_client().genre_for(track)
        except discogs.DiscogsError as e:
            raise ApiError(str(e), 502, "discogs") from e

    def energy(self, source: str) -> dict:
        """Step 4: energy (1-10) and mood of a file in Downloads (local analysis)."""
        path = Path(source)
        downloads = Path(self.import_settings()["downloads_dir"]).expanduser().resolve()
        if not path.is_file() or downloads not in path.resolve().parents:
            raise ApiError("Fichier introuvable dans Téléchargements.", 404)
        if not energy.available():
            raise ApiError("Analyse d'énergie indisponible : installe Essentia "
                           "(pip install essentia-tensorflow).", 501, "no_essentia")
        try:
            return self.analyzer.analyze(path)
        except energy.EnergyUnavailable as e:
            raise ApiError(str(e), 502, "energy") from e
        except Exception as e:  # unreadable file, model error…
            raise ApiError(f"Analyse impossible : {e}", 500, "energy") from e

    def import_download(self, source: str, spotify_key: str, genre: str | None = None,
                        comment: str | None = None) -> dict:
        path = Path(source)
        if not path.is_file():
            raise ApiError(f"Fichier introuvable : {source}", 404)
        if self.imports.is_known(source):
            raise ApiError("Ce fichier a déjà été traité.")
        with self.scan_lock:
            file, sp = self._read_file(path), self._find_spotify_track(spotify_key)
            wanted = importer.target_name(file, sp, path.suffix.lower())
            entry = self._import(file, sp)
        # Genre chosen in step 4 (replaces the file's own); a file without tags
        # also gets the Spotify title and artists, so Rekordbox shows them.
        genre = (genre or "").strip() or None
        comment = (comment or "").strip() or None
        untagged = not file.artists
        tags_error = None
        if genre or comment or untagged:
            try:
                importer.write_tags(entry.file, genre=genre, comment=comment,
                                    title=entry.title if untagged else None,
                                    artists=entry.artists if untagged else None)
            except Exception as e:  # the file is filed anyway
                tags_error = f"tags non écrits ({e})"
        # "renamed": a file with the same name was already there, so "(2)" was added.
        return {"imported": entry.spotify_display, "file": entry.file,
                "renamed": Path(entry.file).name != wanted, "genre": genre,
                "comment": comment, "tags_error": tags_error}

    def search(self, query: str) -> list[dict]:
        return [track_json(t) for t in self.matcher.search(query, limit=12)]

    def export_csv(self, playlist_id: str) -> tuple[str, str]:
        pid = spotify.parse_playlist_id(playlist_id)
        entry = self._playlist(pid)
        results = [self._resolve(t) for t in entry["tracks"]]
        buf = io.StringIO()
        write_csv_to(results, buf)
        safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in entry["name"]).strip()
        return buf.getvalue(), f"compare2mix - {safe or pid}.csv"


def _mmss(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _durations_if_different(file_s: float | None, spotify_s: float | None) -> dict | None:
    """{"file": "9:46", "spotify": "4:26"} when both are known and differ."""
    if not importer.durations_differ(file_s, spotify_s):
        return None
    return {"file": _mmss(file_s), "spotify": _mmss(spotify_s)}


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

_ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "compare2mix"

        def log_message(self, fmt, *args):  # keep the terminal quiet
            pass

        # ---- helpers -------------------------------------------------------
        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            return host in _ALLOWED_HOSTS

        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, status: int = 200):
            self._send(status, json.dumps(data, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")

        def _redirect(self, location: str):
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length))
            except ValueError:
                raise ApiError("JSON invalide.")

        def _run(self, fn):
            try:
                fn()
            except ApiError as e:
                self._json({"error": str(e), "code": e.code}, e.status)
            except spotify.NotLoggedIn as e:
                self._json({"error": str(e), "code": "not_logged_in"}, 401)
            except spotify.SpotifyError as e:
                self._json({"error": str(e), "code": "spotify"}, 502)
            except (ValueError, FileNotFoundError) as e:
                self._json({"error": str(e), "code": "error"}, 400)
            except Exception as e:  # pragma: no cover
                traceback.print_exc()
                self._json({"error": f"Erreur interne : {e}", "code": "internal"}, 500)

        # ---- routes --------------------------------------------------------
        def do_GET(self):  # noqa: N802
            if not self._host_ok():
                return self._send(403, b"Forbidden", "text/plain")
            url = urllib.parse.urlparse(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            path = url.path

            if path in ("/", "/index.html"):
                body = (STATIC_DIR / "index.html").read_bytes()
                return self._send(200, body, "text/html; charset=utf-8")
            if path == "/callback":
                def cb():
                    try:
                        app.finish_login(q)
                        self._redirect("/?login=ok")
                    except (ApiError, spotify.SpotifyError) as e:
                        self._redirect("/?login_error=" + urllib.parse.quote(str(e)))
                return self._run(cb)
            if path == "/login":
                return self._run(lambda: self._redirect(app.start_login()))
            if path == "/api/status":
                return self._run(lambda: self._json({
                    "library": app.library_status(),
                    "spotify": app.spotify_status(),
                    "default_library": app.settings.get("library_path") or str(DEFAULT_LIBRARY),
                    "default_playlist": app.settings.get("default_playlist"),
                }))
            if path == "/api/playlists":
                return self._run(lambda: self._json(app.list_playlists()))
            if path == "/api/recap":
                return self._run(lambda: self._json(app.recap(q.get("playlist", ""))))
            if path == "/api/import-settings":
                return self._run(lambda: self._json(app.import_settings()))
            if path == "/api/energy":
                return self._run(lambda: self._json(app.energy(q.get("source", ""))))
            if path == "/api/genre":
                return self._run(lambda: self._json(app.genre(q.get("spotify_key", ""))))
            if path == "/api/search":
                return self._run(lambda: self._json(app.search(q.get("q", ""))))
            if path == "/api/export.csv":
                def export():
                    text, filename = app.export_csv(q.get("playlist", ""))
                    quoted = urllib.parse.quote(filename)
                    self._send(200, ("﻿" + text).encode("utf-8"), "text/csv; charset=utf-8",
                               {"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"})
                return self._run(export)
            self._send(404, b"Not found", "text/plain")

        def do_POST(self):  # noqa: N802
            if not self._host_ok() or self.headers.get("X-Compare2mix") != "1":
                return self._send(403, b"Forbidden", "text/plain")
            path = urllib.parse.urlparse(self.path).path

            def route():
                if path == "/api/library/upload":
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > app.MAX_UPLOAD:
                        raise ApiError("Fichier trop gros.", 413)
                    data = self.rfile.read(length)
                    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    name = urllib.parse.unquote(self.headers.get("X-Filename") or "")
                    return self._json(app.upload_library(data, name,
                                                         q.get("read_tags", ["0"])[0] == "1"))
                body = self._body()
                if path == "/api/library":
                    return self._json(app.load_library(body.get("path", ""),
                                                       bool(body.get("read_tags"))))
                if path == "/api/library/from-downloads":
                    return self._json(app.library_from_downloads())
                if path == "/api/transfer-done":
                    return self._json(app.transfer_done())
                if path == "/api/import":
                    return self._json(app.import_download(body.get("source", ""),
                                                          body.get("spotify_key", ""),
                                                          body.get("genre"),
                                                          body.get("comment")))
                if path == "/api/import-settings":
                    return self._json(app.set_import_settings(body))
                if path == "/api/default-playlist":
                    app.set_default_playlist(body.get("playlist", ""))
                    return self._json({"default_playlist": app.settings.get("default_playlist")})
                if path == "/api/client-id":
                    app.set_client_id(body.get("client_id", ""))
                    return self._json(app.spotify_status())
                if path == "/api/quit":
                    if app.on_quit is None:
                        raise ApiError("Arrêt impossible depuis la page.", 400)
                    threading.Timer(0.3, app.on_quit).start()  # after the answer is sent
                    return self._json({"ok": True})
                if path == "/api/logout":
                    app.logout()
                    return self._json(app.spotify_status())
                if path == "/api/compare":
                    return self._json(app.compare(body.get("playlist", ""),
                                                  bool(body.get("refresh"))))
                if path == "/api/decision":
                    return self._json(app.decide(body.get("spotify_key", ""),
                                                 body.get("action", ""),
                                                 body.get("library_key")))
                raise ApiError("Route inconnue.", 404)

            self._run(route)

    return Handler


def serve(port: int = DEFAULT_PORT, open_browser: bool = True, library: str | None = None,
          read_tags: bool | None = None, app: App | None = None) -> None:
    app = app or App(port=port)
    if library:
        app.load_library(library, bool(read_tags))
    else:
        app.autoload()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    app.on_quit = server.shutdown  # "Quitter" button
    url = f"http://127.0.0.1:{port}/"
    print(f"compare2mix est ouvert sur {url}")
    print("Laisse cette fenêtre ouverte ; Ctrl+C (ou « Quitter » dans la page) pour arrêter.")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt.")
    finally:
        server.server_close()
