"""Matching Spotify tracks against the Rekordbox library."""

from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz, process

from .models import MatchResult, Status, Track
from .normalize import ParsedTitle, artist_key, fold, is_version_info, parse_title

# Thresholds (0–100).
FOUND_THRESHOLD = 90.0
PROBABLE_THRESHOLD = 72.0
DURATION_TOLERANCE_S = 3.0
CANDIDATES = 40
# Penalty when a library title was reinterpreted (artist name removed from it).
VARIANT_PENALTY = 2.0
# Penalty for reading a library track with title and artist swapped.
SWAP_PENALTY = 3.0


@dataclass
class _Prepared:
    track: Track
    parsed: ParsedTitle
    artists: str  # folded artist tokens
    search_key: str
    penalty: float = 0.0
    note: str = ""  # explanation shown when this interpretation wins


def _prepare(track: Track, title: str | None = None,
             extra_artists: tuple[str, ...] = (), penalty: float = 0.0,
             artists: list[str] | None = None, note: str = "") -> _Prepared:
    title = track.title if title is None else title
    artists = list(track.artists if artists is None else artists) + list(extra_artists)
    # Untagged files are often named "Artist - Title" with no artist field.
    if not artists and " - " in title:
        maybe_artist, maybe_title = title.split(" - ", 1)
        artists, title = [maybe_artist], maybe_title
    parsed = parse_title(title, track.mix)
    if track.remixer and not parsed.significant:
        parsed.significant.append(fold(f"{track.remixer} remix"))
    akey = artist_key(artists, parsed.featured)
    return _Prepared(track, parsed, akey, f"{akey} {parsed.base}".strip(), penalty, note)


def _name_like(segment: str) -> bool:
    """Could this title segment be an artist name rather than part of the title?"""
    seg = segment.strip()
    return bool(seg) and len(seg.split()) <= 6 and not is_version_info(seg)


def _library_variants(track: Track) -> list[_Prepared]:
    """Interpretations of a library track.

    Library titles often carry the artist: "Nutcracker - X Remix - Blue Amazon"
    or "Sidekick - Deep Fear (Remix)", sometimes with a label in the artist
    field. Besides the literal reading, we try the title without its first
    and/or last " - " segment, that segment being treated as an artist.
    """
    variants = [_prepare(track)]
    # Title and artist fields swapped in the tags (Artist="U Ain't Lyin'",
    # Title="Peace Control").
    if track.artists and track.title.strip():
        variants.append(_prepare(
            track, title=", ".join(track.artists), artists=[track.title],
            penalty=SWAP_PENALTY, note="titre et artiste inversés dans Rekordbox",
        ))
    segs = track.title.split(" - ")
    if len(segs) < 2:
        return variants
    first, last = segs[0], segs[-1]
    candidates = []
    if _name_like(last):
        candidates.append((" - ".join(segs[:-1]), (last,)))
    if track.artists and _name_like(first):
        candidates.append((" - ".join(segs[1:]), (first,)))
    if len(segs) >= 3 and _name_like(first) and _name_like(last):
        candidates.append((" - ".join(segs[1:-1]), (first, last)))
    own = artist_key(list(track.artists))
    for title, extra in candidates:
        # No penalty when the removed name is already one of the track's artists.
        known = own and all(fuzz.token_set_ratio(fold(e), own) >= 90 for e in extra)
        variants.append(_prepare(track, title, extra, 0.0 if known else VARIANT_PENALTY))
    return variants


def _version_score(a: ParsedTitle, b: ParsedTitle) -> float:
    ka, kb = a.version_key, b.version_key
    if ka == kb:
        return 100.0
    if not ka or not kb:
        return 0.0
    return float(fuzz.token_set_ratio(ka, kb))


def _artist_score(a: str, b: str) -> float:
    if not a or not b:
        return 50.0  # unknown: neither confirm nor reject
    return float(fuzz.token_set_ratio(a, b))


def _score(sp: _Prepared, rb: _Prepared) -> tuple[float, dict[str, float]]:
    title = float(fuzz.token_sort_ratio(sp.parsed.base, rb.parsed.base))
    # Titles where one contains the other ("Strobe" vs "Strobe Pt 1") are close.
    title = max(title, float(fuzz.token_set_ratio(sp.parsed.base, rb.parsed.base)) - 8)
    artist = _artist_score(sp.artists, rb.artists)
    version = _version_score(sp.parsed, rb.parsed)
    total = 0.5 * title + 0.3 * artist + 0.2 * version - rb.penalty

    d1, d2 = sp.track.duration_s, rb.track.duration_s
    if d1 and d2 and abs(d1 - d2) <= DURATION_TOLERANCE_S:
        total += 4
    # A weak title or artist cannot be rescued by the other criteria.
    if title < 60 or artist < 45:
        total = min(total, PROBABLE_THRESHOLD - 0.1)
    elif title < 75 or artist < 60:
        total = min(total, FOUND_THRESHOLD - 0.1)
    return min(total, 100.0), {"title": title, "artist": artist, "version": version}


