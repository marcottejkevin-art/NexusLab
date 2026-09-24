"""
NexusLab hub — polls every agent and Pi-hole, keeps short history,
serves the dashboard, proxies container actions/logs, handles login
and sends Discord alerts.
"""
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).parent
STATIC = HERE / "static"
CFG = yaml.safe_load(Path(os.environ.get("NEXUSLAB_CONFIG", HERE / "config.yaml")).read_text())
FAST = float(CFG.get("poll_seconds", 3))
SLOW = float(CFG.get("container_poll_seconds", 10))
HISTORY = int(CFG.get("history_points", 120))

AUTH = CFG.get("auth") or {}
AUTH_USER = str(AUTH.get("username", "admin"))
AUTH_PASS = str(AUTH.get("password", "") or "")
SESSION_DAYS = int(AUTH.get("session_days", 30))
COOKIE = "nexuslab_session"
# Changing the password in config.yaml signs everyone out.
SECRET = hashlib.sha256(f"nexuslab:{AUTH_USER}:{AUTH_PASS}".encode()).digest()


# ---------------------------------------------------------------- Pi-hole
class PiHole:
    """Supports Pi-hole v6 (REST API) and v5 (api.php)."""

    def __init__(self, cfg):
        self.url = cfg["url"].rstrip("/")
        self.version = int(cfg.get("version", 6))
        self.password = cfg.get("password", "")
        self.api_token = cfg.get("api_token", "")
        self.sid = None
        self.http = httpx.AsyncClient(timeout=5, verify=bool(cfg.get("verify_tls", False)))

    async def _login(self):
        r = await self.http.post(f"{self.url}/api/auth", json={"password": self.password})
        r.raise_for_status()
        self.sid = r.json()["session"]["sid"]

    async def _get(self, path):
        for attempt in (0, 1):
            headers = {"X-FTL-SID": self.sid} if self.sid else {}
            r = await self.http.get(f"{self.url}{path}", headers=headers)
            if r.status_code == 401 and self.password and attempt == 0:
                await self._login()
                continue
            r.raise_for_status()
            return r.json()

    async def summary(self):
        if self.version == 5:
            r = await self.http.get(f"{self.url}/admin/api.php", params={"summaryRaw": "", "auth": self.api_token})
            r.raise_for_status()
            d = r.json()
            return {
                "total": d.get("dns_queries_today"),
                "blocked": d.get("ads_blocked_today"),
                "percent": d.get("ads_percentage_today"),
                "domains": d.get("domains_being_blocked"),
                "clients": d.get("unique_clients"),
                "blocking": d.get("status"),
            }
        d = await self._get("/api/stats/summary")
        q = d.get("queries", {})
        blocking = None
        try:
            blocking = (await self._get("/api/dns/blocking")).get("blocking")
        except Exception:
            pass
        return {
            "total": q.get("total"),
            "blocked": q.get("blocked"),
            "percent": q.get("percent_blocked"),
            "domains": d.get("gravity", {}).get("domains_being_blocked"),
            "clients": d.get("clients", {}).get("active"),
            "blocking": blocking,
        }


# ---------------------------------------------------------------- devices
class Device:
    def __init__(self, cfg):
        self.cfg = cfg
        self.id = cfg["id"]
        self.name = cfg.get("name", self.id)
        self.url = cfg["url"].rstrip("/")
        self.headers = {"Authorization": f"Bearer {cfg.get('token', '')}"}
        self.docker = cfg.get("docker", "off")
        self.pihole = PiHole(cfg["pihole"]) if cfg.get("pihole") else None
        self.metrics = None
        self.online = False
        self.error = None
        self.last_seen = None
        self.history = deque(maxlen=HISTORY)
        self.containers = None
        self.pihole_state = None
        self.recent_actions = {}  # container name -> time of a dashboard stop/restart
        self.ollama_state = None
        self.power_action = None  # ("reboot" | "shutdown" | "wake", time) when started from the dashboard
        self.power_seen_offline = False
        self.learned = {}  # {"mac": ..., "wol": bool|None, "wired": bool} remembered across restarts

    @property
    def mac(self):
        return str(self.cfg.get("mac") or self.learned.get("mac") or "").lower() or None

    @property
    def can_wake(self):
        if self.cfg.get("mac"):
            return True
        return bool(self.mac and self.learned.get("wired") and self.learned.get("wol") is not False)

    @property
    def caps(self):
        m = self.metrics or {}
        return {"power": bool(m.get("power")) or bool(self.learned.get("power")), "ollama": bool(m.get("ollama"))}

    def public(self):
        error = self.error
        if self.power_action and not self.online:
            error = {"reboot": "Rebooting (started from the dashboard)",
                     "shutdown": "Turned off on purpose",
                     "wake": "Starting up (Start sent from the dashboard)"}[self.power_action[0]]
        return {
            "id": self.id,
            "name": self.name,
            "host": self.url,
            "online": self.online,
            "error": error,
            "last_seen": self.last_seen,
            "features": {
                "gpu": bool(self.cfg.get("gpu")),
                "docker": self.docker,
                "pihole": self.pihole is not None,
                "power": self.caps["power"],
                "ollama": self.caps["ollama"],
                "wake": self.can_wake,
            },
            "power_pending": self.power_action[0] if self.power_action else None,
            "wol": {"supported": self.learned.get("wol"), "armed": ((self.metrics or {}).get("net") or {}).get("wol_armed")},
            "metrics": self.metrics,
            "history": list(self.history),
            "containers": self.containers,
            "pihole": self.pihole_state,
            "ollama": self.ollama_state,
        }


