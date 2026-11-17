#!/usr/bin/env python3
"""节点吞吐巡检 v2 —— 全自动闭环,无任何人工环节(Clash Verge Rev / mihomo)

状态机:
- 平稳态:PROXY=AUTO(延迟选优),每小时轻探测当前出口(~4MB)
- 拥堵态:出口吞吐 < GOOD_MBPS → 全量扫描,PROXY 钉到实测最快节点(≥1.3× 才换)
- 回归:钉住节点连续 RETURN_AFTER 次达标 → 全量扫描复测,若 AUTO 的延迟选优节点
  也达标(说明拥堵消退、吞吐不再是瓶颈),自动把 PROXY 切回 AUTO
- 兜底:出口节点测活失败(mihomo delay API)或 gstatic 无响应 → 立即全量扫描换节点;
  内核没跑 → 静默退出

流量开销:平稳日 ~4MB/小时 + 少量全量扫描(6节点×8MB),约 150-250MB/天。
由 ~/Library/LaunchAgents/local.verge.node-patrol.plist 每小时 :10 触发。

并发与边界:
- PATROL_LOCK(flock 非阻塞)防并发,桥触发与 launchd 调度共用;拿不到锁=已有实例在跑
- events.jsonl 有界:超 256KB 自动截留最近 500 行
- 可调环境变量:PATROL_NODE_FILTER(节点名过滤,默认 "JMS")
"""
import subprocess, json, re, os, datetime, fcntl, urllib.parse

MIXED_PROXY = os.environ.get("PATROL_PROXY", "http://127.0.0.1:7897")
PROBE_BYTES = 4_000_000         # 平稳态轻探测流量
SWEEP_BYTES = 8_000_000         # 全量扫描时每节点流量
GOOD_MBPS = 15.0                # 达标线
BETTER_RATIO = 1.3              # 换节点需比当前好这个倍数
RETURN_AFTER = 2                # 钉住节点连续达标 N 次后,尝试回归 AUTO
LOG_PATH = os.environ.get(
    "PATROL_LOG",
    os.path.expanduser("~/Library/Logs/verge-node-patrol.log"))
STATE_PATH = os.environ.get(
    "PATROL_STATE",
    os.path.expanduser(
        "~/Library/Application Support/verge-node-patrol/state.json"))
EVENTS_PATH = os.environ.get(
    "PATROL_EVENTS",
    os.path.expanduser(
        "~/Library/Application Support/verge-node-patrol/events.jsonl"))
# 双 mihomo 实例并行时(如 fx-gateway 试运行期),用它指定本巡检该管哪个 socket(按路径子串匹配)
SOCKET_HINT = os.environ.get("PATROL_SOCKET_HINT", "")
# 并发防护锁(与 fx-bridge 的 patrol-now 探测共用同一文件;flock 持有期即锁期,孤儿文件无害)
PATROL_LOCK = os.environ.get(
    "PATROL_LOCK",
    os.path.expanduser(
        "~/Library/Application Support/fx-internet/patrol.lock"))
NODE_FILTER = os.environ.get("PATROL_NODE_FILTER", "JMS")

# 实测调度(2026-11-17):夜间静默不测吞吐(应急故障扫描不受影响),
# 白天在 SWEEP_HOURS 的整点巡检做定时全量实测(约每 3 小时,保证面板「实测」新鲜)
QUIET_HOURS = set(range(0, 8))         # 00:00-07:59 只查连通,不测吞吐
SWEEP_HOURS = {8, 11, 14, 17, 20, 23}  # 定时全量实测的小时(白天约每 3 小时)

QUOTA_MB = float(os.environ.get("PATROL_QUOTA_MB", "300"))
EMERGENCY_MB = float(os.environ.get("PATROL_EMERGENCY_MB", "52"))
USAGE_PATH = EVENTS_PATH + ".usage.json"
RUN_BYTES = 0
EMERGENCY = False
MANUAL = False   # 用户手动触发(面板按钮):不计入每日额度,事件标记 manual


class BudgetExceeded(RuntimeError):
    pass


