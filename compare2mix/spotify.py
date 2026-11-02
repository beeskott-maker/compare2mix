"""Reading a Spotify playlist.

Two sources:

1. **Spotify Web API** (OAuth PKCE, no client secret needed).
   Since February 2026, apps in *Development Mode* (which is what you get when
   you create an app on developer.spotify.com):
     * require the app owner to have Spotify Premium;
     * can only read the contents of playlists the user *owns or
       collaborates on* (for others, export to CSV — see below);
     * must use ``GET /playlists/{id}/items`` (``/tracks`` is gone), and each
       entry exposes its track under ``item`` instead of ``track``.
2. **CSV export** (e.g. from Exportify), for any playlist.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import http.server
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from .models import Track
from .normalize import split_artists

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
DEFAULT_REDIRECT = "http://127.0.0.1:8888/callback"
SCOPES = "playlist-read-private playlist-read-collaborative"
TOKEN_FILE = Path.home() / ".compare2mix" / "spotify_token.json"


class SpotifyError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Parsing (pure functions, unit-tested)
# --------------------------------------------------------------------------

def parse_playlist_id(value: str) -> str:
    """Accept an ID, a spotify:playlist:… URI or an open.spotify.com URL."""
    value = value.strip()
    m = re.search(r"playlist[/:]([A-Za-z0-9]+)", value)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9]{10,}", value):
        return value
    raise ValueError(f"Identifiant de playlist Spotify invalide : {value!r}")


def track_from_api(obj: dict) -> Track | None:
    """Convert a Spotify track object into a Track (None for podcasts etc.)."""
    if not obj or obj.get("type", "track") != "track":
        return None
    duration = obj.get("duration_ms")
    return Track(
        title=obj.get("name") or "",
        artists=[a.get("name", "") for a in obj.get("artists") or [] if a.get("name")],
        duration_s=duration / 1000 if duration else None,
        isrc=((obj.get("external_ids") or {}).get("isrc") or None),
        album=(obj.get("album") or {}).get("name"),
        source_id=obj.get("uri") or obj.get("id"),
    )


def tracks_from_items_page(page: dict) -> list[Track]:
    """Extract tracks from a page of GET /playlists/{id}/items.

    Supports the 2026 shape (``item``) and the former one (``track``).
    """
    out = []
    for entry in page.get("items") or []:
        obj = entry.get("item") if "item" in entry else entry.get("track")
        t = track_from_api(obj)
        if t is not None:
            out.append(t)
    return out


_CSV_ALIASES = {
    "title": ["track name", "name", "title", "titre", "song"],
    "artists": ["artist name(s)", "artist names", "artists", "artist", "artiste", "artistes"],
    "duration_ms": ["duration (ms)", "duration_ms", "duration ms"],
    "duration": ["duration", "durée", "time"],
    "isrc": ["isrc"],
    "album": ["album name", "album"],
    "uri": ["track uri", "spotify uri", "uri", "spotify - id", "spotify id"],
}


def load_csv(path: str | os.PathLike) -> list[Track]:
    """Load a playlist exported as CSV (Exportify or similar)."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(fh, dialect=dialect)
        headers = {h.strip().lower(): h for h in reader.fieldnames or []}

        def col(key: str) -> str | None:
            for alias in _CSV_ALIASES[key]:
                if alias in headers:
                    return headers[alias]
            return None

        c_title, c_artists = col("title"), col("artists")
        if not c_title:
            raise ValueError(
                f"Colonne du titre introuvable dans {path} (colonnes : {list(headers)})"
            )
        c_ms, c_dur, c_isrc, c_album, c_uri = (
            col("duration_ms"), col("duration"), col("isrc"), col("album"), col("uri")
        )
        tracks = []
        for row in reader:
            title = (row.get(c_title) or "").strip()
            if not title:
                continue
            duration = None
            if c_ms and row.get(c_ms):
                try:
                    duration = float(row[c_ms]) / 1000
                except ValueError:
                    pass
            elif c_dur and row.get(c_dur):
                duration = _parse_duration(row[c_dur])
            artists_raw = (row.get(c_artists) or "") if c_artists else ""
            # Exportify separates artists with commas (or ';' in recent versions).
            artists = split_artists(artists_raw)
            tracks.append(
                Track(
                    title=title,
                    artists=artists,
                    duration_s=duration,
                    isrc=((row.get(c_isrc) or "").strip() or None) if c_isrc else None,
                    album=(row.get(c_album) or None) if c_album else None,
                    source_id=(row.get(c_uri) or None) if c_uri else None,
                )
            )
    return tracks


def _parse_duration(text: str) -> float | None:
    text = text.strip()
    try:
        if ":" in text:
            parts = [float(p) for p in text.split(":")]
            secs = 0.0
            for p in parts:
                secs = secs * 60 + p
            return secs
        return float(text)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# OAuth PKCE + HTTP
# --------------------------------------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise SpotifyError(f"Échec d'authentification Spotify ({e.code}) : {e.read().decode()}")


