"""Music folder and library XML on a USB key (/Volumes/…), plugged or not."""

import json
import os
import shutil
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from compare2mix import importer, web
from compare2mix.decisions import DecisionStore
from compare2mix.models import Track
from test_importer import make_wav

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture()
def volumes(tmp_path, monkeypatch):
    """Fake /Volumes: a key is 'plugged' when its folder is in ``mounted``."""
    root = tmp_path / "Volumes"
    root.mkdir()
    mounted: set[str] = set()
    monkeypatch.setattr(importer, "VOLUMES_DIR", str(root))
    monkeypatch.setattr(importer, "_is_mount", lambda p: str(p) in mounted)

    class Keys:
        path = root

        def plug(self, name):
            (root / name).mkdir(exist_ok=True)
            mounted.add(str(root / name))
            return root / name

        def unplug(self, name):
            mounted.discard(str(root / name))
            shutil.rmtree(root / name, ignore_errors=True)

    return Keys()


def test_volume_helpers(volumes):
    key = volumes.plug("DJ KEY")
    assert importer.volume_root(key / "Musique" / "a.mp3") == str(key)
    assert importer.volume_root("/Users/me/Music") is None
    assert importer.is_available(key / "Musique")
    volumes.unplug("DJ KEY")
    assert not importer.is_available(key / "Musique")
    assert importer.is_available("/Users/me/Music")


def test_list_volumes_skips_startup_disk(volumes, tmp_path):
    volumes.plug("DJ KEY")
    os.symlink("/", volumes.path / "Macintosh HD")
    assert importer.list_volumes() == [str(volumes.path / "DJ KEY")]


def test_guess_music_dir():
    locs = ["/Volumes/K/Musique/a.mp3", "/Volumes/K/Musique/b.mp3", "/Users/me/c.mp3"]
    assert importer.guess_music_dir(locs) == "/Volumes/K/Musique"
    assert importer.guess_music_dir([]) is None


def test_no_fake_folder_when_key_unplugged(volumes, tmp_path):
    src = make_wav(tmp_path / "dl" / "Nobody - Song.wav")
    log = importer.ImportLog(tmp_path / "i.json")
    dest = volumes.path / "DJ KEY" / "Musique"
    with pytest.raises(importer.VolumeUnavailable):
        log.import_file(importer.read_audio_track(src), Track("Song", ["Nobody"]), dest)
    assert not (volumes.path / "DJ KEY").exists()  # nothing created on the internal disk
    assert src.exists() and log.imported == []


def test_pending_kept_while_key_unplugged(volumes, tmp_path):
    key = volumes.plug("DJ KEY")
    src = make_wav(tmp_path / "dl" / "Nobody - Song.wav")
    log = importer.ImportLog(tmp_path / "i.json")
    log.import_file(importer.read_audio_track(src), Track("Song", ["Nobody"]), key / "Musique")
    assert len(log.pending(set())) == 1
    volumes.unplug("DJ KEY")
    assert len(log.pending(set())) == 1  # can't check the file, keep it


