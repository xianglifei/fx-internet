# fx-internet 优化点审查报告(2026-11-01)

> **实施状态:§6 批次已于同日全部落地并部署验证通过**(见文末「7. 实施记录」)。
> 审查方式:静态读码(全部核心源文件)+ 线上实测(7900 API、文件权限、进程 CPU、事件流)。
> 审查时线上状态:green/steady,内核 v1.19.32,已连续运行 7 天;部署副本与仓库逐字节一致。
> 优先级:P1 = 确认的缺陷 / P2 = 健壮性·安全·性能 / P3 = 维护性卫生。

## 结论速览

| # | 项 | 级别 | 位置 |
|---|---|---|---|
| 1 | 面板"实时速率"实为**连接累计流量**,不是速率(已实测证实) | P1 | `gateway/fx-bridge.py:96-100` |
| 2 | `cutover.sh` 双重 `trap ERR`,**pre 阶段失败也会拆栈回滚**(第二台 Mac 迁移会踩) | P1 | `gateway/cutover.sh:18,27` |
| 3 | 回滚路径不清理巡检 `PATROL_SOCKET_HINT`,回退后巡检永久静默跳过 | P2 | `gateway/cutover.sh:20-26` |
| 4 | 面板 3 处 `innerHTML` 未转义订阅源节点名 → 注入者可同源调用网关全部 API | P2 | `gateway/ui/index.html:227,258-264,272-277` |
| 5 | `runtime.yaml` 0644 且含订阅 URL(token 明文);设计上只给 `subscription.txt` 做了 0600 | P2 | `gateway/build-config.py:97-106` |
| 6 | 桥接不校验 `Host`/`Origin`:DNS rebinding 与恶意页面 POST 可达所有动作 API | P2 | `gateway/fx-bridge.py:319-374` |
| 7 | `patrol.lock` 是"带 10 分钟保底的孤儿锁":桥重启后 `patrol-now` 可被永久阻塞;launchd 巡检又不走此锁 | P2 | `gateway/fx-bridge.py:282-295` |
| 8 | 常驻空转:popover 关闭后页面仍每 2s 全量轮询 ×2,桥每请求现起 curl 子进程(桥 7 天均值 0.7% CPU) | P2 | `ui/index.html:288-297` + `fx-bridge.py:230-234,91-103` |
| 9 | `events.jsonl` 无限增长(现 172 行/42K,含 sweep 全量数据);`read_events` 每 2s 全文件读 | P2 | `patrol/verge-node-patrol.py` + `fx-bridge.py:213-219` |
| 10 | 配额纯展示:今日已 184/250MB,双 sweep 日可破 300MB;`QUOTA_MB` 桥/selftest 双处硬编码 | P3 | `fx-bridge.py:57`,`selftest.sh:18` |
| 11 | 杂项卫生:`next_patrol_time` 硬编码 :10、JMS 过滤硬编码、`state.json` 非原子写、坏 JSON 打崩请求、`/opt/homebrew` 散布硬编码、README 两段落已过时、无 CI | P3 | 见 §4 |

已验证无虞的部分:**仓库=线上**(fx-bridge.py / patrol / index.html 逐字节 diff 一致);停机顺序、launchd KeepAlive、订阅 URL 不入仓库、`emergency-restore.md` 兜底文档、selftest 含 UI 等窗断言——这些设计执行得都对,不重复展开。

---

## 1. P1-1 面板"实时速率"是错的:显示的是累计流量

`monitor_loop` 把每个活跃连接的 `download + upload` 直接求和换算成 "Mbps":

```python
# fx-bridge.py:96-100
for c in (conns.get("connections") or []):
    if chains and chains[-1] != "DIRECT":
        mbps += (c.get("download", 0) + c.get("upload", 0)) * 8 / 1e6
```

mihomo `/connections` 里这两个字段是**该连接自建立以来的累计字节**,不是瞬时速率(各官方面板都是拉两次做差分才算速度)。

**实测证据(今日)**:面板显示 `178.0 Mbps`、17 条代理连接,与 `/connections` 累计字节合计 `178.5 Mb` 完全吻合;3 秒后跳到 `179.0`。真实带宽当时是个位数 Mbps。附带效应:只要存在任何活跃代理连接,数值几乎不可能 ≤0.05,于是 UI 里"无实时数据时回落显示最近实测吞吐"的兜底分支(`ui/index.html:225`)实际永远走不到。

