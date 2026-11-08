#!/usr/bin/env python3
"""fx-bridge:fx-gateway 的本地桥接服务(Python stdlib,零依赖)

职责:
- 服务分级界面(GET /)
- 聚合状态(GET /api/status):内核存活、出口分组、实时速率、系统代理实况 vs 期望、
  巡检事件/配额、三态判定(green/yellow/red 服务端统一推导)
- mihomo API 透明代理(GET|PUT /api/mihomo/*)
- 动作(POST /api/actions/...):立即巡检、内核启停、交还常规模式、应用配置
- 系统代理守护线程(30s):FX_SYSAPPLY=1 时写入 networksetup;=0 时仅监测(试运行期)

性能与安全约定:
- /proxies 与 /connections 由监测线程统一缓存,/api/status 纯读内存,请求路径零子进程
- 实时速率为 2s 差分(mihomo downloadTotal/uploadTotal),不是连接累计字节
- 仅接受 Host: 127.0.0.1:PORT / localhost:PORT(DNS rebinding / 恶意页面 POST 防护)
- 巡检并发由 patrol 侧 flock 统一防护;桥只做非阻塞探测

设计原则:桥不是内核的父进程(内核由 launchd 直管),桥挂了网络照常;
所有写操作遵守"先清系统代理,再停内核"的顺序。
"""
import datetime
import fcntl
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")


def env(key, default):
    return os.environ.get(key, default)


STATE_DIR = env("FX_STATE_DIR", os.path.join(
    HOME, "Library/Application Support/fx-internet"))
BRIDGE_PORT = int(env("FX_BRIDGE_PORT", "7900"))
MIXED_PORT = int(env("FX_MIXED_PORT", "7898"))
SYSAPPLY = env("FX_SYSAPPLY", "0") == "1"
SOCK = os.path.join(STATE_DIR, "fx-mihomo.sock")
RUNTIME = os.path.join(STATE_DIR, "runtime.yaml")
UI_FILE = os.path.join(HOME, ".local/bin/fx-gateway-ui/index.html")
PLIST_MIHOMO = os.path.join(HOME, "Library/LaunchAgents/local.mihomo.plist")
UID_N = str(os.getuid())

# 巡检联动(试运行期由 launchd 环境变量指到 trial 路径;cutover 后指回生产路径)
EVENTS_PATH = env("FX_EVENTS_PATH", os.path.join(
    HOME, "Library/Application Support/verge-node-patrol/events.jsonl"))
PATROL_SCRIPT = os.path.join(HOME, ".local/bin/verge-node-patrol.py")
PATROL_LOCK = os.path.join(STATE_DIR, "patrol.lock")
PATROL_ENV = {
    "PATROL_SOCKET_HINT": "fx-mihomo.sock",
    "PATROL_PROXY": env("FX_PATROL_PROXY", f"http://127.0.0.1:{MIXED_PORT}"),
    "PATROL_STATE": env("FX_PATROL_STATE", os.path.join(
        HOME, "Library/Application Support/verge-node-patrol/state.json")),
    "PATROL_EVENTS": EVENTS_PATH,
    "PATROL_LOG": env("FX_PATROL_LOG", os.path.join(
        HOME, "Library/Logs/verge-node-patrol.log")),
    "PATROL_LOCK": PATROL_LOCK,
    "PATROL_QUOTA_MB": env("FX_QUOTA_MB", "250"),
    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
}
QUOTA_MB = int(env("FX_QUOTA_MB", "250"))       # 每日探测流量配额(展示口径)
PATROL_MINUTE = int(env("FX_PATROL_MINUTE", "10"))  # 与巡检 plist 的调度分钟保持一致

# 逐字复刻 Verge 的 bypass 列表(盘点确认)
BYPASS = ["127.0.0.1", "192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12",
          "localhost", "*.local", "*.crashlytics.com", "<local>"]

# 监测线程 2s 刷新;/proxies 一并缓存,请求路径免子进程
MON = {"kernel": None, "mbps": 0.0, "conns": 0, "proxies": {}, "ts": 0}
KERNEL_WANTED = True
CONTROL_LOCK = threading.RLock()
WRITE_TOKEN = secrets.token_urlsafe(32)
SYS = {"ok": None, "applied": False, "requires_admin": False,
       "error": None, "ts": 0}                                 # 30s 守护线程


