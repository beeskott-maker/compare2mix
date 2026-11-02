from pathlib import Path

import pytest

from compare2mix.rekordbox import load_xml, load_xml_playlist_names
from compare2mix.spotify import load_csv, parse_playlist_id, tracks_from_items_page

FIX = Path(__file__).parent / "fixtures"


def test_load_xml_only_collection():
    tracks = load_xml(FIX / "rekordbox_sample.xml")
    assert len(tracks) == 8  # TRACK entries under PLAYLISTS are ignored
    strobe = tracks[0]
    assert strobe.title == "Strobe" and strobe.mix == "Original Mix"
    assert strobe.duration_s == 637
    assert strobe.location == "/Users/dj/Music/Deadmau5 - Strobe.mp3"
    assert tracks[2].artists == ["CamelPhat", "Elderbrook"]


def test_playlist_names():
    assert load_xml_playlist_names(FIX / "rekordbox_sample.xml") == ["Warm up"]


def test_load_csv():
    tracks = load_csv(FIX / "playlist_exportify.csv")
    assert len(tracks) == 9
    assert tracks[0].isrc == "USUS10900001"
    assert tracks[0].duration_s == 214
    assert tracks[1].isrc is None
    assert tracks[2].artists == ["CamelPhat", "Elderbrook"]


@pytest.mark.parametrize("value", [
    "37i9dQZF1DXcBWIGoYBM5M",
    "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M",
    "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc",
])
def test_parse_playlist_id(value):
    assert parse_playlist_id(value) == "37i9dQZF1DXcBWIGoYBM5M"


def test_items_page_2026_and_legacy_shapes():
    track = {
        "type": "track", "name": "Strobe", "duration_ms": 637000,
        "artists": [{"name": "deadmau5"}], "external_ids": {"isrc": "X1"},
        "album": {"name": "A"}, "uri": "spotify:track:1",
    }
    page = {"items": [
        {"item": track},
        {"track": dict(track, name="Legacy")},
        {"item": {"type": "episode", "name": "Podcast"}},
        {"item": None},
    ]}
    tracks = tracks_from_items_page(page)
    assert [t.title for t in tracks] == ["Strobe", "Legacy"]
    assert tracks[0].isrc == "X1" and tracks[0].duration_s == 637
