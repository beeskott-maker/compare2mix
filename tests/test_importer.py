"""Downloads watching and Rekordbox import."""

import json
import os
import threading
import time
import urllib.parse
import urllib.request
import wave
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from mutagen.wave import WAVE
from mutagen.id3 import TCON, TIT2, TPE1, TSRC

from compare2mix import importer, web
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





# ---- end to end through the web API ------------------------------------------

@pytest.fixture()
def env(tmp_path, monkeypatch):
    from test_web import FakeSpotify  # same fake Spotify as the web tests
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    FakeSpotify.logged_in = True
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "client_id": "abc", "downloads_dir": str(tmp_path / "Downloads"),
        "music_dir": str(tmp_path / "Music"), "keep_original": False,
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


def test_recap_then_transfer(env):
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
    res = call("/api/import", {"source": m["source"], "spotify_key": m["spotify_key"],
                               "genre": "Acid House / Techno"})
    moved = tmp / "Music" / "Nobody - Some Unknown Track.wav"
    assert m["new_name"] == moved.name
    assert "durations" in m and not any(n.startswith("durée") for n in m["notes"])
    assert res == {"imported": "Nobody – Some Unknown Track", "file": str(moved),
                   "renamed": False, "genre": "Acid House / Techno", "comment": None,
                   "tags_error": None}
    assert WAVE(moved).tags["TCON"].text == ["Acid House / Techno"]
    assert moved.exists() and not (tmp / "Downloads" / "whatever.wav").exists()
    assert call("/api/transfer-done", {}) == {"export_archived": None}  # XML given by hand

    # The track now shows as found, flagged as downloaded; the recap no longer lists it.
    res = {r["key"]: r for r in call("/api/compare", {"playlist": pid})["results"]}
    assert res["spotify:track:9"]["status"] == "found" and res["spotify:track:9"]["method"] == "import"
    assert call(f"/api/recap?playlist={pid}")["matches"] == []

    # Once the file is in a new Rekordbox export, it is no longer pending.
    assert app.library_status()["pending_import"] == 1
    app.collection.append(Track("Some Unknown Track", ["Nobody"], location=str(moved)))
    app._rebuild_library()
    assert app.library_status()["pending_import"] == 0


def test_doubtful_match_is_listed_as_probable(env):
    tmp, app, call = env
    make_wav(tmp / "Downloads" / "Nobody - Some Unknown Track (Live).wav")
    r = call("/api/recap?playlist=1xZn2Qs4ytrn5EfOGJDxft")
    assert [(m["status"], m["spotify_key"]) for m in r["matches"]] == [("probable", "spotify:track:9")]
    assert any("live" in n for n in r["matches"][0]["notes"])


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
                               "music_dir": str(tmp_path / "Music")}))
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
    x = call("/api/transfer-done", {})
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

# ---- genre (Discogs) and tags -----------------------------------------------

def test_pick_genre_keeps_most_frequent_styles():
    from compare2mix.discogs import pick_genre
    results = [{"genre": ["Electronic"], "style": ["Techno", "Acid"]},
               {"genre": ["Electronic"], "style": ["Acid House", "Techno", "Acid"]},
               {"genre": ["Electronic"], "style": ["Acid House", "Techno"]}]
    assert pick_genre(results)["genre"] == "Techno / Acid"
    assert pick_genre([{"genre": ["Electronic"], "style": []}])["genre"] == "Electronic"
    assert pick_genre([])["genre"] is None


