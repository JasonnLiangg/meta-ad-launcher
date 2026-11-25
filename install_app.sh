#!/bin/zsh
# Builds ~/Applications/Ad Launcher.app (opens start.sh) and puts a shortcut on the Desktop.
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Applications/Ad Launcher.app"
mkdir -p "$HOME/Applications"
TMP=$(mktemp -d)
cat > "$TMP/launcher.applescript" <<APPLESCRIPT
on run
	do shell script "'$SRC/start.sh' > /dev/null 2>&1 &"
end run
APPLESCRIPT
rm -rf "$APP"
osacompile -o "$APP" "$TMP/launcher.applescript"
mkdir "$TMP/icon.iconset"
for s in 16 32 64 128 256 512; do
  sips -z $s $s "$SRC/app/icon.png" --out "$TMP/icon.iconset/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$SRC/app/icon.png" --out "$TMP/icon.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$TMP/icon.iconset" -o "$APP/Contents/Resources/$(/usr/libexec/PlistBuddy -c "Print :CFBundleIconFile" "$APP/Contents/Info.plist").icns"
# The template's built-in icon catalog overrides the .icns, so remove it
/usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" "$APP/Contents/Info.plist" 2>/dev/null || true
rm -f "$APP/Contents/Resources/Assets.car"
codesign --force --deep -s - "$APP" 2>/dev/null   # re-sign after swapping the icon
touch "$APP"
rm -rf "$TMP"
if [[ ! -e "$HOME/Desktop/Ad Launcher" ]]; then
  osascript -e "tell application \"Finder\" to make alias file to POSIX file \"$APP\" at desktop" >/dev/null
fi
echo "Installed: $APP"