def is_quiet_hour(now=None):
    """夜间静默(00:00-07:59):只查连通,不测吞吐"""
    return (now or datetime.datetime.now()).hour in QUIET_HOURS


def is_sweep_hour(now=None):
    """定时全量实测的小时(白天约每 3 小时)"""
    return (now or datetime.datetime.now()).hour in SWEEP_HOURS


def usage():
    today = datetime.date.today().isoformat()
    try:
        with open(USAGE_PATH) as f:
            rec = json.load(f)
        if rec.get("date") == today:
            return rec
    except (OSError, ValueError):
        pass
    return {"date": today, "bytes": 0}


def charge(count):
    global RUN_BYTES
    RUN_BYTES += count
    if MANUAL:
        return  # 手动触发不计入每日额度(用户知情主动花费;花费仍记入事件 cost_mb 供历史展示)
    rec = usage()
    rec["bytes"] += count
    os.makedirs(os.path.dirname(USAGE_PATH), exist_ok=True)
    tmp = USAGE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, USAGE_PATH)


def check_budget(count):
    if MANUAL:
        return  # 手动触发不受额度限制
    limit = (QUOTA_MB + (EMERGENCY_MB if EMERGENCY else 0)) * 1e6
    if usage()["bytes"] + count > limit:
        raise BudgetExceeded("今日探测预算已用尽，下次巡检继续检查连通性")


def log(msg):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")

def event(etype, **data):
    """结构化事件(JSONL),供 fx-bridge 聚合展示;与人类日志并行,写失败不影响巡检。
    有界增长:文件超 256KB 时原子截留最近 500 行。"""
    rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
           "type": etype}
    data["cost_mb"] = round(RUN_BYTES / 1e6, 4)
    rec.update(data)
    try:
        os.makedirs(os.path.dirname(EVENTS_PATH), exist_ok=True)
        with open(EVENTS_PATH, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if os.path.getsize(EVENTS_PATH) > 262144:
            with open(EVENTS_PATH) as f:
                keep = f.read().splitlines()[-500:]
            tmp = EVENTS_PATH + ".tmp"
            with open(tmp, "w") as f:
                f.write("\n".join(keep) + "\n")
            os.replace(tmp, EVENTS_PATH)
    except Exception:
        pass

def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {"good_streak": 0}

def save_state(st):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_PATH)

def acquire_lock():
    """非阻塞 flock;成功返回持有的文件对象(进程退出自动释放),失败返回 None"""
    try:
        os.makedirs(os.path.dirname(PATROL_LOCK), exist_ok=True)
        f = open(PATROL_LOCK, "w")
    except OSError:
        return None
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f

def find_socket():
    ps = subprocess.run(["ps", "aux"], capture_output=True, text=True).stdout
    if SOCKET_HINT:
        # hint 已设置时只认自己的实例;找不到 = 自己的内核没在运行(不回退,避免误管别的实例)
        for line in ps.splitlines():
            if SOCKET_HINT in line:
                m = re.search(r"-ext-ctl-unix (.+)$", line)
                if m:
                    return m.group(1).strip()
        return None
    # 注意:路径可能含空格(如 .../Application Support/...),必须捕获到行尾;
    # ps 输出多行,需 re.M 让 $ 锚定每行行尾
    m = re.search(r"-ext-ctl-unix (.+)$", ps, re.M)
    return m.group(1).strip() if m else None

def api(sock, method, path, data=None, timeout=6):
    cmd = ["curl", "-sS", "--noproxy", "*", "--unix-socket", sock, "-X", method,
           f"http://localhost{path}", "--max-time", str(timeout), "-w", "\n%{http_code}"]
    if data is not None:
        cmd += ["-d", json.dumps(data)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 4)
    try:
        body, code = r.stdout.rsplit("\n", 1)
        if r.returncode or not 200 <= int(code) < 300:
            raise ValueError("API failed")
        result = json.loads(body) if body.strip() else {}
        if not isinstance(result, dict):
            raise ValueError("unexpected API response")
        return result
    except ValueError as e:
        raise RuntimeError("mihomo API 请求失败") from e


