"""
NexusLab agent — runs on every homelab device.

Exposes:
  GET  /metrics                          CPU, RAM, disk, temps (+ NVIDIA GPU if present)
  GET  /containers                       Docker containers with status + CPU/mem
  POST /containers/{name}/{action}       start | stop | restart   (only when NEXUSLAB_DOCKER=control)
  GET  /containers/{name}/logs?tail=200  Plain-text logs

Configure with environment variables (see /etc/nexuslab-agent.env):
  NEXUSLAB_TOKEN        shared secret the hub sends as "Authorization: Bearer <token>"
  NEXUSLAB_DOCKER       off | monitor | control          (default: off)
  NEXUSLAB_CONTAINERS   optional comma-separated allowlist of container names
  NEXUSLAB_DISK_PATH    filesystem to report (default: /)
  NEXUSLAB_TEMP_SENSOR  optional psutil sensor name to force for CPU temp (e.g. k10temp)
  NEXUSLAB_POWER        on | off  allow reboot/shutdown from the dashboard (default: on)
  NEXUSLAB_OLLAMA_URL   Ollama address to report on (default: http://127.0.0.1:11434, "off" to disable)
  NEXUSLAB_WOL          on | off  switch Wake-on-LAN on for the wired network card at startup (default: on)
"""
import json
import os
import platform
import secrets
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import psutil
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

TOKEN = os.environ.get("NEXUSLAB_TOKEN", "")
DOCKER_MODE = os.environ.get("NEXUSLAB_DOCKER", "off").strip().lower()
ALLOWLIST = {c.strip() for c in os.environ.get("NEXUSLAB_CONTAINERS", "").split(",") if c.strip()}
DISK_PATH = os.environ.get("NEXUSLAB_DISK_PATH", "/")
TEMP_SENSOR = os.environ.get("NEXUSLAB_TEMP_SENSOR", "").strip()
POWER = os.environ.get("NEXUSLAB_POWER", "on").strip().lower() not in ("off", "0", "false", "no")
OLLAMA_URL = os.environ.get("NEXUSLAB_OLLAMA_URL", "http://127.0.0.1:11434").strip().rstrip("/")
WOL = os.environ.get("NEXUSLAB_WOL", "on").strip().lower() not in ("off", "0", "false", "no")

app = FastAPI(title="NexusLab agent", docs_url=None, redoc_url=None)


def require_token(authorization: str = Header(default="")):
    if not TOKEN:
        return
    if not secrets.compare_digest(authorization.encode(), f"Bearer {TOKEN}".encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing token")


# ---------------------------------------------------------------- temperatures
CPU_SENSORS = ("k10temp", "zenpower", "coretemp", "cpu_thermal", "soc_thermal", "acpitz")
CPU_LABELS = ("Tctl", "Tdie", "Package id 0", "")
DISK_SENSORS = ("nvme", "drivetemp")


def _sensors():
    try:
        return psutil.sensors_temperatures() or {}
    except (AttributeError, OSError):  # not supported on Windows/macOS
        return {}


def _pick(entries, labels=CPU_LABELS):
    for label in labels:
        for e in entries:
            if e.label == label and e.current:
                return round(e.current, 1)
    vals = [e.current for e in entries if e.current]
    return round(max(vals), 1) if vals else None


def cpu_temp(sensors):
    order = (TEMP_SENSOR,) if TEMP_SENSOR else CPU_SENSORS
    for name in order:
        if sensors.get(name):
            return _pick(sensors[name])
    return None


def disk_temp(sensors):
    for name in DISK_SENSORS:
        if sensors.get(name):
            return _pick(sensors[name], labels=("Composite", ""))
    return None


# ---------------------------------------------------------------- NVIDIA GPU
try:
    import pynvml

    pynvml.nvmlInit()
    HAS_NVML = True
except Exception:
    HAS_NVML = False


def gpus():
    if not HAS_NVML:
        return []
    out = []
    for i in range(pynvml.nvmlDeviceGetCount()):
        h = pynvml.nvmlDeviceGetHandleByIndex(i)
        name = pynvml.nvmlDeviceGetName(h)
        name = name.decode() if isinstance(name, bytes) else name
        util = pynvml.nvmlDeviceGetUtilizationRates(h)
        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
        gpu = {
            "name": name,
            "util": util.gpu,
            "mem_used": mem.used,
            "mem_total": mem.total,
            "temp_c": pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU),
            "power_w": None,
            "power_limit_w": None,
            "fan": None,
        }
        try:
            gpu["power_w"] = round(pynvml.nvmlDeviceGetPowerUsage(h) / 1000, 1)
            gpu["power_limit_w"] = round(pynvml.nvmlDeviceGetEnforcedPowerLimit(h) / 1000)
        except pynvml.NVMLError:
            pass
        try:
            gpu["fan"] = pynvml.nvmlDeviceGetFanSpeed(h)
        except pynvml.NVMLError:
            pass
        out.append(gpu)
    return out


# ---------------------------------------------------------------- network / Wake-on-LAN
def default_iface():
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) > 3 and parts[1] == "00000000" and int(parts[3], 16) & 2:
                    return parts[0]
    except OSError:
        pass
    return None


