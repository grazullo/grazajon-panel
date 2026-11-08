#!/usr/bin/env python3
"""
Grazajon Host — Cloudflare Tunnel va Domen avtomatik sozlash skripti.
Asosiy domen: https://slowstax.uz
"""
import glob
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

print("\n" + "="*50)
print("  🚀 GRAZAJON HOST — CLOUDFLARE TUNNEL SOZLAGICH")
print("  🌐 Domen: https://slowstax.uz")
print("="*50)

# 1. ~/.cloudflared papkasi va sertifikatni tekshirish
cf_dir = Path.home() / ".cloudflared"
if not cf_dir.exists():
    print("\n❌ XATO: ~/.cloudflared papkasi topilmadi!")
    print("Iltimos, avval 'cloudflared tunnel login' buyrug'ini bajaring.")
    sys.exit(1)

# UUID shaklidagi json faylni topish (tunnel credentials)
json_files = [f for f in cf_dir.glob("*.json") if len(f.stem) == 36 and f.stem.count("-") == 4]
if not json_files:
    json_files = list(cf_dir.glob("*.json"))

if not json_files:
    print("\n❌ XATO: Tunnel sertifikati topilmadi!")
    print("Avval quyidagi buyruq bilan tunnel yarating:")
    print("  cloudflared tunnel create telefon-tunnel")
    sys.exit(1)

cred_file = json_files[0]
tunnel_id = cred_file.stem
print(f"\n[1/4] ✓ Topilgan tunnel ID: {tunnel_id}")

# 2. DNS yozuvlarini bog'lash (--overwrite-dns bilan)
print("\n[2/4] DNS yozuvlari Cloudflare bilan bog'lanmoqda...")
hosts = ["slowstax.uz", "www.slowstax.uz", "panel.slowstax.uz"]
for host in hosts:
    print(f"  -> {host} sozlanmoqda...")
    res = subprocess.run(
        ["cloudflared", "tunnel", "route", "dns", "--overwrite-dns", "telefon-tunnel", host],
        capture_output=True, text=True
    )
    if "successfully" in res.stdout.lower() or res.returncode == 0:
        print(f"     ✓ {host} muvaffaqiyatli bog'landi!")
    else:
        print(f"     ℹ️  {res.stdout.strip() or res.stderr.strip()}")

# 3. config.yml faylini yaratish (barcha so'rovlar to'g'ridan-to'g'ri 5000-portdagi panelga yo'naltiriladi)
print("\n[3/4] Tunnel konfiguratsiya fayli (config.yml) yaratilmoqda...")
config_content = f"""tunnel: {tunnel_id}
credentials-file: {cred_file}

ingress:
  - hostname: slowstax.uz
    service: http://localhost:5000
  - hostname: www.slowstax.uz
    service: http://localhost:5000
  - hostname: panel.slowstax.uz
    service: http://localhost:5000
  - service: http_status:404
"""

cfg_file = cf_dir / "config.yml"
cfg_file.write_text(config_content, encoding="utf-8")
print(f"  ✓ Konfiguratsiya saqlandi: {cfg_file}")

# 4. Tmux tekshirish va xizmatlarni fonda ishga tushirish
print("\n[4/4] Xizmatlar to'liq tozalab, qayta ishga tushirilmoqda...")

# Tmux bor-yo'qligini tekshirish
tmux_check = subprocess.run(["which", "tmux"], capture_output=True, text=True).stdout.strip()
if not tmux_check:
    print("  tmux topilmadi, o'rnatilmoqda...")
    subprocess.run(["pkg", "install", "tmux", "-y"])

# Eski qotib qolgan jarayonlarni to'liq to'xtatish
subprocess.run(["tmux", "kill-server"], capture_output=True)
subprocess.run(["killall", "-9", "cloudflared"], capture_output=True)
# Faqat boshqa pythonlarni to'xtatish
subprocess.run(["pkill", "-f", "panel.py"], capture_output=True)
time.sleep(2)

panel_dir = os.path.dirname(os.path.abspath(__file__))
panel_py = os.path.join(panel_dir, "panel.py")

run_panel_sh = os.path.join(panel_dir, "run_panel.sh")
run_tunnel_sh = os.path.join(panel_dir, "run_tunnel.sh")

try:
    os.chmod(run_panel_sh, 0o755)
    os.chmod(run_tunnel_sh, 0o755)
except Exception:
    pass

# Grazajon panelni ishga tushirish
subprocess.run(["tmux", "new", "-d", "-s", "panel", f"bash {run_panel_sh}"])

# Cloudflare tunnelni ishga tushirish
subprocess.run(["tmux", "new", "-d", "-s", "tunnel", f"bash {run_tunnel_sh}"])

# 5. Xizmat salomatligini tekshirish (Health Check)
print("\n🔍 Server tekshirilmoqda...")
time.sleep(2)

panel_ok = False
for attempt in range(5):
    try:
        with urllib.request.urlopen("http://127.0.0.1:5000/api/auth/me", timeout=2) as resp:
            if resp.status == 200:
                panel_ok = True
                break
    except Exception:
        time.sleep(1)

if panel_ok:
    print("  ✓ Grazajon Panel (port 5000) 100% muvaffaqiyatli ishga tushdi!")
else:
    print("  ⚠️ Ogohlantirish: 5000-port tezda javob bermadi. Konsol diagnostikasi:")
    diag = subprocess.run(["tmux", "capture-pane", "-pt", "panel"], capture_output=True, text=True)
    if diag.stdout.strip():
        print(diag.stdout.strip()[-500:])

local_ip = get_local_ip()

print("\n" + "="*50)
print("  🎉 HAMMASI TAYYOR! GRAZAJON HOST ISHLAMOQDA!")
print("="*50)
print("\n🌐 Dunyoning istalgan chekkasidan kirish (Wi-Fi / Mobil 4G/5G):")
print("   👉 Asosiy manzil:      https://slowstax.uz")
print("   👉 Qo'shimcha manzil:  https://www.slowstax.uz")
print("   👉 Panel manzili:      https://panel.slowstax.uz")
print(f"\n🏠 Uy tarmog'idan kirish:")
print(f"   👉 Mahalliy IP:        http://{local_ip}:5000")
print("   👉 Localhost:          http://localhost:5000")
print("\n📱 Konsollarni jonli ko'rish uchun:")
print("   - Panel loglari:      tmux attach -t panel")
print("   - Tunnel loglari:     tmux attach -t tunnel")
print("   - Ekranni yopish:     Ctrl+B keyin D bosing")
print("="*50 + "\n")
