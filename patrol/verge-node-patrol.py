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

并发与边界:
- PATROL_LOCK(flock 非阻塞)防并发,桥触发与 launchd 调度共用;拿不到锁=已有实例在跑
- events.jsonl 有界:超 256KB 自动截留最近 500 行
- 可调环境变量:PATROL_NODE_FILTER(节点名过滤,默认 "JMS")
"""
import subprocess, json, re, os, datetime, fcntl

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

QUOTA_MB = float(os.environ.get("PATROL_QUOTA_MB", "250"))
EMERGENCY_MB = float(os.environ.get("PATROL_EMERGENCY_MB", "52"))
USAGE_PATH = EVENTS_PATH + ".usage.json"
RUN_BYTES = 0
EMERGENCY = False


class BudgetExceeded(RuntimeError):
    pass


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
    rec = usage()
    rec["bytes"] += count
    os.makedirs(os.path.dirname(USAGE_PATH), exist_ok=True)
    tmp = USAGE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, USAGE_PATH)


def check_budget(count):
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
    # 注意:路径可能含空格(如 .../Application Support/...),必须捕获到行尾
    m = re.search(r"-ext-ctl-unix (.+)$", ps)
    return m.group(1).strip() if m else None

def api(sock, method, path, data=None):
    cmd = ["curl", "-sS", "--noproxy", "*", "--unix-socket", sock, "-X", method,
           f"http://localhost{path}", "--max-time", "6", "-w", "\n%{http_code}"]
    if data is not None:
        cmd += ["-d", json.dumps(data)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
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
        raise RuntimeError("测速服务异常或下载不完整，保留原出口")
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
    nodes = [n for n in proxies.get("PROXY", {}).get("all", []) if NODE_FILTER in n]
    if not nodes:
        log(f"未找到 {NODE_FILTER} 节点(provider 异常?),跳过")
        event("skip", reason="no_nodes", cost_mb=0)
        return

    state = load_state()
    cur_group = proxies.get("PROXY", {}).get("now", "")
    effective = (proxies.get("AUTO", {}).get("now", "")
                 if cur_group == "AUTO" else cur_group)
    pinned = cur_group != "AUTO"

    try:
        if not alive():
            EMERGENCY = True
            ok, mbps = False, 0.0
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
