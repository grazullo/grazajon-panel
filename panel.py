#!/usr/bin/env python3
"""
Grazajon Host Panel — Termux / Android uchun yengil Python boshqaruv paneli.
Tashqi kutubxonalarsiz (faqat standart Python 3) ishlaydi.
"""

import argparse
import hashlib
import hmac
import json
import os
import secrets
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import zipfile
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("PANEL_DATA", Path.home() / "grazajon_data")).resolve()
APPS_DIR = DATA_DIR / "apps"
CONFIG_FILE = DATA_DIR / "config.json"
INDEX_FILE = BASE_DIR / "index.html"

MAX_LOG_BYTES = 2 * 1024 * 1024
TRIM_LOG_BYTES = 512 * 1024
MAX_ZIP_EXTRACT_BYTES = 128 * 1024 * 1024
SESSION_TTL = 7 * 24 * 3600

STATE_LOCK = threading.RLock()
LOG_LOCK = threading.Lock()
PANEL_PORT = 5000


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    APPS_DIR.mkdir(parents=True, exist_ok=True)


def load_config():
    ensure_dirs()
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = {}
    changed = False
    if "secret" not in cfg:
        cfg["secret"] = secrets.token_hex(32)
        changed = True
    if "projects" not in cfg or not isinstance(cfg["projects"], dict):
        cfg["projects"] = {}
        changed = True
    if "port" not in cfg:
        cfg["port"] = int(os.environ.get("PANEL_PORT", "5000"))
        changed = True
    if changed:
        save_config(cfg)
    return cfg


def save_config(cfg):
    ensure_dirs()
    tmp = CONFIG_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_FILE)


def hash_password(password: str, salt: str | None = None):
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return dk.hex(), salt


def verify_password(password: str, cfg: dict) -> bool:
    stored = cfg.get("password_hash")
    salt = cfg.get("salt")
    if not stored or not salt:
        return False
    dk, _ = hash_password(password, salt)
    return hmac.compare_digest(dk, stored)


