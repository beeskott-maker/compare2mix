#!/bin/bash
# Crée l'application « compare2mix » dans ~/Applications (une seule fois).
# Elle lance macos/lancer.sh : double-clic → mise à jour + ouverture de la page.
set -eu
REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP="$HOME/Applications/compare2mix.app"
LAUNCHER="$REPO/macos/lancer.sh"
chmod +x "$LAUNCHER"
mkdir -p "$HOME/Applications"
rm -rf "$APP"
osacompile -o "$APP" <<APPLESCRIPT
try
	do shell script quoted form of "$LAUNCHER"
on error msg
	display dialog "compare2mix n'a pas pu démarrer :" & return & return & msg buttons {"OK"} default button "OK" with icon caution
end try
APPLESCRIPT
echo "Application créée : $APP"
echo "Tu la trouves dans Spotlight (Cmd+Espace, « compare2mix ») ; glisse-la dans le Dock si tu veux."
