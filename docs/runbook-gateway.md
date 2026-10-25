# fx-gateway 运维手册(替代 Clash Verge 的自研栈)

> 状态:**已切换(2026-10-25 18:15 起)**——自研栈接管 7897 生产流量,Verge 已退出且
> 开机自启已关闭(应用保留在 /Applications 作回退手段)。
> 网络断了先看 `docs/emergency-restore.md`(60 秒手动恢复,无需 Agent)。
> 本手册面向该机器上的 Coding Agent 与用户,承接 `docs/operations.md`(巡检部分仍适用)。

## 架构

```
launchd(用户级)
├─ local.mihomo    mihomo 内核,KeepAlive 崩溃自拉起,7897(试运行期 7898)
├─ local.fx-bridge 桥接服务 127.0.0.1:7900:UI + /api/status 聚合 + mihomo API 代理
│                  + 系统代理守护(30s,试运行 dry-run,切换后真实生效)
└─ local.verge.node-patrol  巡检(每小时 :10,仓库原有,已加 JSONL 事件输出)
FxInternetBar.app  菜单栏入口(单色模板图标,纯查看器,崩溃不影响网络;登录自启)
```

关键设计:内核由 launchd 直管(桥接不是它父进程);停机顺序永远是"先清系统代理再停内核";
桥接统一推导三态(green=平稳 / yellow=已自动处理 / red=需要人),UI 只渲染结论。

## 仓库 → 线上 对照

| 仓库 | 线上 |
|---|---|
| `gateway/fx-bridge.py` | `~/.local/bin/fx-bridge.py`(launchd) |
| `gateway/ui/index.html` | `~/.local/bin/fx-gateway-ui/index.html` |
| `gateway/*.plist.template` | `~/Library/LaunchAgents/local.{mihomo,fx-bridge}.plist`(已实例化) |
| `patrol/verge-node-patrol.py` | `~/.local/bin/verge-node-patrol.py`(setup.sh 自动同步) |
| `clash/profile.yaml.template` | `…/fx-internet/runtime.yaml`(build-config.py 生成,勿手改) |

数据目录 `~/Library/Application Support/fx-internet/`:runtime.yaml、subscription.txt(0600,
从 Verge 迁移后不再依赖它)、geo 四件、providers/、logs/、试验期 trial-* 文件。
订阅由 mihomo proxy-provider 每 1800s 自动更新,**不需要任何 GUI**。

## 日常速查

```bash
# 总状态(三态、出口、系统代理、配额、最近决策,一个 JSON 全有)
curl -s http://127.0.0.1:7900/api/status | python3 -m json.tool

# 界面:浏览器开 http://127.0.0.1:7900 或点菜单栏三色点

# 动作(等价界面按钮)
curl -X POST http://127.0.0.1:7900/api/actions/patrol-now      # 立即巡检
curl -X POST http://127.0.0.1:7900/api/actions/handoff-auto    # 交还常规模式
curl -X POST http://127.0.0.1:7900/api/actions/kernel-restart  # 重启内核
curl -X POST http://127.0.0.1:7900/api/actions/kernel-stop     # 停内核(自动先清系统代理)

# 内核 API(bridge 透传)
curl -s http://127.0.0.1:7900/api/mihomo/proxies | python3 -m json.tool | head

# 冒烟(11 项:API/动作/界面等窗断言)
gateway/selftest.sh

# 日志
tail -20 "$HOME/Library/Application Support/fx-internet/logs/fx-bridge.stdout.log"
tail -20 "$HOME/Library/Application Support/fx-internet/logs/mihomo.stderr.log"
```

## 试运行期验收清单(cutover 前逐项确认)

1. `gateway/selftest.sh` 全绿
2. 菜单栏雷达图标正常显示(单色,随系统外观);点开面板数据正确(出口/吞吐/决策/配额,颜色在面板内表达)
3. 经 7898 代理上网正常:`curl -sx http://127.0.0.1:7898 ...generate_204` 返回 204
4. 界面"立即巡检"后 events 更新、节点池出现实测吞吐
5. 观察至少一次完整的巡检周期(含一次拥堵钉住或平稳达标事件)
6. 重启电脑一次:登录后无需任何操作,内核/桥接/菜单栏点全部自动恢复
7. Verge 侧全程不受影响(其日志无异常、7897 正常)

## 切换(cutover.sh 做什么)

预检 → 交互确认 → 退出 Verge 并移除其自启 → `setup.sh --prod`(7897 + 系统代理守护真实生效)
→ 每小时巡检 plist 注入 `PATROL_SOCKET_HINT=fx-mihomo.sock` 指向自研内核
→ 端到端验证(内核/代理/系统代理/桥接三态)→ 任一步失败**自动回滚**(重开 Verge)。

切换后:`scutil --proxy` 应仍指向 127.0.0.1:7897(值没变,接管者变了)。

## 回退

```bash
gateway/teardown.sh          # 只拆试验栈(切前用)
gateway/teardown.sh --full   # 切换后反悔:停自研栈,巡检改回,重开 Verge
```

## 已知边界与预案

- **macOS 26 networksetup 拒权**:桥接守护检测到"requires admin privileges"(退出码 14)时,
  /api/status 置 `sysproxy.requires_admin`,界面转黄提示。预案(届时再做):装一个一次性
  root LaunchDaemon 执行代理写入(参照 Verge 的 service mode 思路),或临时 `sudo` 手动设一次。
- **TUN 模式**:本栈不启用(与 Verge 现状一致)。将来要 TUN 需 root 运行 mihomo,另行评估。
- **订阅更新**:完全由内核 provider 自管;若换订阅地址,改 `…/fx-internet/subscription.txt`
  后 `POST /api/actions/apply-config`(或重跑 build-config.py)。
- **mihomo 升级**:换 `~/.local/bin/mihomo` 二进制后 `kernel-restart`;版本回退同理。
- **geo 数据**:内核目录内独立副本,与 Verge 无关;更新即替换文件 + 重启。

## 与旧文档的关系

`docs/operations.md` 的巡检日志解读、故障排查路径仍然适用;端口/入口相关的命令以本手册为准。
`docs/runbook.md` 是 Verge 时代的迁移手册,切换完成后仅作历史参考。