DEVICES = {d["id"]: Device(d) for d in CFG["devices"]}

DATA_FILE = HERE / "data" / "state.json"


def load_state():
    try:
        data = json.loads(DATA_FILE.read_text())
    except Exception:
        return
    for dev_id, info in (data.get("learned") or {}).items():
        if dev_id in DEVICES:
            DEVICES[dev_id].learned = info
    for dev_id, ts in (data.get("off") or {}).items():
        if dev_id in DEVICES:
            DEVICES[dev_id].power_action = ("shutdown", ts)


def save_state():
    data = {"learned": {d.id: d.learned for d in DEVICES.values() if d.learned},
            "off": {d.id: d.power_action[1] for d in DEVICES.values() if d.power_action and d.power_action[0] == "shutdown"}}
    try:
        DATA_FILE.parent.mkdir(exist_ok=True)
        tmp = DATA_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(DATA_FILE)
    except OSError as e:
        print(f"[nexuslab] Couldn't save {DATA_FILE}: {e}", flush=True)


load_state()


def send_magic_packet(d):
    mac = re.sub(r"[^0-9a-f]", "", d.mac or "")
    if len(mac) != 12:
        raise ValueError(f"No valid MAC address known for {d.name}")
    packet = b"\xff" * 6 + bytes.fromhex(mac) * 16
    targets = {"255.255.255.255"}
    host = urlparse(d.url).hostname or ""
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", host):
        targets.add(".".join(host.split(".")[:3] + ["255"]))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for target in targets:
            for port in (9, 7):
                sock.sendto(packet, (target, port))
http: httpx.AsyncClient = None  # set in lifespan


def _err(e):
    if isinstance(e, httpx.HTTPStatusError):
        try:
            return e.response.json().get("detail") or str(e)
        except Exception:
            return f"HTTP {e.response.status_code}"
    if isinstance(e, httpx.ConnectError):
        return "Connection refused or host unreachable"
    if isinstance(e, httpx.TimeoutException):
        return "Timed out"
    return str(e) or e.__class__.__name__


def _dur(s):
    s = int(s)
    if s < 90:
        return f"{s} seconds"
    if s < 5400:
        return f"{round(s / 60)} minutes"
    return f"{s / 3600:.1f} hours"


# ---------------------------------------------------------------- Discord alerts
RED, AMBER, GREEN, BLUE = 0xE5534B, 0xE6A23C, 0x5CC98A, 0x8C97F2
ALERT_CFG = CFG.get("alerts") or {}
try:
    TZ = ZoneInfo(str(ALERT_CFG.get("timezone", "America/Los_Angeles")))
except Exception:
    print("[nexuslab] Unknown timezone in alerts.timezone, using UTC", flush=True)
    TZ = timezone.utc
SUMMARY_AT = str(ALERT_CFG.get("daily_summary", "08:00") or "").strip()  # "" turns it off


