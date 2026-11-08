#!/bin/bash
# 构建"朋友安装包":universal 菜单栏 App + mihomo 双架构内核 + geo 资产 → dist/*.zip
# 产物完全自包含:朋友机器除本 zip 外零下载。仅作者机器运行(需能访问 GitHub,失败自动走本机代理)。
#   用法:installer/build-release.sh [版本号]    # 默认 v1.0.0
set -euo pipefail
VERSION="${1:-v1.0.0}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
CACHE="$REPO/installer/payload-cache"
DIST="$REPO/dist"
PROXY="http://127.0.0.1:7897"

say(){ printf '\033[1;32m[build]\033[0m %s\n' "$*"; }
fetch(){ # fetch <url> <输出文件>:直连失败走本机 7897 代理重试
  curl -fsSL --retry 2 --connect-timeout 15 "$1" -o "$2" \
    || curl -fsSL --retry 2 --connect-timeout 15 -x "$PROXY" "$1" -o "$2"
}
gh_api(){ # gh_api <repo>/releases/latest:输出资产直链列表
  curl -fsSL --connect-timeout 15 "https://api.github.com/repos/$1" \
    || curl -fsSL --connect-timeout 15 -x "$PROXY" "https://api.github.com/repos/$1"
}
pick_asset(){ # pick_asset <repo/releases/latest> <资产名正则>:输出直链
  gh_api "$1" | grep -o '"browser_download_url": *"[^"]*"' | cut -d'"' -f4 | grep -E "$2" | head -1
}

mkdir -p "$CACHE" "$DIST"

# ---- 1) mihomo 内核(双架构) ----
for arch in arm64 amd64; do
  GZ="$CACHE/mihomo-darwin-$arch.gz"
  BIN="$CACHE/mihomo-darwin-$arch"
  if [[ ! -x "$BIN" ]]; then
    if [[ ! -s "$GZ" ]] || ! gunzip -t "$GZ" 2>/dev/null; then
      URL=$(pick_asset "MetaCubeX/mihomo/releases/latest" "mihomo-darwin-$arch-v[0-9.]+\.gz$")
      [[ -n "$URL" ]] || { echo "未在 mihomo latest 找到 darwin-$arch 资产" >&2; exit 1; }
      say "下载 $(basename "$URL")"
      fetch "$URL" "$GZ"
    fi
    gunzip -kf "$GZ"
    chmod +x "$BIN"
  fi
  # 验证:本机架构直接执行;异架构核对 Mach-O magic(不依赖 Rosetta)
  if [[ "$(uname -m)" == "$([[ $arch == arm64 ]] && echo arm64 || echo x86_64)" ]]; then
    "$BIN" -v >/dev/null 2>&1 || { echo "$BIN 无法运行" >&2; exit 1; }
  else
    file "$BIN" | grep -q "$( [[ $arch == arm64 ]] && echo 'Mach-O 64-bit executable arm64' || echo 'Mach-O 64-bit executable x86_64')" \
      || { echo "$BIN 不是预期的 Mach-O" >&2; exit 1; }
  fi
done
say "内核:$( "$CACHE/mihomo-darwin-arm64" -v | head -1)"

# ---- 2) geo 资产(mihomo 内置默认源 MetaCubeX/meta-rules-dat,与内核自愈下载同源) ----
GEO_BASE="https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest"
[[ -s "$CACHE/geosite.dat" ]] || { say "下载 geosite.dat"; fetch "$GEO_BASE/geosite.dat" "$CACHE/geosite.dat"; }
[[ -s "$CACHE/country.mmdb" ]] || { say "下载 country.mmdb"; fetch "$GEO_BASE/country.mmdb" "$CACHE/country.mmdb"; }
[[ -s "$CACHE/geosite.dat" && -s "$CACHE/country.mmdb" ]] || { echo "geo 资产下载失败" >&2; exit 1; }

# ---- 3) universal 菜单栏 App(构建到 dist,不触碰本机运行实例) ----
rm -rf "$DIST/stage-app"
"$REPO/menubar/build.sh" "$DIST/stage-app/FxInternetBar.app" --universal >/dev/null
lipo -archs "$DIST/stage-app/FxInternetBar.app/Contents/MacOS/FxInternetBar" | grep -q x86_64 \
  || { echo "universal 构建失败" >&2; exit 1; }
say "App 构建完成(universal:$(lipo -archs "$DIST/stage-app/FxInternetBar.app/Contents/MacOS/FxInternetBar" | tr ' ' ','))"

# ---- 4) 组装安装包 ----
STAGE="$DIST/FxInternet-安装包"
rm -rf "$STAGE"
mkdir -p "$STAGE/payload" "$STAGE/stack/gateway" "$STAGE/stack/patrol" "$STAGE/stack/clash"

mv "$DIST/stage-app/FxInternetBar.app" "$STAGE/"
rmdir "$DIST/stage-app"

for f in build-config.py fx-bridge.py setup.sh mihomo.plist.template fx-bridge.plist.template; do
  cp "$REPO/gateway/$f" "$STAGE/stack/gateway/"
done
cp -R "$REPO/gateway/ui" "$STAGE/stack/gateway/ui"
cp "$REPO/patrol/verge-node-patrol.py" "$REPO/patrol/launchd.plist.template" "$STAGE/stack/patrol/"
cp "$REPO/clash/profile.yaml.template" "$STAGE/stack/clash/"
cp "$REPO/installer/安装.command" "$REPO/installer/卸载.command" "$REPO/installer/使用说明.txt" "$STAGE/"

cp "$CACHE/mihomo-darwin-arm64" "$STAGE/payload/mihomo-arm64"
cp "$CACHE/mihomo-darwin-amd64" "$STAGE/payload/mihomo-x86_64"
cp "$CACHE/geosite.dat" "$CACHE/country.mmdb" "$STAGE/payload/"

find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} +
chmod +x "$STAGE/安装.command" "$STAGE/卸载.command" "$STAGE/stack/gateway/setup.sh" "$STAGE/stack/patrol/verge-node-patrol.py"

# ---- 5) 打 zip(zip -X 不产 AppleDouble;保留执行位;UTF-8 文件名) ----
ZIP="$DIST/FxInternet-安装包-$VERSION.zip"
rm -f "$ZIP"
(cd "$DIST" && COPYFILE_DISABLE=1 zip -q -r -X "FxInternet-安装包-$VERSION.zip" "FxInternet-安装包")

say "包内容核验:"
unzip -l "$ZIP" | tail -1
MIHOMO_VER=$("$CACHE/mihomo-darwin-arm64" -v | head -1 | awk '{print $NF}')
say "完成:$ZIP($(du -h "$ZIP" | cut -f1),mihomo $MIHOMO_VER)"
echo
echo "发布:gh release create $VERSION \"$ZIP\" --title \"FxInternet $VERSION\" --notes-file <notes>"
