#!/bin/bash
# 拆除 fx-gateway(默认拆试验栈;--full 在切换后回退到 Verge)
#   ./teardown.sh        # 停掉试验栈(7898/7900),Verge 原状不动
#   ./teardown.sh --full # 切换后反悔:停自研栈、巡检改回无 hint、重开 Verge
set -euo pipefail
UID_N=$(id -u)
LA="$HOME/Library/LaunchAgents"

bootout_safe(){ launchctl bootout "gui/$UID_N/$1" 2>/dev/null || true; }

bootout_safe local.mihomo
bootout_safe local.fx-bridge
rm -f "$LA/local.mihomo.plist" "$LA/local.fx-bridge.plist"
echo "[teardown] 自研内核与桥接已停止,launchd 项已移除"

if [[ "${1:-}" == "--full" ]]; then
  # 巡检去掉实例 hint(回到"发现任何内核"的原始行为,交给 Verge)
  /usr/libexec/PlistBuddy -c "Delete :EnvironmentVariables:PATROL_SOCKET_HINT" \
    "$LA/local.verge.node-patrol.plist" 2>/dev/null || true
  launchctl bootout "gui/$UID_N/local.verge.node-patrol" 2>/dev/null || true
  sleep 1
  launchctl bootstrap "gui/$UID_N" "$LA/local.verge.node-patrol.plist"
  echo "[teardown] 巡检已改回管理 Verge"
  open -a "Clash Verge"
  echo "[teardown] Verge 已启动并将在 30 秒内恢复系统代理"
fi

echo "[teardown] 说明:~/Library/Application Support/fx-internet(配置/资产)与 ~/.local/bin/mihomo 保留,重装只需再跑 setup.sh"
