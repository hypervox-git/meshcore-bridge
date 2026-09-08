# Generic helpers with no radio dependency.
import os
import time
import json
import threading
import urllib.request

from bridge.extensions import socketio
from bridge.state import geo_cache


def get_sysinfo():
    info = {}
    try:
        with open('/proc/loadavg') as f:
            info['cpu'] = round(float(f.read().split()[0]) * 100 / os.cpu_count(), 1)
    except Exception:
        info['cpu'] = None
    try:
        with open('/proc/meminfo') as f:
            mem = {}
            for line in f:
                k, v = line.split(':')
                mem[k.strip()] = int(v.strip().split()[0])
            total = mem.get('MemTotal', 1)
            avail = mem.get('MemAvailable', 0)
            info['ram'] = round((total - avail) / total * 100, 1)
    except Exception:
        info['ram'] = None
    try:
        with open('/sys/class/thermal/thermal_zone0/temp') as f:
            info['temp'] = round(int(f.read()) / 1000, 1)
    except Exception:
        info['temp'] = None
    try:
        with open('/proc/uptime') as f:
            secs = int(float(f.read().split()[0]))
            d, r = divmod(secs, 86400)
            h, r = divmod(r, 3600)
            m = r // 60
            info['uptime'] = (f"{d}d " if d else "") + f"{h}h {m}m"
    except Exception:
        info['uptime'] = None
    return info


def health_score(node):
    s = 100
    rssi = node.get('rssi')
    snr = node.get('snr')
    age = node.get('lastSeen')
    if rssi:
        v = int(rssi)
        s += -40 if v < -110 else -25 if v < -100 else -15 if v < -90 else -5 if v < -80 else 0
    if snr:
        v = float(snr)
        s += -30 if v < -5 else -15 if v < 0 else -5 if v < 5 else 0
    if age:
        mins = (time.time() - age) / 60
        s += -30 if mins > 60 else -15 if mins > 30 else -5 if mins > 5 else 0
    return max(0, min(100, s))


def reverse_geocode(lat, lon):
    key = f"{round(lat,3)},{round(lon,3)}"
    if key in geo_cache:
        return geo_cache[key]

    def fetch():
        try:
            url = (f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}"
                   "&format=json&zoom=14")
            req = urllib.request.Request(url, headers={'User-Agent': 'MeshCoreBridge/1.0'})
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read())
                addr = data.get('address', {})
                name = (addr.get('village') or addr.get('town') or addr.get('suburb')
                        or addr.get('city') or addr.get('county') or 'unknown')
                geo_cache[key] = name
                socketio.emit('geocode', {'key': key, 'name': name}, namespace='/')
        except Exception:
            geo_cache[key] = None
    threading.Thread(target=fetch, daemon=True).start()
    return None
