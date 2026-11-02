"""Downloads watching and Rekordbox import."""

import json
import os
import threading
import time
import urllib.request
import wave
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from mutagen.wave import WAVE
from mutagen.id3 import TIT2, TPE1, TSRC

from compare2mix import importer, rekordbox, web
from compare2mix.decisions import DecisionStore
from compare2mix.models import Status, Track

FIX = Path(__file__).parent / "fixtures"


def make_wav(path: Path, title=None, artist=None, isrc=None, seconds=1, age=60):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0" * 8000 * seconds)
    if title or artist or isrc:
        audio = WAVE(path)
        audio.add_tags()
        if title:
            audio.tags.add(TIT2(encoding=3, text=title))
        if artist:
            audio.tags.add(TPE1(encoding=3, text=artist))
        if isrc:
            audio.tags.add(TSRC(encoding=3, text=isrc))
        audio.save()
    old = time.time() - age
    os.utime(path, (old, old))
    return path


# ---- reading -----------------------------------------------------------------

def test_filename_parsing():
    t = importer.track_from_filename(Path("Eric Prydz - Opus (Extended Mix).mp3"))
    assert t.artists == ["Eric Prydz"] and t.title == "Opus (Extended Mix)"
    t = importer.track_from_filename(Path("Barry Can't Swim - When Will We Land - 03 Kimbara.flac"))
    assert t.artists == ["Barry Can't Swim"] and t.title == "Kimbara"
    t = importer.track_from_filename(Path("01 - Untitled.wav"))
    assert t.title == "Untitled"
    # Numbers that belong to the title are kept.
    assert importer.track_from_filename(Path("KI-KI - 5 Mins Of Acid.mp3")).title == "5 Mins Of Acid"
    assert importer.track_from_filename(Path("2B Or Not 2B.mp3")).title == "2B Or Not 2B"


def test_tags_win_over_filename(tmp_path):
    p = make_wav(tmp_path / "track01.wav", title="Kimbara", artist="Barry Can't Swim",
                 isrc="GB-ABC-25-00001")
    t = importer.read_audio_track(p)
    assert (t.title, t.artists, t.isrc) == ("Kimbara", ["Barry Can't Swim"], "GBABC2500001")
    assert t.duration_s == pytest.approx(1, abs=0.1)


def test_scan_skips_partial_recent_and_hidden(tmp_path):
    make_wav(tmp_path / "ok.wav")
    make_wav(tmp_path / "Album" / "inner.wav")
    make_wav(tmp_path / "fresh.wav", age=0)  # still being written
    make_wav(tmp_path / ".hidden.wav")
    (tmp_path / "x.mp3.crdownload").write_bytes(b"1")
    (tmp_path / "notes.txt").write_text("x")
    names = sorted(p.name for p in importer.scan_folder(tmp_path))
    assert names == ["inner.wav", "ok.wav"]


# ---- matching ----------------------------------------------------------------

def test_propose_pairs_each_file_once():
    files = [Track("Kimbara", ["Barry Can't Swim"], location="/d/k.mp3"),
             Track("Sunsleeper", ["Barry Can't Swim"], location="/d/s.mp3"),
             Track("Unrelated", ["Someone"], location="/d/u.mp3")]
    missing = [Track("Sunsleeper", ["Barry Can't Swim"], source_id="spotify:track:s"),
               Track("Kimbara", ["Barry Can't Swim"], source_id="spotify:track:k"),
               Track("Nope", ["Nobody"], source_id="spotify:track:n")]
    props = {p.spotify.key: p.file.location for p in importer.propose(files, missing)}
    assert props == {"spotify:track:s": "/d/s.mp3", "spotify:track:k": "/d/k.mp3"}


# ---- import log & XML --------------------------------------------------------