def node_alive(sock, name):
    """对出口节点做链路测活:用 /group/PROXY/delay 并发测全组,看当前出口是否命中。
    不能用 alive() 代替:alive 的 gstatic 在现网规则下命中 GeoIP(cn)→DIRECT,
    节点死了它照样返回 204(死节点曾被"保留原出口"悬置 4 小时无人换)。
    也不能用 /proxies/{name}/delay:provider 叶子节点不在全局 proxies 表里,必 404。
    返回:True=存活;False=全组有存活者但当前出口不在其中(确死);
    None=API 失败或全组无结果(存疑,调用方不得据此判死)。"""
    if not name:
        return False
    try:
        r = api(sock, "GET",
                "/group/PROXY/delay"
                f"?url={urllib.parse.quote('https://www.gstatic.com/generate_204', safe='')}"
                "&timeout=4000")
    except Exception:
        return None
    if not isinstance(r, dict):
        return None
    if name in r:
        return True
    return False if r else None


def select(sock, name):
    path = "/proxies/PROXY"
    original = api(sock, "GET", path).get("now")
    if not original:
        raise RuntimeError("无法读取原出口，取消切换")
    try:
        api(sock, "PUT", path, {"name": name})
        if api(sock, "GET", path).get("now") != name:
            raise RuntimeError("节点切换后验证失败")
    except Exception:
        # 请求可能已生效但响应丢失，尝试恢复原出口并让调用者报告失败。
        api(sock, "PUT", path, {"name": original})
        raise


def short(name):
    return name.split("@")[-1] if "@" in name else name

def curl_measure(url, max_time, expected_bytes=None):
    cmd = ["curl", "-sS", "--noproxy", "", "--proxy", MIXED_PROXY,
           "-o", "/dev/null", "-w", "%{http_code} %{speed_download} %{size_download}",
           "--max-time", str(max_time), url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=max_time + 5)
        fields = r.stdout.split()
        code, speed, count = fields[0], float(fields[1]), int(float(fields[2]))
        if count < 0:
            raise ValueError("invalid byte count")
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return "000", 0.0
    charge(count)
    if r.returncode or (expected_bytes is not None and
                        (code != "200" or count != expected_bytes)):
        return "000", 0.0
    return code, speed


def alive():
    return curl_measure("https://www.gstatic.com/generate_204", 8)[0] == "204"


def measure(bytes_):
    if not alive():
        return False, 0.0
    check_budget(bytes_)
    code, sp = curl_measure(f"https://speed.cloudflare.com/__down?bytes={bytes_}", 14, bytes_)
    if code != "200":
        # 测速失败返回失败而不是抛异常:抛异常会被最外层兜底吞掉并以"保留原出口"
        # 收场,应急换节点分支(下方 if not ok)永远走不到——2026-11-08 死节点悬置
        # 4 小时的根因。单节点失败在 sweep 里记 0.0 并继续,不影响其余节点。
        return False, 0.0
    return True, sp * 8 / 1e6


def sweep(sock, nodes):
    """临时切换测速，任何退出路径都恢复原出口；决策由调用者单独提交。"""
    original = api(sock, "GET", "/proxies/PROXY").get("now")
    if not original:
        raise RuntimeError("无法读取原出口，取消扫描")
    results = {}
    try:
        for n in nodes:
            select(sock, n)
            ok, m = measure(SWEEP_BYTES)
            results[n] = m if ok else 0.0
            log(f"  扫描 {short(n)}: {m:.1f}Mbps")
        return results
    finally:
        select(sock, original)


