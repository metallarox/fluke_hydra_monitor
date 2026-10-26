#!/usr/bin/env python3
"""Minimal Fluke Hydra 2620A/2625A serial simulator (Linux/macOS).

Creates a pseudo-terminal that answers the subset of commands used by
fluke_hydra_monitor.py, so the GUI can be tried without the instrument.

    python3 tools/hydra_simulator.py            # prints e.g. /dev/pts/5
    python3 fluke_hydra_monitor.py              # connect to the printed port

Every channel gets its own signal, depending on its FUNC:
  V DC / V AC  slow sine around --volts (each channel offset and phase-shifted)
  OHMS         about 1 kΩ × (1 + ch/10)
  FREQ         about 1 kHz × (ch + 1)
  TEMP         about 22 °C + ch/2
After MON moves to another channel, the first MON_VAL? still returns the previous
channel's value, like a real reading in progress; the GUI discards it.

Options: --volts X (DC offset), --amplitude X, --period S, --overload-every N, --quiet.
"""
import argparse
import math
import os
import pty
import random
import time


def signal(func, ch, t, args):
    kind = func.split(',')[0] if func else 'VDC'
    phase = ch * 0.7
    wave = math.sin(2 * math.pi * t / args.period + phase)
    if kind in ('VDC', 'VAC'):
        v = args.volts * (1 + ch * 0.25) + args.amplitude * wave
        return abs(v) if kind == 'VAC' else v
    if kind == 'OHMS':
        return 1000 * (1 + ch / 10) * (1 + 0.002 * wave)
    if kind == 'FREQ':
        return 1000 * (ch + 1) * (1 + 0.001 * wave)
    if kind == 'TEMP':
        return 22 + ch / 2 + 0.3 * wave
    return 0.0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--volts', type=float, default=0.012, help='DC offset in volts (default 0.012)')
    ap.add_argument('--amplitude', type=float, default=0.004, help='sine amplitude in volts (default 0.004)')
    ap.add_argument('--period', type=float, default=20.0, help='sine period in seconds (default 20)')
    ap.add_argument('--overload-every', type=int, default=0, help='return +9E+9 every N readings (0 = never)')
    ap.add_argument('--quiet', action='store_true', help='do not print the received commands')
    args = ap.parse_args()

    master, slave = pty.openpty()
    print(f'Hydra simulator listening on {os.ttyname(slave)}  (Ctrl-C to quit)', flush=True)
    t0 = time.time()
    funcs = {'0': 'VDC,AUTO'}
    monitor = None          # channel being monitored
    stale = None            # value returned once right after a monitor switch
    last_mon = 0.0
    n = 0
    buf = b''

    def reading(ch):
        nonlocal n
        n += 1
        if args.overload_every and n % args.overload_every == 0:
            return 9e9
        v = signal(funcs.get(str(ch), ''), int(ch), time.time() - t0, args)
        return v + random.gauss(0, abs(v) * 1e-4 + 1e-6)

    while True:
        buf += os.read(master, 256)
        while b'\r' in buf:
            line, buf = buf.split(b'\r', 1)
            cmd = line.decode(errors='replace').replace('\x03', '').strip()
            if not cmd:
                continue
            if not args.quiet:
                print('>', cmd, flush=True)
            up = cmd.upper()
            out, prompt = '', '=>'
            if up == '*IDN?':
                out = 'FLUKE,2625A,0,SIMULATOR'
            elif up.startswith('FUNC?'):
                out = funcs.get(up.split()[1], 'OFF')
            elif up.startswith('FUNC '):
                parts = [x.strip() for x in cmd[5:].split(',')]
                funcs[parts[0]] = ','.join(parts[1:])
            elif up.startswith('MON '):
                arg = [x.strip() for x in up[4:].split(',')]
                if arg[0] == '1':
                    if monitor is not None and arg[1] != monitor:
                        stale = last_mon
                    monitor = arg[1]
                else:
                    monitor = None
            elif up == 'MON_VAL?':
                if monitor is None:
                    prompt = '!>'
                elif stale is not None:
                    out, stale = f'{stale:+.5E}', None
                else:
                    last_mon = reading(monitor)
                    out = f'{last_mon:+.5E}'
            elif up.startswith('LAST?'):
                ch = up.split()[1]
                out = f'{reading(ch):+.5E}' if funcs.get(ch, 'OFF') != 'OFF' else ''
                prompt = '=>' if out else '!>'
            elif up.startswith(('SCAN', 'INTVL', 'TRIGGER', '*TRG')):
                pass
            else:
                prompt = '?>'
            os.write(master, ((out + '\r\n') if out else '').encode() + prompt.encode() + b'\r\n')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
