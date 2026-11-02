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

from . import config, rekordbox, spotify
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
                 spotify_factory: Callable[[str, str], spotify.SpotifyClient] | None = None):
        self.port = port
        self.config_path = config_path or config.CONFIG_FILE
        self.decisions = decisions or DecisionStore()
        self._spotify_factory = spotify_factory or (
            lambda cid, redirect: spotify.SpotifyClient(cid, redirect, interactive=False)
        )
        self.lock = threading.RLock()
        self.library_path: str | None = None
        self.library: list[Track] = []
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
        matcher = Matcher(tracks)
        with self.lock:
            self.library_path, self.library, self.matcher = path, tracks, matcher
            self.isrc_count, self.read_tags, self.loaded_at = isrc, read_tags, time.time()
        config.update(self.config_path, library_path=os.path.abspath(path), read_tags=read_tags)
        return self.library_status()

    def library_status(self) -> dict:
        return {
            "path": self.library_path,
            "count": len(self.library),
            "isrc": self.isrc_count,
            "read_tags": self.read_tags,
            "loaded_at": self.loaded_at,
        }

    def autoload(self) -> None:
        """Reload the last library (or data/rekordbox.xml) at startup."""
        s = self.settings
        path = s.get("library_path")
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
        results = [self.decisions.resolve(self.matcher, t) for t in entry["tracks"]]
        counts = {s.name.lower(): 0 for s in Status}
        for r in results:
            counts[r.status.name.lower()] += 1
        return {
            "id": pid,
            "name": entry["name"],
            "counts": counts,
            "results": [result_json(r, self.decisions.get(r.spotify.key)) for r in results],
        }

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
        r = self.decisions.resolve(self.matcher, track)
        return result_json(r, self.decisions.get(spotify_key))

    def search(self, query: str) -> list[dict]:
        return [track_json(t) for t in self.matcher.search(query, limit=12)]

    def export_csv(self, playlist_id: str) -> tuple[str, str]:
        pid = spotify.parse_playlist_id(playlist_id)
        entry = self._playlist(pid)
        results = [self.decisions.resolve(self.matcher, t) for t in entry["tracks"]]
        buf = io.StringIO()
        write_csv_to(results, buf)
        safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in entry["name"]).strip()
        return buf.getvalue(), f"compare2mix - {safe or pid}.csv"


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
                body = self._body()
                if path == "/api/library":
                    return self._json(app.load_library(body.get("path", ""),
                                                       bool(body.get("read_tags"))))
                if path == "/api/default-playlist":
                    app.set_default_playlist(body.get("playlist", ""))
                    return self._json({"default_playlist": app.settings.get("default_playlist")})
                if path == "/api/client-id":
                    app.set_client_id(body.get("client_id", ""))
                    return self._json(app.spotify_status())
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
    url = f"http://127.0.0.1:{port}/"
    print(f"compare2mix est ouvert sur {url}")
    print("Laisse cette fenêtre ouverte ; Ctrl+C pour arrêter.")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt.")
    finally:
        server.server_close()
