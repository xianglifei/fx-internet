#!/bin/bash
# 构建 FxInternetBar.app(菜单栏入口)并安装到 ~/Applications
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$HOME/Applications/FxInternetBar.app"

# 重建前先停旧实例(rm -rf 正在运行的 .app 会留下幽灵注册)
killall FxInternetBar 2>/dev/null || true
sleep 1

swiftc -O -o /tmp/FxInternetBar "$REPO/menubar/main.swift"

rm -rf "$OUT"
mkdir -p "$OUT/Contents/MacOS"
mv /tmp/FxInternetBar "$OUT/Contents/MacOS/FxInternetBar"

cat > "$OUT/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>              <string>FxInternetBar</string>
    <key>CFBundleIdentifier</key>        <string>local.fxinternet.bar</string>
    <key>CFBundleExecutable</key>        <string>FxInternetBar</string>
    <key>CFBundlePackageType</key>       <string>APPL</string>
    <key>LSUIElement</key>               <true/>
    <key>NSAppTransportSecurity</key>
    <dict>
        <key>NSAllowsLocalNetworking</key> <true/>
    </dict>
</dict>
</plist>
PLIST

codesign --force -s - "$OUT"
echo "已构建 $OUT"
echo "启动:open ~/Applications/FxInternetBar.app"