def manual_run(sock, nodes, effective, pinned, kind):
    """用户手动触发的测试(面板按钮):不计入每日额度(用户知情主动花费),
    事件带 manual=true 供「测试历史」区分手动/自动。花费仍记入 cost_mb 展示。
    延迟测试 = group delay API 并发测全组(近零流量,顺带刷新内核健康历史);
    下载实测 = 全量吞吐实测,钉住态发现 1.3× 更优节点则换钉,其余维持。"""
    global MANUAL
    MANUAL = True
    if kind == "delay":
        try:
            r = api(sock, "GET",
                    "/group/PROXY/delay"
                    f"?url={urllib.parse.quote('https://www.gstatic.com/generate_204', safe='')}"
                    "&timeout=4000", timeout=10)
        except Exception as e:
            event("manual_delay", manual=True, error=str(e), cost_mb=0)
            return
        node_set = set(nodes)   # 返回里会混入 AUTO 组条目,只统计真实 provider 节点
        alive_nodes = {short(n): d for n, d in (r or {}).items()
                       if n in node_set and isinstance(d, (int, float)) and d > 0}
        best = min(alive_nodes.items(), key=lambda kv: kv[1]) if alive_nodes else None
        log(f"手动延迟测试:{len(alive_nodes)}/{len(nodes)} 节点存活"
            + (f",最快 {best[0]} {best[1]}ms" if best else ""))
        event("manual_delay", manual=True, alive=len(alive_nodes), total=len(nodes),
              best=best[0] if best else "", best_ms=int(best[1]) if best else 0, cost_mb=0)
        return
    if kind == "sweep":
        log("手动下载实测:全量扫描(不计入每日额度)")
        res = sweep(sock, nodes)
        live = {n: m for n, m in res.items() if m > 0}
        cost = round(RUN_BYTES / 1e6, 1)
        sweep_data = {short(n): round(m, 1) for n, m in res.items()}
        if not live:
            log("手动实测:所有节点都不可用")
            event("manual_sweep", manual=True, all_dead=True,
                  cost_mb=cost, sweep=sweep_data)
            return
        best = max(live, key=live.get)
        cur = live.get(effective, 0.0)
        if pinned and best != effective and live[best] >= max(cur * BETTER_RATIO, 5.0):
            select(sock, best)
            log(f"手动实测发现更优:换钉 {short(best)}({live[best]:.1f}Mbps,"
                f"原 {short(effective)} {cur:.1f}Mbps)")
            event("manual_sweep", manual=True, to=short(best),
                  mbps=round(live[best], 1), cost_mb=cost, sweep=sweep_data)
            return
        log(f"手动实测完成:维持 {short(effective)}({cur:.1f}Mbps),"
            f"最快 {short(best)} {live[best]:.1f}Mbps")
        event("manual_sweep", manual=True, mbps=round(cur, 1),
              cost_mb=cost, sweep=sweep_data)


def main():
    global RUN_BYTES, EMERGENCY
    RUN_BYTES = 0
    EMERGENCY = False
    lock = acquire_lock()


