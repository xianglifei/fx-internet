# fx-internet 设计文档:需求、决策与踩坑

## 1. 需求起点

原客户端 Jamjams(内嵌 Xray 的 NetworkExtension VPN):UI 好,但**无 CLI、无 API、无 URL scheme**,订阅自动更新形同虚设(配置里 `autoUpdate=true`,实测两周未更新),断线后唯一的恢复手段是人工"断开→更新订阅→测速→连最快"。用户核心诉求:**网络顺畅这件事不应该需要人来维护**。

## 2. 方案选型

| 候选 | 结论 |
|---|---|
| Clash Verge Rev(mihomo GUI) | ✅ 采用。活跃维护、UI 现代、内核暴露 Unix socket API、Tauri 生态 |
| 纯 mihomo headless | 自动化上限最高但日常不友好,留作后续 |
| Mihomo Party / FlClash / Hiddify | 可用但无额外优势 |
| Surge(商业) | 体验好但收费、生态偏 Clash 之外 |

订阅格式:服务商是 Just My Socks,官方提供 `&format=clash` 的 Mihomo YAML 订阅,零转换。技术储备:mihomo v1.19.32 也能直接解析 base64 URI 订阅(实测),作为无官方 YAML 时的后备。

## 3. 关键设计决策

### 3.1 provider 而非 profile 远程订阅
节点列表走 `proxy-providers`(interval 1800s + health-check 300s),profile 本身是本地文件。好处:订阅更新、节点健康、分组策略三者解耦——节点池刷新不需要动配置文件。

### 3.2 双层分组:PROXY(select) → AUTO(url-test)
- AUTO:按延迟自动选优,tolerance 50ms 防抖——**平时的心智默认**
- PROXY:select 组包住 AUTO + 全部节点,给自动化层和人工一个统一的"当前出口"开关
- 巡检钉节点 = 把 PROXY 从 AUTO 切到具体节点;交还 = 切回 AUTO。单一状态变量,无副作用

### 3.3 巡检状态机(核心)
```
平稳态(PROXY=AUTO,轻探测 4MB/h)
  │ 出口 <15Mbps 或挂掉
  ▼
拥堵态(全量扫描 6×8MB,钉最快,≥1.3× 才换)
  │ 连续 2 次达标(≈1h 稳定)
  ▼
复测:AUTO 延迟选优的节点也达标?
  是 → 交还 AUTO        否 → 继续钉住
```
参数依据:晚高峰吞吐差可达 6 倍且分钟级波动→需要吞吐维度+防抖;RETURN_AFTER=2 排除瞬时回暖;探测流量最小化是 JMS 月配额约束。

### 3.4 全天每小时调度(而非仅晚高峰)
夜间钉住的节点白天挂掉时,一小时内自愈;平稳日成本仅 ~4MB/h。

### 3.5 切换的零人工设计
Jamjams 隧道由 root 系统扩展持有,用户态无法释放(实测 pkill / `scutil --nc stop` 均无效)。解法:先关 Jamjams `connectOnStart` + 预置 Verge 自启,然后**重启**即完成切换,无需任何人在场。

## 4. 踩坑记录(工程细节)

1. `sed` 替换串中 `&` 是特殊字符——改订阅 URL 必坏配置,一律用 Python
2. Verge v2.5 API **只有 Unix socket**(TCP 9097 不监听属正常);socket 路径从 `ps aux` 的 `-ext-ctl-unix` 参数动态获取(每次启动不同)
3. `mihomo -t` 校验 GEOSITE 需要 geo 数据已初始化(先让 Verge 首启一次)
4. Verge app 运行时改 `verge.yaml` 会被内存态覆盖;改前退出
5. zsh 不对 `$VAR` 分词,`for n in $NODES` 会把多行当一个整体——遍历节点用 Python
6. launchd 解释器路径:`realpath` 解析到带小版本号的 Cellar 目录,brew 升级即失效;用 `/opt/homebrew/bin/python3` 稳定软链接;`/usr/bin/python3` 在无 CLT 机器上是会弹对话框的 stub
7. curl 并发测速管道接 `wait` 收不到输出;exit 28 是 `--max-time` 正常超时
8. `profiles.yaml` local 条目最少只需 uid/type/name/file,`file` 必须是纯文件名
9. 国内直连生效性验证:下载时查 `/connections` 的 `chains` 字段(应为 `['DIRECT']`)
10. 晚高峰单次测速会"冤枉"节点——防抖(1.3×/连续计数)是必需品,不是优化项

## 5. 已知限制

- 巡检按节点名含 `JMS` 过滤节点池,换服务商需改一行
- 切换节点的瞬间,正在进行的连接会经历一次重路由(秒级,无感但存在)
- JMS 订阅链接 = 节点密码(YAML 明文),分享需按"账号级"信任对待
