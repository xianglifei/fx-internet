#!/bin/bash
# 拆除 fx-gateway(默认拆试验栈;--full 在切换后回退到 Verge)
#   ./teardown.sh        # 停掉试验栈(7898/7900),Verge 原状不动
#   ./teardown.sh --full # 切换后反悔:停自研栈、巡检改回无 hint、重开 Verge
set -euo pipefail
UID_N=$(id -u)
LA="$HOME/Library/LaunchAgents"

bootout_safe(){ launchctl bootout "gui/$UID_N/$1" 2>/dev/null || true; }

# 使用桥的受保护停机流程：清本实例代理成功后才停内核；失败则保留服务。
STATUS=$(curl -fsS --max-time 5 http://127.0.0.1:7900/api/status)
MODE=$(printf '%s' "$STATUS" | python3 -c 'import json,sys; print(json.load(sys.stdin)["sysproxy"]["apply_mode"])')
if [[ "$MODE" == "active" && "${1:-}" != "--full" ]]; then
  echo "生产栈需使用 --full 回退；未执行拆除" >&2
  exit 1
fi
TOKEN=$(curl -fsS http://127.0.0.1:7900/api/token | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
curl -fsS --max-time 60 -H "X-FX-Token: $TOKEN" -X POST \
  http://127.0.0.1:7900/api/actions/kernel-stop | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("ok") else 1)'
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
