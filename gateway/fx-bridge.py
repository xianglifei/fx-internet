#!/usr/bin/env python3
"""fx-bridge:fx-gateway 的本地桥接服务(Python stdlib,零依赖)

职责:
- 服务分级界面(GET /)
- 聚合状态(GET /api/status):内核存活、出口分组、实时速率、系统代理实况 vs 期望、
  巡检事件/配额、三态判定(green/yellow/red 服务端统一推导)
- mihomo API 透明代理(GET|PUT /api/mihomo/*)
- 动作(POST /api/actions/...):立即巡检、内核启停、交还常规模式、应用配置
- 系统代理守护线程(30s):FX_SYSAPPLY=1 时写入 networksetup;=0 时仅监测(试运行期)

设计原则:桥不是内核的父进程(内核由 launchd 直管),桥挂了网络照常;
所有写操作遵守"先清系统代理,再停内核"的顺序。
"""
import datetime
import json
import os
import re
import subprocess
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
PATROL_ENV = {
    "PATROL_SOCKET_HINT": "fx-mihomo.sock",
    "PATROL_PROXY": env("FX_PATROL_PROXY", f"http://127.0.0.1:{MIXED_PORT}"),
    "PATROL_STATE": env("FX_PATROL_STATE", os.path.join(
        HOME, "Library/Application Support/verge-node-patrol/state.json")),
    "PATROL_EVENTS": EVENTS_PATH,
    "PATROL_LOG": env("FX_PATROL_LOG", os.path.join(
        HOME, "Library/Logs/verge-node-patrol.log")),
    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
}
PATROL_LOCK = os.path.join(STATE_DIR, "patrol.lock")
QUOTA_MB = 250

# 逐字复刻 Verge 的 bypass 列表(盘点确认)
BYPASS = ["127.0.0.1", "192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12",
          "localhost", "*.local", "*.crashlytics.com", "<local>"]

MON = {"kernel": None, "mbps": 0.0, "conns": 0, "ts": 0}     # 2s 监测线程
SYS = {"ok": None, "applied": False, "requires_admin": False,
       "error": None, "ts": 0}                                 # 30s 守护线程


def log(msg):
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def mi(method, path, data=None, timeout=5):
    """经 unix socket 调 mihomo REST API(curl 实现,与巡检同款)"""
    cmd = ["curl", "-s", "--unix-socket", SOCK, "-X", method,
           f"http://localhost{path}", "--max-time", str(timeout)]
    if data is not None:
        cmd += ["-d", json.dumps(data)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 3)
        return json.loads(r.stdout) if r.stdout.strip() else {}
    except Exception:
        return {}


def short(name):
    return name.split("@")[-1] if "@" in name else name


# ---------- 监测线程:内核存活 + 实时速率(2s) ----------

def monitor_loop():
    while True:
        ver = mi("GET", "/version", timeout=3)
        conns = mi("GET", "/connections", timeout=3)
        mbps, n = 0.0, 0
        for c in (conns.get("connections") or []):
            chains = c.get("chains") or []
            if chains and chains[-1] != "DIRECT":  # 仅统计走代理的流量
                mbps += (c.get("download", 0) + c.get("upload", 0)) * 8 / 1e6
                n += 1
        MON.update(kernel=ver.get("version"), mbps=round(mbps, 1),
                   conns=n, ts=time.time())
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
    try:
        out = subprocess.run(["networksetup", "-listallnetworkservices"],
                             capture_output=True, text=True, timeout=5).stdout
        return [l.strip() for l in out.splitlines()[1:] if l.strip() and not l.startswith("*")]
    except Exception:
        return []


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


def guard_loop():
    while True:
        try:
            cur = read_sysproxy()
            kernel_up = bool(MON.get("kernel"))
            if SYSAPPLY and kernel_up:
                want_on = (cur.get("HTTPEnable") == "1" and cur.get("HTTPProxy") == "127.0.0.1"
                           and cur.get("HTTPPort") == str(MIXED_PORT))
                if not want_on:
                    # 定位当前默认路由的网络服务,把代理设上去(跟随 Wi-Fi/网线切换)
                    svc = primary_service() or "Wi-Fi"
                    err = apply_proxy_to_service(svc, enable=True)
                    SYS.update(ok=err is None, applied=err is None, error=err,
                               requires_admin=err == "requires_admin", ts=time.time())
                else:
                    SYS.update(ok=True, applied=True, error=None,
                               requires_admin=False, ts=time.time())
            elif SYSAPPLY and not kernel_up:
                # 内核不在:清掉指向死端口的代理,浏览器直连(停机顺序原则)
                for svc in network_services():
                    out = subprocess.run(["networksetup", "-getwebproxy", svc],
                                         capture_output=True, text=True, timeout=5).stdout
                    if "Enabled: Yes" in out:
                        apply_proxy_to_service(svc, enable=False)
                SYS.update(ok=True, applied=False, error=None,
                           requires_admin=False, ts=time.time())
            else:
                # 试运行 dry-run:只报告不写
                SYS.update(
                    ok=True,
                    applied=cur.get("HTTPEnable") == "1",
                    error=None, requires_admin=False, ts=time.time())
        except Exception as e:
            SYS.update(ok=False, error=str(e), ts=time.time())
        time.sleep(30)