**修法(小改)**:`monitor_loop` 已是 2s 固定节拍,记下上一轮的 `/connections` 全局 `downloadTotal/uploadTotal`(mihomo 响应顶层自带),本轮减上一轮除以 2s 即真实 Mbps;内核重启导致计数器清零时钳到 0。连接级速率如不需要就不用逐条算。

## 2. P1-2 cutover.sh 的 ERR trap 被 second trap 覆盖,pre 阶段失败会拆掉健康试验栈

```bash
# cutover.sh:18   本意:仅 live 阶段失败才回滚
trap '[[ "$STAGE" == "live" ]] && rollback || true' ERR
# cutover.sh:27   这行把它整个覆盖了 → 任何阶段任何失败都 rollback
trap 'rollback' ERR
```

后果:预检失败(如试验栈不在 7898)或用户在确认处输入非 `yes`,`die`/`set -e` 退出都会触发 `rollback()`——bootout 正在健康运行的 `local.mihomo` 与 `local.fx-bridge` 并拉起 Verge,与注释 "pre: 失败不动现状" 相反。另外 live 阶段 `die` 会先手动 rollback、再被 trap 再 rollback 一次(幂等但脏)。

**修法**:删掉 :27 这行即可恢复 :18 的语义;`die` 内已有 live 分支调用,可把 `die` 简化为只负责报错退出。README 明言"第二台 Mac 复用本方案迁移",此脚本会被再次真实执行,建议在下一批改动里一起修。

## 3. P2 健壮性 / 安全

**#3 回滚不还原巡检指向** — `rollback()` 只 bootout 双服务并拉起 Verge,但第 4 步写入巡检 plist 的 `PATROL_SOCKET_HINT=fx-mihomo.sock` 不清;回退后巡检按"hint 已设、找不到自己的 socket"策略永久静默跳过(这是 patrol 故意防误管的正确行为,但 rollback 没接住)。修法:rollback 里加 PlistBuddy Delete hint + 重 bootstrap 巡检(即 `teardown.sh --full` 已有的那三行)。

**#4 面板 innerHTML 注入面** — 一级出口名(`:227`)、节点池(`:258-264`)、决策时间线(`:272-277`)都把订阅源控制的节点名原文拼进 `innerHTML`。WKWebView 页面与桥同源,注入的 JS 可直接 fetch 全部 `/api/actions/*` 与 `/api/mihomo/*`——等于订阅服务商(或其被劫持的订阅响应)可遥控网关。修法:加一个 5 行的 `esc()` 并在所有插值处使用;节点名本来也可能含 `&`/`<` 之类的显示噪声,转义顺带修正显示。

**#5 runtime.yaml 明文 token + 0644** — `build-config.py` 写 tmp 后直接 `os.replace`,未 chmod;生成物含订阅 URL(design.md 自己也把订阅链接定性为"节点密码")。一行 `os.chmod(tmp, 0o600)` 在 replace 前加上即可;`subscription.txt` 已经是 0600,这里补齐一致性。

**#6 桥不校验 Host/Origin** — 7900 只绑 127.0.0.1,但浏览器里任意网页可 POST `http://127.0.0.1:7900/api/actions/kernel-stop`(simple request 不被 CORS 拦截,副作用照样发生),DNS rebinding 也能绕过同源策略读到状态。修法(一次性,约 6 行):`do_GET/do_PUT/do_POST` 入口校验 `self.headers["Host"] ∈ {127.0.0.1:7900, localhost:7900}`,不匹配即 403。不影响菜单栏与浏览器正常访问。

**#7 巡检锁的两处裂缝** — a) 锁只在桥进程内有人删(`threading.Timer` 600s):桥一旦在触发后重启,锁文件成孤儿,`patrol-now` 返回"已在运行"直到有人手工删文件——修法:加锁时把 pid/时间戳写进锁文件,取锁时若发现锁龄 >15 分钟即清掉重拿;更稳的是 patrol 侧自己 `fcntl.flock`,桥与 launchd 共用同一把。b) launchd 每小时的巡检根本不走这把锁,手动+定时并发时双巡检互踩(后果轻,双侧都 PUT PROXY,最后写者胜)——flock 方案顺带解决。

