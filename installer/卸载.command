#!/bin/bash
# FxInternet 一键卸载:停服务、清系统代理、删全部文件
set -euo pipefail
UID_N=$(id -u)
LA="$HOME/Library/LaunchAgents"
STATE_DIR="$HOME/Library/Application Support/fx-internet"
BIN_DIR="$HOME/.local/bin"
PS_DIR="$HOME/Library/Application Support/verge-node-patrol"

say(){ printf '\033[1;32m[卸载]\033[0m %s\n' "$*"; }
warn(){ printf '\033[1;33m[卸载]\033[0m %s\n' "$*"; }

echo "======================================================"
echo "   FxInternet — 一键卸载"
echo "======================================================"
echo
echo "将完全卸载:停止服务、清除系统代理、删除全部程序文件与运行数据。"
if [[ -t 0 ]]; then
  printf '确认卸载?输入 yes 继续:'
  read -r ANS || true
else
  read -r ANS || true
fi
[[ "$ANS" == "yes" ]] || { echo "已取消,未做任何改动。"; exit 0; }

# 1) 通过桥的受保护流程停内核(先把系统代理清回直连,再停内核)
if TOKEN=$(curl -fsS --max-time 3 http://127.0.0.1:7900/api/token 2>/dev/null \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' 2>/dev/null) \
   && [[ -n "${TOKEN:-}" ]]; then
  curl -fsS --max-time 60 -H "X-FX-Token: $TOKEN" -X POST \
    http://127.0.0.1:7900/api/actions/kernel-stop >/dev/null 2>&1 || true
  say "内核已停止,系统代理已清除"
else
  # 桥不在线:直接把各网络服务的代理设置清掉
  warn "桥接服务不在线,手动清除系统代理…"
  while IFS= read -r svc; do
    svc="$(printf '%s' "$svc" | sed 's/^[[:space:]*]*//;s/[[:space:]]*$//')"
    [[ -z "$svc" ]] && continue
    networksetup -setwebproxy "$svc" off 2>/dev/null || true
    networksetup -setsecurewebproxy "$svc" off 2>/dev/null || true
  done < <(networksetup -listallnetworkservices 2>/dev/null | tail -n +2)
  say "系统代理已清除"
fi
pkill -x FxInternetBar 2>/dev/null || true

# 2) launchd 服务
for L in local.verge.node-patrol local.fx-bridge local.mihomo; do
  launchctl bootout "gui/$UID_N/$L" 2>/dev/null || true
done
rm -f "$LA/local.verge.node-patrol.plist" "$LA/local.fx-bridge.plist" "$LA/local.mihomo.plist"
say "launchd 服务已移除"

# 3) 程序文件与运行数据
rm -rf "$HOME/Applications/FxInternetBar.app"
rm -f "$BIN_DIR/mihomo" "$BIN_DIR/fx-bridge.py" "$BIN_DIR/verge-node-patrol.py"
rm -rf "$BIN_DIR/fx-gateway-ui"
rm -rf "$STATE_DIR" "$PS_DIR"
rm -f "$HOME/Library/Logs/verge-node-patrol.log" \
      "$HOME/Library/Logs/verge-node-patrol.stdout.log" \
      "$HOME/Library/Logs/verge-node-patrol.stderr.log"
say "程序文件与运行数据已删除"

echo
echo "卸载完成,网络已恢复直连。"
[[ -t 0 ]] && { printf '\n按回车键关闭本窗口…'; read -r || true; }
