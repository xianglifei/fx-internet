#!/bin/bash
# fx-gateway 一次性切换:从 Verge 平滑切到自研栈(交互确认,失败自动回滚)
# 前提:已按 setup.sh 部署试验栈并试运行验收通过
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOME_DIR="$HOME"; UID_N=$(id -u)
STATE_DIR="$HOME/Library/Application Support/fx-internet"
LA="$HOME/Library/LaunchAgents"

say(){ printf '\033[1;36m[cutover]\033[0m %s\n' "$*"; }
STAGE="pre"  # pre: 失败不动现状;live: 已动 Verge,失败必须回滚
die(){
  printf '\033[1;31m[cutover 失败]\033[0m %s\n' "$*" >&2
  exit 1  # live 阶段的回滚由下方 ERR trap 统一执行,勿在此重复调用
}
trap '[[ "$STAGE" == "live" ]] && rollback || true' ERR

rollback(){
  printf '\033[1;31m[cutover 回滚]\033[0m 恢复 Verge 为唯一代理\n' >&2
  launchctl bootout "gui/$UID_N/local.mihomo" 2>/dev/null || true
  launchctl bootout "gui/$UID_N/local.fx-bridge" 2>/dev/null || true
  # 巡检撤掉实例指向并重启加载,交还 Verge(与 teardown --full 一致,否则回退后巡检永久静默跳过)
  /usr/libexec/PlistBuddy -c "Delete :EnvironmentVariables:PATROL_SOCKET_HINT" \
    "$LA/local.verge.node-patrol.plist" 2>/dev/null || true
  launchctl bootout "gui/$UID_N/local.verge.node-patrol" 2>/dev/null || true
  sleep 1
  launchctl bootstrap "gui/$UID_N" "$LA/local.verge.node-patrol.plist" 2>/dev/null || true
  open -a "Clash Verge" || true
  say "已回滚:Verge 重新接管(系统代理 30 秒内由 Verge 守护恢复)"
}

# ---- 0. 预检(全部通过才继续) ----
say "预检…"
curl -sf http://127.0.0.1:7900/api/status | python3 -c "
import json,sys
d = json.load(sys.stdin)
assert d['kernel']['running'], '试验内核未运行'
assert d['kernel']['port'] == 7898, '试验栈不在 7898?'
assert d['patrol']['events'], '试验栈无巡检事件'
print('  试验栈健康:', d['derived']['level'])"
[[ -x "$HOME/.local/bin/mihomo" ]] || die "mihomo 二进制缺失"
ps aux | grep -q "[v]erge-mihomo" || say "(注意:Verge 当前未在运行,可直接切换)"

# ---- 1. 确认 ----
echo
echo "即将执行:退出 Verge → 自研栈接管 7897 → 巡检改管自研内核 → 系统代理由自研守护接管"
read -r -p "输入 yes 继续:" ANS
[[ "$ANS" == "yes" ]] || die "用户取消"

# ---- 2. 退出 Verge 并移除其自启(从这步起进入 live 阶段) ----
STAGE="live"
say "退出 Clash Verge…"
osascript -e 'quit app "Clash Verge"' 2>/dev/null || true
sleep 2
ps aux | grep -q "[v]erge-mihomo" && { pkill -f "/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo" || true; sleep 2; }
# 关掉 Verge 的开机自启(直接改其配置,防止重启后与自研内核抢 7897)
VERGE_YAML="$HOME/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/verge.yaml"
[[ -f "$VERGE_YAML" ]] && sed -i '' 's/^enable_auto_launch:.*/enable_auto_launch: false/' "$VERGE_YAML" || true
osascript -e 'tell application "System Events" to delete login item "Clash Verge"' 2>/dev/null || true
ps aux | grep -q "[v]erge-mihomo" && die "Verge 内核未退出,端口仍被占用"
say "Verge 已退出,其开机自启已关闭(应用保留在 /Applications,随时可手动回退)"

# ---- 3. 生产部署(7897 + 系统代理真实生效) ----
say "以生产模式重新部署自研栈(7897)…"
"$REPO/gateway/setup.sh" --prod

# ---- 4. 巡检指向自研内核 ----
PL="$LA/local.verge.node-patrol.plist"
/usr/libexec/PlistBuddy -c "Add :EnvironmentVariables dict" "$PL" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Delete :EnvironmentVariables:PATROL_SOCKET_HINT" "$PL" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:PATROL_SOCKET_HINT string fx-mihomo.sock" "$PL"
launchctl bootout "gui/$UID_N/local.verge.node-patrol" 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$UID_N" "$PL" 2>/dev/null || launchctl bootstrap "gui/$UID_N" "$PL"
say "每小时巡检已指向自研内核"

# ---- 5. 验证 ----
say "验证内核与代理…"
sleep 3
curl -sf --unix-socket "$STATE_DIR/fx-mihomo.sock" http://localhost/version | grep -q '"version"' || die "内核 API 无响应"
nc -z 127.0.0.1 7897 || die "7897 未监听"
curl -sx http://127.0.0.1:7897 -o /dev/null -w "经 7897 代理: %{http_code}\n" --max-time 15 \
  https://www.gstatic.com/generate_204 | grep -q 204 || die "经新栈代理访问失败"

say "等待系统代理守护生效(≤30s)…"
for i in $(seq 1 15); do
  P=$(scutil --proxy | grep -c "7897" || true)
  [[ "$P" -ge 3 ]] && break
  sleep 2
done
scutil --proxy | grep -q "7897" || die "系统代理未指向 7897(守护未生效,查看 bridge 日志)"

curl -sf http://127.0.0.1:7900/api/status | python3 -c "
import json,sys
d = json.load(sys.stdin)
assert d['kernel']['port'] == 7897
assert d['sysproxy']['apply_mode'] == 'active'
print('  桥接状态:', d['derived']['level'], '/', d['sysproxy']['applied'])"

trap - ERR
say "切换完成。Verge 已退役;菜单栏 FxInternetBar 是唯一入口。"
say "回退方法(任何时候):open -a 'Clash Verge' 并运行 gateway/teardown.sh"
