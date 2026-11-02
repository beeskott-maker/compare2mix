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
    # Bandcamp: "Artist - Album - 03 Title"
    t = importer.track_from_filename(Path("Barry Can't Swim - When Will We Land - 03 Kimbara.flac"))
    assert t.artists == ["Barry Can't Swim"] and t.title == "Kimbara"
    # Otherwise the whole name is kept; the matcher decides where the artist is.
    t = importer.track_from_filename(Path("Eric Prydz - Opus (Extended Mix).mp3"))
    assert t.artists == [] and t.title == "Eric Prydz - Opus (Extended Mix)"
    assert importer.track_from_filename(Path("01 - Untitled.wav")).title == "Untitled"
    # Numbers that belong to the title are kept.
    assert importer.track_from_filename(Path("KI-KI - 5 Mins Of Acid.mp3")).title == "KI-KI - 5 Mins Of Acid"
    assert importer.track_from_filename(Path("2B Or Not 2B.mp3")).title == "2B Or Not 2B"


@pytest.mark.parametrize("filename, title, artists", [
    # "Title - Mix - Artist" (files reported by the user)
    ("Confusion - Dub - Extended Edit - The Pump Panel.mp3", "Confusion - Dub - Extended Edit",
     ["The Pump Panel", "Dan Zamani", "Tim Taylor (Missile Records)"]),
    ("Make Regal Great Again - ACIDBOY.mp3", "Make Regal Great Again", ["ACIDBOY", "Regal"]),
    # "Artist - Title (Mix)"
    ("Eric Prydz - Opus (Extended Mix).mp3", "Opus", ["Eric Prydz"]),
    ("Barry Can't Swim - When Will We Land - 03 Kimbara.flac", "Kimbara", ["Barry Can't Swim"]),
])
def test_untagged_file_names_are_recognised(filename, title, artists):
    f = importer.track_from_filename(Path("/dl") / filename)
    props = importer.propose([f], [Track(title, artists, source_id="spotify:track:x")])
    assert len(props) == 1 and props[0].status == Status.FOUND, props and props[0].result


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


def test_recap_then_transfer_then_xml(env):
    tmp, app, call = env
    make_wav(tmp / "Downloads" / "whatever.wav", title="Some Unknown Track", artist="Nobody",
             seconds=300)  # same length as on Spotify (5:00)
    make_wav(tmp / "Downloads" / "Random Artist - Random Song.wav")
    pid = "1xZn2Qs4ytrn5EfOGJDxft"

    # Step 4: recap only, nothing is moved.
    r = call(f"/api/recap?playlist={pid}")
    assert r["missing"] == 1 and r["still_missing"] == []
    assert [(m["name"], m["status"], m["spotify"]) for m in r["matches"]] == \
        [("whatever.wav", "found", "Nobody – Some Unknown Track")]
    assert [u["name"] for u in r["unmatched"]] == ["Random Artist - Random Song.wav"]
    assert r["destination"]["keep_original"] is False
    assert (tmp / "Downloads" / "whatever.wav").exists()

    # Step 5: transfer the chosen file, then build the XML.
    m = r["matches"][0]
    res = call("/api/import", {"source": m["source"], "spotify_key": m["spotify_key"]})
    moved = tmp / "Music" / "Nobody - Some Unknown Track.wav"
    assert m["new_name"] == moved.name
    assert "durations" in m and not any(n.startswith("durée") for n in m["notes"])
    assert res == {"imported": "Nobody – Some Unknown Track", "file": str(moved),
                   "renamed": False}
    assert moved.exists() and not (tmp / "Downloads" / "whatever.wav").exists()
    x = call("/api/import-xml", {})
    assert (x["playlist"], x["existing"], x["new"]) == ("Warm up", 2, 1)
    pl = rekordbox.load_xml_playlists(x["path"])["Warm up"]
    assert [t.title for t in pl] == ["Strobe", "Losing It", "Some Unknown Track"]
    assert pl[-1].location == str(moved)

    # The track now shows as found, flagged as downloaded; the recap no longer lists it.
    res = {r["key"]: r for r in call("/api/compare", {"playlist": pid})["results"]}
    assert res["spotify:track:9"]["status"] == "found" and res["spotify:track:9"]["method"] == "import"
    assert call(f"/api/recap?playlist={pid}")["matches"] == []

    # Once the file is in a new Rekordbox export, there is nothing left to import.
    app.collection.append(Track("Some Unknown Track", ["Nobody"], location=str(moved)))
    app._rebuild_library()
    with pytest.raises(urllib.error.HTTPError) as e:
        call("/api/import-xml", {})
    assert e.value.code == 404


