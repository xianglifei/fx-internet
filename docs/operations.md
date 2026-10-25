# fx-internet 运维速查

适用:已按 runbook 完成部署的机器。

## 日常一句话操作(给终端 / Coding Agent)

```bash
SOCK=$(ps aux | grep "[v]erge-mihomo" | grep -o '\-ext-ctl-unix [^ ]*' | awk '{print $2}' | head -1)

# 看节点池与延迟历史
curl -s --unix-socket "$SOCK" http://localhost/providers/proxies/JMS

# 全组测速(延迟)
curl -s --unix-socket "$SOCK" "http://localhost/group/AUTO/delay?url=https%3A%2F%2Fwww.gstatic.com%2Fgenerate_204&timeout=5000"

# 当前出口是谁
curl -s --unix-socket "$SOCK" http://localhost/proxies | python3 -c "
import json,sys; d=json.load(sys.stdin)['proxies']
print('PROXY →', d['PROXY']['now'], '| AUTO →', d['AUTO']['now'])"

# 手动钉某个节点 / 交还全自动
curl -s --unix-socket "$SOCK" -X PUT http://localhost/proxies/PROXY -d '{"name":"<节点全名>"}'
curl -s --unix-socket "$SOCK" -X PUT http://localhost/proxies/PROXY -d '{"name":"AUTO"}'

# 强制立即更新订阅(不用等 30 分钟 interval)
curl -s --unix-socket "$SOCK" -X PUT http://localhost/providers/proxies/JMS

# 看某条流量实际走的链路(国内应为 ['DIRECT'])
curl -s --unix-socket "$SOCK" http://localhost/connections | python3 -c "
import json,sys
for c in json.load(sys.stdin).get('connections',[]):
    print(c['metadata'].get('host') or c['metadata'].get('destinationIP'), '→', c['chains'], c['rule'])"
```

## 巡检

```bash
# 手动跑一次(不影响 launchd 调度)
python3 ~/.local/bin/verge-node-patrol.py && tail -5 ~/Library/Logs/verge-node-patrol.log

# 暂停/恢复调度
launchctl bootout gui/$(id -u)/local.verge.node-patrol
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.verge.node-patrol.plist

# 状态(连续达标计数,删除无副作用)
cat ~/Library/Application\ Support/verge-node-patrol/state.json
```

### 日志怎么读(`~/Library/Logs/verge-node-patrol.log`)

| 日志行 | 含义 |
|---|---|
| `AUTO 模式正常(... Mbps),无需干预` | 平稳态,未干预 |
| `拥堵介入:PROXY → ...` | 晚高峰接管,钉住吞吐最优节点 |
| `钉住节点 ... 正常(连续达标 1/2)` | 钉住中,观察回归条件 |
| `拥堵消退:... PROXY 已交还 AUTO` | 自动回到延迟选优 |
| `无更优节点,维持现钉节点` | 防抖生效,拒绝无意义切换 |
| `Verge 内核未运行,跳过` | Verge 没开(正常无害) |
| `所有节点都不可用!` | 订阅/账号层面问题,需人工关注服务商 |

## 从仓库同步部署(改了仓库里的脚本/模板后)

```bash
cp patrol/verge-node-patrol.py ~/.local/bin/verge-node-patrol.py
# launchd 模板需先实例化(替换 __PYTHON__ / __HOME__)再放 LaunchAgents 并 bootstrap
# profile 模板需注入订阅 URL 后覆盖 Verge profiles/jms.yaml,并重启 Verge
```

## 故障排查路径

1. 网页全断 → `scutil --proxy` 应指向 127.0.0.1:7897(Verge 代理守护会自动纠正被抢的系统代理)
2. 代理通但慢 → 看巡检日志当前钉的节点,手动跑一次巡检触发全量扫描
3. API 无响应 → `ps aux | grep verge-mihomo` 确认内核在跑;socket 路径是动态的,每次重新从 ps 取
4. 节点全挂 → JMS 服务商问题(查订阅能否手动 curl 通、账号流量是否耗尽)
