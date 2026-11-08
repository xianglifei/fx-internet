#!/bin/bash
# 构建 FxInternetBar.app(菜单栏入口)并安装到 ~/Applications
#   用法:build.sh [输出目录] [--universal]
#   默认输出 ~/Applications 并停旧实例;传自定义目录时仅构建,不触碰运行中的实例
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$HOME/Applications/FxInternetBar.app}"

# 重建前先停旧实例(rm -rf 正在运行的 .app 会留下幽灵注册);构建到别处时不需要
if [[ "$OUT" == "$HOME/Applications/FxInternetBar.app" ]]; then
    killall FxInternetBar 2>/dev/null || true
    sleep 1
fi

if [[ "${2:-}" == "--universal" ]]; then
    SDK=$(xcrun --show-sdk-path)
    swiftc -O -target arm64-apple-macos13 -sdk "$SDK" -o /tmp/FxInternetBar.arm64 "$REPO/menubar/main.swift"
    swiftc -O -target x86_64-apple-macos13 -sdk "$SDK" -o /tmp/FxInternetBar.x86_64 "$REPO/menubar/main.swift"
    lipo -create /tmp/FxInternetBar.arm64 /tmp/FxInternetBar.x86_64 -o /tmp/FxInternetBar
    rm -f /tmp/FxInternetBar.arm64 /tmp/FxInternetBar.x86_64
else
    swiftc -O -o /tmp/FxInternetBar "$REPO/menubar/main.swift"
fi

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
echo "已构建 $OUT ($(lipo -archs "$OUT/Contents/MacOS/FxInternetBar" | tr ' ' ','))"
[[ "$OUT" == "$HOME/Applications/FxInternetBar.app" ]] && echo "启动:open ~/Applications/FxInternetBar.app"
exit 0
