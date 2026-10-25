# Mac 代理迁移手册 v2:Jamjams → Clash Verge Rev + 全自动节点巡检

> **版本**:v2(2026-10-10)。源自 2026-10-09~10 在另一台 Mac(macOS 26.3, Apple Silicon)上完整执行并稳定运行的最终方案。
> **相对 v1 的变化**:① 新增全天候节点吞吐巡检闭环(第 3 步);② 切换流程改为零人工设计(第 4 步);③ 修正 Jamjams plist 路径笔误、去掉所有写死的用户名;④ 附录新增 zsh 分词、launchd 解释器路径等实测坑。

---

## 给执行本任务的 Coding Agent 的总则

1. **严格按步骤顺序执行**,每步核对「预期输出」;不符时停下报告,不要自行发明步骤。
2. **零人工原则**:这套系统的设计目标是无人值守。流程中不允许出现“请用户点一下”这类步骤。唯一允许向用户申请的是**一次重启授权**(见第 4 步),这是 macOS 对 root 系统扩展的权限限制,不是流程缺陷。建议用户下达任务时就写明“允许你在需要时重启这台机器”。
3. **三条铁律**:
   - 绝不在 Jamjams 隧道仍连接时启动 Clash Verge(会双隧道嵌套,网络严重劣化)
   - 不要卸载 Jamjams、不要开 TUN 模式(都留给用户决定)
   - 不要修改用户浏览器或其他网络设置
4. 所有脚本用 `os.path.expanduser("~")` / `$HOME` 取家目录,**不要写死用户名**。
5. 改含 `&` 的 URL 一律用 Python,**不要用 sed**(见附录 A-1)。

---

## 背景与最终状态

本机在用 Jamjams(内嵌 Xray、无 CLI/API 的 NetworkExtension VPN,订阅来自 Just My Socks),断线只能手动维护。迁移目标——**完成后机器上应有**:

| 组件 | 作用 |
|---|---|
| Clash Verge Rev(mihomo 内核) | 系统代理 `127.0.0.1:7897`,开机自启、静默启动、代理守护 |
| proxy-provider `JMS` | 每 30 分钟自动拉取 JMS 官方 Clash YAML 订阅;每 5 分钟健康检查 |
| 分组 `PROXY`(select) → `AUTO`(url-test) | 平时按延迟自动选节点 |
| 规则 GEOSITE/GEOIP cn → DIRECT | 国内流量直连不绕代理 |
| **节点吞吐巡检**(launchd 每小时) | 晚高峰按**吞吐**自动钉最优节点,拥堵消退自动交还 AUTO,节点挂掉自动换 |
| Jamjams | 保持断开闲置,`connectOnStart` 已关(不卸载) |

为什么需要巡检:url-test 只看延迟。晚高峰中国→美国链路拥堵时六个节点延迟都差不多(100~200ms),但实测吞吐可差 6 倍(4~30Mbps)且分钟级波动,AUTO 常选中拥堵节点。

---

## 第 0 步:环境检查与信息提取

### 0.1 基础环境

```bash
ls /Applications/Jamjams.app >/dev/null 2>&1 && echo "Jamjams 存在" || echo "未找到 Jamjams,停止并报告用户"
which brew && brew --version | head -1
xcode-select -p >/dev/null 2>&1 && echo "CLT 已装" || echo "CLT 未装(见附录 A-12)"
```

