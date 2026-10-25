#!/usr/bin/env python3
"""Raw probe of the Sky-Watcher motor controller over USB-serial, independent of the bridge.

Stop the bridge first (sudo systemctl stop meshcore-bridge) and power the mount. Usage:
    .venv/bin/python tools/mount_probe.py [--deg 3] [--brake 5] [--port /dev/serial/by-id/...]
--brake N [N ...] (several allowed) uses the bridge's own rule (brake = min(half the move, N degrees)); without it a tenth of the move is used.
Prints the controller's identity, then moves the axis `degrees` (default 3) out and back with high-speed goto,
printing position and status every 0.25 s, so you can see whether the step count itself overshoots or hunts.
Ctrl-C sends stop.
"""
import argparse
import glob
import time

import serial

ap = argparse.ArgumentParser()
ap.add_argument('--port', default=(glob.glob('/dev/serial/by-id/usb-FTDI*') or ['/dev/ttyUSB0'])[0])
ap.add_argument('--deg', type=float, default=3.0)
ap.add_argument('--brake', type=float, nargs='*', default=None)
a = ap.parse_args()
port, deg = a.port, a.deg
s = serial.Serial(port, 9600, timeout=1)


def x(c):
    s.reset_input_buffer()
    s.write((c + '\r').encode())
    s.flush()
    while True:
        line = s.read_until(b'\r')
        if not line.endswith(b'\r'):
            return None
        line = line.strip().decode(errors='replace')
        if line and line[0] in '=!':                      # skip any echo of the command
            return line


def n(h):
    return int(h[5:7] + h[3:5] + h[1:3], 16)


def h24(v):
    return '%02X%02X%02X' % (v & 255, (v >> 8) & 255, (v >> 16) & 255)


for c in (':e1', ':a1', ':b1', ':g1', ':s1', ':d1', ':f1', ':j1'):
    print(c, x(c))
cpr = n(x(':a1'))
inc = round(cpr * deg / 360)
print('counts/deg %.1f, move %d counts' % (cpr / 360, inc))


def run(label, d, brake):
    """One move; stops watching once it has been stopped for ~1.5 s. Prints a verdict."""
    print('---', label, 'brake', brake, 'counts')
    x(':L1')
    time.sleep(.3)
    start_pos = n(x(':j1')) - 0x800000
    target = start_pos + (-inc if d else inc)
    x(':G10' + str(d)), x(':H1' + h24(inc)), x(':M1' + h24(brake)), x(':J1')
    t0, still, peak, pos = time.time(), 0, 0, start_pos
    while time.time() - t0 < 12:
        pos = n(x(':j1')) - 0x800000
        st = x(':f1')
        peak = max(peak, abs(pos - start_pos))
        still = still + 1 if st and st[2] == '0' else 0
        if still >= 5:
            break
        time.sleep(.25)
    miss = (pos - target) * (-1 if d else 1)
    if still >= 5:
        print('  SETTLED in %.1f s, end %d counts (%+.2f deg) from target' % (time.time() - t0, miss, miss / (cpr / 360)))
    else:
        print('  HUNTING: still moving after 12 s, peak travel %d counts vs %d commanded' % (peak, inc))
        x(':L1')
    time.sleep(.5)


try:
    for b in (a.brake if a.brake else [None]):
        brake = min(inc // 2, max(1, round(b * cpr / 360))) if b is not None else round(inc / 10)
        run('%g deg out' % deg, 0, brake)
        run('%g deg back' % deg, 1, brake)
finally:
    x(':L1')