def test_import_moves_file_and_logs(tmp_path):
    src = make_wav(tmp_path / "dl" / "Nobody - Song.wav")
    log = importer.ImportLog(tmp_path / "imports.json")
    entry = log.import_file(importer.read_audio_track(src), Track("Song", ["Nobody"],
                            source_id="spotify:track:1"), tmp_path / "music")
    assert not src.exists() and Path(entry.file).exists()
    assert log.is_known(str(src))
    # name clash -> numbered copy, nothing overwritten
    src2 = make_wav(tmp_path / "dl" / "Nobody - Song.wav")
    entry2 = log.import_file(importer.read_audio_track(src2), Track("Song", ["Nobody"]),
                             tmp_path / "music")
    assert Path(entry2.file).name == "Nobody - Song (2).wav"
    again = importer.ImportLog(tmp_path / "imports.json")
    assert len(again.imported) == 2
    assert again.pending({entry.file}) == [again.imported[1]]


def test_import_xml_roundtrip(tmp_path):
    tracks = [Track("Café", ["Énergie"], 300, location="/Users/me/Music/a b/Café.mp3", mix="Dub")]
    out = tmp_path / "imp.xml"
    rekordbox.write_import_xml(out, "À mixer", tracks)
    back = rekordbox.load_xml_playlists(out)
    assert list(back) == ["À mixer"]
    t = back["À mixer"][0]
    assert (t.title, t.artists, t.location, t.mix, t.duration_s) == \
        ("Café", ["Énergie"], "/Users/me/Music/a b/Café.mp3", "Dub", 300)


# ---- end to end through the web API ------------------------------------------

@pytest.fixture()
def env(tmp_path, monkeypatch):
    from test_web import FakeSpotify  # same fake Spotify as the web tests
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    FakeSpotify.logged_in = True
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "client_id": "abc", "downloads_dir": str(tmp_path / "Downloads"),
        "music_dir": str(tmp_path / "Music"), "import_playlist": "Warm up",
        "import_xml": str(tmp_path / "Music" / "rekordbox-import.xml"), "keep_original": False,
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

    call("/api/library", {"path": str(FIX / "rekordbox_sample.xml")})
    call("/api/compare", {"playlist": "1xZn2Qs4ytrn5EfOGJDxft"})
    yield tmp_path, app, call
    httpd.shutdown()
    httpd.server_close()


def test_auto_import_end_to_end(env):
    tmp, app, call = env
    make_wav(tmp / "Downloads" / "whatever.wav", title="Some Unknown Track", artist="Nobody")
    make_wav(tmp / "Downloads" / "Random Artist - Random Song.wav")

    d = call("/api/downloads")
    assert d["imported_now"] == ["Nobody – Some Unknown Track"]
    assert [u["name"] for u in d["unmatched"]] == ["Random Artist - Random Song.wav"]
    assert len(d["pending"]) == 1
    moved = tmp / "Music" / "whatever.wav"
    assert moved.exists() and not (tmp / "Downloads" / "whatever.wav").exists()

    # The track is now found, flagged as downloaded.
    res = {r["key"]: r for r in call("/api/compare", {"playlist": "1xZn2Qs4ytrn5EfOGJDxft"})["results"]}
    assert res["spotify:track:9"]["status"] == "found"
    assert res["spotify:track:9"]["method"] == "import"

    # Import XML: designated playlist keeps its existing tracks + the new one.
    pl = rekordbox.load_xml_playlists(d["settings"]["import_xml"])["Warm up"]
    assert [t.title for t in pl] == ["Strobe", "Losing It", "Some Unknown Track"]
    assert pl[-1].location == str(moved)

    # Nothing new on the next scan; ignoring the unrelated file hides it.
    call("/api/ignore", {"source": d["unmatched"][0]["source"]})
    d2 = call("/api/downloads")
    assert d2["imported_now"] == [] and d2["unmatched"] == []

    # Once the file is in a new Rekordbox export, it is no longer pending.
    app.collection.append(Track("Some Unknown Track", ["Nobody"], location=str(moved)))
    app._rebuild_library()
    assert call("/api/downloads")["pending"] == []


