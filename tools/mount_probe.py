#!/usr/bin/env python3
"""Raw probe of the Sky-Watcher motor controller over USB-serial, independent of the bridge.

Stop the bridge first (sudo systemctl stop meshcore-bridge) and power the mount. Usage:
    .venv/bin/python tools/mount_probe.py [--deg 3] [--brake 5] [--port /dev/serial/by-id/...]
--brake N uses the bridge's own rule (brake = min(half the move, N degrees)); without it a tenth of the move is used.
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
ap.add_argument('--brake', type=float, default=None)
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
brake = min(inc // 2, max(1, round(a.brake * cpr / 360))) if a.brake is not None else round(inc / 10)
print('counts/deg %.1f, move %d counts, brake %d counts' % (cpr / 360, inc, brake))


def run(label, d):
    print('---', label)
    x(':L1')
    time.sleep(.3)
    print(x(':G10' + str(d)), x(':H1' + h24(inc)), x(':M1' + h24(brake)), x(':J1'))
    t0 = time.time()
    while time.time() - t0 < 12:
        print('%4.1fs pos=%d st=%s' % (time.time() - t0, n(x(':j1')) - 0x800000, x(':f1')))
        time.sleep(.25)


try:
    run('%g deg, direction 0' % deg, 0)
    run('%g deg, direction 1 (back)' % deg, 1)
finally:
    x(':L1')
