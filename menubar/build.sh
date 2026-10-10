#!/bin/bash
# 重新构建 ZUsage.app：编辑 main.swift 后运行 ./build.sh
# 图标：menubar/AppIcon.icns（由 make-icon.py 生成），随 bundle 一起安装
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="$DIR/main.swift"
APP="$HOME/Applications/ZUsage.app"

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
swiftc -O "$SRC" -o "$APP/Contents/MacOS/ZUsage" -framework AppKit
if [ -f "$DIR/AppIcon.icns" ]; then
  cp "$DIR/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
fi

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>ZUsage</string>
    <key>CFBundleIdentifier</key><string>local.zusage.zusage</string>
    <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
    <key>CFBundleName</key><string>ZUsage</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>LSUIElement</key><true/>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

echo "构建完成：$APP"
