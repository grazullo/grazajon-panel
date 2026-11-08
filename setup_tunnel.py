#!/usr/bin/env python3
"""
Grazajon Host — Cloudflare Tunnel va Domen avtomatik sozlash skripti.
Muallif: Grazajon
"""
import glob
import os
import socket
import subprocess
import sys
import time
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
print("  -> panel.slowstax.uz sozlanmoqda...")
res1 = subprocess.run(
    ["cloudflared", "tunnel", "route", "dns", "--overwrite-dns", "telefon-tunnel", "panel.slowstax.uz"],
    capture_output=True, text=True
)
if "successfully" in res1.stdout.lower() or res1.returncode == 0:
    print("     ✓ panel.slowstax.uz muvaffaqiyatli bog'landi!")
else:
    print(f"     ℹ️  {res1.stdout.strip() or res1.stderr.strip()}")

print("  -> slowstax.uz sozlanmoqda...")
res2 = subprocess.run(
    ["cloudflared", "tunnel", "route", "dns", "--overwrite-dns", "telefon-tunnel", "slowstax.uz"],
    capture_output=True, text=True
)
if "successfully" in res2.stdout.lower() or res2.returncode == 0:
    print("     ✓ slowstax.uz muvaffaqiyatli bog'landi!")
else:
    print(f"     ℹ️  {res2.stdout.strip() or res2.stderr.strip()}")

# 3. config.yml faylini yaratish
print("\n[3/4] Tunnel konfiguratsiya fayli (config.yml) yaratilmoqda...")
config_content = f"""tunnel: {tunnel_id}
credentials-file: {cred_file}

ingress:
  - hostname: panel.slowstax.uz
    service: http://localhost:5000
  - hostname: slowstax.uz
    service: http://localhost:8080
  - service: http_status:404
"""

cfg_file = cf_dir / "config.yml"
cfg_file.write_text(config_content, encoding="utf-8")
print(f"  ✓ Konfiguratsiya saqlandi: {cfg_file}")

# 4. Tmux tekshirish va xizmatlarni fonda ishga tushirish
print("\n[4/4] Xizmatlar tmux fon rejimida ishga tushirilmoqda...")

# Tmux bor-yo'qligini tekshirish
tmux_check = subprocess.run(["which", "tmux"], capture_output=True, text=True).stdout.strip()
if not tmux_check:
    print("  tmux topilmadi, o'rnatilmoqda...")
    subprocess.run(["pkg", "install", "tmux", "-y"])

# Eski sessiyalarni to'xtatish
subprocess.run(["tmux", "kill-session", "-t", "panel"], capture_output=True)
subprocess.run(["tmux", "kill-session", "-t", "tunnel"], capture_output=True)
time.sleep(1)

home_dir = str(Path.home())
panel_py = os.path.join(home_dir, "grazajon-panel", "panel.py")

# Grazajon panelni ishga tushirish (agar to'xtasa, avtomatik qayta yonadi)
cmd_panel = f"while true; do python3 {panel_py}; sleep 3; done"
subprocess.run(["tmux", "new", "-d", "-s", "panel", cmd_panel])
print("  ✓ Grazajon Panel (port 5000) fon rejimida ishga tushdi.")

# Cloudflare tunnelni ishga tushirish (agar to'xtasa, avtomatik qayta ulanadi)
cmd_tunnel = f"while true; do cloudflared tunnel --config {cfg_file} run; sleep 5; done"
subprocess.run(["tmux", "new", "-d", "-s", "tunnel", cmd_tunnel])
print("  ✓ Cloudflare Tunnel xalqaro tarmog'i ishga tushdi.")

local_ip = get_local_ip()

print("\n" + "="*50)
print("  🎉 HAMMASI TAYYOR! GRAZAJON HOST ISHLAMOQDA!")
print("="*50)
print("\n🌐 Dunyoning istalgan chekkasidan kirish (Wi-Fi / Mobil 4G/5G):")
print("   👉 Boshqaruv paneli:  https://panel.slowstax.uz")
print("   👉 Saytingiz:         https://slowstax.uz")
print(f"\n🏠 Uy tarmog'idan kirish:")
print(f"   👉 Mahalliy IP:       http://{local_ip}:5000")
print("   👉 Localhost:         http://localhost:5000")
print("\n📱 Konsollarni jonli ko'rish uchun:")
print("   - Panel loglari:      tmux attach -t panel")
print("   - Tunnel loglari:     tmux attach -t tunnel")
print("   - Ekranni yopish:     Ctrl+B keyin D bosing")
print("="*50 + "\n")
