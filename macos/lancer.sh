#!/bin/bash
# Lance compare2mix sans terminal : met le code à jour (git pull), réinstalle
# si les dépendances ont changé, démarre l'interface en arrière-plan et ouvre
# la page. Si elle tourne déjà, l'ouvre simplement (ou la redémarre si le code
# vient d'être mis à jour). Utilisé par l'application créée par creer-app.sh.
set -u
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin:$PATH"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PORT=8888
URL="http://127.0.0.1:$PORT/"
STATE="$HOME/.compare2mix"
LOG="$STATE/web.log"
PIDFILE="$STATE/web.pid"
APP_BIN="$REPO/.venv/bin/compare2mix"
PIP="$REPO/.venv/bin/pip"
mkdir -p "$STATE"

notify() {
  osascript -e "display notification \"$1\" with title \"compare2mix\"" >/dev/null 2>&1 || true
}
running() {
  curl -s -o /dev/null -m 2 "${URL}api/status"
}

if [ ! -x "$APP_BIN" ]; then
  echo "Environnement Python introuvable : $REPO/.venv" >&2
  exit 1
fi
cd "$REPO" || exit 1
echo "---- $(date '+%Y-%m-%d %H:%M:%S') lancement" >>"$LOG"

# 1. Mise à jour du code (sans jamais écraser de modification locale).
before="$(git rev-parse HEAD 2>/dev/null)"
if ! git pull --ff-only -q >>"$LOG" 2>&1; then
  notify "Mise à jour impossible (git pull) : la version actuelle est lancée."
fi
after="$(git rev-parse HEAD 2>/dev/null)"
updated=0
if [ -n "$before" ] && [ "$before" != "$after" ]; then
  updated=1
  if git diff --name-only "$before" "$after" | grep -qx 'pyproject.toml'; then
    notify "Mise à jour des dépendances…"
    "$PIP" install -q -e ".[tags]" >>"$LOG" 2>&1
  fi
fi

# 2. Déjà lancée : on ouvre la page, ou on redémarre pour prendre le nouveau code.
if running; then
  if [ "$updated" = 0 ]; then
    open "$URL"
    exit 0
  fi
  [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null
  for _ in $(seq 1 20); do running || break; sleep 0.25; done
fi

# 3. Démarrage en arrière-plan, journal dans ~/.compare2mix/web.log.
nohup "$APP_BIN" web --no-browser >>"$LOG" 2>&1 </dev/null &
echo $! >"$PIDFILE"
for _ in $(seq 1 60); do running && break; sleep 0.25; done
if ! running; then
  echo "Le démarrage a échoué : voir ~/.compare2mix/web.log" >&2
  exit 1
fi
[ "$updated" = 1 ] && notify "Mis à jour et lancé."
open "$URL"
