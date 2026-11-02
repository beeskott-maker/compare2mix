"""Cases observed on a real Rekordbox library ("À mixer" playlist)."""

import pytest

from compare2mix.matcher import Matcher
from compare2mix.models import Status, Track

LIBRARY = [
    Track("Nutcracker - Nihil Young Less Hate Remix - Blue Amazon",
          ["Blue Amazon", "Zak Gee", "Nihil Young"], source_id="nutcracker"),
    Track("Hand Hand Paw - Doctr Remix - Laion", ["Laion", "Doctr"], source_id="laion"),
    Track("Voodoo People - Pendulum Mix - The Prodigy", ["The Prodigy"], source_id="voodoo"),
    Track("The Sound Of San Francisco - Progressive Album Mix - Global Deejays",
          ["Global Deejays"], source_id="sf"),
    Track("Ice In My Eyes - WELKER", ["Release"], source_id="ice"),
    Track("Satisfaction - Just_Us Edit Remix - Benny Benassi", ["Ultra Records"],
          source_id="satisfaction"),
    Track("Sidekick - Deep Fear (Lumberjack & BRØMANCE Remix Edit)", ["Sidekick"],
          source_id="deepfear"),
    Track("Calling 1968 - Dave Van Guten", ["Dave Van Guten"], source_id="calling"),
    Track("Praise You - Purple Disco Machine Remix - Radio Edit",
          ["Fatboy Slim", "Purple Disco Machine"], source_id="praise"),
    Track("Don't You Want Me - KI/KI Remix", ["Felix", "KI", "KI"], source_id="felix"),
    Track("Stolen Dance", ["Milky Chance"], source_id="stolen"),
    Track("I Am", ["Alloy Mental"], source_id="iam"),
    Track("Part 1 - The Beginning", ["Some Band"], source_id="part1"),
]


@pytest.fixture(scope="module")
def matcher():
    return Matcher(LIBRARY)


@pytest.mark.parametrize("title, artists, expected_id", [
    ("Nutcracker - Nihil Young Less Hate Remix", ["Blue Amazon", "Zak Gee", "Nihil Young"], "nutcracker"),
    ("Hand Hand Paw - Doctr Remix", ["Laion", "Doctr"], "laion"),
    ("Voodoo People - Pendulum Mix", ["The Prodigy", "Pendulum"], "voodoo"),
    ("The Sound Of San Francisco - Progressive Album Mix", ["Global Deejays"], "sf"),
    ("Ice In My Eyes", ["WELKER"], "ice"),
    ("Satisfaction - Just_Us Edit Remix", ["Benny Benassi", "The Biz"], "satisfaction"),
    ("Deep Fear - Lumberjack & BRØMANCE Remix Edit", ["Sidekick", "Lumberjack", "BRØMANCE"], "deepfear"),
    ("Calling 1968", ["Dave Van Guten"], "calling"),
    ("Part 1 - The Beginning", ["Some Band"], "part1"),
])
def test_found(matcher, title, artists, expected_id):
    r = matcher.match(Track(title, artists))
    assert r.status == Status.FOUND, (r.score, r.notes)
    assert r.match.source_id == expected_id


@pytest.mark.parametrize("title, artists", [
    ("On My Mind - Purple Disco Machine Remix", ["Diplo", "SIDEPIECE", "Purple Disco Machine"]),
    ("Don't You Want Me - Purple Disco Machine Remix", ["The Human League", "Purple Disco Machine"]),
    ("Stolen Dance", ["Dubdogz", "Zaark"]),
    ("Alloy Mental", ["Alloy Mental"]),
    ("Part 1", ["Other Artist"]),
])
def test_missing(matcher, title, artists):
    r = matcher.match(Track(title, artists))
    assert r.status == Status.MISSING, (r.score, r.match and r.match.title)
