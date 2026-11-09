#!/bin/bash
# Builds dist/StockAlerts.dmg (Stock Alerts.app + a shortcut to Applications).  Usage: mac/build_dmg.sh [version]
set -e
cd "$(dirname "$0")/.."
VERSION="${1:-1.0}"
APP="dist/dmg/Stock Alerts.app"
rm -rf dist && mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp mac/launcher.sh "$APP/Contents/MacOS/StockAlerts" && chmod +x "$APP/Contents/MacOS/StockAlerts"
cp stock_news.py watchlist.json README.md mac/AppIcon.icns "$APP/Contents/Resources/"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Stock Alerts</string>
  <key>CFBundleDisplayName</key><string>Stock Alerts</string>
  <key>CFBundleIdentifier</key><string>com.stockalerts.app</string>
  <key>CFBundleExecutable</key><string>StockAlerts</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
</dict></plist>
PLIST
codesign --force --deep -s - "$APP" 2>/dev/null || true   # ad-hoc signature (not notarized)
ln -s /Applications dist/dmg/Applications
hdiutil create -volname "Stock Alerts" -srcfolder dist/dmg -ov -format UDZO dist/StockAlerts.dmg >/dev/null
echo "Built dist/StockAlerts.dmg (version $VERSION)"
