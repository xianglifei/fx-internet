# 网络紧急恢复手册(不需要任何 Coding Agent / 不需要懂这套系统)

> 场景:fx-gateway 切换失败或日后出问题,网络断了,你需要立刻恢复。
> 全文只需要「终端」(Terminal)和「Finder」。**从上往下做,做到哪步网络通了就停。**

---

## 60 秒恢复(首选,先做这个)

**第 1 步:重新打开 Clash Verge**

按 `Command + 空格`,输入 `Clash Verge`,回车打开它。
(或打开 Finder → 应用程序 → 双击 Clash Verge)

**第 2 步:停掉自研栈的内核(必须!否则它和 Verge 抢 7897 端口)**

打开「终端」,粘贴这两行,每行回车(报错 `No such process` 是正常的,说明本来就停了):

```bash
launchctl bootout gui/$(id -u)/local.mihomo
launchctl bootout gui/$(id -u)/local.fx-bridge
```

**第 3 步:等 30 秒**

Verge 会自动把系统代理修正回它自己。网络恢复,结束。

---

## 备用方案 A:第 1 步打不开 Verge / 不想用 Verge

直接关掉系统代理,裸连(国内网站全部正常,国外暂时访问不了):

- **图形界面**:系统设置 → Wi-Fi → 详细信息… → 代理 → 把里面所有开关关掉
- 或终端逐行:

```bash
networksetup -setwebproxystate "Wi-Fi" off
networksetup -setsecurewebproxystate "Wi-Fi" off
networksetup -setsocksfirewallproxystate "Wi-Fi" off
```

网络立即恢复直连。之后有空再按「60 秒恢复」处理。

## 备用方案 B:终端都开不了(极端情况)

系统设置 → 网络 → Wi-Fi → 详细信息 → 代理 → 全部关闭。(系统设置永远可用)

---

## 故障对照表(网络通了之后再看)

| 症状 | 检查 | 处理 |
|---|---|---|
| 网页全打不开 | 终端:`scutil --proxy` | 应显示 7897;若显示别的/空 → 备用方案 A 关代理或走 60 秒恢复 |
| 代理端口有没有活人 | 终端:`nc -z 127.0.0.1 7897 && echo 通` | 不通 = 7897 上没内核 → 60 秒恢复 |
| 自研栈是不是在跑 | 终端:`launchctl list \| grep local` | 看到 local.mihomo 且左侧不是 `-` 即在跑 |
| 想彻底拆掉自研栈 | — | `bash ~/Code/Github/fx-internet/gateway/teardown.sh --full`(自动停栈+重开 Verge) |
| 想重新启用自研栈 | — | `bash ~/Code/Github/fx-internet/gateway/setup.sh --prod` |

## 两条铁律

1. **7897 端口同一时刻只能有一个内核**:恢复 Verge 前必须先停自研内核(`launchctl bootout gui/$(id -u)/local.mihomo`);反之,重启用自研栈前必须先退出 Verge。
2. **改乱了不怕**:重启电脑 + 打开 Verge,永远能回到能上网的状态。

## 菜单栏雷达图标还能用时

点开面板:如果显示红色「内核未运行」,面板里有「重启内核」按钮可以点;
面板本身打不开 = 桥接挂了,不影响网络,按上面手册处理即可。
