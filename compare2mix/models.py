"""Common data structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


@dataclass
class Track:
    """A track, from either Spotify or the Rekordbox library."""

    title: str
    artists: list[str]
    duration_s: float | None = None
    isrc: str | None = None
    album: str | None = None
    # Rekordbox "Mix" field (e.g. "Extended Mix"); empty for Spotify.
    mix: str | None = None
    remixer: str | None = None
    # Spotify URI / Rekordbox TrackID.
    source_id: str | None = None
    # Local file path (Rekordbox only).
    location: str | None = None

    @property
    def artist_str(self) -> str:
        return ", ".join(self.artists)

    @property
    def display(self) -> str:
        title = self.title
        if self.mix and self.mix.lower() not in title.lower():
            title = f"{title} ({self.mix})"
        return f"{self.artist_str} – {title}" if self.artists else title


class Status(str, Enum):
    FOUND = "trouvé"
    PROBABLE = "probable"
    MISSING = "manquant"


@dataclass
class MatchResult:
    spotify: Track
    status: Status
    match: Track | None = None
    score: float = 0.0
    method: str = ""
    notes: list[str] = field(default_factory=list)
    # Other plausible candidates (for manual review).
    alternatives: list[tuple[Track, float]] = field(default_factory=list)
