# compare2mix

Compare une playlist Spotify avec ta bibliothèque **Rekordbox** et indique, pour chaque morceau :

- ✅ **trouvé** : il est dans ta collection (avec une remarque si c'est un autre format, ex. *Radio Edit* ↔ *Extended Mix*) ;
- 🟡 **probable** : un candidat proche existe, à vérifier (ex. remix différent) ;
- ❌ **manquant** : absent, avec liens de recherche Beatport / Bandcamp dans l'export CSV.

## Installation (macOS)

```bash
git clone https://github.com/beeskott-maker/compare2mix.git
cd compare2mix
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[tags]"
```

## 1. Exporter la bibliothèque Rekordbox

Dans Rekordbox : **Fichier → Exporter la collection au format xml**. Enregistre par exemple
`data/rekordbox.xml` (le dossier `data/` est ignoré par git).

> Alternative : lire directement `master.db` avec `pip install -e ".[db]"` puis `--db`
> (Rekordbox doit être fermé). Cette voie n'a pas encore été testée sur une vraie installation.

## 2. Récupérer la playlist Spotify

### Option A : export CSV (le plus simple, marche pour n'importe quelle playlist)

Exporte la playlist avec [Exportify](https://exportify.app), puis :

```bash
compare2mix compare --xml data/rekordbox.xml --csv data/ma_playlist.csv -o data/resultats.csv
```

### Option B : API Spotify (directement par l'URL de la playlist)

1. Crée une app sur <https://developer.spotify.com/dashboard>.
2. Ajoute la Redirect URI `http://127.0.0.1:8888/callback` et coche *Web API*.
3. Copie le **Client ID** (pas besoin du secret) :

```bash
export SPOTIFY_CLIENT_ID=xxxxxxxx
compare2mix compare --xml data/rekordbox.xml --playlist "https://open.spotify.com/playlist/…"
```

La première fois, le navigateur s'ouvre pour autoriser l'accès. Le jeton est ensuite gardé dans
`~/.compare2mix/`. `compare2mix playlists` liste tes playlists.

**Limites du mode développement Spotify (depuis février 2026)** : le propriétaire de l'app doit
avoir **Spotify Premium**, et seules les playlists dont tu es **propriétaire ou collaborateur**
sont lisibles. Pour une playlist éditoriale ou celle de quelqu'un d'autre, utilise l'option A,
ou duplique-la d'abord dans ta bibliothèque Spotify.

## Options utiles

| Option | Effet |
|---|---|
| `--read-tags` | lit l'**ISRC** dans les tags des fichiers audio (correspondance exacte quand il est présent) |
| `--show missing` | n'afficher que les manquants (`found`, `probable`, `missing`) |
| `-o fichier.csv` | export complet (chemins des fichiers, liens d'achat…) |

## Comment fonctionne la comparaison

1. **ISRC** identique : trouvé, sans ambiguïté.
2. Sinon, **normalisation** :
   - casse, accents, ponctuation ;
   - `feat.` déplacé vers les artistes ;
   - formats neutres ignorés (*Original Mix, Radio Edit, Extended Mix, Remastered…*) ;
   - versions significatives conservées (*X Remix, VIP, Dub, Live…*) ;
   - le champ *Mix* et le champ *Remixer* de Rekordbox sont pris en compte ;
   - les fichiers non taggés « Artiste - Titre » sont gérés.
3. **Score approximatif** ([rapidfuzz](https://github.com/rapidfuzz/RapidFuzz)) : titre 50 %, artistes 30 %, version 20 %, avec un petit bonus si les durées concordent.

## Développement

```bash
pip install -e ".[dev,tags]"
pytest
```

Structure : `compare2mix/rekordbox.py` (lecture XML / master.db), `spotify.py` (API + CSV),
`normalize.py`, `matcher.py`, `report.py`, `cli.py`.

## Feuille de route

- [x] Moteur de comparaison + ligne de commande
- [ ] Interface web locale (choix de la playlist, validation manuelle des « probables »)
- [ ] Mémorisation des correspondances validées à la main
- [ ] Comparaison avec une playlist Rekordbox précise plutôt que toute la collection
- [ ] Application macOS packagée
