"""Web interface: API tested against a real local server with a fake Spotify."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from compare2mix import importer, spotify, web
from compare2mix.decisions import DecisionStore
from compare2mix.models import Track

FIX = Path(__file__).parent / "fixtures"

PLAYLIST = [
    Track("Strobe - Radio Edit", ["deadmau5"], 214, source_id="spotify:track:1"),
    Track("Innerbloom - What So Not Remix", ["RÜFÜS DU SOL", "What So Not"], 400,
          source_id="spotify:track:6"),
    Track("Some Unknown Track", ["Nobody"], 300, source_id="spotify:track:9"),
]


class FakeSpotify:
    logged_in = True

    def __init__(self, client_id, redirect_uri):
        self.client_id, self.redirect_uri = client_id, redirect_uri

    def is_logged_in(self):
        return FakeSpotify.logged_in

    def me(self):
        if not FakeSpotify.logged_in:
            raise spotify.NotLoggedIn("Connexion à Spotify nécessaire.")
        return {"id": "carredagno", "display_name": "Loïc"}

    def my_playlists(self):
        return [
            {"id": "1xZn2Qs4ytrn5EfOGJDxft", "name": "À mixer", "owner": "carredagno", "owner_id": "carredagno",
             "collaborative": False, "tracks": 3},
            {"id": "0Zarq4BVkFkZOWkmqsfrjA", "name": "Top 100", "owner": "UKF", "owner_id": "ukf",
             "collaborative": False, "tracks": 100},
        ]

    def playlist_name(self, pid):
        return "À mixer"

    def playlist_tracks(self, pid):
        return [Track(**t.__dict__) for t in PLAYLIST]

    def authorization_request(self):
        return "https://accounts.spotify.com/authorize?x=1", "verifier", "state123"

    def exchange_code(self, code, verifier):
        assert verifier == "verifier"
        FakeSpotify.logged_in = True

    def logout(self):
        FakeSpotify.logged_in = False


@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    FakeSpotify.logged_in = True
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"client_id": "abc",
                               "downloads_dir": str(tmp_path / "Downloads"),
                               "music_dir": str(tmp_path / "Music")}))
    app = web.App(port=0, config_path=cfg, decisions=DecisionStore(tmp_path / "decisions.json"),
                  spotify_factory=FakeSpotify,
                  imports=importer.ImportLog(tmp_path / "imports.json"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, app
    httpd.shutdown()
    httpd.server_close()


def call(base, path, body=None, headers=None):
    h = {"X-Compare2mix": "1", "Content-Type": "application/json"} if body is not None else {}
    h.update(headers or {})
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 headers=h)
    with urllib.request.urlopen(req) as r:
        raw = r.read()
        return json.loads(raw) if r.headers.get_content_type() == "application/json" else raw.decode("utf-8-sig")


def test_page_served(server):
    base, _ = server
    assert '<div class="brand">' in call(base, "/")


def test_full_flow(server):
    base, _ = server
    st = call(base, "/api/status")
    assert st["spotify"] == {"client_id": True, "logged_in": True, "user": "Loïc",
                             "redirect_uri": st["spotify"]["redirect_uri"]}
    assert st["library"]["count"] == 0

    lib = call(base, "/api/library", {"path": str(FIX / "rekordbox_sample.xml")})
    assert lib["count"] == 8

    pls = call(base, "/api/playlists")
    assert [(p["id"], p["readable"]) for p in pls] == [("1xZn2Qs4ytrn5EfOGJDxft", True), ("0Zarq4BVkFkZOWkmqsfrjA", False)]

    data = call(base, "/api/compare", {"playlist": "1xZn2Qs4ytrn5EfOGJDxft"})
    assert data["name"] == "À mixer"
    assert data["counts"] == {"found": 1, "probable": 1, "missing": 1}
    by_key = {r["key"]: r for r in data["results"]}
    probable = by_key["spotify:track:6"]
    assert probable["status"] == "probable"

    # Confirm the probable match -> found, manual, remembered.
    upd = call(base, "/api/decision", {"spotify_key": "spotify:track:6", "action": "confirm",
                                      "library_key": probable["match"]["key"]})
    assert upd["status"] == "found" and upd["method"] == "manuel" and upd["decided"]
    again = call(base, "/api/compare", {"playlist": "1xZn2Qs4ytrn5EfOGJDxft"})
    assert again["counts"] == {"found": 2, "probable": 0, "missing": 1}

    # Reject the automatic Strobe match -> no longer matched.
    strobe = by_key["spotify:track:1"]
    upd = call(base, "/api/decision", {"spotify_key": "spotify:track:1", "action": "reject",
                                      "library_key": strobe["match"]["key"]})
    assert upd["status"] == "missing"
    assert any("écarté" in n for n in upd["notes"])

    # Reset restores the automatic result.
    upd = call(base, "/api/decision", {"spotify_key": "spotify:track:1", "action": "reset"})
    assert upd["status"] == "found" and not upd["decided"]

    # Manual association through search.
    hits = call(base, "/api/search?q=opus")
    assert hits and "Opus" in hits[0]["title"]
    upd = call(base, "/api/decision", {"spotify_key": "spotify:track:9", "action": "confirm",
                                      "library_key": hits[0]["key"]})
    assert upd["status"] == "found"

    csv_text = call(base, "/api/export.csv?playlist=1xZn2Qs4ytrn5EfOGJDxft")
    assert csv_text.startswith("statut,") and "manuel" in csv_text


def test_post_requires_header(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        call(base, "/api/logout", {}, headers={"X-Compare2mix": "0"})
    assert e.value.code == 403


def test_foreign_host_rejected(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        call(base, "/api/status", headers={"Host": "evil.example"})
    assert e.value.code == 403


def test_compare_without_library(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        call(base, "/api/compare", {"playlist": "1xZn2Qs4ytrn5EfOGJDxft"})
    assert e.value.code == 400


def test_login_flow(server):
    base, app = server
    FakeSpotify.logged_in = False
    assert call(base, "/api/status")["spotify"]["logged_in"] is False
    assert app.start_login().startswith("https://accounts.spotify.com/")
    app.finish_login({"code": "c", "state": "state123"})
    assert call(base, "/api/status")["spotify"]["logged_in"] is True


def test_decisions_persist(tmp_path):
    store = DecisionStore(tmp_path / "d.json")
    store.confirm("spotify:track:1", "file:/a.mp3")
    store.reject("spotify:track:1", "file:/b.mp3")
    again = DecisionStore(tmp_path / "d.json")
    assert again.get("spotify:track:1") == {"confirmed": "file:/a.mp3", "rejected": ["file:/b.mp3"]}


def test_default_playlist(server):
    base, _ = server
    assert call(base, "/api/status")["default_playlist"] is None
    r = call(base, "/api/default-playlist",
             {"playlist": "https://open.spotify.com/playlist/0Zarq4BVkFkZOWkmqsfrjA?si=x"})
    assert r["default_playlist"] == "0Zarq4BVkFkZOWkmqsfrjA"
    assert call(base, "/api/status")["default_playlist"] == "0Zarq4BVkFkZOWkmqsfrjA"


def test_move_is_the_default(server):
    base, _ = server
    assert call(base, "/api/import-settings")["keep_original"] is False


def test_library_upload(server, tmp_path):
    base, app = server
    data = (FIX / "rekordbox_sample.xml").read_bytes()
    req = urllib.request.Request(base + "/api/library/upload", data=data, headers={
        "X-Compare2mix": "1", "X-Filename": "rekordbox%20cl%C3%A9.xml"})
    with urllib.request.urlopen(req) as r:
        st = json.loads(r.read())
    assert st["count"] == 8 and st["name"] == "rekordbox clé.xml"
    # A copy is kept next to the config, so it reloads at the next start.
    assert Path(st["path"]).read_bytes() == data
    # Something that is not a Rekordbox export is refused.
    bad = urllib.request.Request(base + "/api/library/upload", data=b"<html></html>",
                                 headers={"X-Compare2mix": "1"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(bad)
    assert e.value.code == 400

def test_quit_button_stops_the_server(server):
    base, app = server
    stopped = threading.Event()
    with pytest.raises(urllib.error.HTTPError):  # not started by serve(): refused
        call(base, "/api/quit", {})
    app.on_quit = stopped.set
    assert call(base, "/api/quit", {}) == {"ok": True}
    assert stopped.wait(2)


def test_favicon(server):
    base, _ = server
    with urllib.request.urlopen(base + "/favicon.svg") as r:
        assert r.headers["Content-Type"] == "image/svg+xml" and b"<svg" in r.read()