**#8 常驻空转开销** — 面板页面常驻(不随 popover 关闭休眠),`setInterval(poll, 2000)` 每拍 2 个请求(status + provider 列表);桥侧 `build_status` 每请求现起一个 curl 打 mihomo,`monitor_loop` 又每 2s 两个 curl。合计常态 ~4 次/秒的进程派生,桥 7 天平均 CPU 0.7%(对它干的事不算少)。修法分三层,按性价比排序:
1. **provider 列表降频**:它只在订阅更新(30min)时变化,UI 端改 60s 或只在 events 有新 sweep 时拉;
2. **桥内缓存**:`monitor_loop` 顺手把 `/proxies` 也抓进缓存,`build_status` 纯读内存,请求路径上不再有 curl(顺带消除 mihomo 卡顿时 2s 轮询叠 curl 8s 超时的线程堆积);
3. **popover 可见性联动**:原生侧在 `togglePopover` 时经 messageHandler 通知页面切 2s/30s 双档轮询。

**#9 events 无限增长** — 每 2s 全文件读 + JSONL 只增不删。7 天 42KB 尚可,一年 ~2MB×每 2s 读一次会开始可感。修法:patrol 写入侧行数超 1000 时重写保留尾 500;或最省事——`read_events` 改为从文件尾部读定长(如 64KB)。注意 patrol 是独立进程,桥侧尾部读可独立先行。

## 4. P3 维护性卫生(打包一批顺手做)

- **`next_patrol_time()` 硬编码 :10**(`fx-bridge.py:224-227`):与 `patrol/launchd.plist.template` 的 24 个 `<dict>` 是同一事实的两次书写。可从环境变量 `FX_PATROL_MINUTE` 读,模板与桥共用。
- **`QUOTA_MB=250` 双处硬编码**(`fx-bridge.py:57`、`selftest.sh:18` 断言):改环境变量,seed 到 plist 模板;selftest 断言同步读 env。
- **JMS 过滤硬编码**(`patrol:130`):README 已列为已知限制,建议至少把 `"JMS"` 提为 `PATROL_NODE_FILTER` 环境变量默认值,迁服务商时零改码。
- **`save_state` 非原子**(`patrol:59-62`):写 tmp + `os.replace`,与 build-config 同款;当前崩溃最多丢 `good_streak`,危害小。
- **坏 JSON 打崩请求**(`fx-bridge.py:358`):`do_PUT` 的 `json.loads` 无兜底,畸形 body 会 500 断连;`do_PUT` 也未限 Content-Length。包一层 try + 64KB 上限。
- **`/opt/homebrew/bin/python3` 散布**(setup.sh、cutover.sh、fx-bridge.py:291、两个 plist 模板):design.md 已论证这是刻意选择(brew 升级安全),保留,但建议集中成 setup.sh 里一个 `PY=` 变量注入模板,减少四处同步。
- **README 两处过时**:「本机部署状态」表还是 Verge 时代的路径(profiles/jms.yaml),与 runbook-gateway 的对照表矛盾;「迭代方向」第 4 条"内核异常自动拉起"已被 launchd KeepAlive 满足。删改这两段,防新机器迁移时读错表。
- **无 CI**:加一个最小 GitHub Actions(macos-14 runner):`bash -n` 三个 sh、`python3 -m py_compile` 两个 py、`mihomo -t` 校验模板生成物(可用假 sub URL + `--state-dir /tmp`)。半天工作量,防"手滑提交坏模板"这类回归。

## 5. 流量成本观察

今日(11-01)巡检开销 184MB:19×steady_ok(4MB)+ 1×congestion_pin(52MB)+ 1×pin_ok + 1×handoff(52MB)。机制上拥堵日两次全量扫描即可到 ~300MB/天,而配额条 250MB 满了也只是显示截断,巡检不减速。若 JMS 月配额吃紧,两个低成本旋钮:平稳态 `PROBE_BYTES` 4MB→2MB(夜间 0-7 点可降到 1MB);`SWEEP_BYTES` 8MB→6MB(1.3× 防抖的判据精度几乎不受影响)。属设计取舍,非缺陷,按需取用。