def test_doubtful_match_is_listed_as_probable(env):
    tmp, app, call = env
    make_wav(tmp / "Downloads" / "Nobody - Some Unknown Track (Live).wav")
    r = call("/api/recap?playlist=1xZn2Qs4ytrn5EfOGJDxft")
    assert [(m["status"], m["spotify_key"]) for m in r["matches"]] == [("probable", "spotify:track:9")]
    assert any("live" in n for n in r["matches"][0]["notes"])


def test_import_playlist_defaults_to_spotify_name(tmp_path):
    from test_web import FakeSpotify
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"client_id": "abc", "downloads_dir": str(tmp_path / "Downloads"),
                               "music_dir": str(tmp_path / "Music"),
                               "import_xml": str(tmp_path / "import.xml")}))
    app = web.App(port=0, config_path=cfg, decisions=DecisionStore(tmp_path / "d.json"),
                  spotify_factory=FakeSpotify, imports=importer.ImportLog(tmp_path / "i.json"))
    app.load_library(str(FIX / "rekordbox_sample.xml"))
    app.compare("1xZn2Qs4ytrn5EfOGJDxft")
    src = make_wav(tmp_path / "Downloads" / "x.wav", title="Some Unknown Track", artist="Nobody")
    app.import_download(str(src), "spotify:track:9")
    assert app.build_import_xml()["playlist"] == "À mixer"


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
    st = app.library_from_downloads()
    assert st["just_loaded"] == "rekordbox (1).xml" and st["origin"] == "downloads"
    # Nothing new: the current library is kept, no error.
    assert app.library_from_downloads()["just_loaded"] is None


def test_library_from_downloads_without_export(tmp_path):
    from test_web import FakeSpotify
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"client_id": "abc", "downloads_dir": str(tmp_path / "Downloads")}))
    app = web.App(port=0, config_path=cfg, decisions=DecisionStore(tmp_path / "d.json"),
                  spotify_factory=FakeSpotify, imports=importer.ImportLog(tmp_path / "i.json"))
    with pytest.raises(web.ApiError) as e:
        app.library_from_downloads()
    assert e.value.code == "no_export"


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


def test_untagged_file_described_with_spotify_metadata(tmp_path):
    log = importer.ImportLog(tmp_path / "i.json")
    sp = Track("Make Regal Great Again", ["ACIDBOY", "Regal"], 300, isrc="QZ1", album="EP",
               source_id="spotify:track:r")
    untagged = make_wav(tmp_path / "dl" / "Make Regal Great Again - ACIDBOY.wav")
    e = log.import_file(importer.read_audio_track(untagged), sp, tmp_path / "music")
    assert (e.title, e.artists, e.isrc, e.album) == ("Make Regal Great Again", ["ACIDBOY", "Regal"], "QZ1", "EP")
    # Tags present: they are kept.
    tagged = make_wav(tmp_path / "dl" / "x.wav", title="Make Regal Great Again (Extended)", artist="ACIDBOY")
    e = log.import_file(importer.read_audio_track(tagged), sp, tmp_path / "music")
    assert (e.title, e.artists) == ("Make Regal Great Again (Extended)", ["ACIDBOY"])


def test_target_name():
    sp = Track("Spotify Title", ["Sp Artist"])
    tagged = Track("Confusion", ["The Pump Panel"], mix="Dub", location="/d/x.MP3")
    assert importer.target_name(tagged, sp, ".mp3") == "The Pump Panel - Confusion (Dub).mp3"
    # No tags: the Spotify track it was matched to.
    bare = Track("whatever", [], location="/d/whatever.wav")
    assert importer.target_name(bare, sp, ".wav") == "Sp Artist - Spotify Title.wav"
    # Several artists, forbidden characters.
    t = Track("A/B: Song?", ["X", "Y"], location="/d/f.flac")
    assert importer.target_name(t, sp, ".flac") == "X, Y - A B Song.flac"
    # Already named that way: unchanged, even with another case or spacing.
    ok = Track("5 Mins Of Acid", ["KI_KI"], location="/d/ki_ki  -  5 mins of acid.mp3")
    assert importer.target_name(ok, sp, ".mp3") == "ki_ki  -  5 mins of acid.mp3"


