"""Displaying and exporting results."""

from __future__ import annotations

import csv
import os
import urllib.parse

from .models import MatchResult, Status

_ICONS = {Status.FOUND: "✅", Status.PROBABLE: "🟡", Status.MISSING: "❌"}


def summary(results: list[MatchResult]) -> dict[Status, int]:
    counts = {s: 0 for s in Status}
    for r in results:
        counts[r.status] += 1
    return counts


def search_links(r: MatchResult) -> dict[str, str]:
    q = f"{r.spotify.artist_str} {r.spotify.title}"
    enc = urllib.parse.quote_plus(q)
    return {
        "beatport": f"https://www.beatport.com/search?q={enc}",
        "bandcamp": f"https://bandcamp.com/search?q={enc}",
    }


def print_report(results: list[MatchResult], show: set[Status], title: str = "") -> None:
    counts = summary(results)
    total = len(results)
    if title:
        print(f"\n=== {title} ===")
    print(
        f"\n{total} morceaux : {counts[Status.FOUND]} trouvés, "
        f"{counts[Status.PROBABLE]} probables, {counts[Status.MISSING]} manquants\n"
    )
    for status in (Status.MISSING, Status.PROBABLE, Status.FOUND):
        if status not in show:
            continue
        group = [r for r in results if r.status == status]
        if not group:
            continue
        print(f"{_ICONS[status]} {status.value.upper()} ({len(group)})")
        for r in group:
            line = f"   {r.spotify.display}"
            if r.match is not None:
                line += f"\n      → {r.match.display}  [{r.method} {r.score:.0f}]"
            for note in r.notes:
                line += f"\n        · {note}"
            if status == Status.MISSING and r.alternatives:
                alt, score = r.alternatives[0]
                line += f"\n        · le plus proche : {alt.display} [{score:.0f}]"
            print(line)
        print()


def write_csv(results: list[MatchResult], path: str | os.PathLike) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([
            "statut", "score", "méthode",
            "spotify_artistes", "spotify_titre", "spotify_isrc", "spotify_uri",
            "rekordbox_artistes", "rekordbox_titre", "rekordbox_mix",
            "rekordbox_id", "rekordbox_fichier",
            "remarques", "beatport", "bandcamp",
        ])
        for r in results:
            m = r.match
            links = search_links(r) if r.status != Status.FOUND else {}
            w.writerow([
                r.status.value, r.score, r.method,
                r.spotify.artist_str, r.spotify.title, r.spotify.isrc or "",
                r.spotify.source_id or "",
                m.artist_str if m else "", m.title if m else "", (m.mix or "") if m else "",
                (m.source_id or "") if m else "", (m.location or "") if m else "",
                " | ".join(r.notes),
                links.get("beatport", ""), links.get("bandcamp", ""),
            ])