@pytest.fixture()
def server(volumes, tmp_path, monkeypatch):
    from test_web import FakeSpotify
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    FakeSpotify.logged_in = True
    key = volumes.plug("DJ KEY")
    # The library XML lives on the key, like the music.
    shutil.copy(FIX / "rekordbox_sample.xml", key / "rekordbox.xml")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "client_id": "abc", "downloads_dir": str(tmp_path / "Downloads"),
        "music_dir": str(key / "Musique"),
        "keep_original": False,
    }))
    app = web.App(port=0, config_path=cfg, decisions=DecisionStore(tmp_path / "d.json"),
                  spotify_factory=FakeSpotify, imports=importer.ImportLog(tmp_path / "i.json"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None):
        h = {"X-Compare2mix": "1", "Content-Type": "application/json"} if body is not None else {}
        req = urllib.request.Request(base + path, headers=h,
                                     data=None if body is None else json.dumps(body).encode())
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    call("/api/library", {"path": str(key / "rekordbox.xml")})
    call("/api/compare", {"playlist": "1xZn2Qs4ytrn5EfOGJDxft"})
    yield tmp_path, key, app, call
    httpd.shutdown()
    httpd.server_close()


def test_key_unplugged_then_plugged(server, volumes):
    tmp, key, app, call = server
    volumes.unplug("DJ KEY")
    src = make_wav(tmp / "Downloads" / "Nobody - Some Unknown Track.wav")

    r = call("/api/recap?playlist=1xZn2Qs4ytrn5EfOGJDxft")
    assert r["destination"]["available"] is False and r["destination"]["volume"] == str(key)
    assert len(r["matches"]) == 1
    # Transfer refused with a clear message, nothing written on the internal disk.
    with pytest.raises(urllib.error.HTTPError) as e:
        call("/api/import", {"source": str(src), "spotify_key": "spotify:track:9"})
    assert e.value.code == 409
    assert "pas branchée" in json.loads(e.value.read())["error"]
    assert src.exists() and not key.exists()

    volumes.plug("DJ KEY")
    assert call("/api/recap?playlist=1xZn2Qs4ytrn5EfOGJDxft")["destination"]["available"] is True
    call("/api/import", {"source": str(src), "spotify_key": "spotify:track:9"})
    assert (key / "Musique" / src.name).exists() and not src.exists()


def test_keep_original_copies(server):
    tmp, key, app, call = server
    call("/api/import-settings", {"keep_original": True})
    src = make_wav(tmp / "Downloads" / "Nobody - Some Unknown Track.wav")
    call("/api/import", {"source": str(src), "spotify_key": "spotify:track:9"})
    assert src.exists() and (key / "Musique" / src.name).exists()
    # The original left in Downloads is not proposed a second time.
    assert call("/api/recap?playlist=1xZn2Qs4ytrn5EfOGJDxft")["matches"] == []


def test_key_stats(server, volumes):
    tmp, key, app, call = server
    # Sample collection: 8 tracks, none on the key (they live in /Users/dj/Music).
    st = call("/api/status")["library"]["key"]
    assert (st["name"], st["absent"], st["on_key"], st["mounted"]) == ("DJ KEY", 8, 0, True)
    # Two tracks on the key: one present, one whose file has disappeared.
    (key / "Musique").mkdir(exist_ok=True)
    (key / "Musique" / "a.mp3").write_bytes(b"x")
    app.collection[0].location = str(key / "Musique" / "a.mp3")
    app.collection[1].location = str(key / "Musique" / "gone.mp3")
    st = app.key_stats()
    assert (st["absent"], st["on_key"]) == (7, 1)
    # Key unplugged: files on it cannot be checked, they count as on the key.
    volumes.unplug("DJ KEY")
    st = app.key_stats()
    assert (st["absent"], st["on_key"], st["mounted"]) == (6, 2, False)


def test_library_on_key_loaded_when_plugged(server, volumes, tmp_path, monkeypatch):
    tmp, key, app, call = server
    from test_web import FakeSpotify
    # An old local copy exists (data/rekordbox.xml): it must NOT replace the key's library.
    local = tmp / "data" / "rekordbox.xml"
    local.parent.mkdir()
    shutil.copy(FIX / "rekordbox_sample.xml", local)
    monkeypatch.setattr(web, "DEFAULT_LIBRARY", local)
    volumes.unplug("DJ KEY")
    app2 = web.App(port=0, config_path=tmp / "config.json",
                   decisions=DecisionStore(tmp / "d.json"), spotify_factory=FakeSpotify,
                   imports=importer.ImportLog(tmp / "i.json"))
    app2.autoload()
    st = app2.library_status()
    assert st["count"] == 0 and st["available"] is False
    volumes.plug("DJ KEY")
    shutil.copy(FIX / "rekordbox_sample.xml", key / "rekordbox.xml")
    app2.autoload()  # next start with the key plugged in
    assert app2.library_status()["count"] == 8
    assert app2.library_status()["path"] == str(key / "rekordbox.xml")
    assert json.loads((tmp / "config.json").read_text())["library_path"] == str(key / "rekordbox.xml")


def test_suggested_music_dir_from_collection(server):
    tmp, key, app, call = server
    s = call("/api/import-settings")
    # The sample collection lives in /Users/dj/Music; the plugged key is suggested too.
    assert s["suggested_music_dirs"][0] == "/Users/dj/Music"
    assert str(key) in s["suggested_music_dirs"]


def test_key_absent_list_skips_rekordbox_samples(server, volumes):
    tmp, key, app, call = server
    (key / "Musique").mkdir(exist_ok=True)
    app.collection[0].location = str(key / "Musique" / "gone.mp3")  # lost on the plugged key
    app.collection[1].location = ""  # Apple Music link: no file
    app.collection[2].location = "/Users/dj/Music/rekordbox/Sampler/PRESET/House1.wav"  # sample
    data = call("/api/key-absent")
    where = {t["display"]: t["where"] for t in data["tracks"]}
    assert data["key"] == "DJ KEY" and len(where) == 7  # 8 tracks - 1 sample
    assert where[app.collection[0].display] == "lost"
    assert where[app.collection[1].display] == "am"
    assert app.collection[2].display not in where
    assert set(where.values()) == {"lost", "am"}  # the other sample tracks have no file
    app.collection[3].location = "/Users/dj/Music/elsewhere.mp3"
    assert {t["display"]: t["where"] for t in call("/api/key-absent")["tracks"]}[
        app.collection[3].display] == "mac"
    t = next(t for t in data["tracks"] if t["display"] == app.collection[1].display)
    assert t["copy"] == " ".join([*app.collection[1].artists, app.collection[1].title])
    assert app.key_stats()["absent"] == 7


def test_recap_also_finds_tracks_absent_from_key(server):
    tmp, key, app, call = server
    # "Deadmau5 – Strobe" is in Rekordbox but its file is on the Mac, not on the key.
    strobe = app.collection[0]
    assert call("/api/key-absent")["tracks"] and strobe.location.startswith("/Users/")
    src = make_wav(tmp / "Downloads" / "Deadmau5 - Strobe.wav", seconds=637)
    r = call("/api/recap?playlist=1xZn2Qs4ytrn5EfOGJDxft")
    m = next(m for m in r["matches"] if m["source"] == str(src))
    assert (m["kind"], m["spotify_key"], m["spotify_title"]) == ("absent", strobe.key, "Strobe")
    absent_before = r["absent"]
    res = call("/api/import", {"source": str(src), "spotify_key": m["spotify_key"], "genre": "Progressive House"})
    assert res["file"] == str(key / "Musique" / "Deadmau5 - Strobe.wav")
    # Back on the key: no longer listed nor counted as absent.
    keys = {t["display"] for t in call("/api/key-absent")["tracks"]}
    assert strobe.display not in keys and app.key_stats()["absent"] == absent_before - 1