# ---------- 巡检事件 ----------

def read_events(n=80):
    try:
        with open(EVENTS_PATH) as f:
            lines = [l for l in f.read().splitlines() if l.strip()]
        return [json.loads(l) for l in lines[-n:]]
    except Exception:
        return []


def next_patrol_time():
    now = datetime.datetime.now()
    nxt = now.replace(minute=10, second=0, microsecond=0)
    if now.minute >= 10:
        nxt += datetime.timedelta(hours=1)
    return f"{nxt:%H:%M}"


def build_status():
    proxies = mi("GET", "/proxies").get("proxies", {})
    proxy_now = proxies.get("PROXY", {}).get("now", "")
    auto_now = proxies.get("AUTO", {}).get("now", "")
    pinned = bool(proxy_now) and proxy_now != "AUTO"
    events = read_events()
    last = events[-1] if events else None
    today = f"{datetime.date.today():%Y-%m-%d}"
    cost_today = sum(int(e.get("cost_mb", 0)) for e in events
                     if str(e.get("ts", "")).startswith(today))

    kernel_up = bool(MON.get("kernel"))
    level, reason = "green", "steady"
    if not kernel_up:
        level, reason = "red", "kernel_down"
    elif last and last.get("type") == "all_dead":
        level, reason = "red", "all_dead"
    elif SYS.get("requires_admin"):
        level, reason = "yellow", "sysproxy_requires_admin"
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
                   "cost_today_mb": cost_today, "quota_mb": QUOTA_MB,
                   "next_run": next_patrol_time()},
        "derived": {"level": level, "reason": reason},
    }


# ---------- 动作 ----------

def launchctl(*args):
    r = subprocess.run(["launchctl", *args], capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def run_patrol():
    if os.path.exists(PATROL_LOCK):
        return False, "巡检已在运行中"
    try:
        os.close(os.open(PATROL_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        return False, "巡检已在运行中"
    e = dict(os.environ)
    e.update(PATROL_ENV)
    subprocess.Popen([e.get("PATROL_PYTHON", "/opt/homebrew/bin/python3"),
                      PATROL_SCRIPT], env=e,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    threading.Timer(600, lambda: os.path.exists(PATROL_LOCK) and os.remove(PATROL_LOCK)).start()
    return True, "已触发"


ACTIONS = {
    "patrol-now": lambda: run_patrol(),
    "kernel-start": lambda: launchctl("bootstrap", f"gui/{UID_N}", PLIST_MIHOMO)
    if not launchctl("print", f"gui/{UID_N}/local.mihomo")[0]
    else launchctl("kickstart", f"gui/{UID_N}/local.mihomo"),
    "kernel-restart": lambda: launchctl("kickstart", "-k", f"gui/{UID_N}/local.mihomo"),
    "handoff-auto": lambda: (True, "已交还常规模式") if mi(
        "PUT", "/proxies/PROXY", {"name": "AUTO"}) is not None else (False, "API 无响应"),
}


def do_kernel_stop():
    # 停机顺序:先清系统代理,再停内核
    if SYSAPPLY:
        for svc in network_services():
            apply_proxy_to_service(svc, enable=False)
    return launchctl("bootout", f"gui/{UID_N}/local.mihomo")


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

    def do_GET(self):
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
        elif self.path == "/api/status":
            self._json(build_status())
        elif self.path == "/api/events":
            self._json({"events": read_events()})
        elif self.path.startswith("/api/mihomo/"):
            path = self.path[len("/api/mihomo"):]
            self._json(mi("GET", path))
        else:
            self._json({"error": "not found"}, 404)

    def do_PUT(self):
        if self.path.startswith("/api/mihomo/"):
            path = self.path[len("/api/mihomo"):]
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}") if length else None
            self._json(mi("PUT", path, data))
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path.startswith("/api/actions/"):
            name = self.path.rsplit("/", 1)[-1]
            if name == "kernel-stop":
                ok, msg = do_kernel_stop()
            elif name in ACTIONS:
                ok, msg = ACTIONS[name]()
            else:
                return self._json({"error": "unknown action"}, 404)
            self._json({"ok": bool(ok), "msg": str(msg)})
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
