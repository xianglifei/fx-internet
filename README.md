# fx-internet

Mac 家庭网络的**智能代理网关自动化方案**:把不可编程的商业 VPN 客户端(Jamjams)替换为全开源可编程栈(Clash Verge Rev / mihomo),并在其上补齐一个"按实测吞吐自动调度节点"的智能层,实现从订阅更新、节点选优到故障自愈的全链路无人值守。

> 首次部署于 2026-10-09~10(felix 的 MacBook Air,macOS 26.3),已稳定运行。第二台 Mac 复用本方案迁移。

## 架构

```
应用流量 ──系统代理──▶ mihomo 内核 (127.0.0.1:7897)
                          │
              ┌───────────┴───────────┐
         规则引擎                  分组
      GEOSITE/GEOIP cn → DIRECT   PROXY (select)
      其余 → MATCH PROXY            └─ AUTO (url-test, 按延迟)
                          ▲                      ▲
        订阅源(每30min)   │                      │ 每小时按吞吐调度
   JMS Clash YAML ──▶ proxy-provider JMS ──▶ 节点池
                                              │
              verge-node-patrol (launchd 每小时 :10)
              经 Unix socket API 实测各节点吞吐:
              拥堵→钉最快节点 | 消退→交还 AUTO | 挂了→立即换
```

为什么需要巡检层:mihomo 的 url-test 只看**延迟**。晚高峰中国→美国链路拥堵时,各节点延迟都在 100~200ms 无区分度,但实测吞吐可差 6 倍(4~30Mbps)且分钟级波动——延迟选优会选中拥堵节点。巡检层补上"吞吐"这个维度。

## 目录结构

| 路径 | 内容 |
|---|---|
| `gateway/` | **fx-gateway:替代 Clash Verge 的自研栈**(内核 launchd 直管 + 桥接服务 + 分级 UI) |
| `gateway/fx-bridge.py` | 桥接服务:UI 服务、/api/status 三态聚合、mihomo API 代理、系统代理守护(30s) |
| `gateway/ui/index.html` | 分级界面:统一紧凑 400 宽三态面板 + 二级详情(绿态更矮) |
| `gateway/setup.sh` / `cutover.sh` / `teardown.sh` | 部署(试运行 7898)/ 一次性切换(7897,带回滚)/ 拆除 |
| `gateway/selftest.sh` | 冒烟 11 项:API、动作、界面等窗断言(playwright) |
| `menubar/` | FxInternetBar 菜单栏 App(Swift,单色雷达图标 + 面板,状态颜色只在面板内表达) |
| `patrol/verge-node-patrol.py` | 节点吞吐巡检(核心智能层,单文件零依赖;v2 增 JSONL 事件 + 实例隔离) |
| `patrol/launchd.plist.template` | macOS launchd 调度模板(`__PYTHON__`/`__HOME__` 占位) |
| `clash/profile.yaml.template` | mihomo profile 模板(`__SUB_URL__` 占位) |
| `docs/runbook-gateway.md` | **fx-gateway 运维手册**(试运行验收、切换、回退、预案) |
| `docs/runbook.md` | Verge 时代迁移手册(切换完成后仅作历史参考) |
| `docs/design.md` | 需求、设计决策与踩坑记录 |
| `docs/operations.md` | 日常运维速查(API、日志解读、常见操作) |
| `docs/optimization-report-2026-11-01.md` | 优化审查报告与修复批次记录 |
| `ui/proposals/` | 界面设计评审稿(HTML) |

## 当前状态:fx-gateway 已切换为生产栈(2026-10-25 起)

自研栈已完全替代 Clash Verge:**launchd 直管 mihomo 内核(7897)+ 桥接(7900)+ 系统代理守护 +
每小时巡检**;菜单栏 FxInternetBar(雷达图标)是唯一入口。Verge 已退出且自启已关闭,应用保留作回退。
断网急恢复看 `docs/emergency-restore.md`;架构与日常操作看 `docs/runbook-gateway.md`。

## 朋友一键安装(非技术用户)

到 [Releases](https://github.com/xianglifei/fx-internet/releases) 下载最新 `FxInternet-Installer-*.zip`(解压得到「FxInternet-安装包」文件夹):

1. 双击「安装.command」(若 macOS 拦截:系统设置 → 隐私与安全性 → 仍要打开)
2. 粘贴机场的 Clash/Mihomo 订阅链接,等待"🎉 安装完成"
3. 菜单栏雷达图标就是控制面板;首次装完可能需按提示装一次 macOS 命令行开发者工具

安装包完全自包含(mihomo 双架构内核 + geo 资产 + universal 菜单栏 App),朋友机器零额外下载;
卸载双击「卸载.command」。构建发布包:`installer/build-release.sh <版本号>`。

## 在新 Mac 上部署

把本目录交给那台机器上的 Coding Agent,指令:

> 阅读 docs/runbook.md,严格按照它执行,核对每步预期输出。授权你在第 4 步需要时重启本机。遇到与预期不符的情况停下来报告。

全程零人工;唯一可能需要向用户申请的是一次重启授权(受 macOS root 系统扩展权限限制,详见 runbook 第 4 步分支 B)。

## 本机(felix MacBook Air)部署状态

仓库是 source of truth,**线上副本**完整对照见 `docs/runbook-gateway.md` 的「仓库 → 线上」表
(fx-bridge / UI / plist / 巡检脚本 / runtime.yaml 的生成关系都在那里,改仓库后需同步部署)。
运行时数据:巡检日志 `~/Library/Logs/verge-node-patrol.log`,状态与事件在
`~/Library/Application Support/verge-node-patrol/`,网关配置与日志在
`~/Library/Application Support/fx-internet/`。
Verge 时代本节的 profiles/jms.yaml 对照表已随切换作废(2026-10-25)。

## 设计原则

1. **零人工闭环**——任何状态(拥堵、恢复、故障)都由系统自己判断并迁移,不存在"请用户手动切回"的路径
2. **配额自适应**——探测流量最小化(平稳态 4MB/小时),只在异常时全量扫描
3. **防抖**——换节点需 ≥1.3× 优势,回归默认策略需连续 2 次达标确认
4. 权限边界诚实——仅真正需要管理员密码/GUI 授权的 macOS 机制(如 TUN 服务模式)留给用户

## 迭代方向(未实现,按需取用)

- 巡检决策通知推送(macOS 通知 / 飞书)
- 巡检数据持久化,输出节点质量趋势报表
- 多订阅/多 provider 支持(巡检过滤已参数化 `PATROL_NODE_FILTER`,默认 `JMS`)
- 按域名/应用维度的分流策略增强
- 面板随 popover 开合降频轮询(当前常驻 2s,桥侧已有缓存,收益已不大)

已完成的优化审查与修复批次见 `docs/optimization-report-2026-11-01.md`(2026-11-01 实施:
真实速率差分、请求路径零子进程、Host 校验、flock 巡检锁、面板转义、runtime.yaml 0600、
cutover 回滚补全、events 有界化)。