预期:Jamjams 存在;brew 已安装。**brew 未安装**时先装 brew(官方脚本;GitHub 直连失败则用 gitee 镜像脚本),或退化为从 [clash-verge.org](https://clash-verge.org/download) 下载 DMG 拖入 /Applications。

### 0.2 当前 VPN 状态(决定第 4 步分支)

```bash
scutil --nc list | grep -i fiberlogic
route -n get default 2>/dev/null | awk '/interface:/ {print $2}'
```

记录:VPN 是 Connected 还是 Disconnected;默认路由接口是 `utunX`(隧道占线)还是 `en0`(空闲)。

### 0.3 提取 JMS 订阅链接(不要使用别的机器的链接)

```bash
python3 - <<'EOF' > /tmp/jms_sub_url
import plistlib, json, os
p = os.path.expanduser("~/Library/Containers/net.fiberlogic.jamjams-standalone/Data/Library/Preferences/net.fiberlogic.jamjams-standalone.plist")
d = plistlib.load(open(p, 'rb'))
g = json.loads(d['groups'])
url = g['subscriptions'][0]['type']['subscription']['url']
assert 'getsub.php' in url, 'subscriptions 结构异常: ' + json.dumps(g.get('subscriptions'))[:200]
print(url)
EOF
SUB=$(cat /tmp/jms_sub_url)
echo "$SUB" | sed -E 's/(service=)[0-9]+&(id=)[a-f0-9-]+/\1***\2***/'    # 脱敏确认
printf '%s&format=clash\n' "$SUB" > /tmp/jms_sub_url_clash                 # 官方 Mihomo/Clash YAML 格式
```

预期:形如 `https://jmssub.net/members/getsub.php?service=***id=***`。**提取失败**时向用户索要 JMS 客户区「Subscription → Mihomo / Clash.Meta YAML subscription」链接,写入 `/tmp/jms_sub_url_clash`。

验证链接返回的是 YAML(不是 base64):

```bash
curl -s --max-time 15 "$(cat /tmp/jms_sub_url_clash)" -o /tmp/jms_clash.yaml -w "http_code: %{http_code}, size: %{size_download}B\n"
head -c 150 /tmp/jms_clash.yaml; echo
grep -cE "^\s+type: (ss|vless|vmess|trojan)" /tmp/jms_clash.yaml
```

预期:`http_code: 200`;内容以 `mixed-port:`/`proxies:` 等 YAML 键开头;节点数 ≥ 1(JMS 通常 6 个)。

---

## 第 1 步:安装 Clash Verge Rev

```bash
brew install --cask clash-verge-rev 2>&1 | tail -3
```

预期:`🍺 clash-verge-rev was successfully installed!`;App 在 `/Applications/Clash Verge.app`,内核 `.../Contents/MacOS/verge-mihomo`(v1.19.x)。

首次启动一次以初始化配置目录和 geo 数据(**Jamjams 隧道连着也没关系**,此时 Verge 不激活任何代理,10 秒后退出):

```bash
open -a "Clash Verge"; sleep 10
osascript -e 'tell application "Clash Verge" to quit' 2>/dev/null; sleep 3
CFG="$HOME/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev"
ls "$CFG" | tr '\n' ' '; echo
```

预期:看到 `verge.yaml profiles.yaml profiles geosite.dat Country.mmdb` 等(第 2.5 步校验依赖 geo 文件)。

---

## 第 2 步:写配置(Verge 处于退出状态)

### 2.1 生成 profile `profiles/jms.yaml`

```bash
python3 - <<'EOF'
import os
url = open('/tmp/jms_sub_url_clash').read().strip()
cfg = os.path.expanduser("~/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev")
tpl = '''# Just My Socks 自动化配置
mode: rule
log-level: info
unified-delay: true
tcp-concurrent: true

dns:
  enable: true
  enhanced-mode: fake-ip
  fake-ip-range: 198.18.0.1/16
  fake-ip-filter:
    - "*.lan"
    - "*.local"
    - "+.msftconnecttest.com"
    - "+.msftncsi.com"
  default-nameserver:
    - 223.5.5.5
    - 119.29.29.29
  nameserver:
    - https://223.5.5.5/dns-query
    - https://120.53.53.53/dns-query

proxy-providers:
  JMS:
    type: http
    url: "__SUB_URL__"
    interval: 1800
    path: ./providers/jms-sub.yaml
    health-check:
      enable: true
      url: https://www.gstatic.com/generate_204
      interval: 300
      timeout: 3000

proxy-groups:
  - name: PROXY
    type: select
    proxies:
      - AUTO
    use:
      - JMS
  - name: AUTO
    type: url-test
    use:
      - JMS
    url: https://www.gstatic.com/generate_204
    interval: 300
    tolerance: 50

rules:
  - GEOSITE,private,DIRECT
  - GEOSITE,cn,DIRECT
  - GEOIP,private,DIRECT,no-resolve
  - GEOIP,CN,DIRECT
  - MATCH,PROXY
'''
open(os.path.join(cfg, 'profiles', 'jms.yaml'), 'w').write(tpl.replace('__SUB_URL__', url))
print('profile 已写入')
EOF
```

### 2.2 写 profile 索引 `profiles.yaml`(整文件覆盖)

```bash
CFG="$HOME/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev"
cat > "$CFG/profiles.yaml" <<'EOF'
# Clash Verge

current: jms
items:
  - uid: jms
    type: local
    name: Just My Socks
    desc: JMS 自动订阅 + 自动切换
    file: jms.yaml
EOF
```

### 2.3 Verge 应用开关 `verge.yaml`

```bash
sed -i '' -e 's/^enable_system_proxy: false/enable_system_proxy: true/' \
  -e 's/^enable_auto_launch: false/enable_auto_launch: true/' \
  -e 's/^enable_silent_start: false/enable_silent_start: true/' \
  -e 's/^enable_proxy_guard: false/enable_proxy_guard: true/' \
  "$CFG/verge.yaml"
grep -E "^enable_(system_proxy|auto_launch|silent_start|proxy_guard)" "$CFG/verge.yaml"
```

预期:四项全为 `true`。某项若不存在(版本差异),在文件末尾追加 `键: true`。

### 2.4 关闭 Jamjams 开机自动连接(断根,重启后才不会抢网)

```bash
python3 - <<'EOF'
import plistlib, json, os
p = os.path.expanduser("~/Library/Containers/net.fiberlogic.jamjams-standalone/Data/Library/Preferences/net.fiberlogic.jamjams-standalone.plist")
d = plistlib.load(open(p, 'rb'))
s = json.loads(d.get('settings', b'{}'))
s.setdefault('general', {})['connectOnStart'] = False
d['settings'] = json.dumps(s, separators=(',', ':')).encode()
plistlib.dump(d, open(p, 'wb'))
print('connectOnStart 已改为 false')
EOF
```

### 2.5 校验(此时仍不启动 Verge)

```bash
/Applications/Clash\ Verge.app/Contents/MacOS/verge-mihomo -t -d "$CFG" -f "$CFG/profiles/jms.yaml" 2>&1 | tail -1
```

预期:`... test is successful`。失败就不要往下走(GEOSITE 报错 → 第 1 步初始化未完成,重做 `open -a`)。

---

## 第 3 步:安装全自动节点巡检(切换前装好,之后无需 Agent 在场)

巡检脚本在 Verge 未运行时静默退出,所以现在装是安全的;这样即使第 4 步走重启路径,重启后整套系统也是完整的。

### 3.1 写脚本

```bash
mkdir -p ~/.local/bin ~/Library/Logs
cat > ~/.local/bin/verge-node-patrol.py <<'PYEOF'
#!/usr/bin/env python3
"""节点吞吐巡检 v2 —— 全自动闭环,无任何人工环节(Clash Verge Rev / mihomo)

状态机:
- 平稳态:PROXY=AUTO(延迟选优),每小时轻探测当前出口(~4MB)
- 拥堵态:出口吞吐 < GOOD_MBPS → 全量扫描,PROXY 钉到实测最快节点(≥1.3× 才换)
- 回归:钉住节点连续 RETURN_AFTER 次达标 → 全量扫描复测,若 AUTO 的延迟选优节点
  也达标(说明拥堵消退、吞吐不再是瓶颈),自动把 PROXY 切回 AUTO
- 兜底:出口挂了(gstatic 204 失败)→ 立即全量扫描换节点;Verge 没跑 → 静默退出

流量开销:平稳日 ~4MB/小时 + 少量全量扫描(6节点×8MB),约 150-250MB/天。
由 ~/Library/LaunchAgents/local.verge.node-patrol.plist 每小时 :10 触发。
"""
import subprocess, json, re, os, datetime

MIXED_PROXY = "http://127.0.0.1:7897"
PROBE_BYTES = 4_000_000         # 平稳态轻探测流量
SWEEP_BYTES = 8_000_000         # 全量扫描时每节点流量
GOOD_MBPS = 15.0                # 达标线
BETTER_RATIO = 1.3              # 换节点需比当前好这个倍数
RETURN_AFTER = 2                # 钉住节点连续达标 N 次后,尝试回归 AUTO
LOG_PATH = os.path.expanduser("~/Library/Logs/verge-node-patrol.log")
STATE_PATH = os.path.expanduser(
    "~/Library/Application Support/verge-node-patrol/state.json")

def log(msg):
    with open(LOG_PATH, "a") as f:
        f.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")

def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {"good_streak": 0}

def save_state(st):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(st, f)

def find_socket():
    ps = subprocess.run(["ps", "aux"], capture_output=True, text=True).stdout
    m = re.search(r"-ext-ctl-unix (\S+)", ps)
    return m.group(1) if m else None

def api(sock, method, path, data=None):
    cmd = ["curl", "-s", "--unix-socket", sock, "-X", method,
           f"http://localhost{path}", "--max-time", "6"]
    if data is not None:
        cmd += ["-d", json.dumps(data)]
    try:
        return json.loads(subprocess.run(
            cmd, capture_output=True, text=True, timeout=10).stdout)
    except Exception:
        return {}

def short(name):
    return name.split("@")[-1] if "@" in name else name

def curl_measure(url, max_time):
    cmd = ["curl", "-sx", MIXED_PROXY, "-o", "/dev/null",
           "-w", "%{http_code} %{speed_download}", "--max-time", str(max_time), url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=max_time + 5).stdout.split()
        if len(r) >= 2:
            return r[0], float(r[1])
    except Exception:
        pass
    return "000", 0.0

def alive():
    return curl_measure("https://www.gstatic.com/generate_204", 8)[0] == "204"

def measure(bytes_):
    if not alive():
        return False, 0.0
    _, sp = curl_measure(f"https://speed.cloudflare.com/__down?bytes={bytes_}", 14)
    return True, sp * 8 / 1e6

def sweep(sock, nodes):
    """逐节点实测,返回 {节点名: Mbps}(挂了的为 0)"""
    results = {}
    for n in nodes:
        api(sock, "PUT", "/proxies/PROXY", {"name": n})
        ok, m = measure(SWEEP_BYTES)
        results[n] = m if ok else 0.0
        log(f"  扫描 {short(n)}: {'挂了' if not ok else f'{m:.1f}Mbps'}")
    return results

def main():
    sock = find_socket()
    if not sock:
        log("Verge 内核未运行,跳过")
        return
    proxies = api(sock, "GET", "/proxies").get("proxies", {})
    nodes = [n for n in proxies.get("PROXY", {}).get("all", []) if "JMS" in n]
    if not nodes:
        log("未找到 JMS 节点(provider 异常?),跳过")
        return

    state = load_state()
    cur_group = proxies.get("PROXY", {}).get("now", "")
    effective = (proxies.get("AUTO", {}).get("now", "")
                 if cur_group == "AUTO" else cur_group)
    pinned = cur_group != "AUTO"

    ok, mbps = measure(PROBE_BYTES)
    if not ok:
        log(f"出口 {short(effective)} 无响应,立即全量扫描")
        res = sweep(sock, nodes)
        live = {n: m for n, m in res.items() if m > 0}
        if not live:
            log("所有节点都不可用!")
            save_state({"good_streak": 0})
            return
        best = max(live, key=live.get)
        api(sock, "PUT", "/proxies/PROXY", {"name": best})
        log(f"切换 PROXY → {short(best)} ({live[best]:.1f}Mbps)")
        save_state({"good_streak": 0})
        return

    if not pinned:
        # 平稳态:PROXY=AUTO
        if mbps >= GOOD_MBPS:
            state["good_streak"] = state.get("good_streak", 0) + 1
            save_state(state)
            log(f"AUTO 模式正常({short(effective)} {mbps:.1f}Mbps),无需干预")
            return
        log(f"AUTO 选中的 {short(effective)} 吞吐仅 {mbps:.1f}Mbps,全量扫描")
        res = sweep(sock, nodes)
        live = {n: m for n, m in res.items() if m > 0}
        if not live:
            log("所有节点都不可用!")
            save_state({"good_streak": 0})
            return
        best = max(live, key=live.get)
        if live[best] >= max(mbps * BETTER_RATIO, 5.0):
            api(sock, "PUT", "/proxies/PROXY", {"name": best})
            log(f"拥堵介入:PROXY → {short(best)} ({live[best]:.1f}Mbps,"
                f"原 AUTO/{short(effective)} {mbps:.1f}Mbps)")
        else:
            api(sock, "PUT", "/proxies/PROXY", {"name": "AUTO"})
            log("无更优节点,保持 AUTO")
        save_state({"good_streak": 0})
        return

    # 拥堵态:PROXY 钉在具体节点
    if mbps < GOOD_MBPS:
        log(f"钉住节点 {short(effective)} 降至 {mbps:.1f}Mbps,全量扫描")
        res = sweep(sock, nodes)
        live = {n: m for n, m in res.items() if m > 0}
        if not live:
            log("所有节点都不可用!")
            save_state({"good_streak": 0})
            return
        best = max(live, key=live.get)
        if live[best] >= max(mbps * BETTER_RATIO, 5.0) and best != effective:
            api(sock, "PUT", "/proxies/PROXY", {"name": best})
            log(f"换钉:PROXY → {short(best)} ({live[best]:.1f}Mbps)")
        else:
            api(sock, "PUT", "/proxies/PROXY", {"name": effective})
            log("无更优节点,维持现钉节点")
        save_state({"good_streak": 0})
        return

    # 钉住节点达标:考虑把控制权交还 AUTO
    state["good_streak"] = state.get("good_streak", 0) + 1
    streak = state["good_streak"]
    if streak < RETURN_AFTER:
        save_state(state)
        log(f"钉住节点 {short(effective)} 正常({mbps:.1f}Mbps,"
            f"连续达标 {streak}/{RETURN_AFTER})")
        return
    log(f"钉住节点连续 {streak} 次达标,复测判断拥堵是否消退")
    res = sweep(sock, nodes)
    live = {n: m for n, m in res.items() if m > 0}
    if not live:
        log("所有节点都不可用!")
        save_state({"good_streak": 0})
        return
    auto_pick = proxies.get("AUTO", {}).get("now", "")
    auto_mbps = live.get(auto_pick, 0.0)
    if auto_mbps >= GOOD_MBPS:
        api(sock, "PUT", "/proxies/PROXY", {"name": "AUTO"})
        log(f"拥堵消退:AUTO 延迟选优的 {short(auto_pick)} 也有 "
            f"{auto_mbps:.1f}Mbps,PROXY 已交还 AUTO(全自动恢复)")
    else:
        best = max(live, key=live.get)
        api(sock, "PUT", "/proxies/PROXY", {"name": best})
        log(f"拥堵仍在(AUTO 选中的 {short(auto_pick)} 仅 {auto_mbps:.1f}Mbps),"
            f"继续钉 {short(best)} ({live[best]:.1f}Mbps)")
    save_state({"good_streak": 0})

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"异常: {e}")
PYEOF
chmod +x ~/.local/bin/verge-node-patrol.py
python3 -c "import ast; ast.parse(open('$HOME/.local/bin/verge-node-patrol.py').read()); print('脚本语法 OK')"
```

### 3.2 生成并注册 launchd(每小时 :10,全天)

解释器路径必须是绝对路径、能跨 brew 小版本升级存活、且不能是会弹“安装命令行工具”对话框的 stub(附录 A-12)。优先 brew 维护的稳定软链接,退而用运行本生成器的 python 自身路径(**不要 realpath**,那会解析到带小版本号的 Cellar 目录):

```bash
python3 - <<'EOF'
import os, sys
home = os.path.expanduser("~")
py = next((p for p in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3")
           if os.path.exists(p)), sys.executable)
entries = "\n".join(
    f"        <dict><key>Hour</key><integer>{h}</integer><key>Minute</key><integer>10</integer></dict>"
    for h in range(24))
plist = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>local.verge.node-patrol</string>
    <key>ProgramArguments</key>
    <array>
        <string>{py}</string>
        <string>{home}/.local/bin/verge-node-patrol.py</string>
    </array>
    <key>StartCalendarInterval</key>
    <array>
{entries}
    </array>
    <key>StandardOutPath</key>
    <string>{home}/Library/Logs/verge-node-patrol.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>{home}/Library/Logs/verge-node-patrol.stderr.log</string>
</dict>
</plist>'''
os.makedirs(f"{home}/Library/LaunchAgents", exist_ok=True)
open(f"{home}/Library/LaunchAgents/local.verge.node-patrol.plist", "w").write(plist)
print("plist 已写入,解释器:", py)
EOF
plutil -lint ~/Library/LaunchAgents/local.verge.node-patrol.plist
launchctl bootout gui/$(id -u)/local.verge.node-patrol 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.verge.node-patrol.plist 2>/dev/null \
  || launchctl load -w ~/Library/LaunchAgents/local.verge.node-patrol.plist
launchctl list | grep "verge.node-patrol"
```

预期:`plist: OK`;`launchctl list` 里出现 `local.verge.node-patrol`。此时 Verge 未运行,巡检被触发也只会记一行“Verge 内核未运行,跳过”。

---

## 第 4 步:切换(零人工决策树)

```bash
IF=$(route -n get default 2>/dev/null | awk '/interface:/ {print $2}')
echo "默认路由接口: $IF"
```

### 分支 A:接口是 `en0`(Jamjams 隧道未连接)→ 直接接管

```bash
CFG="$HOME/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev"
rm -f "$CFG/providers/jms-sub.yaml"      # 清 provider 缓存,强制立即拉订阅
open -a "Clash Verge"; sleep 14
scutil --proxy | grep -E "HTTPEnable|HTTPProxy|HTTPPort"    # 预期 127.0.0.1:7897
pgrep -fl verge-mihomo | head -1 | cut -c1-60               # 预期内核在跑
```

→ 进入第 5 步。

### 分支 B:接口是 `utunX`(Jamjams 正连接)

先做无害的软尝试(偶尔能放掉隧道):

```bash
osascript -e 'tell application "Jamjams" to quit' 2>/dev/null; sleep 3
pkill -x Jamjams 2>/dev/null; sleep 2
scutil --nc stop "$(scutil --nc list | grep -i fiberlogic | awk -F'"' '{print $2}')" 2>/dev/null; sleep 3
route -n get default 2>/dev/null | awk '/interface:/ {print $2}'
```

- 回到 `en0` → 走分支 A。
- 仍是 `utunX` → 隧道由 **root 系统扩展**持有,用户态无法释放(实测 pkill / scutil --nc stop 均无效)。此时**不要启动 Verge**。采用“预置接管 + 重启”:前面已把 Jamjams `connectOnStart` 关掉、Verge 已设开机自启+静默启动+系统代理+巡检 launchd——**重启后无需任何人操作即完成切换**。执行:

```bash
osascript -e 'tell application "System Events" to restart'
```

  若该命令被拒(需要授权),向用户说明:“全部配置已就绪,只差重启一次;重启后不需要任何操作。”——然后结束本次任务,等用户重启后再被唤起执行第 5 步验证。**不要**把“请在 Jamjams 里点断开”写成流程步骤;若用户恰好在场主动断开了,也走分支 A 即可。

---

## 第 5 步:验证(每项都要实际执行并核对)

### 5.1 API(Verge v2.5 只监听 Unix socket,TCP 9097 不通是正常的)

```bash
SOCK=$(ps aux | grep "[v]erge-mihomo" | grep -o '\-ext-ctl-unix [^ ]*' | awk '{print $2}' | head -1)
echo "socket: $SOCK"
curl -s --unix-socket "$SOCK" http://localhost/version
```

预期:`{"meta":true,"version":"v1.19.xx"}`。

### 5.2 节点与分组

```bash
curl -s --unix-socket "$SOCK" http://localhost/providers/proxies/JMS | python3 -c "
import json,sys; d=json.load(sys.stdin)
for p in d['proxies']: print(f\"  {p['type']:<12} {p['name']}\")
print('  updatedAt:', d.get('updatedAt','?'))"
curl -s --unix-socket "$SOCK" http://localhost/proxies | python3 -c "
import json,sys; d=json.load(sys.stdin)['proxies']
print('  PROXY →', d['PROXY']['now'], '| AUTO →', d['AUTO']['now'])"
```

预期:JMS 节点全部列出(通常 1 Shadowsocks + 5 Vless);PROXY → AUTO(或巡检已钉的节点);AUTO → 某节点。

### 5.3 全节点延迟

```bash
curl -s --unix-socket "$SOCK" "http://localhost/group/AUTO/delay?url=https%3A%2F%2Fwww.gstatic.com%2Fgenerate_204&timeout=5000" | python3 -c "
import json,sys; d=json.load(sys.stdin)
for k,v in sorted(d.items(), key=lambda x: x[1] if isinstance(x[1],int) else 99999): print(f'  {str(v):>7}  {k}')"
```

预期:多数节点几十到几百 ms;全部 error 则停下排查。

### 5.4 真实流量

```bash
for u in https://www.gstatic.com/generate_204 https://www.google.com https://github.com; do
  curl -sx http://127.0.0.1:7897 -o /dev/null -w "$u → http %{http_code}, %{time_total}s\n" --max-time 15 "$u"
done
```

预期:204 / 200 / 200。

### 5.5 国内直连确认(关键)

```bash
curl -sx http://127.0.0.1:7897 -r 0-20971519 -o /dev/null --max-time 25 "https://mirrors.tuna.tsinghua.edu.cn/ubuntu-releases/22.04/ubuntu-22.04.5-live-server-amd64.iso" &
sleep 5
curl -s --unix-socket "$SOCK" http://localhost/connections | python3 -c "
import json,sys
hits=[c for c in json.load(sys.stdin).get('connections',[]) if 'tsinghua' in c['metadata'].get('host','')]
for c in hits: print(f\"  {c['metadata']['host']} → chains: {c['chains']}  rule: {c['rule']}({c.get('rulePayload','')})\")
print('  ⚠ 未捕获到连接,重跑' if not hits else '  ✓')"
wait
```

预期:`chains: ['DIRECT']`,rule `GeoSite(cn)`。

### 5.6 巡检首跑

```bash
python3 ~/.local/bin/verge-node-patrol.py
tail -8 ~/Library/Logs/verge-node-patrol.log
launchctl list | grep "verge.node-patrol"
```

预期:日志出现 `AUTO 模式正常(... Mbps),无需干预`(白天/网络好),或 `拥堵介入:PROXY → ...`(晚高峰)——两者都是正确行为。launchd 条目存在。

---

## 第 6 步:收尾报告

向用户汇报各项验证结果、当前出口节点、订阅更新时间、巡检首跑结论。另外说明两件**由用户决定**的事(不是流程步骤):

1. **Jamjams 卸载**:已无害闲置(隧道断开、开机不自连);确认卸载时删除 `/Applications/Jamjams.app`,其 root 系统扩展可能需重启一次才彻底消失。
2. **TUN 模式(可选)**:当前系统代理模式已覆盖浏览器和绝大多数应用;若需像 Jamjams 一样全局接管(含不认系统代理的终端工具),在 Verge「设置 → TUN 模式」安装服务模式,需输一次管理员密码——这是 macOS 权限要求,只能 GUI 操作。

---

## 附录 A:已知坑(全部为实测踩过)

| # | 坑 | 对策 |
|---|---|---|
| 1 | `sed` 替换串里的 `&` 是特殊字符,改含 `&` 的 URL 必坏(实测损坏过配置) | URL 一律用 Python 写 |
| 2 | Verge v2.5 的 API **只有 Unix socket**,TCP 9097 不监听属正常 | socket 路径从 `ps aux` 的 `-ext-ctl-unix` 参数取;socket 上不需要 Bearer |
| 3 | `mihomo -t` 校验 GEOSITE 规则需要 geo 数据已初始化 | 先让 Verge 完成首次启动再校验 |
| 4 | app 运行时改 `verge.yaml` 会被内存态覆盖 | 改前退出 app,改完再启动 |
| 5 | Jamjams 的 PacketTunnel 是 root 系统扩展,`pkill`/`scutil --nc stop` 都放不掉隧道 | 走第 4 步分支 B 的重启路径 |
| 6 | `python3` 无 `yaml` 模块 | 校验用 `verge-mihomo -t`;语法检查用 `ast.parse` |
| 7 | curl 并发测速时管道接 `wait` 收不到输出 | 各线程输出落临时文件再汇总 |
| 8 | `profiles.yaml` 的 local 条目最少只需 `uid/type/name/file`,`file` 必须是纯文件名 | 照 2.2 模板写 |
| 9 | brew cask 装到的是 2.5.x 最新小版本 | 无需在意 |
| 10 | curl exit code 28 = `--max-time` 超时,限速下载测试里属正常 | 别只看 exit code 判成败 |
| 11 | **zsh 不对 `$VAR` 做分词**(与 bash 不同):`for n in $NODES` 会把多行节点名当一个整体 | 遍历节点列表一律用 Python(巡检脚本已如此) |
| 12 | launchd 里的解释器路径:`/usr/bin/python3` 在未装 CLT 的机器上是会弹对话框的 stub;`realpath` 过的 brew python 指向带小版本号的 Cellar 目录,brew 升级后失效 | 用 `/opt/homebrew/bin/python3`(或 Intel 的 `/usr/local/bin/python3`)这类稳定软链接,3.2 已处理 |
| 13 | `launchctl load` 在新系统上是旧接口 | 优先 `bootstrap gui/$(id -u)`,失败再 `load -w` |
| 14 | Jamjams 的 plist 在 `~/Library/Containers/net.fiberlogic.jamjams-standalone/Data/Library/Preferences/`(v1 手册此处曾多写一层 `Application/`) | 用 0.3 / 2.4 的路径 |
| 15 | 晚高峰国际链路分钟级波动,单次测速会“冤枉”节点 | 巡检有 1.3× 防抖和连续达标计数,不要手动调低阈值追求灵敏 |

## 附录 B:API 速查(接管后的日常运维)

```bash
SOCK=$(ps aux | grep "[v]erge-mihomo" | grep -o '\-ext-ctl-unix [^ ]*' | awk '{print $2}' | head -1)
curl -s --unix-socket "$SOCK" http://localhost/providers/proxies/JMS                       # 节点与延迟历史
curl -s --unix-socket "$SOCK" "http://localhost/group/AUTO/delay?url=https%3A%2F%2Fwww.gstatic.com%2Fgenerate_204&timeout=5000"   # 全组测速
curl -s --unix-socket "$SOCK" -X PUT http://localhost/proxies/PROXY -d '{"name":"<节点全名>"}'  # 手动钉节点
curl -s --unix-socket "$SOCK" -X PUT http://localhost/proxies/PROXY -d '{"name":"AUTO"}'      # 交还 AUTO
curl -s --unix-socket "$SOCK" -X PUT http://localhost/providers/proxies/JMS                   # 强制更新订阅
curl -s --unix-socket "$SOCK" http://localhost/connections                                    # 当前连接走的链路
```

## 附录 C:巡检日志怎么读(`~/Library/Logs/verge-node-patrol.log`)

| 日志行 | 含义 |
|---|---|
| `AUTO 模式正常(c51sX 23.1Mbps),无需干预` | 平稳态,什么都没动 |
| `AUTO 选中的 c51sX 吞吐仅 5.6Mbps,全量扫描` → `拥堵介入:PROXY → c51sY` | 晚高峰接管 |
| `钉住节点 c51sY 正常(18Mbps,连续达标 1/2)` | 钉住中,观察是否可回归 |
| `拥堵消退:... PROXY 已交还 AUTO(全自动恢复)` | 自动回到延迟选优 |
| `无更优节点,维持现钉节点` | 防抖生效,拒绝无意义切换 |
| `Verge 内核未运行,跳过` | Verge 没开(重启前/用户退出了) |
| `所有节点都不可用!` | 订阅/账号层面问题,需人工关注 JMS 状态 |

状态文件:`~/Library/Application Support/verge-node-patrol/state.json`(只存连续达标计数,删除无副作用)。
