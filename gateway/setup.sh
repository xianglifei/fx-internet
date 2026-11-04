#!/bin/bash
# fx-gateway 组装脚本:把新栈部署为与 Verge 并行的试验实例
#   默认:  mixed-port 7898,系统代理守护 dry-run(只监测不动系统代理)→ 对现状零影响
#   --prod: mixed-port 7897 + 守护真实生效(仅 cutover.sh 调用,勿手动)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOME_DIR="$HOME"
BIN_DIR="$HOME/.local/bin"
STATE_DIR="$HOME/Library/Application Support/fx-internet"
VERGE_DIR="$HOME/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev"
VERGE_APP_BIN="/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
UID_N=$(id -u)

PORT=7898; SYSAPPLY=0
if [[ "${1:-}" == "--prod" ]]; then PORT=7897; SYSAPPLY=1; fi
QUOTA="${FX_QUOTA_MB:-250}"            # 每日探测流量配额(桥展示口径)
PATROL_MINUTE="${FX_PATROL_MINUTE:-10}"  # 巡检调度分钟,需与 patrol plist 保持一致

say() { printf '\033[1;32m[setup]\033[0m %s\n' "$*"; }

# (重)加载 launchd 服务;bootout 后立刻 bootstrap 偶发 I/O error 5,补一次重试
load_service() {  # load_service <label> <plist>
    launchctl bootout "gui/$UID_N/$1" 2>/dev/null || true
    sleep 1
    launchctl bootstrap "gui/$UID_N" "$2" 2>/dev/null || {
        sleep 2
        launchctl bootstrap "gui/$UID_N" "$2"
    }
}

mkdir -p "$BIN_DIR" "$STATE_DIR/logs" "$STATE_DIR/providers" "$LAUNCH_AGENTS"

# 1) mihomo 二进制(优先复用 Verge 内已在本订阅上验证过的版本)
if [[ ! -x "$BIN_DIR/mihomo" ]]; then
    [[ -x "$VERGE_APP_BIN" ]] || { echo "找不到 $VERGE_APP_BIN,请手动下载 mihomo release 放到 $BIN_DIR/mihomo"; exit 1; }
    cp "$VERGE_APP_BIN" "$BIN_DIR/mihomo"
    say "已复制 mihomo 二进制 → $BIN_DIR/mihomo ($("$BIN_DIR/mihomo" -v 2>/dev/null | head -1))"
fi

# 2) geo 资产 + provider 缓存种子(首次启动不必等订阅拉取)
for f in geosite.dat geoip.dat Country.mmdb ASN.mmdb; do
    [[ -f "$STATE_DIR/$f" ]] || { cp "$VERGE_DIR/$f" "$STATE_DIR/$f"; say "已复制 $f"; }
done
if [[ ! -f "$STATE_DIR/providers/jms-sub.yaml" && -f "$VERGE_DIR/providers/jms-sub.yaml" ]]; then
    cp "$VERGE_DIR/providers/jms-sub.yaml" "$STATE_DIR/providers/jms-sub.yaml"
    chmod 600 "$STATE_DIR/providers/jms-sub.yaml"
    say "已播种 provider 缓存"
fi

# 3) 巡检脚本同步(仓库是 source of truth)
cp "$REPO/patrol/verge-node-patrol.py" "$BIN_DIR/verge-node-patrol.py"
say "巡检脚本已同步"

# 4) 生成 runtime.yaml(含 mihomo -t 校验)
/opt/homebrew/bin/python3 "$REPO/gateway/build-config.py" --port "$PORT" --state-dir "$STATE_DIR"

# 5) 实例化并安装 launchd
sed -e "s|__HOME__|$HOME_DIR|g" "$REPO/gateway/mihomo.plist.template" > "$LAUNCH_AGENTS/local.mihomo.plist"
load_service local.mihomo "$LAUNCH_AGENTS/local.mihomo.plist"
say "local.mihomo 已加载(KeepAlive)"

if [[ -f "$REPO/gateway/fx-bridge.py" ]]; then
    cp "$REPO/gateway/fx-bridge.py" "$BIN_DIR/fx-bridge.py"
    mkdir -p "$BIN_DIR/fx-gateway-ui" && cp "$REPO/gateway/ui/index.html" "$BIN_DIR/fx-gateway-ui/index.html"
    if [[ "$PORT" == "7897" ]]; then
        EVENTS="$HOME/Library/Application Support/verge-node-patrol/events.jsonl"
        PSTATE="$HOME/Library/Application Support/verge-node-patrol/state.json"
        PLOG="$HOME/Library/Logs/verge-node-patrol.log"; PPROXY="http://127.0.0.1:7897"
    else
        EVENTS="$STATE_DIR/trial-events.jsonl"; PSTATE="$STATE_DIR/trial-state.json"
        PLOG="$STATE_DIR/trial-patrol.log"; PPROXY="http://127.0.0.1:$PORT"
    fi
    sed -e "s|__HOME__|$HOME_DIR|g" -e "s|__MIXED_PORT__|$PORT|g" -e "s|__SYSAPPLY__|$SYSAPPLY|g" \
        -e "s|__EVENTS__|$EVENTS|g" -e "s|__PSTATE__|$PSTATE|g" -e "s|__PLOG__|$PLOG|g" -e "s|__PPROXY__|$PPROXY|g" \
        -e "s|__QUOTA__|$QUOTA|g" -e "s|__PATROL_MINUTE__|$PATROL_MINUTE|g" \
        "$REPO/gateway/fx-bridge.plist.template" > "$LAUNCH_AGENTS/local.fx-bridge.plist"
    load_service local.fx-bridge "$LAUNCH_AGENTS/local.fx-bridge.plist"
    say "local.fx-bridge 已加载(端口 7900,sysapply=$SYSAPPLY)"
else
    say "(fx-bridge.py 尚未就绪,跳过——内核不受影响)"
fi

# 6) 验证
sleep 2
SOCK="$STATE_DIR/fx-mihomo.sock"
VER=$(curl -s --unix-socket "$SOCK" http://localhost/version || true)
[[ -n "$VER" ]] || { echo "内核 API 未响应,查 $STATE_DIR/logs/mihomo.stderr.log"; exit 1; }
nc -z 127.0.0.1 "$PORT" || { echo "混合端口 $PORT 未监听"; exit 1; }
say "内核就绪:$VER(mixed-port $PORT)"

echo
echo "试验巡检(管这个实例、写独立状态):"
echo "  PATROL_SOCKET_HINT=fx-mihomo.sock PATROL_PROXY=http://127.0.0.1:$PORT \\"
echo "  PATROL_STATE='$STATE_DIR/trial-state.json' PATROL_EVENTS='$STATE_DIR/trial-events.jsonl' \\"
echo "  PATROL_LOG='$STATE_DIR/trial-patrol.log' $BIN_DIR/verge-node-patrol.py"