def log(msg):
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def mi(method, path, data=None, timeout=5):
    """经 unix socket 调 mihomo REST API(curl 实现,与巡检同款)"""
    cmd = ["curl", "-sS", "--noproxy", "*", "--unix-socket", SOCK, "-X", method,
           f"http://localhost{path}", "--max-time", str(timeout), "-w", "\n%{http_code}"]
    if data is not None:
        cmd += ["-d", json.dumps(data)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 3)
        body, status = r.stdout.rsplit("\n", 1)
        if r.returncode or not 200 <= int(status) < 300:
            raise RuntimeError(f"mihomo request failed ({status})")
        result = json.loads(body) if body.strip() else {}
        if not isinstance(result, dict):
            raise ValueError("unexpected API response")
        return result
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        raise RuntimeError("mihomo API unavailable") from e


def short(name):
    return name.split("@")[-1] if "@" in name else name


# ---------- 监测线程:内核存活 + 实时速率 + /proxies 缓存(2s) ----------

def monitor_loop():
    prev_total = None   # 上轮全局累计字节(downloadTotal+uploadTotal)
    prev_ts = None
    while True:
        try:
            ver = mi("GET", "/version", timeout=3)
            conns = mi("GET", "/connections", timeout=3)
            proxies = mi("GET", "/proxies", timeout=5)
            n = 0
            for c in (conns.get("connections") or []):
                chains = c.get("chains") or []
                if chains and chains[-1] != "DIRECT":  # 仅统计走代理的连接数
                    n += 1
            # 速率 = 全局累计字节的 2s 差分(内核重启计数清零时钳到 0);
            # 全局计数器不受连接建立/关闭影响,连接级累计字节会漏短连接且被误当速率
            total = conns.get("downloadTotal", 0) + conns.get("uploadTotal", 0)
            mbps = 0.0
            now = time.time()
            if prev_total is not None and prev_ts and now > prev_ts:
                mbps = max(0.0, (total - prev_total) * 8 / (now - prev_ts) / 1e6)
            prev_total, prev_ts = total, now
            MON.update(kernel=ver.get("version"), mbps=round(mbps, 1),
                       conns=n, proxies=proxies.get("proxies", {}), ts=now)
        except Exception as e:
            prev_total = prev_ts = None
            MON.update(kernel=None, proxies={}, mbps=0, error=str(e), ts=time.time())
            log(f"监测线程异常(下拍重试): {e}")
        time.sleep(2)


# ---------- 系统代理守护线程(30s) ----------