class DailyStats:
    """What happened since the last daily summary."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.since = time.time()
        self.dev = {}
        self.events = []  # (time, title)

    def record(self, d):
        s = self.dev.setdefault(d.id, {"samples": 0, "online": 0, "cpu_sum": 0.0, "cpu_n": 0, "cpu_temp": None,
                                        "gpu_temp": None, "gpu_util": None, "ram_peak": None})
        s["samples"] += 1
        if not d.online or not d.metrics:
            return
        s["online"] += 1
        m = d.metrics
        s["cpu_sum"] += m["cpu"]["percent"]
        s["cpu_n"] += 1
        hi = lambda a, b: b if a is None else (a if b is None else max(a, b))
        s["cpu_temp"] = hi(s["cpu_temp"], m["cpu"].get("temp_c"))
        s["ram_peak"] = hi(s["ram_peak"], m["ram"]["percent"])
        for g in m.get("gpus") or []:
            s["gpu_temp"] = hi(s["gpu_temp"], g.get("temp_c"))
            s["gpu_util"] = hi(s["gpu_util"], g.get("util"))

    def event(self, title):
        self.events.append((time.time(), title))


STATS = DailyStats()


class Alerter:
    def __init__(self, cfg):
        cfg = cfg or {}
        self.url = str(cfg.get("discord_webhook", "") or "").strip()
        self.offline_after = int(cfg.get("offline_after_seconds", 60))
        self.cpu_limit = float(cfg.get("cpu_temp_c", 85))
        self.gpu_limit = float(cfg.get("gpu_temp_c", 83))
        self.sustain = int(cfg.get("temp_sustain_seconds", 60))
        self.message_on_start = bool(cfg.get("message_on_start", True))
        self.enabled = {k: bool(cfg.get(k, True)) for k in ("device_offline", "high_temp", "container_stopped")}
        self.state = {}

    @property
    def active(self):
        return self.url.startswith("https://")

    async def send(self, title, description, color, log=None, fields=None):
        if log if log is not None else color in (RED, AMBER):
            STATS.event(title)
        if not self.active:
            return
        payload = {
            "username": "NexusLab",
            "embeds": [{
                "title": title,
                "description": description,
                "color": color,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **({"fields": fields} if fields else {}),
            }],
        }
        for _ in range(3):
            try:
                r = await http.post(self.url, json=payload, timeout=10)
                if r.status_code == 429:
                    await asyncio.sleep(float(r.json().get("retry_after", 2)))
                    continue
                if r.status_code >= 400:
                    print(f"[alerts] Discord answered {r.status_code}: {r.text[:200]}", flush=True)
                return
            except Exception as e:
                print(f"[alerts] Couldn't reach Discord: {_err(e)}", flush=True)
                return

    async def check_device(self, d: Device):
        now = time.time()
        if self.enabled["device_offline"]:
            s = self.state.setdefault(("offline", d.id), {"since": None, "alerted": False})
            pa = d.power_action
            if pa and not d.online:
                d.power_seen_offline = True
            if pa and d.online and (now - pa[1] > 20 or pa[0] == "wake"):
                if d.power_seen_offline:
                    if pa[0] == "reboot":
                        await self.send(f"🟢 {d.name} is back up after the reboot", f"Offline for about {_dur(now - pa[1])}.", GREEN)
                    elif pa[0] == "wake":
                        await self.send(f"🟢 {d.name} is on", f"It took about {_dur(now - pa[1])} to start.", GREEN)
                    else:
                        await self.send(f"🟢 {d.name} is on again", "It was turned off on purpose.", GREEN)
                    d.power_action = pa = None
                    d.power_seen_offline = False
                    save_state()
                elif now - pa[1] > 120:
                    d.power_action = pa = None  # it never went down; stop waiting
                    save_state()
            if pa and pa[0] == "wake" and not d.online and now - pa[1] > 180:
                await self.send(f"⚠️ {d.name} didn't turn on",
                                "No answer 3 minutes after Start. Check that it's plugged in, on Ethernet, and that "
                                "Wake on LAN is enabled in its BIOS.", AMBER)
                d.power_action = pa = ("shutdown", now)
                save_state()
            if pa and not d.online and (pa[0] in ("shutdown", "wake") or now - pa[1] < 600):
                s.update(since=None, alerted=False)
            elif not d.online:
                s["since"] = s["since"] or now
                if not s["alerted"] and now - s["since"] >= self.offline_after:
                    s["alerted"] = True
                    await self.send(f"🔴 {d.name} is offline",
                                    f"No answer from its agent for {_dur(now - s['since'])}.\nLast error: {d.error or 'unknown'}", RED)
            else:
                if s["alerted"]:
                    await self.send(f"🟢 {d.name} is back online", f"It was unreachable for about {_dur(now - s['since'])}.", GREEN)
                s.update(since=None, alerted=False)

        if not d.online or not d.metrics or not self.enabled["high_temp"]:
            return
        m = d.metrics
        await self._temp(d, "cpu", "CPU", (m.get("cpu") or {}).get("temp_c"), self.cpu_limit)
        for i, g in enumerate(m.get("gpus") or []):
            label = str(g.get("name", "GPU")).replace("NVIDIA ", "").replace("GeForce ", "")
            await self._temp(d, f"gpu{i}", label, g.get("temp_c"), self.gpu_limit)

    async def _temp(self, d, key, label, temp, limit):
        if temp is None:
            return
        now = time.time()
        s = self.state.setdefault(("temp", d.id, key), {"since": None, "alerted": False})
        if temp >= limit:
            s["since"] = s["since"] or now
            if not s["alerted"] and now - s["since"] >= self.sustain:
                s["alerted"] = True
                await self.send(f"🔥 {d.name}: {label} is running hot",
                                f"{label} is at **{temp:.0f} °C**, above the {limit:.0f} °C alert level for over {_dur(self.sustain)}.", AMBER)
        elif temp <= limit - 5:
            if s["alerted"]:
                await self.send(f"✅ {d.name}: {label} has cooled down", f"{label} is back to {temp:.0f} °C.", GREEN)
            s.update(since=None, alerted=False)
        elif not s["alerted"]:
            s["since"] = None

    async def check_containers(self, d: Device):
        if not self.enabled["container_stopped"]:
            return
        c = d.containers
        if not d.online or not c or not c.get("ok"):
            return
        current = {i["name"]: i["status"] for i in c["items"]}
        key = ("ctr", d.id)
        prev = self.state.get(key)
        alerted = self.state.setdefault(("ctr-alerted", d.id), set())
        self.state[key] = current
        if prev is None:  # first look: just remember what's running
            return
        now = time.time()
        for name in sorted(set(prev) | set(current)):
            was, now_status = prev.get(name), current.get(name)
            if was == "running" and now_status != "running":
                if now - d.recent_actions.get(name, 0) < 120:
                    continue  # stopped or restarted from the dashboard
                alerted.add(name)
                await self.send(f"⚠️ {d.name}: {name} stopped",
                                f"Container **{name}** is now {now_status or 'removed'}.", RED)
            elif now_status == "running" and name in alerted:
                alerted.discard(name)
                await self.send(f"✅ {d.name}: {name} is running again", f"Container **{name}** is back up.", GREEN)


ALERTER = Alerter(CFG.get("alerts"))


def _local(ts, fmt="%-I:%M %p"):
    return datetime.fromtimestamp(ts, TZ).strftime(fmt)


def build_summary():
    now = time.time()
    fields = []
    for d in DEVICES.values():
        s = STATS.dev.get(d.id)
        lines = []
        if s and s["samples"]:
            up = s["online"] / s["samples"] * 100
            lines.append(f"Online **{up:.1f}%** of the time" if up < 99.95 else "Online the whole time")
            if s["cpu_n"]:
                t = f", hottest {s['cpu_temp']:.0f} °C" if s["cpu_temp"] is not None else ""
                lines.append(f"CPU average {s['cpu_sum'] / s['cpu_n']:.0f}%{t}")
            if s["ram_peak"] is not None:
                lines.append(f"Memory peak {s['ram_peak']:.0f}%")
            if s["gpu_temp"] is not None:
                lines.append(f"GPU peak load {s['gpu_util']:.0f}%, hottest {s['gpu_temp']:.0f} °C")
        m = d.metrics or {}
        if m.get("disk"):
            lines.append(f"SSD {m['disk']['percent']:.0f}% full")
        c = d.containers
        if c and c.get("ok"):
            run = sum(1 for i in c["items"] if i["status"] == "running")
            lines.append(f"Containers {run} of {len(c['items'])} running")
        p = d.pihole_state
        if p and p.get("ok") and p.get("total") is not None:
            lines.append(f"Pi-hole blocked {p['blocked']:,} of {p['total']:,} queries ({p['percent']:.1f}%)")
        state = "🟢" if d.online else ("⚫" if d.power_action and d.power_action[0] == "shutdown" else "🔴")
        fields.append({"name": f"{state} {d.name}", "value": "\n".join(lines) or "No data yet", "inline": False})
    if STATS.events:
        ev = [f"`{_local(t)}` {title}" for t, title in STATS.events[-10:]]
        more = len(STATS.events) - 10
        if more > 0:
            ev.insert(0, f"…and {more} earlier")
        fields.append({"name": f"Events ({len(STATS.events)})", "value": "\n".join(ev)[:1024], "inline": False})
    else:
        fields.append({"name": "Events", "value": "No problems ✅", "inline": False})
    hours = (now - STATS.since) / 3600
    span = "the last 24 hours" if hours >= 23 else f"the last {hours:.1f} hours (since the hub started {_local(STATS.since, '%a %-I:%M %p')})"
    return f"Covering {span}.", fields


async def send_summary(reset=True):
    desc, fields = build_summary()
    await ALERTER.send("☀️ NexusLab daily summary", desc, BLUE, log=False, fields=fields)
    if reset:
        STATS.reset()


async def summary_loop():
    if not SUMMARY_AT:
        return
    try:
        hh, mm = (int(x) for x in SUMMARY_AT.split(":"))
    except ValueError:
        print(f"[nexuslab] alerts.daily_summary should look like \"08:00\", got {SUMMARY_AT!r}", flush=True)
        return
    now = datetime.now(TZ)
    last = now.date() if (now.hour, now.minute) >= (hh, mm) else None
    while True:
        await asyncio.sleep(30)
        now = datetime.now(TZ)
        if (now.hour, now.minute) >= (hh, mm) and last != now.date():
            last = now.date()
            try:
                await send_summary()
            except Exception as e:
                print(f"[alerts] summary failed: {e}", flush=True)


# ---------------------------------------------------------------- polling
async def poll_metrics(d: Device):
    try:
        r = await http.get(f"{d.url}/metrics", headers=d.headers, timeout=4)
        r.raise_for_status()
        m = r.json()
        d.metrics, d.online, d.error, d.last_seen = m, True, None, time.time()
        net = m.get("net") or {}
        learned = {"mac": net.get("mac"), "wired": bool(net.get("wired")),
                   "wol": net.get("wol_supported") if net.get("ethtool") else None, "power": bool(m.get("power"))}
        if net.get("mac") and learned != d.learned:
            d.learned = learned
            save_state()
        gpu = m["gpus"][0]["util"] if m.get("gpus") else None
        d.history.append({"t": int(time.time()), "cpu": m["cpu"]["percent"], "ram": m["ram"]["percent"], "gpu": gpu})
    except Exception as e:
        d.online, d.error = False, _err(e)


async def poll_containers(d: Device):
    if d.docker in ("monitor", "control") and d.online:
        try:
            r = await http.get(f"{d.url}/containers", headers=d.headers, timeout=8)
            r.raise_for_status()
            d.containers = {"ok": True, "items": r.json()["items"], "at": time.time()}
        except Exception as e:
            d.containers = {"ok": False, "error": _err(e), "items": (d.containers or {}).get("items", [])}
    if d.caps["ollama"] and d.online:
        await poll_ollama(d)
    if d.pihole:
        try:
            d.pihole_state = {"ok": True, **(await d.pihole.summary())}
        except Exception as e:
            d.pihole_state = {"ok": False, "error": _err(e)}


async def poll_ollama(d: Device):
    try:
        r = await http.get(f"{d.url}/ollama", headers=d.headers, timeout=6)
        r.raise_for_status()
        d.ollama_state = {"ok": True, **r.json()}
    except Exception as e:
        d.ollama_state = {"ok": False, "error": _err(e), **{k: (d.ollama_state or {}).get(k) for k in ("version", "installed", "loaded")}}


async def metrics_loop():
    while True:
        await asyncio.gather(*(poll_metrics(d) for d in DEVICES.values()), return_exceptions=True)
        for d in DEVICES.values():
            STATS.record(d)
            try:
                await ALERTER.check_device(d)
            except Exception as e:
                print(f"[alerts] {e}", flush=True)
        await asyncio.sleep(FAST)


async def containers_loop():
    while True:
        await asyncio.gather(*(poll_containers(d) for d in DEVICES.values()), return_exceptions=True)
        for d in DEVICES.values():
            try:
                await ALERTER.check_containers(d)
            except Exception as e:
                print(f"[alerts] {e}", flush=True)
        await asyncio.sleep(SLOW)


@asynccontextmanager
async def lifespan(app):
    global http
    http = httpx.AsyncClient()
    print(f"[nexuslab] Login {'on' if AUTH_PASS else 'OFF (set auth.password in config.yaml)'}", flush=True)
    print(f"[nexuslab] Discord alerts {'on' if ALERTER.active else 'off (no alerts.discord_webhook)'}", flush=True)
    print(f"[nexuslab] Daily summary {('at ' + SUMMARY_AT + ' ' + str(TZ)) if SUMMARY_AT and ALERTER.active else 'off'}", flush=True)
    tasks = [asyncio.create_task(metrics_loop()), asyncio.create_task(containers_loop()), asyncio.create_task(summary_loop())]
    if ALERTER.active and ALERTER.message_on_start:
        names = ", ".join(d.name for d in DEVICES.values())
        tasks.append(asyncio.create_task(ALERTER.send("NexusLab is running", f"Watching {names}. Alerts are connected.", BLUE)))
    yield
    for t in tasks:
        t.cancel()
    await http.aclose()


app = FastAPI(title="NexusLab", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# ---------------------------------------------------------------- login
def make_session():
    exp = str(int(time.time()) + SESSION_DAYS * 86400)
    return f"{exp}.{hmac.new(SECRET, exp.encode(), hashlib.sha256).hexdigest()}"


def valid_session(value):
    exp, _, sig = (value or "").partition(".")
    if not exp.isdigit() or not sig:
        return False
    good = hmac.new(SECRET, exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and int(exp) > time.time()


PUBLIC_PATHS = {"/login", "/api/login", "/logout", "/manifest.webmanifest", "/favicon.ico", "/apple-touch-icon.png"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if not AUTH_PASS or path in PUBLIC_PATHS or path.startswith("/static/icons/"):
        return await call_next(request)
    if valid_session(request.cookies.get(COOKIE)):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Login required"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


FAILED = defaultdict(list)


@app.get("/login")
async def login_page(request: Request):
    if not AUTH_PASS or valid_session(request.cookies.get(COOKIE)):
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC / "login.html", headers={"Cache-Control": "no-store"})


@app.post("/api/login")
async def login(request: Request):
    ip = request.client.host if request.client else "?"
    now = time.time()
    FAILED[ip] = [t for t in FAILED[ip] if now - t < 300]
    if len(FAILED[ip]) >= 8:
        raise HTTPException(429, "Too many attempts. Wait 5 minutes and try again.")
    try:
        body = await request.json()
    except Exception:
        body = {}
    user, pw = str(body.get("username", "")), str(body.get("password", ""))
    if AUTH_PASS and secrets.compare_digest(user.encode(), AUTH_USER.encode()) \
            and secrets.compare_digest(pw.encode(), AUTH_PASS.encode()):
        FAILED.pop(ip, None)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, make_session(), max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax")
        return resp
    FAILED[ip].append(now)
    await asyncio.sleep(1)
    raise HTTPException(401, "Wrong username or password.")


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# ---------------------------------------------------------------- app shell
@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/manifest.webmanifest")
async def manifest():
    return JSONResponse({
        "name": "NexusLab",
        "short_name": "NexusLab",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#10151E",
        "theme_color": "#10151E",
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }, media_type="application/manifest+json")


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(STATIC / "icons" / "favicon-32.png")


@app.get("/apple-touch-icon.png")
async def apple_icon():
    return FileResponse(STATIC / "icons" / "apple-touch-icon.png")


@app.get("/api/state")
async def state():
    return {"generated": time.time(), "auth": bool(AUTH_PASS), "devices": [d.public() for d in DEVICES.values()]}


def _device(dev_id):
    d = DEVICES.get(dev_id)
    if not d:
        raise HTTPException(404, f"Unknown device {dev_id}")
    return d


@app.post("/api/devices/{dev_id}/containers/{name}/{action}")
async def container_action(dev_id: str, name: str, action: str):
    d = _device(dev_id)
    if d.docker != "control":
        raise HTTPException(403, f"{d.name} is monitor-only")
    if action in ("stop", "restart"):
        d.recent_actions[name] = time.time()
    try:
        r = await http.post(f"{d.url}/containers/{name}/{action}", headers=d.headers, timeout=40)
        r.raise_for_status()
    except Exception as e:
        code = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else 502
        raise HTTPException(code, _err(e))
    await poll_containers(d)
    return r.json()


@app.post("/api/devices/{dev_id}/power/{action}")
async def power_action(dev_id: str, action: str):
    d = _device(dev_id)
    if action not in ("reboot", "shutdown", "on"):
        raise HTTPException(400, "Action must be reboot, shutdown or on")
    if action == "on":
        if d.online:
            raise HTTPException(409, f"{d.name} is already on")
        if not d.mac:
            raise HTTPException(400, f"NexusLab hasn't learned {d.name}'s network address yet. Turn it on once by hand with the updated agent.")
        try:
            for _ in range(3):
                send_magic_packet(d)
                await asyncio.sleep(0.3)
        except Exception as e:
            raise HTTPException(500, f"Couldn't send the Start signal: {e}")
        d.power_action = ("wake", time.time())
        d.power_seen_offline = True
        save_state()
        await ALERTER.send(f"⚡ Starting {d.name}", "Started from the NexusLab dashboard. It usually takes 20 to 60 seconds.", BLUE, log=True)
        return {"action": "on", "sent": True}
    if not d.caps["power"]:
        raise HTTPException(403, f"Power control isn't enabled on {d.name}. Update its agent first.")
    try:
        r = await http.post(f"{d.url}/power/{action}", headers=d.headers, timeout=10)
        r.raise_for_status()
    except Exception as e:
        code = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else 502
        raise HTTPException(code, _err(e))
    d.power_action = (action, time.time())
    d.power_seen_offline = False
    save_state()
    if action == "reboot":
        await ALERTER.send(f"🔁 {d.name} is rebooting", "Started from the NexusLab dashboard.", BLUE, log=True)
    else:
        await ALERTER.send(f"⏻ {d.name} was shut down", "Started from the NexusLab dashboard. It stays off until you power it on.", BLUE, log=True)
    return r.json()


@app.post("/api/devices/{dev_id}/ollama/{action}")
async def ollama_action(dev_id: str, action: str, request: Request):
    d = _device(dev_id)
    if action not in ("load", "unload"):
        raise HTTPException(400, "Action must be load or unload")
    try:
        body = await request.json()
        model = str(body["model"])
    except Exception:
        raise HTTPException(400, "Send {\"model\": \"name\"}")
    try:
        r = await http.post(f"{d.url}/ollama/{action}", headers=d.headers, json={"model": model}, timeout=200)
        r.raise_for_status()
    except Exception as e:
        code = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else 502
        raise HTTPException(code, _err(e))
    await poll_ollama(d)
    return r.json()


# ---------------------------------------------------------------- chat
CHAT_CFG = CFG.get("chat") or {}
NUM_CTX = int(CHAT_CFG.get("num_ctx", 8192))


def _gb(b):
    return "?" if b is None else f"{b / 1024 ** 3:.1f} GB"


def status_text():
    now = datetime.now(TZ)
    out = [f"Live NexusLab status at {now.strftime('%A %B %-d, %-I:%M %p')} ({TZ})."]
    for d in DEVICES.values():
        m = d.metrics or {}
        if not d.online:
            seen = f", last seen {_local(d.last_seen, '%a %-I:%M %p')}" if d.last_seen else ""
            pa = d.power_action[0] if d.power_action else None
            if pa == "shutdown":
                out.append(f"\n## {d.name}: OFF ON PURPOSE ({WHO} shut it down; this is normal, not a problem{seen}). "
                           + ("It can be turned on with the Start button in NexusLab." if d.can_wake else ""))
            elif pa in ("reboot", "wake"):
                out.append(f"\n## {d.name}: STARTING UP ({'rebooting' if pa == 'reboot' else 'Start was just sent'})")
            else:
                out.append(f"\n## {d.name}: OFFLINE unexpectedly ({d.error or 'no response'}{seen})")
            continue
        up = m.get("uptime_s") or 0
        out.append(f"\n## {d.name}: online, up {up // 86400}d {up % 86400 // 3600}h, {m.get('os', '')}")
        c, r, k = m.get("cpu", {}), m.get("ram", {}), m.get("disk", {})
        t = f", {c['temp_c']:.0f} °C" if c.get("temp_c") is not None else ", no temp sensor"
        out.append(f"- CPU {c.get('percent', 0):.0f}% of {c.get('cores')} cores{t}")
        out.append(f"- Memory {_gb(r.get('used'))} of {_gb(r.get('total'))} ({r.get('percent', 0):.0f}%)")
        dt = f", {k['temp_c']:.0f} °C" if k.get("temp_c") is not None else ""
        out.append(f"- SSD {_gb(k.get('used'))} of {_gb(k.get('total'))} used ({k.get('percent', 0):.0f}%){dt}")
        for g in m.get("gpus") or []:
            pw = f", {g['power_w']:.0f} W of {g['power_limit_w']} W" if g.get("power_w") is not None else ""
            out.append(f"- GPU {g['name']}: {g['util']}% load, VRAM {_gb(g['mem_used'])} of {_gb(g['mem_total'])}, {g['temp_c']} °C{pw}")
        cs = d.containers
        if cs and cs.get("items"):
            mode = "can be controlled from NexusLab" if d.docker == "control" else "view only"
            out.append(f"- Docker containers ({mode}):")
            for i in cs["items"]:
                extra = ""
                if i["status"] == "running" and i.get("mem_used") is not None:
                    extra = f", CPU {i.get('cpu_percent') or 0:.1f}%, memory {i['mem_used'] / 1024 ** 2:.0f} MB"
                health = f", {i['health']}" if i.get("health") else ""
                out.append(f"  - {i['name']} ({i['image']}): {i['status']}{health}{extra}")
        p = d.pihole_state
        if p and p.get("ok"):
            out.append(f"- Pi-hole (last 24 h): blocking {p.get('blocking') or 'unknown'}, {p.get('blocked', 0):,} of "
                       f"{p.get('total', 0):,} queries blocked ({p.get('percent') or 0:.1f}%), "
                       f"{p.get('domains', 0):,} domains on blocklist, {p.get('clients')} active clients")
        elif p:
            out.append(f"- Pi-hole: not answering ({p.get('error')})")
        o = d.ollama_state
        if o and o.get("ok"):
            loaded = ", ".join(f"{x['name']} ({_gb(x.get('vram'))} VRAM)" for x in o.get("loaded") or []) or "none"
            out.append(f"- Ollama {o.get('version')}: loaded models: {loaded}; {len(o.get('installed') or [])} installed")
    recent = [(t, title) for t, title in STATS.events if time.time() - t < 86400]
    if recent:
        out.append("\n## Alerts in the last 24 hours")
        out += [f"- {_local(t, '%a %-I:%M %p')}: {title}" for t, title in recent[-10:]]
    else:
        out.append("\n## Alerts: none recently")
    return "\n".join(out)


OWNER = str(CHAT_CFG.get("owner", "") or "").strip()
WHO = OWNER or "the owner"

SYSTEM_PROMPT = """You are NexusLab Assistant, built into {whose} homelab dashboard app.
You can see LIVE data about the homelab machines below. It was collected seconds ago and is the only source of truth: never invent numbers or machines.