def _format_label(p: ParsedTitle) -> str:
    return ", ".join(p.formats) or "original"


class Matcher:
    def __init__(self, library: list[Track]):
        # One entry per interpretation of each track (see _library_variants).
        self.library: list[_Prepared] = []
        self._by_isrc: dict[str, list[_Prepared]] = {}
        for track in library:
            variants = _library_variants(track)
            self.library.extend(variants)
            if track.isrc:
                self._by_isrc.setdefault(track.isrc.upper().strip(), []).append(variants[0])
        self._keys = [p.search_key for p in self.library]
        self._bases = [p.parsed.base for p in self.library]
        self._by_base: dict[str, list[int]] = {}
        for i, p in enumerate(self.library):
            self._by_base.setdefault(p.parsed.base, []).append(i)

    def _candidates(self, sp: _Prepared) -> set[int]:
        found = set(self._by_base.get(sp.parsed.base, []))
        for _, _, idx in process.extract(
            sp.search_key, self._keys, scorer=fuzz.token_set_ratio, limit=CANDIDATES
        ):
            found.add(idx)
        if sp.artists:  # also search by title alone, in case artists differ a lot
            for _, _, idx in process.extract(
                sp.parsed.base,
                self._bases,
                scorer=fuzz.ratio,
                limit=5,
            ):
                found.add(idx)
        return found

    def match(self, track: Track) -> MatchResult:
        sp = _prepare(track)

        if track.isrc:
            hits = self._by_isrc.get(track.isrc.upper().strip())
            if hits:
                rb = hits[0]
                res = MatchResult(track, Status.FOUND, rb.track, 100.0, "ISRC")
                res.alternatives = [(h.track, 100.0) for h in hits[1:]]
                self._add_notes(res, sp, rb)
                return res

        if not self.library:
            return MatchResult(track, Status.MISSING)

        best_per_track: dict[int, tuple[float, _Prepared]] = {}
        for idx in self._candidates(sp):
            rb = self.library[idx]
            total, _parts = _score(sp, rb)
            prev = best_per_track.get(id(rb.track))
            if prev is None or total > prev[0]:
                best_per_track[id(rb.track)] = (total, rb)
        scored = sorted(best_per_track.values(), key=lambda x: x[0], reverse=True)

        best_score, best = scored[0]
        if best_score >= FOUND_THRESHOLD:
            status = Status.FOUND
        elif best_score >= PROBABLE_THRESHOLD:
            status = Status.PROBABLE
        else:
            status = Status.MISSING

        res = MatchResult(
            track,
            status,
            best.track if status != Status.MISSING else None,
            round(best_score, 1),
            "fuzzy",
        )
        alt_from = 1 if status != Status.MISSING else 0
        res.alternatives = [
            (rb.track, round(s, 1)) for s, rb in scored[alt_from : alt_from + 3] if s >= 55
        ]
        if res.match is not None:
            self._add_notes(res, sp, best)
        return res

    @staticmethod
    def _add_notes(res: MatchResult, sp: _Prepared, rb: _Prepared) -> None:
        if rb.note:
            res.notes.append(rb.note)
        if sp.parsed.version_key != rb.parsed.version_key:
            res.notes.append(
                f"version différente : « {sp.parsed.version_key or 'originale'} » "
                f"↔ « {rb.parsed.version_key or 'originale'} »"
            )
        elif sorted(sp.parsed.formats) != sorted(rb.parsed.formats):
            res.notes.append(f"format : {_format_label(sp.parsed)} ↔ {_format_label(rb.parsed)}")
        d1, d2 = sp.track.duration_s, rb.track.duration_s
        if d1 and d2 and abs(d1 - d2) > 30:
            res.notes.append(f"durée : {_mmss(d1)} ↔ {_mmss(d2)}")

    def match_all(self, tracks: list[Track]) -> list[MatchResult]:
        return [self.match(t) for t in tracks]


def _mmss(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"