class SpotifyClient:
    def __init__(self, client_id: str, redirect_uri: str = DEFAULT_REDIRECT,
                 token_file: Path = TOKEN_FILE):
        if not client_id:
            raise SpotifyError(
                "Client ID Spotify manquant : passe --client-id ou définis SPOTIFY_CLIENT_ID."
            )
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.token_file = token_file
        self._token: dict | None = None

    # ---- token management ------------------------------------------------
    def _save(self, token: dict) -> None:
        token["expires_at"] = time.time() + int(token.get("expires_in", 3600)) - 60
        if self._token and "refresh_token" not in token:
            token["refresh_token"] = self._token.get("refresh_token")
        self._token = token
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps(token))
        try:
            os.chmod(self.token_file, 0o600)
        except OSError:
            pass

    def _load(self) -> dict | None:
        if self._token is None and self.token_file.exists():
            try:
                self._token = json.loads(self.token_file.read_text())
            except ValueError:
                self._token = None
        return self._token

    def access_token(self) -> str:
        tok = self._load()
        if tok and tok.get("expires_at", 0) > time.time():
            return tok["access_token"]
        if tok and tok.get("refresh_token"):
            try:
                self._save(_post_form(TOKEN_URL, {
                    "grant_type": "refresh_token",
                    "refresh_token": tok["refresh_token"],
                    "client_id": self.client_id,
                }))
                return self._token["access_token"]
            except SpotifyError:
                pass
        self.login()
        return self._token["access_token"]

    def login(self) -> None:
        """Interactive login: opens the browser and waits for the redirect."""
        verifier = _b64url(secrets.token_bytes(64))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        state = secrets.token_urlsafe(16)
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "scope": SCOPES,
            "state": state,
        }
        url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

        result: dict = {}
        redirect = urllib.parse.urlparse(self.redirect_uri)

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                result.update({k: v[0] for k, v in q.items()})
                ok = "code" in q and q.get("state", [""])[0] == state
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                msg = ("Connexion réussie, tu peux fermer cet onglet."
                       if ok else "Échec de la connexion Spotify.")
                self.wfile.write(f"<h2>compare2mix</h2><p>{msg}</p>".encode())

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer((redirect.hostname, redirect.port or 80), Handler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        print(f"Ouverture du navigateur pour la connexion Spotify…\n{url}")
        webbrowser.open(url)
        thread.join(timeout=300)
        server.server_close()

        if result.get("state") != state or "code" not in result:
            raise SpotifyError(f"Connexion Spotify annulée ou refusée : {result.get('error', 'délai dépassé')}")
        self._save(_post_form(TOKEN_URL, {
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "code_verifier": verifier,
        }))

    # ---- API calls -------------------------------------------------------
    def get(self, url: str, params: dict | None = None) -> dict:
        if not url.startswith("http"):
            url = f"{API}{url}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        for attempt in range(5):
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {self.access_token()}"}
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(int(e.headers.get("Retry-After", "2")) + 1)
                    continue
                if e.code == 401 and attempt == 0 and self._token:
                    self._token["expires_at"] = 0
                    continue
                body = e.read().decode(errors="replace")
                if e.code == 403:
                    raise SpotifyError(
                        "Accès refusé par Spotify (403). En mode développement, seules les "
                        "playlists dont tu es propriétaire ou collaborateur sont lisibles, et "
                        "le propriétaire de l'app doit avoir Spotify Premium. Pour une autre "
                        "playlist, exporte-la en CSV (ex. Exportify) et utilise --csv.\n"
                        f"Réponse : {body}"
                    )
                raise SpotifyError(f"Erreur API Spotify {e.code} : {body}")
        raise SpotifyError("Trop de requêtes (429), réessaie plus tard.")

    def playlist_name(self, playlist_id: str) -> str:
        return self.get(f"/playlists/{playlist_id}", {"fields": "name"}).get("name", playlist_id)

    def playlist_tracks(self, playlist: str) -> list[Track]:
        pid = parse_playlist_id(playlist)
        tracks: list[Track] = []
        url: str | None = f"{API}/playlists/{pid}/items"
        params: dict | None = {"limit": 50, "additional_types": "track"}
        while url:
            page = self.get(url, params)
            tracks.extend(tracks_from_items_page(page))
            url, params = page.get("next"), None
        return tracks

    def my_playlists(self) -> list[dict]:
        out: list[dict] = []
        url: str | None = f"{API}/me/playlists"
        params: dict | None = {"limit": 50}
        while url:
            page = self.get(url, params)
            for p in page.get("items") or []:
                if not p:
                    continue
                count = (p.get("items") or p.get("tracks") or {}).get("total")
                out.append({
                    "id": p.get("id"),
                    "name": p.get("name"),
                    "owner": (p.get("owner") or {}).get("display_name"),
                    "tracks": count,
                })
            url, params = page.get("next"), None
        return out