def test_export_renamed_after_transfer(env, tmp_path):
    tmp, app, call = env
    dl = tmp / "Downloads"
    export = dl / "rekordbox.xml"
    dl.mkdir(exist_ok=True)
    export.write_bytes((FIX / "rekordbox_sample.xml").read_bytes())
    old = time.time() - 60
    os.utime(export, (old, old))
    assert call("/api/library/from-downloads", {})["just_loaded"] == "rekordbox.xml"
    make_wav(dl / "whatever.wav", title="Some Unknown Track", artist="Nobody")
    m = call("/api/recap?playlist=1xZn2Qs4ytrn5EfOGJDxft")["matches"][0]
    call("/api/import", {"source": m["source"], "spotify_key": m["spotify_key"]})
    x = call("/api/import-xml", {})
    assert x["export_archived"] == "rekordbox (traité).xml"
    assert not export.exists() and (dl / "rekordbox (traité).xml").exists()
    # The renamed export is no longer picked up, the library stays loaded.
    assert importer.find_rekordbox_export(dl, min_age_s=0) is None
    assert call("/api/library/from-downloads", {})["just_loaded"] is None


def test_durations_only_when_different():
    assert web._durations_if_different(586, 266) == {"file": "9:46", "spotify": "4:26"}
    assert web._durations_if_different(280, 266) is None  # within 30 s
    assert web._durations_if_different(None, 266) is None


def test_duration_difference_downgrades_to_check():
    sp = Track("Sunsleeper", ["Barry Can't Swim"], duration_s=266, source_id="spotify:track:s")
    same = Track("Sunsleeper", ["Barry Can't Swim"], duration_s=270, location="/d/a.mp3")
    longer = Track("Sunsleeper", ["Barry Can't Swim"], duration_s=586, location="/d/b.mp3")
    assert importer.propose([same], [sp])[0].status == Status.FOUND
    p = importer.propose([longer], [sp])[0]
    assert p.status == Status.PROBABLE and p.result.score < 90


def test_is_known_only_while_the_import_is_real(tmp_path):
    log = importer.ImportLog(tmp_path / "imports.json")
    sp = Track("Song", ["Nobody"], source_id="spotify:track:1")
    src = make_wav(tmp_path / "dl" / "Nobody - Song.wav")
    entry = log.import_file(importer.read_audio_track(src), sp, tmp_path / "music", move=False)
    assert log.is_known(str(src))  # copied: the original stays, not proposed again
    Path(entry.file).unlink()
    assert not log.is_known(str(src))  # the copy is gone: proposed again
    entry = log.import_file(importer.read_audio_track(src), sp, tmp_path / "music", move=False)
    future = time.time() + 120
    os.utime(src, (future, future))
    assert not log.is_known(str(src))  # downloaded again since: a new file
    log.ignore(str(tmp_path / "x.wav"))
    assert not log.is_known(str(tmp_path / "x.wav"))  # old "ignored" list unused


def test_import_xml_looks_like_a_rekordbox_export(tmp_path):
    out = tmp_path / "i.xml"
    path = "/Volumes/BSK/_Musique/Sidekick - Deep Fear (Lumberjack & BRØMANCE Remix Edit).wav"
    rekordbox.write_import_xml(out, "À mixer", [Track("Deep Fear", ["Sidekick"], location=path)])
    text = out.read_text(encoding="utf-8")
    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    # Exactly how Rekordbox 7 writes this path in its own export.
    assert ('Location="file://localhost/Volumes/BSK/_Musique/Sidekick%20-%20Deep%20Fear%20'
            '(Lumberjack%20%26%20BR%c3%98MANCE%20Remix%20Edit).wav"') in text
    assert rekordbox.load_xml(out)[0].location == path