def read_sysproxy():
    """读当前生效系统代理(scutil 汇总,只读无副作用)"""
    try:
        out = subprocess.run(["scutil", "--proxy"], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return {}
    d = {}
    for line in out.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            d[k.strip()] = v.strip()
    return d


def network_services():
    r = subprocess.run(["networksetup", "-listallnetworkservices"],
                       capture_output=True, text=True, timeout=5)
    if r.returncode or not r.stdout.strip():
        raise RuntimeError("无法枚举网络服务，取消代理清理")
    return [l.strip() for l in r.stdout.splitlines()[1:]
            if l.strip() and not l.startswith("*")]


def primary_service():
    """当前默认路由走的网络服务(如 Wi-Fi)——系统代理应该设在它身上"""
    try:
        out = subprocess.run(["route", "-n", "get", "default"],
                             capture_output=True, text=True, timeout=5).stdout
        iface = re.search(r"interface: (\S+)", out).group(1)
        order = subprocess.run(["networksetup", "-listnetworkserviceorder"],
                               capture_output=True, text=True, timeout=5).stdout
        cur = None
        for line in order.splitlines():
            m = re.search(r"^\(\d+\) (.+)$", line.strip())
            if m:
                cur = m.group(1)
            if f"Device: {iface}" in line and cur:
                return cur
    except Exception:
        pass
    return None


def apply_proxy_to_service(svc, enable):
    """对单个网络服务设/清三项代理;bypass 仅在启用时写。返回错误信息或 None。"""
    cmds = []
    if enable:
        for setter in ("-setwebproxy", "-setsecurewebproxy", "-setsocksfirewallproxy"):
            cmds.append(["networksetup", setter, svc, "127.0.0.1", str(MIXED_PORT)])
        cmds.append(["networksetup", "-setproxybypassdomains", svc] + BYPASS)
    else:
        for setter in ("-setwebproxystate", "-setsecurewebproxystate",
                       "-setsocksfirewallproxystate"):
            cmds.append(["networksetup", setter, svc, "off"])
    for cmd in cmds:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        err = (r.stderr or "") + (r.stdout or "")
        if r.returncode != 0 or "admin" in err.lower() or r.returncode == 14:
            # macOS 26 起 networksetup 可能返回"requires admin privileges"(退出码 14)
            if r.returncode == 14 or "admin" in err.lower():
                return "requires_admin"
            return err.strip()[:200] or f"exit {r.returncode}"
    return None


def proxy_matches(cur):
    return all(cur.get(k + "Enable") == "1"
               and cur.get(k + "Proxy") == "127.0.0.1"
               and cur.get(k + "Port") == str(MIXED_PORT)
               for k in ("HTTP", "HTTPS", "SOCKS"))


def clear_owned_proxies():
    """仅关闭指向本实例的代理项；失败抛出，禁止继续停内核。"""
    for svc in network_services():
        for getter, setter in (("-getwebproxy", "-setwebproxystate"),
                               ("-getsecurewebproxy", "-setsecurewebproxystate"),
                               ("-getsocksfirewallproxy", "-setsocksfirewallproxystate")):
            r = subprocess.run(["networksetup", getter, svc], capture_output=True,
                               text=True, timeout=5)
            if r.returncode:
                raise RuntimeError(f"无法读取 {svc} 代理")
            fields = dict(line.split(": ", 1) for line in r.stdout.splitlines() if ": " in line)
            if (fields.get("Enabled") == "Yes" and fields.get("Server") == "127.0.0.1"
                    and fields.get("Port") == str(MIXED_PORT)):
                r = subprocess.run(["networksetup", setter, svc, "off"],
                                   capture_output=True, text=True, timeout=10)
                if r.returncode or "admin" in (r.stdout + r.stderr).lower():
                    raise RuntimeError(f"无法清理 {svc} 代理，需要检查权限")


def guard_once():
    with CONTROL_LOCK:
        if not MON.get("ts"):
            return
        cur = read_sysproxy()
        applied = proxy_matches(cur)
        if SYSAPPLY:
            if KERNEL_WANTED and MON.get("kernel") and time.time() - MON["ts"] < 30:
                if not applied:
                    svc = primary_service()
                    if not svc:
                        raise RuntimeError("无法定位默认网络服务")
                    err = apply_proxy_to_service(svc, enable=True)
                    if err:
                        SYS.update(ok=False, applied=False, error=err,
                                   requires_admin=err == "requires_admin", ts=time.time())
                        return
                    applied = proxy_matches(read_sysproxy())
                    if not applied:
                        raise RuntimeError("系统代理写入后验证失败")
            else:
                clear_owned_proxies()
                applied = False
        SYS.update(ok=True, applied=applied, error=None,
                   requires_admin=False, ts=time.time())


def guard_loop():
    while True:
        try:
            guard_once()
        except Exception as e:
            SYS.update(ok=False, applied=False, error=str(e), ts=time.time())
        time.sleep(30)


# ---------- 巡检事件 ----------

def read_events(n=80):
    """只读文件尾部(64KB),事件文件长期增长时请求路径不随之变慢"""
    try:
        size = os.path.getsize(EVENTS_PATH)
        with open(EVENTS_PATH, "rb") as f:
            if size > 65536:
                f.seek(-65536, os.SEEK_END)
            data = f.read().decode("utf-8", "replace")
        lines = [l for l in data.splitlines() if l.strip()]
        if size > 65536 and lines:
            lines = lines[1:]  # 尾读首行可能被截断,丢弃
        out = []
        for l in lines[-n:]:
            try:
                out.append(json.loads(l))
            except ValueError:
                pass
        return out
    except OSError:
        return []


def next_patrol_time():
    now = datetime.datetime.now()
    nxt = now.replace(minute=PATROL_MINUTE, second=0, microsecond=0)
    if now.minute >= PATROL_MINUTE:
        nxt += datetime.timedelta(hours=1)
    return f"{nxt:%H:%M}"


def build_status():
    proxies = MON.get("proxies") or {}
    proxy_now = proxies.get("PROXY", {}).get("now", "")
    auto_now = proxies.get("AUTO", {}).get("now", "")
    pinned = bool(proxy_now) and proxy_now != "AUTO"
    events = read_events()
    last = events[-1] if events else None
    today = f"{datetime.date.today():%Y-%m-%d}"
    try:
        with open(EVENTS_PATH + ".usage.json") as f:
            usage = json.load(f)
        cost_today = usage.get("bytes", 0) / 1e6 if usage.get("date") == today else 0
    except (OSError, ValueError):
        cost_today = sum(float(e.get("cost_mb", 0)) for e in events
                         if str(e.get("ts", "")).startswith(today))

    kernel_up = bool(MON.get("kernel"))
    level, reason = "green", "steady"
    if not MON.get("ts") or time.time() - MON["ts"] > 30:
        level, reason = "yellow", "status_unknown"
    elif not kernel_up:
        level, reason = "red", "kernel_down"
    elif last and last.get("type") == "all_dead":
        level, reason = "red", "all_dead"
    elif SYS.get("requires_admin"):
        level, reason = "yellow", "sysproxy_requires_admin"
    elif not any(n != "AUTO" for n in proxies.get("PROXY", {}).get("all", [])) or not proxy_now:
        level, reason = "red", "no_nodes"
    elif SYSAPPLY and (not SYS.get("ok") or time.time() - SYS.get("ts", 0) > 65):
        level, reason = "yellow", "sysproxy_off"
    elif not last or last.get("type") in ("error", "skip", "budget_exhausted") or (
            datetime.datetime.now() - datetime.datetime.fromisoformat(last["ts"])).total_seconds() > 7200:
        level, reason = "yellow", "patrol_unknown"
    elif pinned:
        level, reason = "yellow", "pinned"
    elif SYSAPPLY and not SYS.get("applied"):
        level, reason = "yellow", "sysproxy_off"

    return {
        "ts": f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}",
        "kernel": {"running": kernel_up, "version": MON.get("kernel"),
                   "port": MIXED_PORT, "managed_by": "launchd:local.mihomo"},
        "groups": {"proxy_now": proxy_now, "proxy_now_short": short(proxy_now),
                   "auto_now": auto_now, "auto_now_short": short(auto_now),
                   "pinned": pinned},
        "traffic": {"mbps": MON.get("mbps", 0.0), "proxy_conns": MON.get("conns", 0)},
        "sysproxy": {"apply_mode": "active" if SYSAPPLY else "trial-dry",
                     "applied": SYS.get("applied"), "ok": SYS.get("ok"),
                     "requires_admin": SYS.get("requires_admin"),
                     "desired": {"host": "127.0.0.1", "port": MIXED_PORT,
                                 "bypass": BYPASS},
                     "error": SYS.get("error")},
        "patrol": {"last_event": last, "events": events[-20:],
                   "cost_today_mb": round(cost_today, 2), "quota_mb": QUOTA_MB,
                   "next_run": next_patrol_time()},
        "derived": {"level": level, "reason": reason},
    }


