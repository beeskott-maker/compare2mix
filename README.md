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

## Lancer sans terminal (macOS)

Une seule fois, dans le dossier du projet :

```bash
bash macos/creer-app.sh
```

Cela crée l'application **compare2mix** dans `~/Applications` (Spotlight : Cmd+Espace,
« compare2mix » ; glisse-la dans le Dock si tu veux). Un double-clic :

1. met le code à jour (`git pull`, sans jamais écraser une modification locale) et réinstalle les
   dépendances si elles ont changé ;
2. démarre l'interface en arrière-plan (journal : `~/.compare2mix/web.log`), ou la redémarre si le
   code vient d'être mis à jour ;
3. ouvre la page dans le navigateur.

Pour l'arrêter : lien **Quitter** en haut de la page.

## Interface web locale (recommandé)

```bash
compare2mix web
```

La page s'ouvre sur <http://127.0.0.1:8888/> (elle ne tourne que sur ton Mac). C'est un assistant
en 5 étapes :

1. **Playlist** : choix de la playlist Spotify (« À mixer » par défaut). Le témoin en haut à droite
   indique la connexion à Spotify.
2. **Exporter** : exporte ta collection Rekordbox (Fichier → Exporter la collection au format xml)
   dans **Téléchargements**, puis « C'est fait, comparer » ; **ou** dépose directement le fichier
   XML dans la page.
3. **Manquants** : en tête, la bibliothèque lue (tracks dans Rekordbox, nouveaux titres à
   télécharger, tracks absents de la clé USB) ; puis les morceaux manquants. Clic sur un titre =
   copie du lien Spotify ; copie de la liste, export CSV, corrections d'un clic.
4. **Récapitulatif** : fichiers audio trouvés dans Téléchargements comparés aux manquants
   (« 2 / 37 manquants retrouvés »), avec une case par fichier (✅ sûr, 🟡 à vérifier), les fichiers
   non reconnus, les morceaux toujours manquants et la destination. Chaque fichier est renommé
   « ARTISTE - TITRE.ext » d'après ses tags (ou d'après Spotify s'il n'en a pas), sauf s'il porte
   déjà ce nom ; le nouveau nom est affiché ici. La colonne **Genre** est remplie depuis
   [Discogs](https://www.discogs.com) (les deux styles les plus fréquents parmi les disques qui
   contiennent le morceau, ex. « Techno / Acid ») et reste modifiable. La colonne **Énergie**
   (1 à 10, avec l'humeur : sombre, joyeux, planant, agressif) est calculée sur ton Mac par
   analyse audio (voir plus bas). **Rien n'est déplacé ici.**
5. **Copie** : « Lancer la copie » déplace les fichiers cochés vers la clé avec une barre de
   progression. Il reste à les glisser depuis le Finder sur ta playlist dans Rekordbox. L'export
   Rekordbox lu dans Téléchargements est alors renommé « … (traité).xml » : il ne sera plus relu
   (la bibliothèque reste chargée).

« Tracks absents de la clé » : morceaux de ta collection Rekordbox dont le fichier n'est pas sur la
clé USB (restés sur le Mac, liens Apple Music…) et, clé branchée, ceux dont le fichier n'y est
plus.

Le lien **Réglages** (en haut) permet de changer le dossier Téléchargements, le dossier de
rangement sur la clé (avec suggestion), copier au lieu de déplacer, et un jeton Discogs
(facultatif : discogs.com → Paramètres → Développeurs → « Generate new token » ; il fait passer la
limite de 25 à 60 recherches par minute).

À la copie, le genre est écrit dans le tag Genre du fichier et l'énergie dans le tag Commentaire
(« E7 · sombre ») ; ils remplacent ce qui s'y trouvait,
et un fichier sans tags reçoit le titre et les artistes Spotify : Rekordbox les lit quand tu
ajoutes le morceau.

compare2mix n'écrit jamais dans Rekordbox : il range les fichiers, tu les ajoutes toi-même. Si la
clé n'est pas branchée, rien n'est écrit (pas de faux dossier sur le disque du Mac) : branche-la
puis « Revérifier ».

## Analyse d'énergie (facultative)

```bash
pip install essentia-tensorflow   # ou : pip install -e ".[energie]"
```

[Essentia](https://essentia.upf.edu) n'existe que jusqu'à **Python 3.14** (macOS 15 ou plus) : avec
un Python plus récent, crée l'environnement avec `python3.14 -m venv .venv`. Au premier usage, les
modèles d'analyse (~24 Mo, licence CC BY-NC-SA, usage personnel) sont téléchargés dans
`~/.compare2mix/models`. L'analyse se fait sur les deux minutes centrales du morceau (quelques
secondes) et rien ne quitte ton Mac. La note combine l'excitation perçue (*arousal*), le caractère
« festif » et le niveau sonore : environ 4-5 pour un warm-up, 7-8 pour du peak time.

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
- [x] Récupération des fichiers téléchargés, renommés et rangés sur la clé
- [x] Genre détaillé depuis Discogs, écrit dans les tags
- [x] Niveau d'énergie et humeur (analyse audio locale avec Essentia)
- [ ] Comparer toutes les playlists d'un coup (liste d'achats unique)
- [ ] Playlist Spotify « À acheter »
- [ ] Comparaison avec une playlist Rekordbox précise plutôt que toute la collection
- [ ] Application macOS packagée