def test_manual_confirmation_when_auto_import_off(env):
    tmp, app, call = env
    call("/api/import-settings", {"auto_import": False})
    make_wav(tmp / "Downloads" / "Nobody - Some Unknown Track.wav")
    d = call("/api/downloads")
    assert d["imported_now"] == [] and len(d["to_confirm"]) == 1
    c = d["to_confirm"][0]
    assert c["spotify_key"] == "spotify:track:9"
    r = call("/api/import", {"source": c["source"], "spotify_key": c["spotify_key"]})
    assert r["imported"] == "Nobody – Some Unknown Track"
    assert call("/api/downloads")["to_confirm"] == []


# ---- Rekordbox export dropped in Downloads ------------------------------------

def _drop_export(folder: Path, name="rekordbox.xml", age=60, product="rekordbox"):
    folder.mkdir(parents=True, exist_ok=True)
    data = (FIX / "rekordbox_sample.xml").read_text(encoding="utf-8")
    data = data.replace('Name="rekordbox"', f'Name="{product}"')
    p = folder / name
    p.write_text(data, encoding="utf-8")
    old = time.time() - age
    os.utime(p, (old, old))
    return p


def test_find_rekordbox_export(tmp_path):
    assert importer.find_rekordbox_export(tmp_path) is None
    _drop_export(tmp_path, "compare2mix-import.xml", product="compare2mix")  # ours: ignored
    (tmp_path / "other.xml").write_text("<rss></rss>")
    assert importer.find_rekordbox_export(tmp_path) is None
    older = _drop_export(tmp_path, "rekordbox.xml", age=600)
    newer = _drop_export(tmp_path, "rekordbox (1).xml", age=60)
    assert importer.find_rekordbox_export(tmp_path) == newer
    _drop_export(tmp_path, "fresh.xml", age=0)  # still being written: ignored
    assert importer.find_rekordbox_export(tmp_path) == newer
    assert older.exists()


def test_library_picked_up_from_downloads(tmp_path, monkeypatch):
    from test_web import FakeSpotify
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"client_id": "abc", "downloads_dir": str(tmp_path / "Downloads"),
                               "music_dir": str(tmp_path / "Music"),
                               "import_xml": str(tmp_path / "import.xml")}))
    app = web.App(port=0, config_path=cfg, decisions=DecisionStore(tmp_path / "d.json"),
                  spotify_factory=FakeSpotify, imports=importer.ImportLog(tmp_path / "i.json"))
    assert app.check_downloads_for_library() is None  # nothing there

    export = _drop_export(tmp_path / "Downloads")
    assert app.check_downloads_for_library() == "rekordbox.xml"
    st = app.library_status()
    assert st["count"] == 8 and st["name"] == "rekordbox.xml"
    assert export.exists()  # left where it was
    assert app.check_downloads_for_library() is None  # same file: not reloaded

    # A newer export replaces it; scan_downloads reports it.
    newer = _drop_export(tmp_path / "Downloads", "rekordbox (1).xml", age=10)
    assert app.scan_downloads()["library_auto_loaded"] == "rekordbox (1).xml"


def test_old_export_does_not_replace_newer_library(tmp_path):
    from test_web import FakeSpotify
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"client_id": "abc", "downloads_dir": str(tmp_path / "Downloads")}))
    app = web.App(port=0, config_path=cfg, decisions=DecisionStore(tmp_path / "d.json"),
                  spotify_factory=FakeSpotify, imports=importer.ImportLog(tmp_path / "i.json"))
    # Library uploaded from the page just now...
    app.upload_library((FIX / "rekordbox_sample.xml").read_bytes(), "cle.xml")
    # ...while an export from last week lingers in Downloads.
    _drop_export(tmp_path / "Downloads", age=7 * 86400)
    assert app.check_downloads_for_library() is None
    assert app.library_status()["name"] == "cle.xml"
