#!/bin/bash
# FxInternet 一键安装(朋友版):双击运行,粘贴订阅链接即可
# 自包含:内核、geo 资产、菜单栏 App 全在安装包里,无需联网下载其他组件
set -euo pipefail

cd "$(dirname "$0")"
STAGE="$(pwd)"
STATE_DIR="$HOME/Library/Application Support/fx-internet"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
BIN_DIR="$HOME/.local/bin"
UID_N=$(id -u)
APP_SRC="$STAGE/FxInternetBar.app"

say(){ printf '\033[1;32m[安装]\033[0m %s\n' "$*"; }
warn(){ printf '\033[1;33m[注意]\033[0m %s\n' "$*"; }
die(){
  printf '\033[1;31m[安装失败]\033[0m %s\n' "$*" >&2
  printf '\n请截图本窗口,联系分享者求助。按回车键关闭…'
  read -r || true
  exit 1
}

echo "======================================================"
echo "   FxInternet 智能代理 — 一键安装"
echo "======================================================"
echo

# ---- 0. 环境与包完整性 ----
[[ "$(uname)" == "Darwin" ]] || die "仅支持 macOS"
[[ -d "$APP_SRC" ]] || die "找不到 FxInternetBar.app —— 请先把压缩包解压,再运行本程序"
[[ -d "$STAGE/payload" && -d "$STAGE/stack" ]] || die "安装包不完整(缺 payload/stack),请重新下载解压"

case "$(uname -m)" in
  arm64)  MIHOMO_SRC="$STAGE/payload/mihomo-arm64" ;;
  x86_64) MIHOMO_SRC="$STAGE/payload/mihomo-x86_64" ;;
  *) die "不支持的 CPU 架构:$(uname -m)" ;;
esac
[[ -f "$MIHOMO_SRC" ]] || die "找不到内核文件,安装包不完整,请重新下载"

# 一次性去掉下载带来的隔离标记(双击本程序时已获用户许可)
xattr -dr com.apple.quarantine "$STAGE" 2>/dev/null || true

