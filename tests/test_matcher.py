from pathlib import Path

import pytest

from compare2mix.matcher import Matcher
from compare2mix.models import Status, Track
from compare2mix.rekordbox import load_xml
from compare2mix.spotify import load_csv

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def results():
    library = load_xml(FIX / "rekordbox_sample.xml")
    playlist = load_csv(FIX / "playlist_exportify.csv")
    return {r.spotify.source_id: r for r in Matcher(library).match_all(playlist)}


def test_radio_edit_matches_original_mix(results):
    r = results["spotify:track:1"]
    assert r.status == Status.FOUND and r.match.source_id == "1"
    assert any("format" in n for n in r.notes)


def test_exact(results):
    assert results["spotify:track:2"].status == Status.FOUND


def test_edit_with_ampersand_and_artist_list(results):
    r = results["spotify:track:3"]
    assert r.status == Status.FOUND and r.match.source_id == "3"


def test_remix_in_mix_field_and_accents(results):
    r = results["spotify:track:4"]
    assert r.status == Status.FOUND and r.match.source_id == "4"


def test_extended_vs_plain(results):
    r = results["spotify:track:5"]
    assert r.status == Status.FOUND and r.match.source_id == "5"


def test_different_remix_is_only_probable(results):
    r = results["spotify:track:6"]
    assert r.status == Status.PROBABLE and r.match.source_id == "6"
    assert any("version différente" in n for n in r.notes)


def test_untagged_artist_in_title(results):
    r = results["spotify:track:7"]
    assert r.status == Status.FOUND and r.match.source_id == "7"


def test_same_title_other_artist_is_missing(results):
    r = results["spotify:track:8"]
    assert r.status == Status.MISSING
    assert r.alternatives and r.alternatives[0][0].source_id == "8"


def test_unknown_is_missing(results):
    assert results["spotify:track:9"].status == Status.MISSING


def test_isrc_wins():
    lib = [Track("Totally Different Name", ["X"], isrc="GBAAA0000001", source_id="a")]
    r = Matcher(lib).match(Track("Song", ["Y"], isrc="gbaaa0000001"))
    assert r.status == Status.FOUND and r.method == "ISRC"


def test_empty_library():
    assert Matcher([]).match(Track("Song", ["A"])).status == Status.MISSING