def test_discogs_lookup_falls_back_and_caches(tmp_path, monkeypatch):
    from compare2mix import discogs
    monkeypatch.setattr(discogs.time, "sleep", lambda s: None)
    calls = []

    def fetch(url, params):
        calls.append(params)
        if "artist" in params:  # nothing under the exact artist spelling
            return {"results": []}
        return {"results": [{"genre": ["Electronic"], "style": ["Acid", "Techno"]}]}

    sp = Track("Make Regal Great Again", ["ACIDBOY", "Regal"], source_id="spotify:track:r")
    d = discogs.Discogs(None, cache_path=tmp_path / "g.json", fetch=fetch)
    assert d.genre_for(sp)["genre"] == "Acid / Techno"
    assert d.genre_for(sp)["genre"] == "Acid / Techno"  # cached
    assert [c.get("artist") or c.get("q") for c in calls] == \
        ["ACIDBOY", "ACIDBOY make regal great again"]
    again = discogs.Discogs(None, cache_path=tmp_path / "g.json", fetch=fetch)
    assert again.genre_for(sp)["genre"] == "Acid / Techno" and len(calls) == 2


def test_write_tags_replaces_genre_and_fills_untagged(tmp_path):
    f = make_wav(tmp_path / "x.wav", title="Old", artist="Someone")
    audio = WAVE(f)
    audio.tags.add(TCON(encoding=3, text=["Pop"]))
    audio.save()
    importer.write_tags(f, genre="Deep House")
    t = WAVE(f).tags
    assert t["TCON"].text == ["Deep House"] and t["TIT2"].text == ["Old"]
    bare = make_wav(tmp_path / "bare.wav")
    importer.write_tags(bare, genre="Trance", title="Sunsleeper", artists=["Barry Can't Swim"])
    t = WAVE(bare).tags
    assert (t["TCON"].text, t["TIT2"].text, t["TPE1"].text) == \
        (["Trance"], ["Sunsleeper"], ["Barry Can't Swim"])
    assert importer.read_audio_track(bare).artists == ["Barry Can't Swim"]


def test_genre_endpoint(env):
    tmp, app, call = env
    app._discogs_factory = lambda token: type("D", (), {
        "genre_for": staticmethod(lambda t: {"genre": "House", "styles": ["House"], "releases": 3})})()
    assert call("/api/genre?spotify_key=spotify:track:9")["genre"] == "House"


# ---- energy (local analysis) --------------------------------------------------

def test_energy_score_and_mood():
    from compare2mix import energy
    # Values measured on real tracks: piano solo, deep house, peak-time techno.
    assert energy.energy_score(3.38, 0.06, -17.8) == 2
    assert energy.energy_score(5.70, 0.86, -14.0) == 7
    assert energy.energy_score(6.47, 0.73, -11.4) == 8
    assert energy.energy_score(9.0, 1.0, 0.0) == 10 and energy.energy_score(1.0, 0.0, -60) == 1
    assert energy.mood_word({"aggressive": 0.1, "happy": 0.2, "relaxed": 0.9, "sad": 0.3}) == "planant"
    assert energy.mood_word({"aggressive": 0.4, "happy": 0.3, "relaxed": 0.2, "sad": 0.1}) is None
    assert energy.comment_text(7, "sombre") == "E7 · sombre"
    assert energy.comment_text(7, None) == "E7" and energy.comment_text(None, None) is None


def test_energy_endpoint_and_comment_tag(env):
    tmp, app, call = env

    class FakeAnalyzer:
        def analyze(self, path):
            return {"energy": 7, "mood": "sombre"}

    app.analyzer = FakeAnalyzer()
    src = make_wav(tmp / "Downloads" / "whatever.wav", title="Some Unknown Track",
                   artist="Nobody", seconds=300)
    from compare2mix import energy
    if energy.available():
        assert call("/api/energy?source=" + urllib.parse.quote(str(src)))["energy"] == 7
    # Only files in Downloads can be analysed.
    with pytest.raises(urllib.error.HTTPError) as e:
        call("/api/energy?source=" + urllib.parse.quote(str(FIX / "rekordbox_sample.xml")))
    assert e.value.code == 404
    m = call("/api/recap?playlist=1xZn2Qs4ytrn5EfOGJDxft")["matches"][0]
    res = call("/api/import", {"source": m["source"], "spotify_key": m["spotify_key"],
                               "comment": "E7 · sombre"})
    assert WAVE(res["file"]).tags.getall("COMM")[0].text == ["E7 · sombre"]