# ---- 1. 订阅链接 ----
echo "请先在机场(你购买流量的网站)后台复制『Clash/Mihomo 订阅链接』"
echo "(通常叫 一键订阅 / Clash 订阅,以 https:// 开头)。"
echo
if [[ -t 0 ]]; then
  SUB_URL=""
  while :; do
    printf '粘贴订阅链接后按回车:'
    read -r SUB_URL || true
    SUB_URL="$(printf '%s' "$SUB_URL" | tr -d '[:space:]')"
    [[ "$SUB_URL" =~ ^https?://. ]] && break
    echo "  ✗ 这不是有效的订阅链接(应以 http:// 或 https:// 开头),请重试。"
  done
else
  # 非交互(脚本化测试):从标准输入读一行
  read -r SUB_URL
  SUB_URL="$(printf '%s' "$SUB_URL" | tr -d '[:space:]')"
  [[ "$SUB_URL" =~ ^https?://. ]] || die "无效订阅链接"
fi
echo

# ---- 2. Python3(全新机器若无,触发系统安装并等待) ----
PY=""
find_python() {
  local c
  for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    [[ -x "$c" ]] || continue
    "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null || continue
    printf '%s' "$c"; return 0
  done
  return 1
}
if PY=$(find_python); then
  say "Python 就绪:$PY"
else
  warn "本机还没有 Python3。马上会弹出『需要安装命令行开发者工具』的系统窗口。"
  warn "请点击其中的『安装』按钮(下载需几分钟),装好后本程序会自动继续 —— 不要关闭本窗口。"
  /usr/bin/python3 -c '' 2>/dev/null || true    # 调起系统安装弹窗
  OK=""
  for i in $(seq 1 120); do                     # 最多等 20 分钟
    if PY=$(find_python); then OK=1; break; fi
    sleep 10
  done
  [[ -n "$OK" ]] || die "等待 Python 安装超时。请确认『命令行开发者工具』已装完,再重新运行本程序。"
  say "Python 就绪:$PY"
fi
echo

# ---- 3. mihomo 内核 + geo 资产 ----
mkdir -p "$BIN_DIR" "$STATE_DIR"
install -m 755 "$MIHOMO_SRC" "$BIN_DIR/mihomo"
say "内核:$("$BIN_DIR/mihomo" -v 2>/dev/null | head -1)"

[[ -f "$STATE_DIR/geosite.dat" ]] || { cp "$STAGE/payload/geosite.dat" "$STATE_DIR/geosite.dat"; say "已就位 geosite.dat"; }
[[ -f "$STATE_DIR/Country.mmdb" ]] || { cp "$STAGE/payload/country.mmdb" "$STATE_DIR/Country.mmdb"; say "已就位 Country.mmdb"; }

# ---- 4. 订阅持久化(仅本机可读) ----
umask 077
printf '%s\n' "$SUB_URL" > "$STATE_DIR/subscription.txt"
umask 022
say "订阅链接已保存(文件仅本用户可读)"

# ---- 5. 部署网关(内核 7897 + 桥接 7900 + 系统代理守护) ----
say "部署网关服务…"
FX_PYTHON="$PY" "$STAGE/stack/gateway/setup.sh" --prod

# ---- 6. 每小时巡检(按实测吞吐调度节点) ----
PLIST="$LAUNCH_AGENTS/local.verge.node-patrol.plist"
sed -e "s|__PYTHON__|$PY|g" \
    -e "s|__HOME__|$HOME|g" \
    -e "s|__SOCKET_HINT__|fx-mihomo.sock|g" \
    -e "s|__NODE_FILTER__||g" \
    -e "s|__QUOTA_MB__|250|g" \
    "$STAGE/stack/patrol/launchd.plist.template" > "$PLIST"
launchctl bootout "gui/$UID_N/local.verge.node-patrol" 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$UID_N" "$PLIST"
say "每小时巡检已安装(自动按实测网速挑最快节点)"

# ---- 7. 菜单栏 App ----
if pgrep -x FxInternetBar >/dev/null 2>&1; then
  killall FxInternetBar 2>/dev/null || true
  sleep 1
fi
rm -rf "$HOME/Applications/FxInternetBar.app"
cp -R "$APP_SRC" "$HOME/Applications/FxInternetBar.app"
open "$HOME/Applications/FxInternetBar.app"
say "菜单栏图标已安装并启动"

# ---- 8. 验证 ----
say "验证中…"
sleep 3
curl -sf --unix-socket "$STATE_DIR/fx-mihomo.sock" http://localhost/version | grep -q '"version"' \
  || die "内核未响应(可能订阅链接无效)。查看日志:$STATE_DIR/logs/"
curl -sf http://127.0.0.1:7900/api/status >/dev/null 2>&1 || die "状态服务未响应"
curl -sf -x http://127.0.0.1:7897 -o /dev/null --max-time 20 https://www.gstatic.com/generate_204 \
  || die "代理连通性验证失败 —— 请确认订阅链接是否正确、套餐是否有效"
say "代理连通 ✓(经 7897 访问外网成功)"

warn "等待系统代理生效(≤30 秒)…"
SYS_OK=""
for i in $(seq 1 15); do
  [[ "$(scutil --proxy | grep -c 7897 || true)" -ge 3 ]] && { SYS_OK=1; break; }
  sleep 2
done
if [[ -n "$SYS_OK" ]]; then
  say "系统代理 ✓(浏览器已自动接管)"
else
  warn "系统代理 30 秒内未生效,可能需要 administrator 授权 —— 稍等 1 分钟再试;"
  warn "若浏览器仍上不了外网,请截图反馈。"
fi

echo
echo "======================================================"
echo "   🎉 安装完成!"
echo
echo "   • 屏幕右上角菜单栏的 雷达图标 就是控制面板"
echo "   • 之后不用管它:节点拥堵/故障自动切换,开机自启"
echo "   • 想卸载:双击安装包里的 卸载.command"
echo "======================================================"
[[ -t 0 ]] && { printf '\n按回车键关闭本窗口…'; read -r || true; }
