"""Genre (detailed style) of a track from Discogs.

Discogs classifies each release with broad genres ("Electronic") and detailed
styles ("Acid House", "Deep House", "Progressive Trance"…). We search the
releases containing the track and keep the styles that come up most often.

The search works without an account; a personal token (Discogs → Settings →
Developers → Generate new token) raises the rate limit from 25 to 60
requests per minute. Answers are cached in ~/.compare2mix/genres.json.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Callable

from .models import Track
from .normalize import parse_title
from .spotify import ssl_context

SEARCH_URL = "https://api.discogs.com/database/search"
USER_AGENT = "compare2mix/0.1 +https://github.com/beeskott-maker/compare2mix"
CACHE_FILE = Path.home() / ".compare2mix" / "genres.json"
MAX_STYLES = 2


class DiscogsError(Exception):
    pass


def pick_genre(results: list[dict], max_styles: int = MAX_STYLES) -> dict:
    """Most frequent styles over the releases found ("Acid House / Techno"),
    else the broad genre ("Electronic")."""
    styles: Counter[str] = Counter()
    genres: Counter[str] = Counter()
    order: dict[str, int] = {}
    for r in results:
        for s in r.get("style") or []:
            styles[s] += 1
            order.setdefault(s, len(order))
        for g in r.get("genre") or []:
            genres[g] += 1
            order.setdefault(g, len(order))
    rank = lambda c: sorted(c, key=lambda k: (-c[k], order[k]))  # noqa: E731
    top = rank(styles)[:max_styles] or rank(genres)[:1]
    return {"genre": " / ".join(top) or None, "styles": rank(styles), "releases": len(results)}


class Discogs:
    def __init__(self, token: str | None = None, cache_path: str | os.PathLike = CACHE_FILE,
                 fetch: Callable[[str, dict], dict] | None = None):
        self.token = (token or "").strip() or None
        self.cache_path = Path(cache_path)
        self._fetch = fetch or self._http_get
        self._lock = threading.Lock()
        self._last_call = 0.0
        try:
            self._cache: dict[str, dict] = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._cache = {}

    # ---- HTTP ------------------------------------------------------------------
    def _http_get(self, url: str, params: dict) -> dict:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Discogs token={self.token}"
        req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20, context=ssl_context()) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise DiscogsError("Discogs demande de ralentir : réessaie dans une minute.") from e
            if e.code == 401:
                raise DiscogsError("Jeton Discogs refusé : vérifie-le dans les Réglages.") from e
            raise DiscogsError(f"Discogs a répondu {e.code}.") from e
        except urllib.error.URLError as e:
            raise DiscogsError(f"Connexion à Discogs impossible : {e.reason}") from e

    def _search(self, params: dict) -> list[dict]:
        # Stay under the rate limit (25/min without token, 60/min with).
        with self._lock:
            wait = (1.1 if self.token else 2.5) - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
        data = self._fetch(SEARCH_URL, dict(params, type="release", per_page=15))
        return data.get("results") or []

    # ---- lookup ----------------------------------------------------------------
    def genre_for(self, track: Track) -> dict:
        """{"genre": "Acid House / Techno", "styles": [...], "releases": n}."""
        key = track.key
        if key in self._cache:
            return self._cache[key]
        title = parse_title(track.title, track.mix).base or track.title
        artist = track.artists[0] if track.artists else ""
        results = self._search({"artist": artist, "track": title}) if artist else []
        if not results:  # spelling differs (e.g. "ACIDBOY" / "Acidboy feat. …")
            results = self._search({"q": f"{artist} {title}".strip()})
        info = pick_genre(results)
        with self._lock:
            self._cache[key] = info
            self._save()
        return info

    def _save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._cache, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.cache_path)
