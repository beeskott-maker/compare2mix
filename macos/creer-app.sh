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
# Icône (macos/icon.png, 1024×1024) → .icns, à la place de celle d'AppleScript.
ICON="$REPO/macos/icon.png"
if [ -f "$ICON" ]; then
  SET="$(mktemp -d)/compare2mix.iconset"
  mkdir -p "$SET"
  for size in 16 32 128 256 512; do
    sips -z $size $size "$ICON" --out "$SET/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    sips -z $double $double "$ICON" --out "$SET/icon_${size}x${size}@2x.png" >/dev/null
  done
  iconutil -c icns "$SET" -o "$APP/Contents/Resources/applet.icns"
  rm -rf "$(dirname "$SET")"
  touch "$APP"  # le Finder et le Dock rafraîchissent l'icône
fi

echo "Application créée : $APP"
echo "Tu la trouves dans Spotlight (Cmd+Espace, « compare2mix ») ; glisse-la dans le Dock si tu veux."