def make_session_token(secret: str) -> str:
    exp = int(time.time()) + SESSION_TTL
    nonce = secrets.token_hex(8)
    payload = f"{exp}.{nonce}"
    sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_session_token(token: str, secret: str) -> bool:
    if not token or not secret:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    exp_s, nonce, sig = parts
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < time.time():
        return False
    expected = hmac.new(secret.encode("utf-8"), f"{exp_s}.{nonce}".encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def slugify(name: str) -> str:
    cleaned = "".join(c.lower() if c.isalnum() else "-" for c in name.strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or f"app-{int(time.time())}"


def safe_join(root: Path, rel_path: str) -> Path:
    rel = (rel_path or "").lstrip("/\\")
    target = (root / rel).resolve()
    root_res = root.resolve()
    if target != root_res and root_res not in target.parents:
        raise ValueError("Ruxsat etilmagan yo'l (path traversal)")
    return target


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_battery_stats() -> dict:
    for p in ["/sys/class/power_supply/battery", "/sys/class/power_supply/bms", "/sys/class/power_supply/BAT0"]:
        cap_file = os.path.join(p, "capacity")
        if os.path.exists(cap_file):
            try:
                cap = int(open(cap_file).read().strip())
                status = "Discharging"
                st_file = os.path.join(p, "status")
                if os.path.exists(st_file):
                    status = open(st_file).read().strip()
                temp = None
                t_file = os.path.join(p, "temp")
                if os.path.exists(t_file):
                    raw_t = int(open(t_file).read().strip())
                    temp = round(raw_t / 10 if raw_t < 1000 else raw_t / 1000, 1)
                return {
                    "percentage": cap,
                    "status": status,
                    "temperature_c": temp,
                    "plugged": status.lower() in ("charging", "full"),
                }
            except Exception:
                pass

    try:
        res = subprocess.run(["termux-battery-status"], capture_output=True, text=True, timeout=0.8)
        if res.returncode == 0:
            d = json.loads(res.stdout)
            return {
                "percentage": int(d.get("percentage", 100)),
                "status": str(d.get("status", "Unknown")),
                "temperature_c": d.get("temperature"),
                "plugged": d.get("plugged", "UNPLUGGED") != "UNPLUGGED",
            }
    except Exception:
        pass

    return {
        "percentage": None,
        "status": "Tarmoqda (Noma'lum)",
        "temperature_c": None,
        "plugged": True,
    }


def get_process_memory_mb(pid: int | None) -> float | None:
    if not pid:
        return None
    try:
        with open(f"/proc/{pid}/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        return round(int(parts[1]) / 1024, 1)
    except Exception:
        pass
    return None


def get_system_stats():
    disk = shutil.disk_usage(DATA_DIR)
    mem_total_mb = None
    mem_avail_mb = None
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            info = {}
            for line in f:
                k, _, v = line.partition(":")
                nums = [int(x) for x in v.split() if x.isdigit()]
                if nums:
                    info[k.strip()] = nums[0]
            if "MemTotal" in info:
                mem_total_mb = round(info["MemTotal"] / 1024)
            if "MemAvailable" in info:
                mem_avail_mb = round(info["MemAvailable"] / 1024)
            elif "MemFree" in info:
                mem_avail_mb = round((info.get("MemFree", 0) + info.get("Buffers", 0) + info.get("Cached", 0)) / 1024)
    except Exception:
        pass

    mem_used_mb = (mem_total_mb - mem_avail_mb) if (mem_total_mb and mem_avail_mb) else None
    mem_percent = round((mem_used_mb / mem_total_mb) * 100) if (mem_total_mb and mem_used_mb) else None

    load_1m = 0.0
    try:
        with open("/proc/loadavg", "r", encoding="utf-8") as f:
            load_1m = round(float(f.read().split()[0]), 2)
    except Exception:
        pass

    device_uptime_sec = 0
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as f:
            device_uptime_sec = int(float(f.read().split()[0]))
    except Exception:
        pass

    return {
        "python_version": sys.version.split()[0],
        "disk_free_mb": round(disk.free / (1024 * 1024)),
        "disk_total_mb": round(disk.total / (1024 * 1024)),
        "mem_total_mb": mem_total_mb,
        "mem_avail_mb": mem_avail_mb,
        "mem_used_mb": mem_used_mb,
        "mem_percent": mem_percent,
        "cpu_load_1m": load_1m,
        "device_uptime_sec": device_uptime_sec,
        "battery": get_battery_stats(),
        "local_ip": get_local_ip(),
    }


def append_log(log_path: Path, text: str):
    with LOG_LOCK:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if log_path.exists() and log_path.stat().st_size > MAX_LOG_BYTES:
            with open(log_path, "rb") as f:
                f.seek(-TRIM_LOG_BYTES, os.SEEK_END)
                tail = f.read()
            if b"\n" in tail:
                tail = tail.split(b"\n", 1)[1]
            with open(log_path, "wb") as f:
                f.write(b"... [Log qisqartirildi] ...\n" + tail)
        with open(log_path, "a", encoding="utf-8", errors="replace") as f:
            f.write(text)


class ManagedApp:
    def __init__(self, app_id: str):
        self.app_id = app_id
        self.proc: subprocess.Popen | None = None
        self.desired_running = False
        self.status = "stopped"  # stopped | running | restarting
        self.started_at: float | None = None
        self.restarts = 0
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()


class Supervisor:
    def __init__(self):
        self.apps: dict[str, ManagedApp] = {}
        self.lock = threading.Lock()
        self.panel_started_at = time.time()

    def get_managed(self, app_id: str) -> ManagedApp:
        with self.lock:
            if app_id not in self.apps:
                self.apps[app_id] = ManagedApp(app_id)
            return self.apps[app_id]

    def get_python_bin(self, app_dir: Path) -> str:
        for candidate in [app_dir / ".venv" / "bin" / "python", app_dir / "venv" / "bin" / "python"]:
            if candidate.exists():
                return str(candidate)
        return sys.executable

    def build_command(self, proj: dict, app_dir: Path) -> list[str]:
        entry = (proj.get("entry") or "bot.py").strip()
        ptype = proj.get("type", "bot")
        port = str(proj.get("port") or "8080")
        py = self.get_python_bin(app_dir)

        if ptype == "static" or entry.endswith(".html"):
            return [py, "-m", "http.server", port, "--bind", "0.0.0.0"]
        if (app_dir / entry).exists():
            if entry.endswith(".py"):
                return [py, "-u", entry]
            return [str(app_dir / entry)]
        try:
            parts = shlex.split(entry)
        except ValueError:
            parts = entry.split()
        if not parts:
            return [py, "-u", "bot.py"]
        if parts[0] in ("python", "python3"):
            return [py, "-u"] + parts[1:]
        if parts[0].endswith(".py"):
            return [py, "-u"] + parts
        return parts

    def start(self, app_id: str):
        with STATE_LOCK:
            cfg = load_config()
            if app_id not in cfg["projects"]:
                raise KeyError("Loyiha topilmadi")
        m = self.get_managed(app_id)
        with m.lock:
            if m.desired_running and m.thread and m.thread.is_alive():
                return
            m.desired_running = True
            m.stop_event.clear()
            m.restarts = 0
            m.status = "running"
            m.thread = threading.Thread(target=self._run_loop, args=(app_id, m), daemon=True)
            m.thread.start()

    def stop(self, app_id: str):
        m = self.get_managed(app_id)
        with m.lock:
            m.desired_running = False
            m.stop_event.set()
            proc = m.proc
        if proc and proc.poll() is None:
            self._kill_proc(proc)
        if m.thread and m.thread.is_alive():
            m.thread.join(timeout=4)
        with m.lock:
            m.status = "stopped"
            m.started_at = None
            m.proc = None

    def restart(self, app_id: str):
        self.stop(app_id)
        self.start(app_id)

    def _kill_proc(self, proc: subprocess.Popen):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=3)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass

    def _run_loop(self, app_id: str, m: ManagedApp):
        app_dir = APPS_DIR / app_id
        log_path = app_dir / ".panel.log"

        while not m.stop_event.is_set():
            with STATE_LOCK:
                cfg = load_config()
                proj = cfg["projects"].get(app_id)
            if not proj:
                break

            cmd = self.build_command(proj, app_dir)
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            venv_bin = app_dir / ".venv" / "bin"
            if venv_bin.exists():
                env["PATH"] = f"{venv_bin}:{env.get('PATH', '')}"
            if proj.get("port"):
                env["PORT"] = str(proj["port"])
            for k, v in (proj.get("env") or {}).items():
                if k:
                    env[str(k)] = str(v)

            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            append_log(log_path, f"\n[{ts}] [PANEL] Ishga tushmoqda: {' '.join(cmd)}\n")

            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(app_dir),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
                    bufsize=1,
                    start_new_session=True,
                )
            except Exception as e:
                append_log(log_path, f"[{time.strftime('%H:%M:%S')}] [XATO] Ishga tushirib bo'lmadi: {e}\n")
                with m.lock:
                    m.status = "stopped"
                    m.desired_running = False
                break

            with m.lock:
                m.proc = proc
                m.status = "running"
                m.started_at = time.time()

            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    append_log(log_path, line)
            except Exception:
                pass

            code = proc.wait()
            with m.lock:
                m.proc = None
                m.started_at = None

            if m.stop_event.is_set() or not m.desired_running:
                append_log(log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [PANEL] To'xtatildi.\n")
                with m.lock:
                    m.status = "stopped"
                break

            append_log(log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [PANEL] Jarayon tugadi (kod: {code}).\n")
            if not proj.get("auto_restart", True):
                with m.lock:
                    m.status = "stopped"
                    m.desired_running = False
                break

            with m.lock:
                m.status = "restarting"
                m.restarts += 1
            append_log(log_path, f"[{time.strftime('%H:%M:%S')}] [PANEL] 5 soniyadan keyin qayta ishga tushadi (#{m.restarts})...\n")
            if m.stop_event.wait(5):
                with m.lock:
                    m.status = "stopped"
                break

    def status_dict(self, app_id: str) -> dict:
        m = self.get_managed(app_id)
        with m.lock:
            pid = m.proc.pid if (m.proc and m.proc.poll() is None) else None
            uptime = int(time.time() - m.started_at) if (m.started_at and m.status == "running") else 0
            ram_mb = get_process_memory_mb(pid)
            return {
                "status": m.status,
                "pid": pid,
                "uptime_sec": uptime,
                "restarts": m.restarts,
                "ram_mb": ram_mb,
            }

    def start_autostarts(self):
        cfg = load_config()
        for app_id, proj in cfg["projects"].items():
            if proj.get("autostart"):
                try:
                    self.start(app_id)
                except Exception:
                    pass

    def stop_all(self):
        for app_id in list(self.apps.keys()):
            self.stop(app_id)


SUPERVISOR = Supervisor()

TEMPLATES = {
    "bot": {
        "entry": "bot.py",
        "files": {
            "bot.py": '''import json
import os
import time
import urllib.parse
import urllib.request

TOKEN = os.environ.get("BOT_TOKEN", "BU_YERGA_BOT_TOKEN_YOZING")
API = f"https://api.telegram.org/bot{TOKEN}"


def call(method, params=None):
    url = f"{API}/{method}"
    if params:
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=data)
    else:
        req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=35) as r:
        return json.loads(r.read().decode())


if __name__ == "__main__":
    print("Bot ishga tushmoqda...")
    if "BU_YERGA" in TOKEN:
        print("DIQQAT: Sozlamalar -> Env bo'limiga BOT_TOKEN kiriting!")
        while True:
            time.sleep(30)

    offset = 0
    while True:
        try:
            res = call("getUpdates", {"offset": offset, "timeout": 25})
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat_id = (msg.get("chat") or {}).get("id")
                text = msg.get("text", "")
                if chat_id and text:
                    reply = f"Salom! Telefondan javob: {text}"
                    call("sendMessage", {"chat_id": chat_id, "text": reply})
                    print(f"Xabar keldi [{chat_id}]: {text}")
        except Exception as e:
            print("Tarmoq xatosi (5s kutish):", e)
            time.sleep(5)
''',
            "requirements.txt": "# Kerakli kutubxonalarni shu yerga yozing, masalan:\n# pyTelegramBotAPI\n# python-telegram-bot\n",
        },
    },
    "web_py": {
        "entry": "server.py",
        "files": {
            "server.py": '''import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8080"))


class Handler(SimpleHTTPRequestHandler):
    pass


if __name__ == "__main__":
    print(f"Python sayt {PORT}-portda ishga tushdi!")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
''',
            "index.html": '''<!doctype html>
<html lang="uz">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Mening Saytim</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 720px; margin: 60px auto; padding: 0 20px; color: #0f172a; }
    .card { border: 1px solid #e2e8f0; border-radius: 12px; padding: 24px; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Salom! Bu sayt telefondan ishlayapti</h1>
    <p>Fayllar bo'limidan <code>index.html</code> yoki <code>server.py</code> ni tahrirlashingiz mumkin.</p>
  </div>
</body>
</html>
''',
            "requirements.txt": "",
        },
    },
    "static": {
        "entry": "index.html",
        "files": {
            "index.html": '''<!doctype html>
<html lang="uz">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Statik Sayt</title>
</head>
<body style="font-family:system-ui;padding:40px;">
  <h1>Sayt muvaffaqiyatli ishga tushdi!</h1>
</body>
</html>
''',
        },
    },
    "empty": {
        "entry": "main.py",
        "files": {
            "main.py": 'print("Salom, Termux!")\n',
        },
    },
}


def validate_port(raw_port) -> str:
    s = str(raw_port or "").strip()
    if not s:
        return ""
    if not s.isdigit() or not (1 <= int(s) <= 65535):
        raise ValueError("Port 1 dan 65535 gacha bo'lgan son bo'lishi kerak")
    return s


def safe_app_file(app_dir: Path, rel_path: str) -> Path:
    target = safe_join(app_dir, rel_path)
    if target == (app_dir / ".panel.log").resolve():
        raise ValueError("Bu tizim log faylini tahrirlab bo'lmaydi")
    return target


class PanelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def _write_response(self, status: int, content_type: str, body: bytes, extra_headers: list[tuple[str, str]] | None = None):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if extra_headers:
                for k, v in extra_headers:
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, data: dict | list, status: int = 200, extra_headers: list[tuple[str, str]] | None = None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._write_response(status, "application/json; charset=utf-8", body, extra_headers)

    def _send_html(self, content: bytes, status: int = 200):
        self._write_response(status, "text/html; charset=utf-8", content)

    def _read_body(self, max_bytes: int = 64 * 1024 * 1024) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            raise ValueError("Noto'g'ri Content-Length")
        if length < 0:
            raise ValueError("Noto'g'ri Content-Length")
        if length > max_bytes:
            raise ValueError("Fayl hajmi juda katta (maksimum 64 MB)")
        if length == 0:
            return b""
        data = self.rfile.read(length)
        return data

    def _read_json(self) -> dict:
        raw = self._read_body(4 * 1024 * 1024)
        if not raw:
            return {}
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("JSON obyekt bo'lishi kerak")
        return parsed

    def _is_authenticated(self, cfg: dict) -> bool:
        cookie_hdr = self.headers.get("Cookie", "")
        if not cookie_hdr:
            return False
        c = cookies.SimpleCookie()
        try:
            c.load(cookie_hdr)
        except Exception:
            return False
        morsel = c.get("grazajon_sid")
        if not morsel:
            return False
        return verify_session_token(morsel.value, cfg["secret"])

    def _require_auth(self) -> dict | None:
        with STATE_LOCK:
            cfg = load_config()
        if not cfg.get("password_hash") or not self._is_authenticated(cfg):
            self._send_json({"error": "Avtorizatsiya talab qilinadi"}, status=401)
            return None
        return cfg

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            if INDEX_FILE.exists():
                self._send_html(INDEX_FILE.read_bytes())
            else:
                self._send_html(b"<h1>index.html topilmadi</h1>", status=404)
            return

        if path == "/api/auth/status":
            with STATE_LOCK:
                cfg = load_config()
            needs_setup = not bool(cfg.get("password_hash"))
            auth = False if needs_setup else self._is_authenticated(cfg)
            self._send_json({"authenticated": auth, "needs_setup": needs_setup})
            return

        cfg = self._require_auth()
        if cfg is None:
            return

        if path == "/api/system":
            stats = get_system_stats()
            projects = cfg["projects"]
            running = sum(1 for pid in projects if SUPERVISOR.status_dict(pid)["status"] == "running")
            stats["running_count"] = running
            stats["total_count"] = len(projects)
            stats["panel_uptime_sec"] = int(time.time() - SUPERVISOR.panel_started_at)
            stats["panel_port"] = PANEL_PORT or int(cfg.get("port", 5000))
            self._send_json(stats)
            return

        if path == "/api/projects":
            items = []
            for app_id, proj in cfg["projects"].items():
                st = SUPERVISOR.status_dict(app_id)
                items.append({**proj, **st, "id": app_id})
            self._send_json({"projects": items})
            return

        parts = [p for p in path.split("/") if p]
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "projects":
            app_id = parts[2]
            if app_id not in cfg["projects"]:
                self._send_json({"error": "Loyiha topilmadi"}, status=404)
                return
            app_dir = APPS_DIR / app_id

            if len(parts) == 4 and parts[3] == "logs":
                try:
                    lines_n = max(1, min(int(qs.get("lines", ["200"])[0]), 2000))
                except ValueError:
                    lines_n = 200
                log_path = app_dir / ".panel.log"
                if not log_path.exists():
                    self._send_json({"logs": ""})
                    return
                with LOG_LOCK:
                    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                        all_lines = f.readlines()
                self._send_json({"logs": "".join(all_lines[-lines_n:])})
                return

            if len(parts) == 4 and parts[3] == "files":
                rel = qs.get("path", [""])[0]
                try:
                    target = safe_join(app_dir, rel)
                except ValueError as e:
                    self._send_json({"error": str(e)}, status=400)
                    return
                if not target.exists() or not target.is_dir():
                    self._send_json({"error": "Papka topilmadi"}, status=404)
                    return
                entries = []
                for entry in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                    if entry.name == ".panel.log":
                        continue
                    rel_item = str(entry.relative_to(app_dir)).replace("\\", "/")
                    entries.append({
                        "name": entry.name,
                        "path": rel_item,
                        "is_dir": entry.is_dir(),
                        "size": 0 if entry.is_dir() else entry.stat().st_size,
                    })
                self._send_json({"path": rel.strip("/"), "entries": entries})
                return

            if len(parts) == 4 and parts[3] == "file":
                rel = qs.get("path", [""])[0]
                try:
                    target = safe_app_file(app_dir, rel)
                except ValueError as e:
                    self._send_json({"error": str(e)}, status=400)
                    return
                if not target.exists() or target.is_dir():
                    self._send_json({"error": "Fayl topilmadi"}, status=404)
                    return
                if target.stat().st_size > 2 * 1024 * 1024:
                    self._send_json({"error": "Fayl 2 MB dan katta — tahrirlash cheklangan"}, status=400)
                    return
                try:
                    text = target.read_text(encoding="utf-8")
                    self._send_json({"path": rel, "content": text})
                except UnicodeDecodeError:
                    self._send_json({"error": "Bu matnli fayl emas (binary)"}, status=400)
                return

        self._send_json({"error": "Topilmadi"}, status=404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/api/auth/setup":
            try:
                data = self._read_json()
            except Exception:
                self._send_json({"error": "Noto'g'ri JSON"}, status=400)
                return
            pw = (data.get("password") or "").strip()
            if len(pw) < 4:
                self._send_json({"error": "Parol kamida 4 ta belgidan iborat bo'lsin"}, status=400)
                return
            with STATE_LOCK:
                cfg = load_config()
                if cfg.get("password_hash"):
                    self._send_json({"error": "Parol allaqachon o'rnatilgan"}, status=403)
                    return
                h, salt = hash_password(pw)
                cfg["password_hash"] = h
                cfg["salt"] = salt
                save_config(cfg)
                token = make_session_token(cfg["secret"])
            self._send_json({"ok": True}, extra_headers=[("Set-Cookie", f"grazajon_sid={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}")])
            return

        if path == "/api/auth/login":
            try:
                data = self._read_json()
            except Exception:
                self._send_json({"error": "Noto'g'ri JSON"}, status=400)
                return
            pw = data.get("password") or ""
            with STATE_LOCK:
                cfg = load_config()
                if not verify_password(pw, cfg):
                    self._send_json({"error": "Parol noto'g'ri"}, status=401)
                    return
                token = make_session_token(cfg["secret"])
            self._send_json({"ok": True}, extra_headers=[("Set-Cookie", f"grazajon_sid={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}")])
            return

        if path == "/api/auth/logout":
            self._send_json({"ok": True}, extra_headers=[("Set-Cookie", "grazajon_sid=; Path=/; HttpOnly; Max-Age=0")])
            return

        cfg = self._require_auth()
        if cfg is None:
            return

        if path == "/api/system/clean_ram":
            import gc
            collected = gc.collect()
            try:
                if hasattr(os, "sync"):
                    os.sync()
            except Exception:
                pass
            stats = get_system_stats()
            self._send_json({"ok": True, "message": "Operativ xotira (RAM) tozalandi!", "stats": stats, "collected_objects": collected})
            return

        if path == "/api/projects":
            try:
                data = self._read_json()
            except Exception:
                self._send_json({"error": "Noto'g'ri JSON"}, status=400)
                return
            name = (data.get("name") or "").strip()
            if not name:
                self._send_json({"error": "Loyiha nomini kiriting"}, status=400)
                return
            tpl_key = data.get("template") or "bot"
            if tpl_key not in TEMPLATES:
                tpl_key = "empty"
            tpl = TEMPLATES[tpl_key]

            req_type = data.get("type", "bot")
            if req_type not in ("bot", "web", "static"):
                req_type = "bot"
            ptype = "bot" if tpl_key == "bot" else ("static" if tpl_key == "static" else ("web" if tpl_key == "web_py" else req_type))

            try:
                raw_port = data.get("port")
                if raw_port is None or str(raw_port).strip() == "":
                    port = "8080" if ptype in ("web", "static") else ""
                else:
                    port = validate_port(raw_port)
            except ValueError as e:
                self._send_json({"error": str(e)}, status=400)
                return

            with STATE_LOCK:
                cfg = load_config()
                base_id = slugify(name)
                app_id = base_id
                i = 2
                while app_id in cfg["projects"]:
                    app_id = f"{base_id}-{i}"
                    i += 1

                app_dir = APPS_DIR / app_id
                app_dir.mkdir(parents=True, exist_ok=True)
                for fname, content in tpl["files"].items():
                    fpath = safe_join(app_dir, fname)
                    fpath.parent.mkdir(parents=True, exist_ok=True)
                    fpath.write_text(content, encoding="utf-8")

                proj = {
                    "name": name,
                    "type": ptype,
                    "entry": (data.get("entry") or tpl["entry"]).strip() or tpl["entry"],
                    "port": port,
                    "auto_restart": bool(data.get("auto_restart", True)),
                    "autostart": bool(data.get("autostart", True)),
                    "env": {str(k).strip(): str(v) for k, v in data.get("env", {}).items() if str(k).strip()} if isinstance(data.get("env"), dict) else {},
                }
                cfg["projects"][app_id] = proj
                save_config(cfg)

            self._send_json({"project": {**proj, "id": app_id, **SUPERVISOR.status_dict(app_id)}}, status=201)
            return

        parts = [p for p in path.split("/") if p]
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "projects":
            app_id = parts[2]
            if app_id not in cfg["projects"]:
                self._send_json({"error": "Loyiha topilmadi"}, status=404)
                return
            app_dir = APPS_DIR / app_id

            if parts[3] == "action":
                try:
                    data = self._read_json()
                except Exception:
                    self._send_json({"error": "Noto'g'ri JSON"}, status=400)
                    return
                act = data.get("action")
                if act == "start":
                    SUPERVISOR.start(app_id)
                elif act == "stop":
                    SUPERVISOR.stop(app_id)
                elif act == "restart":
                    SUPERVISOR.restart(app_id)
                elif act in ("pip_install", "pip_pkg"):
                    pkg_arg = (data.get("package") or "").strip()
                    if act == "pip_install":
                        req_file = app_dir / "requirements.txt"
                        if not req_file.exists():
                            self._send_json({"error": "requirements.txt topilmadi"}, status=400)
                            return
                        cmd = [SUPERVISOR.get_python_bin(app_dir), "-m", "pip", "install", "-r", "requirements.txt"]
                    else:
                        if not pkg_arg or any(ch in pkg_arg for ch in ";&|`$><\n\r"):
                            self._send_json({"error": "Paket nomi noto'g'ri"}, status=400)
                            return
                        try:
                            pkg_parts = shlex.split(pkg_arg)
                        except ValueError:
                            self._send_json({"error": "Paket nomi noto'g'ri"}, status=400)
                            return
                        if not pkg_parts:
                            self._send_json({"error": "Paket nomi noto'g'ri"}, status=400)
                            return
                        cmd = [SUPERVISOR.get_python_bin(app_dir), "-m", "pip", "install"] + pkg_parts

                    def _run_pip():
                        log_path = app_dir / ".panel.log"
                        pip_env = os.environ.copy()
                        pip_env["PYTHONUNBUFFERED"] = "1"
                        append_log(log_path, f"\n[{time.strftime('%H:%M:%S')}] [PIP] {' '.join(cmd)}\n")
                        try:
                            p = subprocess.Popen(cmd, cwd=str(app_dir), env=pip_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
                            assert p.stdout is not None
                            for line in p.stdout:
                                append_log(log_path, line)
                            code = p.wait()
                            append_log(log_path, f"[{time.strftime('%H:%M:%S')}] [PIP] Tugadi (kod: {code})\n")
                        except Exception as e:
                            append_log(log_path, f"[PIP XATO] {e}\n")

                    threading.Thread(target=_run_pip, daemon=True).start()
                    self._send_json({"ok": True, "message": "Kutubxona o'rnatish boshlandi, loglarni kuzating"})
                    return
                else:
                    self._send_json({"error": "Noma'lum amal"}, status=400)
                    return
                self._send_json({"ok": True, **SUPERVISOR.status_dict(app_id)})
                return

            if parts[3] == "upload":
                rel_dir = qs.get("dir", [""])[0]
                raw_name = qs.get("name", [""])[0]
                extract = qs.get("extract", ["0"])[0] == "1"
                if not raw_name:
                    self._send_json({"error": "Fayl nomi berilmagan"}, status=400)
                    return
                safe_name = Path(raw_name.replace("\\", "/")).name
                if not safe_name or safe_name in (".", "..", ".panel.log"):
                    self._send_json({"error": "Fayl nomi yaroqsiz"}, status=400)
                    return
                try:
                    dest_dir = safe_join(app_dir, rel_dir)
                    dest_file = safe_app_file(dest_dir, safe_name)
                    if dest_file == dest_dir:
                        raise ValueError("Fayl nomi yaroqsiz")
                    raw = self._read_body()
                except ValueError as e:
                    self._send_json({"error": str(e)}, status=400)
                    return

                dest_dir.mkdir(parents=True, exist_ok=True)
                dest_file.write_bytes(raw)
                extracted = 0
                if extract and safe_name.lower().endswith(".zip"):
                    try:
                        total_extracted = 0
                        with zipfile.ZipFile(dest_file, "r") as zf:
                            for member in zf.infolist():
                                clean_member = member.filename.replace("\\", "/").lstrip("/")
                                if not clean_member or clean_member in (".", "./"):
                                    continue
                                if stat.S_ISLNK(member.external_attr >> 16):
                                    continue
                                target = safe_join(dest_dir, clean_member)
                                if target == (app_dir / ".panel.log").resolve():
                                    raise ValueError(".panel.log faylini almashtirib bo'lmaydi")
                                if member.is_dir():
                                    target.mkdir(parents=True, exist_ok=True)
                                elif target != dest_dir:
                                    total_extracted += member.file_size
                                    if total_extracted > MAX_ZIP_EXTRACT_BYTES:
                                        raise ValueError("ZIP arxiv ichidagi hajm juda katta (maksimum 128 MB)")
                                    target.parent.mkdir(parents=True, exist_ok=True)
                                    with zf.open(member, "r") as src, open(target, "wb") as dst:
                                        shutil.copyfileobj(src, dst)
                                    extracted += 1
                    except Exception as e:
                        self._send_json({"error": f"ZIP ochishda xato: {e}"}, status=400)
                        return
                self._send_json({"ok": True, "file": str(dest_file.relative_to(app_dir)).replace("\\", "/"), "extracted": extracted})
                return

        self._send_json({"error": "Topilmadi"}, status=404)

    def do_PUT(self):
        cfg = self._require_auth()
        if cfg is None:
            return
        path = urllib.parse.urlparse(self.path).path
        parts = [p for p in path.split("/") if p]
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "projects" and parts[3] == "file":
            app_id = parts[2]
            if app_id not in cfg["projects"]:
                self._send_json({"error": "Loyiha topilmadi"}, status=404)
                return
            try:
                data = self._read_json()
            except Exception:
                self._send_json({"error": "Noto'g'ri JSON"}, status=400)
                return
            rel = (data.get("path") or "").strip()
            if not rel:
                self._send_json({"error": "Fayl yo'li kiritilmadi"}, status=400)
                return
            app_dir = APPS_DIR / app_id
            try:
                target = safe_app_file(app_dir, rel)
            except ValueError as e:
                self._send_json({"error": str(e)}, status=400)
                return
            if target == app_dir.resolve():
                self._send_json({"error": "Fayl nomi kiritilmadi"}, status=400)
                return
            if data.get("is_dir"):
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(data.get("content") or ""), encoding="utf-8")
            self._send_json({"ok": True})
            return
        self._send_json({"error": "Topilmadi"}, status=404)

    def do_PATCH(self):
        cfg = self._require_auth()
        if cfg is None:
            return
        path = urllib.parse.urlparse(self.path).path
        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "projects":
            app_id = parts[2]
            try:
                data = self._read_json()
            except Exception:
                self._send_json({"error": "Noto'g'ri JSON"}, status=400)
                return
            with STATE_LOCK:
                cfg = load_config()
                if app_id not in cfg["projects"]:
                    self._send_json({"error": "Loyiha topilmadi"}, status=404)
                    return
                proj = cfg["projects"][app_id]
                if "name" in data:
                    nm = str(data["name"]).strip()
                    if not nm:
                        self._send_json({"error": "Loyiha nomi bo'sh bo'lmasin"}, status=400)
                        return
                    proj["name"] = nm
                if "type" in data:
                    pt = str(data["type"]).strip()
                    if pt not in ("bot", "web", "static"):
                        self._send_json({"error": "Loyiha turi noto'g'ri"}, status=400)
                        return
                    proj["type"] = pt
                if "entry" in data:
                    ent = str(data["entry"]).strip()
                    if not ent:
                        self._send_json({"error": "Entry fayl kiritilmadi"}, status=400)
                        return
                    proj["entry"] = ent
                if "port" in data:
                    try:
                        proj["port"] = validate_port(data["port"])
                    except ValueError as e:
                        self._send_json({"error": str(e)}, status=400)
                        return
                if "auto_restart" in data:
                    proj["auto_restart"] = bool(data["auto_restart"])
                if "autostart" in data:
                    proj["autostart"] = bool(data["autostart"])
                if "env" in data and isinstance(data["env"], dict):
                    proj["env"] = {str(k).strip(): str(v) for k, v in data["env"].items() if str(k).strip()}
                save_config(cfg)
            self._send_json({"project": {**proj, "id": app_id, **SUPERVISOR.status_dict(app_id)}})
            return
        self._send_json({"error": "Topilmadi"}, status=404)

    def do_DELETE(self):
        cfg = self._require_auth()
        if cfg is None:
            return
        parsed = urllib.parse.urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        qs = urllib.parse.parse_qs(parsed.query)

        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "projects":
            app_id = parts[2]
            if app_id not in cfg["projects"]:
                self._send_json({"error": "Loyiha topilmadi"}, status=404)
                return
            app_dir = APPS_DIR / app_id

            if len(parts) == 3:
                SUPERVISOR.stop(app_id)
                with STATE_LOCK:
                    cfg = load_config()
                    cfg["projects"].pop(app_id, None)
                    save_config(cfg)
                if app_dir.exists():
                    shutil.rmtree(app_dir, ignore_errors=True)
                self._send_json({"ok": True})
                return

            if len(parts) == 4 and parts[3] == "logs":
                log_path = app_dir / ".panel.log"
                with LOG_LOCK:
                    if log_path.exists():
                        log_path.write_text("", encoding="utf-8")
                self._send_json({"ok": True})
                return

            if len(parts) == 4 and parts[3] == "file":
                rel = qs.get("path", [""])[0]
                try:
                    target = safe_app_file(app_dir, rel)
                except ValueError as e:
                    self._send_json({"error": str(e)}, status=400)
                    return
                if target == app_dir.resolve() or not target.exists():
                    self._send_json({"error": "O'chirib bo'lmaydi"}, status=400)
                    return
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
                self._send_json({"ok": True})
                return

        self._send_json({"error": "Topilmadi"}, status=404)


def main():
    global PANEL_PORT
    parser = argparse.ArgumentParser(description="Grazajon Host Panel")
    parser.add_argument("--port", type=int, default=0, help="Panel porti (standart 5000)")
    parser.add_argument("--set-password", type=str, default="", help="Admin parol o'rnatish")
    args = parser.parse_args()

    cfg = load_config()
    if args.set_password:
        h, salt = hash_password(args.set_password)
        cfg["password_hash"] = h
        cfg["salt"] = salt
        save_config(cfg)
        print("Parol muvaffaqiyatli yangilandi!")
        return

    env_pass = os.environ.get("PANEL_PASS", "").strip()
    if env_pass and not cfg.get("password_hash"):
        h, salt = hash_password(env_pass)
        cfg["password_hash"] = h
        cfg["salt"] = salt
        save_config(cfg)

    port = args.port or int(cfg.get("port", 5000))
    SUPERVISOR.start_autostarts()

    server = ThreadingHTTPServer(("0.0.0.0", port), PanelHandler)
    PANEL_PORT = server.server_port
    ip = get_local_ip()
    print(f"Grazajon Host Panel ishga tushdi:")
    print(f"  Mahalliy manzil : http://{ip}:{PANEL_PORT}")
    print(f"  Localhost       : http://127.0.0.1:{PANEL_PORT}")
    print(f"  Loyihalar papka : {APPS_DIR}")

    def shutdown_handler(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown_handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nPanel to'xtatilmoqda...")
    finally:
        SUPERVISOR.stop_all()
        server.server_close()


if __name__ == "__main__":
    main()