# ---------- 动作 ----------

def launchctl(*args):
    r = subprocess.run(["launchctl", *args], capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def run_patrol():
    """触发巡检。并发防护统一由 patrol 侧 flock 负责(桥触发与 launchd 调度共用一把锁);
    这里先非阻塞探测一次,避免明知在跑还拉起一个注定退出的进程。
    探测到即让位(flock 随 fd 关闭释放),孤儿锁文件无害——flock 只在持有期间存在。"""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        fd = os.open(PATROL_LOCK, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return False, "无法打开巡检锁"
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False, "巡检已在运行中"
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    e = dict(os.environ)
    e.update(PATROL_ENV)
    subprocess.Popen([e.get("PATROL_PYTHON", sys.executable),
                      PATROL_SCRIPT], env=e,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True, "已触发"


def handoff_auto():
    with CONTROL_LOCK:
        os.makedirs(STATE_DIR, exist_ok=True)
        # 与巡检共用锁，防止人工交还被正在扫描的巡检覆盖。
        with open(PATROL_LOCK, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            mi("PUT", "/proxies/PROXY", {"name": "AUTO"})
            if mi("GET", "/proxies/PROXY").get("now") != "AUTO":
                raise RuntimeError("交还后验证失败")
    return True, "已交还常规模式"


def start_kernel(restart=False):
    global KERNEL_WANTED
    with CONTROL_LOCK:
        if not launchctl("print", f"gui/{UID_N}/local.mihomo")[0]:
            result = launchctl("bootstrap", f"gui/{UID_N}", PLIST_MIHOMO)
        else:
            args = ["kickstart"] + (["-k"] if restart else [])
            result = launchctl(*args, f"gui/{UID_N}/local.mihomo")
        if result[0]:
            KERNEL_WANTED = True
        return result


ACTIONS = {
    "patrol-now": lambda: run_patrol(),
    "kernel-start": lambda: start_kernel(),
    "kernel-restart": lambda: start_kernel(restart=True),
    "handoff-auto": handoff_auto,
}


def do_kernel_stop():
    # 停机顺序:先清系统代理,再停内核
    global KERNEL_WANTED
    with CONTROL_LOCK:
        if SYSAPPLY:
            clear_owned_proxies()
        result = launchctl("bootout", f"gui/{UID_N}/local.mihomo")
        if result[0]:
            KERNEL_WANTED = False
            MON.update(kernel=None, proxies={}, ts=time.time())
        return result


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        """DNS rebinding / 恶意页面 POST 防护:Host 必须是本机回环地址。
        浏览器对 127.0.0.1 的跨源 POST 是 simple request 不受 CORS 拦截,
        攻击页还可把自有域名解析到 127.0.0.1,故必须校验 Host。"""
        host = (self.headers.get("Host") or "").strip().lower()
        return host in (f"127.0.0.1:{BRIDGE_PORT}", f"localhost:{BRIDGE_PORT}")

    def _write_ok(self):
        origin = self.headers.get("Origin")
        return (self._host_ok() and
                (origin is None or origin in (f"http://127.0.0.1:{BRIDGE_PORT}",
                                              f"http://localhost:{BRIDGE_PORT}")) and
                self.headers.get("X-FX-Token") == WRITE_TOKEN)

    def _upstream(self, method, path, data=None):
        try:
            self._json(mi(method, path, data))
        except RuntimeError as e:
            self._json({"error": str(e)}, 502)

    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "bad host"}, 403)
        if self.path in ("/", "/index.html"):
            try:
                with open(UI_FILE) as f:
                    body = f.read().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                self._json({"error": "UI 文件缺失"}, 500)
        elif self.path == "/api/token":
            self._json({"token": WRITE_TOKEN})
        elif self.path == "/api/status":
            self._json(build_status())
        elif self.path == "/api/events":
            self._json({"events": read_events()})
        elif self.path.startswith("/api/mihomo/"):
            path = self.path[len("/api/mihomo"):]
            self._upstream("GET", path)
        else:
            self._json({"error": "not found"}, 404)

    def do_PUT(self):
        if not self._write_ok():
            return self._json({"error": "write authorization required"}, 403)
        if self.path.startswith("/api/mihomo/"):
            path = self.path[len("/api/mihomo"):]
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 <= length <= 65536:
                    return self._json({"error": "body too large"}, 413)
            except ValueError:
                return self._json({"error": "bad length"}, 400)
            try:
                raw = self.rfile.read(length) if length > 0 else b""
                data = json.loads(raw) if raw.strip() else None
            except (ValueError, OSError):
                return self._json({"error": "bad json"}, 400)
            self._upstream("PUT", path, data)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._write_ok():
            return self._json({"error": "write authorization required"}, 403)
        if self.path.startswith("/api/actions/"):
            name = self.path.rsplit("/", 1)[-1]
            try:
                if name == "kernel-stop":
                    ok, msg = do_kernel_stop()
                elif name in ACTIONS:
                    ok, msg = ACTIONS[name]()
                else:
                    return self._json({"error": "unknown action"}, 404)
                self._json({"ok": bool(ok), "msg": str(msg)}, 200 if ok else 503)
            except (RuntimeError, OSError, subprocess.SubprocessError) as e:
                self._json({"ok": False, "msg": str(e)}, 503)
        else:
            self._json({"error": "not found"}, 404)


def main():
    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=guard_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", BRIDGE_PORT), Handler)
    log(f"fx-bridge 就绪 http://127.0.0.1:{BRIDGE_PORT} "
        f"(port={MIXED_PORT} sysapply={SYSAPPLY} events={EVENTS_PATH})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