## 6. 建议的实施批次(一次交付,不做渐进)

按既有偏好一次性打包为单批改动 + selftest 验证 + 部署同步(仓库→`~/.local/bin`→重载双服务),全程可回退(bootout + 重启即回旧版):

1. **代码批**:fx-bridge(mbps 差分、/proxies 缓存、Host 校验、锁龄兜底、PUT 兜底、QUOTA/PATROL_MINUTE 环境变量化)+ patrol(flock、events 截断、save_state 原子化、过滤参数化)+ build-config(chmod 0600)+ cutover.sh(trap 修正 + rollback 补 hint 清理)+ UI(esc 转义、provider 降频)+ plist 模板(新环境变量)。
2. **断言批**:selftest 增补——速率字段语义(两次采样差分合理性)、Host 校验 403 分支、quota env;UI 断言维持 400 宽/150·210 高不变(本轮不涉视觉改动,无需 HTML 评审稿)。
3. **文档批**:README 两段过时内容修订;runbook-gateway 增"锁文件/事件文件运维"两行。

净效果:面板数字变为真实速率,第二台 Mac 迁移时 cutover 不会再拆健康栈,面板对订阅源注入免疫,桥的常驻 CPU 约降一个数量级,长期运行文件不再无界增长。

## 7. 实施记录(2026-11-01 同日落地)

| 报告项 | 落点 |
|---|---|
| #1 速率差分 | `monitor_loop` 改用 `downloadTotal/uploadTotal` 2s 差分(含直连的全局口径),内核重启计数清零钳 0 |
| #2/#3 cutover | 删除覆盖性 `trap 'rollback' ERR`;`die` 只报错退出,回滚统一走条件 ERR trap;rollback 补巡检 hint 清理 + 重载 |
| #4 转义 | UI 增加 `esc()`,一级出口/节点池/时间线/配额 4 处 innerHTML 全部转义;`skip` 文案按 reason 分流 |
| #5 0600 | build-config 新生成与旧文件两条路径都补 `chmod 0600` |
| #6 Host | `_host_ok()` 于 GET/PUT/POST 入口校验,违规 403;selftest 增伪 Host 403 + 回环放行两条断言 |
| #7 flock | patrol 侧 `acquire_lock()`(flock 非阻塞,进程退出自动释放);桥的 patrol-now 改为非阻塞探测后让位拉起;废弃 O_EXCL + Timer 旧机制 |
| #8 空转 | 监测线程缓存 /proxies,`/api/status` 纯读内存(请求路径零 curl);UI provider 拉取 2s→60s |
| #9 有界化 | patrol 侧 events 超 256KB 原子截留 500 行;桥侧 `read_events` 尾读 64KB |
| #10/#11 卫生 | `FX_QUOTA_MB`/`FX_PATROL_MINUTE`/`PATROL_NODE_FILTER` 环境变量化(模板+setup 注入);`state.json` 原子写;PUT 兜底(坏 JSON 400、64KB 上限);README 过时两段修订;runbook 增「运行时边界」 |

部署方式(零网络影响):仅同步三个脚本 + UI 与 plist 实例化,**只 bootout/bootstrap `local.fx-bridge`**,
内核 `local.mihomo` 与巡检 plist 未动,系统代理无写入(mihomo 持续在跑,7897 不断流)。
selftest 以 `FX_SKIP_RESTART=1` 全绿(内核重启项跳过属预期,见 #10 表述)。

### 部署中额外发现并修复:桥启动竞态

首次重载暴露一个旧有隐患:guard_loop 首拍早于监测线程首拍时,`MON.kernel` 尚为 None 被误判为
"内核不在",走"清系统代理"分支,造成重启后最长 30s 的系统代理空窗(本次实测系统代理被清、
30s 内由下一拍自动恢复)。修复:guard_loop 在 `MON.ts==0`(尚无实测数据)时只等待不下判断;
监测线程体加兜底异常捕获。已二次重载验证:重启全程系统代理未被清,HTTP/HTTPS/SOCKS 三项
全程指向 7897。