def iface_mac(iface):
    for a in psutil.net_if_addrs().get(iface or "", []):
        if a.family == psutil.AF_LINK and a.address and a.address != "00:00:00:00:00:00":
            return a.address.lower()
    return None


def ethtool_wol(iface):
    """Returns (supported modes, current mode), e.g. ("pumbg", "g"), or (None, None) without ethtool."""
    if not iface:
        return None, None
    try:
        out = subprocess.run(["ethtool", iface], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return None, None
    sup = cur = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Supports Wake-on:"):
            sup = line.split(":", 1)[1].strip()
        elif line.startswith("Wake-on:"):
            cur = line.split(":", 1)[1].strip()
    return sup, cur


def arm_wol():
    if not WOL:
        return
    iface = default_iface()
    sup, cur = ethtool_wol(iface)
    if sup and "g" in sup and (not cur or "g" not in cur):
        try:
            subprocess.run(["ethtool", "-s", iface, "wol", "g"], capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass


_net = {"at": 0.0, "val": None}


def net_info():
    now = time.time()
    if now - _net["at"] > 60 or _net["val"] is None:
        iface = default_iface()
        sup, cur = ethtool_wol(iface)
        _net["val"] = {
            "iface": iface,
            "mac": iface_mac(iface),
            "wired": bool(iface) and not iface.startswith(("wl", "ww")),
            "ethtool": sup is not None,
            "wol_supported": bool(sup and "g" in sup),
            "wol_armed": bool(cur and "g" in cur),
        }
        _net["at"] = now
    return _net["val"]


try:
    arm_wol()
except Exception:
    pass


# ---------------------------------------------------------------- metrics
psutil.cpu_percent(None)  # prime the counter so the first reading isn't 0


@app.get("/metrics", dependencies=[Depends(require_token)])
def metrics():
    sensors = _sensors()
    vm = psutil.virtual_memory()
    du = psutil.disk_usage(DISK_PATH)
    freq = psutil.cpu_freq()
    return {
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "uptime_s": int(time.time() - psutil.boot_time()),
        "cpu": {
            "percent": psutil.cpu_percent(None),
            "cores": psutil.cpu_count(),
            "freq_mhz": round(freq.current) if freq else None,
            "temp_c": cpu_temp(sensors),
            "load": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
        },
        "ram": {"percent": vm.percent, "used": vm.total - vm.available, "total": vm.total},
        "disk": {
            "percent": du.percent,
            "used": du.used,
            "total": du.total,
            "path": DISK_PATH,
            "temp_c": disk_temp(sensors),
        },
        "gpus": gpus(),
        "docker": DOCKER_MODE,
        "power": POWER,
        "ollama": ollama_available(),
        "net": net_info(),
    }


# ---------------------------------------------------------------- docker
_docker = None


def docker_client():
    global _docker
    if DOCKER_MODE not in ("monitor", "control"):
        raise HTTPException(status_code=404, detail="Docker monitoring is off on this device")
    if _docker is None:
        import docker

        try:
            _docker = docker.from_env()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Can't reach Docker: {e}")
    return _docker


def _get_container(name):
    if ALLOWLIST and name not in ALLOWLIST:
        raise HTTPException(status_code=403, detail=f"{name} isn't in NEXUSLAB_CONTAINERS")
    import docker

    try:
        return docker_client().containers.get(name)
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail=f"No container named {name}")


def _stats(c):
    try:
        s = c.stats(stream=False)
        cpu, pre = s["cpu_stats"], s.get("precpu_stats", {})
        cpu_delta = cpu["cpu_usage"]["total_usage"] - pre.get("cpu_usage", {}).get("total_usage", 0)
        sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        online = cpu.get("online_cpus") or len(cpu["cpu_usage"].get("percpu_usage") or [1])
        cpu_pct = round(cpu_delta / sys_delta * online * 100, 1) if sys_delta > 0 else 0.0
        mem = s.get("memory_stats", {})
        cache = mem.get("stats", {}).get("inactive_file", mem.get("stats", {}).get("cache", 0))
        return cpu_pct, max(mem.get("usage", 0) - cache, 0), mem.get("limit")
    except Exception:
        return None, None, None


_pool = ThreadPoolExecutor(max_workers=8)


@app.get("/containers", dependencies=[Depends(require_token)])
def containers():
    items = docker_client().containers.list(all=True)
    if ALLOWLIST:
        items = [c for c in items if c.name in ALLOWLIST]
    running = [c for c in items if c.status == "running"]
    stats = dict(zip([c.id for c in running], _pool.map(_stats, running)))
    out = []
    for c in sorted(items, key=lambda c: c.name):
        state = c.attrs.get("State", {})
        cpu, mem, limit = stats.get(c.id, (None, None, None))
        out.append({
            "name": c.name,
            "image": c.attrs.get("Config", {}).get("Image", ""),
            "status": c.status,
            "health": (state.get("Health") or {}).get("Status"),
            "started_at": state.get("StartedAt"),
            "cpu_percent": cpu,
            "mem_used": mem,
            "mem_limit": limit,
        })
    return {"mode": DOCKER_MODE, "items": out}


@app.post("/containers/{name}/{action}", dependencies=[Depends(require_token)])
def container_action(name: str, action: str):
    if DOCKER_MODE != "control":
        raise HTTPException(status_code=403, detail="This device is monitor-only (NEXUSLAB_DOCKER=monitor)")
    if action not in ("start", "stop", "restart"):
        raise HTTPException(status_code=400, detail="Action must be start, stop or restart")
    c = _get_container(name)
    getattr(c, action)(**({"timeout": 15} if action != "start" else {}))
    c.reload()
    return {"name": name, "action": action, "status": c.status}


@app.get("/containers/{name}/logs", dependencies=[Depends(require_token)])
def container_logs(name: str, tail: int = Query(200, ge=10, le=5000)):
    c = _get_container(name)
    return PlainTextResponse(c.logs(tail=tail, timestamps=True).decode("utf-8", errors="replace"))


# ---------------------------------------------------------------- power
@app.post("/power/{action}", dependencies=[Depends(require_token)])
def power(action: str):
    if not POWER:
        raise HTTPException(status_code=403, detail="Power control is off on this device (NEXUSLAB_POWER=off)")
    if action not in ("reboot", "shutdown"):
        raise HTTPException(status_code=400, detail="Action must be reboot or shutdown")
    if action == "shutdown":
        arm_wol()  # make sure the card listens for Power on while the machine is off
    cmd = ["systemctl", "reboot" if action == "reboot" else "poweroff"]
    # small delay so this answer reaches the hub before the machine goes down
    threading.Timer(2.0, lambda: subprocess.Popen(cmd)).start()
    return {"action": action, "in_seconds": 2}


# ---------------------------------------------------------------- Ollama
_ollama_seen = {"at": 0.0, "ok": False}


def _ollama(path, body=None, timeout=5):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(OLLAMA_URL + path, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw.strip() else {}


def ollama_available():
    if not OLLAMA_URL or OLLAMA_URL.lower() == "off":
        return False
    now = time.time()
    if now - _ollama_seen["at"] > 60:
        try:
            _ollama("/api/version", timeout=0.5)
            ok = True
        except Exception:
            ok = False
        _ollama_seen.update(at=now, ok=ok)
    return _ollama_seen["ok"]


def _details(m):
    d = m.get("details") or {}
    return {"params": d.get("parameter_size"), "quant": d.get("quantization_level"), "family": d.get("family")}


@app.get("/ollama", dependencies=[Depends(require_token)])
def ollama_info():
    if not ollama_available():
        raise HTTPException(status_code=404, detail=f"No Ollama answering at {OLLAMA_URL}")
    try:
        version = _ollama("/api/version").get("version")
        tags = _ollama("/api/tags").get("models", [])
        ps = _ollama("/api/ps").get("models", [])
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Ollama didn't answer: {e}")
    installed = sorted(({"name": m.get("name"), "size": m.get("size"), **_details(m)} for m in tags), key=lambda m: m["name"] or "")
    loaded = [{"name": m.get("name"), "size": m.get("size"), "vram": m.get("size_vram"),
               "expires_at": m.get("expires_at"), **_details(m)} for m in ps]
    return {"version": version, "installed": installed, "loaded": loaded}


class ChatBody(BaseModel):
    model: str
    messages: list
    options: Optional[dict] = None


@app.post("/ollama/chat", dependencies=[Depends(require_token)])
def ollama_chat(body: ChatBody):
    """Stream a chat reply from Ollama (NDJSON lines, passed straight through)."""
    if not ollama_available():
        raise HTTPException(status_code=404, detail=f"No Ollama answering at {OLLAMA_URL}")
    payload = {"model": body.model, "messages": body.messages, "stream": True}
    if body.options:
        payload["options"] = body.options
    req = urllib.request.Request(OLLAMA_URL + "/api/chat", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=300)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise HTTPException(status_code=502, detail=f"Ollama refused the chat: {detail}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ollama didn't answer: {e}")

    def lines():
        with resp:
            for line in resp:
                yield line

    return StreamingResponse(lines(), media_type="application/x-ndjson")


class ModelBody(BaseModel):
    model: str


@app.post("/ollama/{action}", dependencies=[Depends(require_token)])
def ollama_action(action: str, body: ModelBody):
    if action not in ("load", "unload"):
        raise HTTPException(status_code=400, detail="Action must be load or unload")
    payload = {"model": body.model}
    if action == "unload":
        payload["keep_alive"] = 0
    try:
        _ollama("/api/generate", {**payload, "stream": False}, timeout=180)
    except urllib.error.HTTPError as e:
        # embedding-only models can't "generate"; use the embed endpoint instead
        try:
            _ollama("/api/embed", {**payload, "input": "ok"}, timeout=180)
        except Exception:
            raise HTTPException(status_code=502, detail=f"Ollama refused to {action} {body.model}: HTTP {e.code}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ollama didn't answer: {e}")
    return {"model": body.model, "action": action}


@app.get("/health")
def health():
    return {"ok": True}