When asked to check status (or anything like "how are my machines"), reply with one short section per machine:
**Machine name**: online or offline, then CPU, memory, SSD, temperature, and for the Nexus AI PC the GPU. Mention containers (say if any are stopped) and Pi-hole stats for the Raspberry Pi. End with a one-line verdict.
A machine that is OFF ON PURPOSE is fine: just say it's off, never call it critical.
Call out anything worrying: a machine offline unexpectedly, CPU or GPU above 80 °C, memory above 90%, SSD above 85%, stopped or unhealthy containers, Pi-hole blocking off, recent alerts.
Keep it tight and scannable. Use **bold** machine names and "- " bullets. No tables.
You can't take actions. If {who} asks to reboot, stop or start something, say which button to use in NexusLab (Start, Reboot, Shut down, or the container buttons).
For questions that aren't about the homelab, just answer normally.

{status}"""


def _pick_model(requested):
    d = next((x for x in DEVICES.values() if x.caps["ollama"] and x.online), None)
    if not d:
        return None, None
    o = d.ollama_state or {}
    names = [m["name"] for m in o.get("installed") or [] if "embed" not in m["name"].lower()]
    if requested and (requested in names or not names):
        return d, requested
    loaded = [m["name"] for m in o.get("loaded") or [] if "embed" not in m["name"].lower()]
    default = CHAT_CFG.get("model")
    return d, (default if default in names else (loaded or names or [None])[0])


@app.post("/api/chat")
async def chat(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    d, model = _pick_model(str(body.get("model") or ""))
    if not d:
        raise HTTPException(503, "No machine with Ollama is online right now.")
    if not model:
        raise HTTPException(503, "Ollama has no chat models installed.")
    history = [{"role": m["role"], "content": str(m.get("content", ""))[:8000]}
               for m in (body.get("messages") or []) if isinstance(m, dict) and m.get("role") in ("user", "assistant")][-16:]
    if not history or history[-1]["role"] != "user":
        raise HTTPException(400, "Send a message first.")
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT.format(
            status=status_text(), who=WHO, whose=f"{OWNER}'s" if OWNER else "the user's")}] + history,
        "options": {"num_ctx": NUM_CTX},
    }

    async def stream():
        yield json.dumps({"model": model}) + "\n"
        try:
            async with http.stream("POST", f"{d.url}/ollama/chat", headers=d.headers, json=payload,
                                   timeout=httpx.Timeout(300, connect=5)) as r:
                if r.status_code >= 400:
                    raw = await r.aread()
                    try:
                        msg = json.loads(raw).get("detail")
                    except Exception:
                        msg = raw.decode(errors="replace")[:200]
                    yield json.dumps({"error": msg or f"HTTP {r.status_code}"}) + "\n"
                    return
                async for line in r.aiter_lines():
                    if line.strip():
                        yield line + "\n"
        except Exception as e:
            yield json.dumps({"error": _err(e)}) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})


@app.get("/api/summary/send")
async def summary_now():
    if not ALERTER.active:
        raise HTTPException(400, "Add alerts.discord_webhook to config.yaml first")
    await send_summary(reset=False)
    return {"sent": True, "note": "A test summary was posted to Discord. The daily one still goes out on schedule."}


@app.get("/api/devices/{dev_id}/containers/{name}/logs")
async def container_logs(dev_id: str, name: str, tail: int = Query(200, ge=10, le=5000)):
    d = _device(dev_id)
    try:
        r = await http.get(f"{d.url}/containers/{name}/logs", headers=d.headers, params={"tail": tail}, timeout=15)
        r.raise_for_status()
    except Exception as e:
        code = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else 502
        raise HTTPException(code, _err(e))
    return PlainTextResponse(r.text)


app.mount("/static", StaticFiles(directory=STATIC), name="static")
