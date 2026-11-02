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

La première fois, le navigateur s'ouvre pour autoriser l'accès. Le Client ID et le jeton sont
ensuite gardés dans `~/.compare2mix/` : plus besoin de les redonner (`compare2mix login --client-id …`
suffit une fois). `compare2mix playlists` liste tes playlists.

**Limites du mode développement Spotify (depuis février 2026)** : le propriétaire de l'app doit
avoir **Spotify Premium**, et seules les playlists dont tu es **propriétaire ou collaborateur**
sont lisibles. Pour une playlist éditoriale ou celle de quelqu'un d'autre, utilise l'option A,
ou duplique-la d'abord dans ta bibliothèque Spotify.

## Interface web locale (recommandé)

```bash
compare2mix web
```

La page s'ouvre sur <http://127.0.0.1:8888/> (elle ne tourne que sur ton Mac) sous forme d'assistant
en 3 étapes. Quand tout est déjà prêt, elle s'ouvre directement à l'étape 2.

1. **Connexion & bibliothèque** : test de la connexion Spotify (Client ID, connexion,
   déconnexion) et envoi du fichier XML Rekordbox (glisser-déposer ou sélection ; par ex. depuis la
   clé USB). Une copie est gardée : à refaire seulement après un nouvel export.
   Plus simple encore : exporte le XML directement dans ton dossier **Téléchargements**, il est
   repéré et chargé tout seul (seulement s'il est plus récent que la bibliothèque actuelle ; le
   fichier n'est pas déplacé).
2. **Résultats** : la playlist par défaut (« À mixer » ou celle marquée ★) est comparée
   automatiquement ; onglets ❌ / 🟡 / ✅, clic sur un titre = copie du lien Spotify, copie de la
   liste des manquants, export CSV, corrections d'un clic (mémorisées).
3. **Téléchargements → Rekordbox** : surveillance du dossier Téléchargements, copie des morceaux
   reconnus vers la clé, puis téléchargement du fichier XML à importer dans Rekordbox.

## Téléchargements → Rekordbox

Quand l'interface web est ouverte, compare2mix surveille ton dossier **Téléchargements**
(et ses sous-dossiers directs, ex. un album Bandcamp décompressé) :

1. chaque nouveau fichier audio (MP3, WAV, AIFF, FLAC, M4A) est lu (tags, sinon nom du fichier)
   et comparé aux morceaux **manquants** des playlists comparées ;
2. correspondance sûre ✅ : le fichier est **copié** vers le dossier choisi (ta clé USB ; l'original
   reste dans Téléchargements, option pour le déplacer) et le morceau passe en ✅ « téléchargé » ;
   correspondance douteuse 🟡 : bouton **Copier vers la clé** / **Ignorer** ;
3. bouton **Télécharger le fichier XML à importer** : il contient la playlist Rekordbox désignée
   (⚙ Réglages) avec **ses morceaux actuels + les nouveaux**. Une copie toujours à jour est aussi
   écrite dans `~/Music/compare2mix/rekordbox-import.xml`.

Dans Rekordbox (réglage à faire une fois) : Préférences → Avancé → Base de données →
« Bibliothèque rekordbox xml » → choisir ce fichier, et Préférences → Affichage → Disposition →
cocher « rekordbox xml ». Ensuite, à chaque fois : barre latérale → rekordbox xml → Listes de
lecture → clic droit sur la playlist → **Importer la liste de lecture**.

compare2mix n'écrit jamais dans la base de Rekordbox (`master.db`) : l'import XML est la voie
officielle et sans risque pour ta collection. Les dossiers, la playlist cible et l'import
automatique se règlent dans ⚙ Réglages.

### Musique et bibliothèque sur une clé USB

Indique dans ⚙ Réglages le dossier de la clé où ranger les morceaux
(ex. `/Volumes/MA CLE/Musique` ; le dossier qui contient déjà l'essentiel de ta collection
est proposé). Charge l'export XML Rekordbox depuis la clé (ex. `/Volumes/MA CLE/rekordbox.xml`).

- Clé débranchée : rien n'est écrit (pas de faux dossier sur le disque du Mac), les fichiers
  restent dans Téléchargements et sont rangés automatiquement dès que la clé est branchée.
  La bibliothèque sur la clé est rechargée à ce moment-là.
- Option « garder aussi une copie dans le dossier surveillé » pour copier au lieu de déplacer.
- Le fichier d'import pour Rekordbox reste par défaut sur le Mac
  (`~/Music/compare2mix/rekordbox-import.xml`), pour que le réglage de Rekordbox ne change pas.

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
`normalize.py`, `matcher.py`, `decisions.py` (choix manuels), `importer.py` (téléchargements), `web.py` + `static/index.html`
(interface), `report.py`, `cli.py`.

## Feuille de route

- [x] Moteur de comparaison + ligne de commande
- [x] Interface web locale (choix de la playlist, validation manuelle des « probables »)
- [x] Mémorisation des correspondances validées à la main
- [x] Surveillance des téléchargements et import dans Rekordbox (via XML)
- [ ] Comparer toutes les playlists d'un coup (liste d'achats unique)
- [ ] Playlist Spotify « À acheter »
- [ ] Comparaison avec une playlist Rekordbox précise plutôt que toute la collection
- [ ] Application macOS packagée
