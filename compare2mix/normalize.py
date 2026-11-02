"""Normalisation of titles and artists so that Spotify and Rekordbox spellings
can be compared.

Typical differences we need to absorb:

    Spotify   : "Strobe - Radio Edit"            artists=["deadmau5"]
    Rekordbox : "Strobe (Original Mix)"          Artist="Deadmau5"

    Spotify   : "Losing It - Fisher Rework"      ...
    Rekordbox : Name="Losing It", Mix="Fisher Rework"

A title is split into a *base* title plus version information:
  * featured artists ("feat. X")                   -> merged with the artists
  * *format* variants (radio edit, extended mix,
    original mix, remastered...)                   -> same recording/song, only noted
  * *significant* variants (X Remix, VIP, dub,
    live, acoustic, instrumental...)                -> must match
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

_FEAT_RE = re.compile(r"^(feat\.?|ft\.?|featuring|with)\s+(.+)$")

# Variants that do not change "which song / which mix" for our purposes.
_FORMAT_RE = re.compile(
    r"^("
    r"original( mix| version)?"
    r"|extended( mix| version| edit| club mix)?"
    r"|(radio|single|album|short|main|club|clean|explicit|dirty) (edit|version|mix)"
    r"|radio|edit|clean|explicit|dirty|mono|stereo|mono version|stereo version"
    r"|(\d{4} )?(digital(ly)? )?remaster(ed)?( \d{4})?( version)?"
    r"|remastered version|\d{4} mix|\d{4} version"
    r")$"
)

# Keywords that tell us a " - suffix" is version information rather than part
# of the actual title.
_VERSION_KEYWORDS_RE = re.compile(
    r"\b(remix|mix|edit|version|remaster(ed)?|live|dub|vip|rework|bootleg|"
    r"acoustic|instrumental|feat\.?|ft\.?|featuring|reprise|demo|flip|refix)\b"
)

_ARTIST_SPLIT_RE = re.compile(
    r"\s*(?:,|;|/|\s&\s|\s\+\s|\sx\s|\sand\s|\sfeat\.?\s|\sft\.?\s|\sfeaturing\s|\svs\.?\s)\s*"
)


def fold(text: str | None) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = text.replace("’", "'").replace("‘", "'").replace("`", "'")
    text = text.replace("&", " and ")
    text = re.sub(r"'", "", text)  # don't -> dont
    text = re.sub(r"[^\w\s]", " ", text)
    text = text.replace("_", " ")
    return re.sub(r"\s+", " ", text).strip()


def split_artists(text: str | None) -> list[str]:
    """Split a Rekordbox-style artist string ("A, B & C feat. D")."""
    if not text:
        return []
    parts = _ARTIST_SPLIT_RE.split(f" {text.strip()} ")
    return [p.strip() for p in parts if p and p.strip()]


@dataclass
class ParsedTitle:
    base: str  # folded title without version info
    featured: list[str] = field(default_factory=list)  # folded names
    formats: list[str] = field(default_factory=list)  # folded format labels
    significant: list[str] = field(default_factory=list)  # folded, e.g. "fisher rework"

    @property
    def version_key(self) -> str:
        return " ".join(sorted(self.significant))


def _classify(segment: str, parsed: ParsedTitle) -> None:
    raw = segment.strip()
    if not raw:
        return
    low = raw.lower().strip()
    m = _FEAT_RE.match(low)
    if m:
        parsed.featured.extend(fold(a) for a in split_artists(m.group(2)))
        return
    folded = fold(raw)
    if not folded:
        return
    if _FORMAT_RE.match(folded):
        if folded not in ("original", "original mix", "original version"):
            parsed.formats.append(folded)
        return
    parsed.significant.append(folded)


def parse_title(title: str | None, mix: str | None = None) -> ParsedTitle:
    """Split a title (and optional Rekordbox Mix field) into base + version info."""
    title = (title or "").strip()
    segments: list[str] = []

    # Bracketed parts: "Song (feat. X) [Extended Mix]"
    def _grab(m: re.Match) -> str:
        segments.append(m.group(1))
        return " "

    base = re.sub(r"\(([^()]*)\)", _grab, title)
    base = re.sub(r"\[([^\[\]]*)\]", _grab, base)

    # Spotify-style suffixes: "Song - Radio Edit", "Song - X Remix".
    # Only strip when the suffix looks like version info.
    while " - " in base:
        head, tail = base.rsplit(" - ", 1)
        if _VERSION_KEYWORDS_RE.search(tail.lower()) or _FORMAT_RE.match(fold(tail)):
            segments.append(tail)
            base = head
        else:
            break

    # Inline "Song feat. X" without brackets.
    m = re.search(r"\s(feat\.?|ft\.?|featuring)\s(.+)$", base, flags=re.IGNORECASE)
    if m:
        segments.append(f"{m.group(1)} {m.group(2)}")
        base = base[: m.start()]

    parsed = ParsedTitle(base=fold(base))
    for seg in segments:
        _classify(seg, parsed)

    if mix:
        mix_parsed = ParsedTitle(base="")
        _classify(mix, mix_parsed)
        # Avoid duplicates when the Mix is also written in the Name.
        for f in mix_parsed.formats:
            if f not in parsed.formats:
                parsed.formats.append(f)
        for s in mix_parsed.significant:
            if s not in parsed.significant:
                parsed.significant.append(s)
        parsed.featured.extend(x for x in mix_parsed.featured if x not in parsed.featured)

    # Title made only of version info, e.g. "(Intro)": keep something.
    if not parsed.base and title:
        parsed.base = fold(title)
    return parsed


def artist_key(artists: list[str], featured: list[str] | None = None) -> str:
    """Folded, space-joined artist tokens (for token_set comparisons)."""
    names: list[str] = []
    for a in artists:
        names.extend(split_artists(a))
    folded = [fold(n) for n in names] + list(featured or [])
    return " ".join(sorted({f for f in folded if f}))
