"""Command-line interface.

    compare2mix compare --xml rekordbox.xml --playlist <url Spotify>
    compare2mix compare --xml rekordbox.xml --csv playlist.csv
    compare2mix playlists
"""

from __future__ import annotations

import argparse
import os
import sys

from . import config, rekordbox, spotify
from .decisions import DecisionStore
from .matcher import Matcher
from .models import Status
from .report import print_report, write_csv

_STATUS_ARGS = {"found": Status.FOUND, "probable": Status.PROBABLE, "missing": Status.MISSING}


CONFIG_FILE = config.CONFIG_FILE


def _client(args) -> spotify.SpotifyClient:
    """Client ID priority: --client-id, then SPOTIFY_CLIENT_ID, then the saved one.

    A Client ID given explicitly is saved for next time.
    """
    saved = config.load(CONFIG_FILE)
    client_id = args.client_id or os.environ.get("SPOTIFY_CLIENT_ID", "")
    if client_id and client_id != saved.get("client_id"):
        config.update(CONFIG_FILE, client_id=client_id)
    return spotify.SpotifyClient(
        client_id or saved.get("client_id", ""),
        redirect_uri=args.redirect_uri,
    )


def _load_library(args):
    if args.xml:
        tracks = rekordbox.load_xml(args.xml)
        src = args.xml
    else:
        tracks = rekordbox.load_master_db(args.db_dir)
        src = "master.db"
    print(f"Bibliothèque Rekordbox : {len(tracks)} morceaux ({src})", file=sys.stderr)
    if args.read_tags:
        n = rekordbox.enrich_isrc_from_tags(tracks)
        print(f"ISRC lus dans les tags : {n}", file=sys.stderr)
    return tracks


def cmd_compare(args) -> int:
    library = _load_library(args)

    if args.csv:
        playlist = spotify.load_csv(args.csv)
        title = os.path.basename(args.csv)
    else:
        client = _client(args)
        pid = spotify.parse_playlist_id(args.playlist)
        title = client.playlist_name(pid)
        playlist = client.playlist_tracks(pid)
    print(f"Playlist « {title} » : {len(playlist)} morceaux", file=sys.stderr)

    matcher, store = Matcher(library), DecisionStore()
    results = [store.resolve(matcher, t) for t in playlist]
    show = {_STATUS_ARGS[s] for s in args.show.split(",") if s in _STATUS_ARGS}
    print_report(results, show, title)
    if args.out:
        write_csv(results, args.out)
        print(f"Résultats exportés dans {args.out}", file=sys.stderr)
    return 0


def cmd_playlists(args) -> int:
    for p in _client(args).my_playlists():
        print(f"{p['id']}  {p['name']}  ({p['tracks']} morceaux, par {p['owner']})")
    return 0


def cmd_login(args) -> int:
    _client(args).login()
    print("Connecté à Spotify.")
    return 0


def cmd_web(args) -> int:
    from . import web

    web.serve(port=args.port, open_browser=not args.no_browser,
              library=args.xml, read_tags=args.read_tags)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="compare2mix",
        description="Compare une playlist Spotify avec ta bibliothèque Rekordbox.",
    )
    sp_opts = argparse.ArgumentParser(add_help=False)
    sp_opts.add_argument("--client-id", help="Client ID de ton app Spotify (ou SPOTIFY_CLIENT_ID)")
    sp_opts.add_argument("--redirect-uri", default=spotify.DEFAULT_REDIRECT,
                         help=f"Redirect URI déclarée dans l'app Spotify (défaut : {spotify.DEFAULT_REDIRECT})")

    sub = parser.add_subparsers(dest="command", required=True)

    c = sub.add_parser("compare", parents=[sp_opts], help="Comparer une playlist")
    lib = c.add_mutually_exclusive_group(required=True)
    lib.add_argument("--xml", help="Export XML de la collection Rekordbox")
    lib.add_argument("--db", action="store_true",
                     help="Lire directement master.db (nécessite pyrekordbox, Rekordbox fermé)")
    c.add_argument("--db-dir", help="Dossier contenant master.db (auto-détecté sinon)")
    src = c.add_mutually_exclusive_group(required=True)
    src.add_argument("--playlist", help="URL, URI ou ID de la playlist Spotify")
    src.add_argument("--csv", help="Playlist exportée en CSV (Exportify…)")
    c.add_argument("--read-tags", action="store_true",
                   help="Lire l'ISRC dans les tags des fichiers audio (nécessite mutagen)")
    c.add_argument("--show", default="missing,probable,found",
                   help="Catégories à afficher : found,probable,missing")
    c.add_argument("-o", "--out", help="Exporter les résultats en CSV")
    c.set_defaults(func=cmd_compare)

    p = sub.add_parser("playlists", parents=[sp_opts], help="Lister tes playlists Spotify")
    p.set_defaults(func=cmd_playlists)

    lg = sub.add_parser("login", parents=[sp_opts], help="Se connecter à Spotify")
    lg.set_defaults(func=cmd_login)

    w = sub.add_parser("web", help="Ouvrir l'interface dans le navigateur")
    w.add_argument("--port", type=int, default=8888,
                   help="Port local (défaut 8888, doit correspondre à la Redirect URI Spotify)")
    w.add_argument("--xml", help="Export XML Rekordbox (sinon : le dernier utilisé ou data/rekordbox.xml)")
    w.add_argument("--read-tags", action="store_true", help="Lire l'ISRC dans les tags des fichiers")
    w.add_argument("--no-browser", action="store_true", help="Ne pas ouvrir le navigateur")
    w.set_defaults(func=cmd_web)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (spotify.SpotifyError, ValueError, RuntimeError, FileNotFoundError) as e:
        print(f"Erreur : {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