def main():
    global RUN_BYTES, EMERGENCY
    RUN_BYTES = 0
    EMERGENCY = False
    lock = acquire_lock()
    if lock is None:
        log("已有巡检在运行(桥触发/launchd 并发),本次静默退出")
        event("skip", reason="already_running", cost_mb=0)
        return
    sock = find_socket()
    if not sock:
        log("Verge 内核未运行,跳过")
        event("skip", reason="kernel_down", cost_mb=0)
        return
    proxies = api(sock, "GET", "/proxies").get("proxies", {})
    nodes = [n for n in proxies.get("PROXY", {}).get("all", [])
             if n != "AUTO" and NODE_FILTER in n]
    if not nodes:
        log(f"未找到 {NODE_FILTER} 节点(provider 异常?),跳过")
        event("skip", reason="no_nodes", cost_mb=0)
        return

    state = load_state()
    cur_group = proxies.get("PROXY", {}).get("now", "")
    effective = (proxies.get("AUTO", {}).get("now", "")
                 if cur_group == "AUTO" else cur_group)
    pinned = cur_group != "AUTO"

    # 手动模式(面板按钮):只做指定动作,不计额度,不跑自动决策流
    manual = os.environ.get("PATROL_MANUAL", "")
    if manual:
        return manual_run(sock, nodes, effective, pinned, manual)

    now = datetime.datetime.now()
    scheduled = is_sweep_hour(now)

    try:
        if not alive():
            # 本地栈无响应(gstatic 失败):按出口故障兜底
            EMERGENCY = True
            ok, mbps = False, 0.0
            log("gstatic 无响应,按出口故障处理")
        elif node_alive(sock, effective) is False:
            # 节点级测活:当前出口 dial 超时(节点死亡)也必须触发应急扫描。
            # 返回 None(API 存疑)不触发,交给下方 measure 兜底
            EMERGENCY = True
            ok, mbps = False, 0.0
            log(f"出口节点 {short(effective) or '?'} 全组测活未命中(死亡),立即全量扫描")
        elif is_quiet_hour(now):
            # 夜间静默:连通与节点测活均正常,不测吞吐(省流量;故障应急扫描不受影响)
            log(f"夜间静默({now.hour} 点):连通正常,跳过吞吐测量")
            event("night_ok", exit=short(effective), cost_mb=0)
            return
        elif scheduled:
            # 定时全量实测:跳过轻探测,直接全量(sweep 自含当前出口)
            ok, mbps = True, 0.0
        else:
            ok, mbps = measure(PROBE_BYTES)
    except BudgetExceeded as e:
        event("budget_exhausted", message=str(e))
        return
    if not ok:
        EMERGENCY = True
        log(f"出口 {short(effective)} 无响应,立即全量扫描")
        res = sweep(sock, nodes)
        live = {n: m for n, m in res.items() if m > 0}
        if not live:
            log("所有节点都不可用!")
            event("all_dead", prev=short(effective), cost_mb=4)
            save_state({"good_streak": 0})
            return
        best = max(live, key=live.get)
        select(sock, best)
        log(f"切换 PROXY → {short(best)} ({live[best]:.1f}Mbps)")
        event("fault_switch", prev=short(effective), to=short(best),
              mbps=round(live[best], 1), sweep={short(n): round(m, 1) for n, m in res.items()},
              cost_mb=52)
        save_state({"good_streak": 0})
        return

    if scheduled:
        # 定时全量实测:所有节点各测一次,有显著更优(钉住态)才切换,否则维持现状
        log(f"定时全量实测({now.hour} 点)")
        res = sweep(sock, nodes)
        live = {n: m for n, m in res.items() if m > 0}
        if not live:
            log("所有节点都不可用!")
            event("all_dead", prev=short(effective), cost_mb=8)
            save_state({"good_streak": 0})
            return
        best = max(live, key=live.get)
        cur = live.get(effective, 0.0)
        if pinned and best != effective and live[best] >= max(cur * BETTER_RATIO, 5.0):
            select(sock, best)
            log(f"定时实测发现更优:换钉 {short(best)}({live[best]:.1f}Mbps,"
                f"原 {short(effective)} {cur:.1f}Mbps)")
            event("repin", prev=short(effective), to=short(best),
                  mbps=round(live[best], 1),
                  sweep={short(n): round(m, 1) for n, m in res.items()}, cost_mb=48)
        else:
            log(f"定时实测:无显著更优节点,维持 {short(effective)}({cur:.1f}Mbps)")
            event("keep_pin" if pinned else "keep_auto",
                  pinned=short(effective), exit=short(effective),
                  mbps=round(cur, 1),
                  sweep={short(n): round(m, 1) for n, m in res.items()}, cost_mb=48)
        save_state({"good_streak": 0})
        return

    if not pinned:
        # 平稳态:PROXY=AUTO
        if mbps >= GOOD_MBPS:
            state["good_streak"] = state.get("good_streak", 0) + 1
            save_state(state)
            log(f"AUTO 模式正常({short(effective)} {mbps:.1f}Mbps),无需干预")
            event("steady_ok", exit=short(effective), mbps=round(mbps, 1), cost_mb=4)
            return
        log(f"AUTO 选中的 {short(effective)} 吞吐仅 {mbps:.1f}Mbps,全量扫描")
        res = sweep(sock, nodes)
        live = {n: m for n, m in res.items() if m > 0}
        if not live:
            log("所有节点都不可用!")
            event("all_dead", prev=short(effective), cost_mb=52)
            save_state({"good_streak": 0})
            return
        best = max(live, key=live.get)
        if live[best] >= max(mbps * BETTER_RATIO, 5.0):
            select(sock, best)
            log(f"拥堵介入:PROXY → {short(best)} ({live[best]:.1f}Mbps,"
                f"原 AUTO/{short(effective)} {mbps:.1f}Mbps)")
            event("congestion_pin", auto_pick=short(effective), to=short(best),
                  mbps=round(live[best], 1), prev_mbps=round(mbps, 1),
                  sweep={short(n): round(m, 1) for n, m in res.items()}, cost_mb=52)
        else:
            select(sock, "AUTO")
            log("无更优节点,保持 AUTO")
            event("keep_auto", exit=short(effective), mbps=round(mbps, 1),
                  sweep={short(n): round(m, 1) for n, m in res.items()}, cost_mb=52)
        save_state({"good_streak": 0})
        return

    # 拥堵态:PROXY 钉在具体节点
    if mbps < GOOD_MBPS:
        log(f"钉住节点 {short(effective)} 降至 {mbps:.1f}Mbps,全量扫描")
        res = sweep(sock, nodes)
        live = {n: m for n, m in res.items() if m > 0}
        if not live:
            log("所有节点都不可用!")
            event("all_dead", prev=short(effective), cost_mb=52)
            save_state({"good_streak": 0})
            return
        best = max(live, key=live.get)
        if live[best] >= max(mbps * BETTER_RATIO, 5.0) and best != effective:
            select(sock, best)
            log(f"换钉:PROXY → {short(best)} ({live[best]:.1f}Mbps)")
            event("repin", prev=short(effective), to=short(best),
                  mbps=round(live[best], 1),
                  sweep={short(n): round(m, 1) for n, m in res.items()}, cost_mb=52)
        else:
            select(sock, effective)
            log("无更优节点,维持现钉节点")
            event("keep_pin", pinned=short(effective), mbps=round(mbps, 1),
                  sweep={short(n): round(m, 1) for n, m in res.items()}, cost_mb=52)
        save_state({"good_streak": 0})
        return

    # 钉住节点达标:考虑把控制权交还 AUTO
    state["good_streak"] = state.get("good_streak", 0) + 1
    streak = state["good_streak"]
    if streak < RETURN_AFTER:
        save_state(state)
        log(f"钉住节点 {short(effective)} 正常({mbps:.1f}Mbps,"
            f"连续达标 {streak}/{RETURN_AFTER})")
        event("pin_ok", pinned=short(effective), mbps=round(mbps, 1),
              streak=streak, total=RETURN_AFTER, cost_mb=4)
        return
    log(f"钉住节点连续 {streak} 次达标,复测判断拥堵是否消退")
    res = sweep(sock, nodes)
    live = {n: m for n, m in res.items() if m > 0}
    if not live:
        log("所有节点都不可用!")
        event("all_dead", prev=short(effective), cost_mb=52)
        save_state({"good_streak": 0})
        return
    auto_pick = api(sock, "GET", "/proxies/AUTO").get("now", "")
    auto_mbps = live.get(auto_pick, 0.0)
    if auto_mbps >= GOOD_MBPS:
        select(sock, "AUTO")
        log(f"拥堵消退:AUTO 延迟选优的 {short(auto_pick)} 也有 "
            f"{auto_mbps:.1f}Mbps,PROXY 已交还 AUTO(全自动恢复)")
        event("handoff", pinned=short(effective), auto_pick=short(auto_pick),
              auto_mbps=round(auto_mbps, 1),
              sweep={short(n): round(m, 1) for n, m in res.items()}, cost_mb=52)
    else:
        best = max(live, key=live.get)
        if best != effective and live[best] < mbps * BETTER_RATIO:
            best = effective
        select(sock, best)
        log(f"拥堵仍在(AUTO 选中的 {short(auto_pick)} 仅 {auto_mbps:.1f}Mbps),"
            f"继续钉 {short(best)} ({live[best]:.1f}Mbps)")
        event("still_congested", prev=short(effective), to=short(best),
              mbps=round(live[best], 1), auto_pick=short(auto_pick),
              auto_mbps=round(auto_mbps, 1),
              sweep={short(n): round(m, 1) for n, m in res.items()}, cost_mb=52)
    save_state({"good_streak": 0})

if __name__ == "__main__":
    try:
        main()
    except BudgetExceeded as e:
        log(str(e))
        event("budget_exhausted", message=str(e))
    except Exception as e:
        log(f"异常: {e}")
        event("error", error=str(e), cost_mb=0)
