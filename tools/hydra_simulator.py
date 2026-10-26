#!/usr/bin/env python3
"""Minimal Fluke Hydra 2620A/2625A serial simulator (Linux/macOS).

Creates a pseudo-terminal that answers the subset of commands used by
fluke_hydra_monitor.py, so the GUI can be tried without the instrument.

    python3 tools/hydra_simulator.py            # prints e.g. /dev/pts/5
    python3 fluke_hydra_monitor.py              # connect to the printed port

The simulated signal is a slow sine around 12 mV with a little noise.
Options: --volts X (DC offset), --amplitude X, --period S, --overload-every N.
"""
import argparse
import math
import os
import pty
import random
import time


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--volts', type=float, default=0.012, help='DC offset in volts (default 0.012)')
    ap.add_argument('--amplitude', type=float, default=0.004, help='sine amplitude in volts (default 0.004)')
    ap.add_argument('--period', type=float, default=20.0, help='sine period in seconds (default 20)')
    ap.add_argument('--overload-every', type=int, default=0, help='return +9E+9 every N readings (0 = never)')
    args = ap.parse_args()

    master, slave = pty.openpty()
    print(f'Hydra simulator listening on {os.ttyname(slave)}  (Ctrl-C to quit)', flush=True)
    t0 = time.time()
    funcs = {}
    monitor = None
    n = 0
    buf = b''
    while True:
        buf += os.read(master, 256)
        while b'\r' in buf:
            line, buf = buf.split(b'\r', 1)
            cmd = line.decode(errors='replace').replace('\x03', '').strip()
            if not cmd:
                continue
            print('>', cmd, flush=True)
            up = cmd.upper()
            out, prompt = '', '=>'
            if up == '*IDN?':
                out = 'FLUKE,2625A,0,SIMULATOR'
            elif up.startswith('FUNC?'):
                ch = up.split()[1]
                out = funcs.get(ch, 'OFF')
            elif up.startswith('FUNC '):
                parts = cmd[5:].split(',')
                funcs[parts[0].strip()] = ','.join(p.strip() for p in parts[1:])
            elif up.startswith('MON '):
                arg = up[4:].split(',')
                monitor = arg[1].strip() if arg[0].strip() == '1' else None
            elif up in ('MON_VAL?',) or up.startswith('LAST?'):
                if up == 'MON_VAL?' and monitor is None:
                    prompt = '!>'
                else:
                    n += 1
                    t = time.time() - t0
                    v = args.volts + args.amplitude * math.sin(2 * math.pi * t / args.period)
                    v += random.gauss(0, args.amplitude * 0.01 + 1e-6)
                    if args.overload_every and n % args.overload_every == 0:
                        v = 9e9
                    out = f'{v:+.5E}'
            elif up.startswith(('SCAN', '*TRG')):
                pass
            else:
                prompt = '?>'
            os.write(master, ((out + '\r\n') if out else '').encode() + prompt.encode() + b'\r\n')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
