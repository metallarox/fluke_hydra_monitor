#!/usr/bin/env python3
"""Fake Hydra Web device: serves web/index.html with the same HTTP API as the ESP8266.

Lets you work on the page from a PC, without flashing:

    python3 firmware/mock_server.py            # then open http://localhost:8080

The readings come from the same signal model as tools/hydra_simulator.py.
Options: --port N, --overload-every N, --no-instrument (Hydra not responding).
"""
import argparse
import base64
import importlib.util
import json
import math
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
PAGE = HERE / 'hydra_web' / 'web' / 'index.html'
LOGO = HERE.parent / 'assets' / 'logo_64.png'
spec = importlib.util.spec_from_file_location('sim', HERE.parent / 'tools' / 'hydra_simulator.py')
sim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sim)

T0 = time.monotonic()
millis = lambda: int((time.monotonic() - T0) * 1000)


class Device:
    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.ring, self.seq, self.scan = [], 0, 0
        self.running, self.mode, self.hscan, self.period = False, 'live', False, 1000
        self.channels, self.funcs, self.desc, self.scan0 = [], {}, None, 0
        self.msg, self.err, self.func_state = 'Ready', '', ''
        self.config = '{}'
        self.n = 0
        self.sig = argparse.Namespace(volts=0.012, amplitude=0.004, period=20.0)
        threading.Thread(target=self.engine, daemon=True).start()

    def value(self, ch):
        self.n += 1
        if self.args.overload_every and self.n % self.args.overload_every == 0:
            return None
        v = sim.signal(self.funcs.get(ch, 'VDC'), ch, time.monotonic() - T0, self.sig)
        return v + random.gauss(0, abs(v) * 1e-4 + 1e-6)

    def store(self, ch, v):
        self.seq += 1
        self.ring.append((self.seq, millis(), ch, v, self.scan))
        del self.ring[:-600]

    def engine(self):
        next_at = 0
        while True:
            time.sleep(0.02)
            with self.lock:
                if not self.running or millis() < next_at:
                    continue
                chans = list(self.channels)
                per_ch = 0.45 if self.mode == 'multi' and not self.hscan else 0.0
            for ch in chans:                           # sweep: one channel at a time
                time.sleep(per_ch if per_ch else 0.05)
                with self.lock:
                    if not self.running:
                        break
                    self.store(ch, self.value(ch))
            with self.lock:
                self.scan += 1
                next_at = max(next_at + self.period, millis()) if next_at else millis() + self.period
                self.msg = 'Measuring'

    def status(self):
        return {'fw': '1.0.0-mock', 'now': millis(), 'inst': not self.args.no_instrument,
                'idn': 'FLUKE,2625A,0,MOCK', 'run': self.running, 'mode': self.mode,
                'method': 'hscan' if self.hscan else 'sweep', 'period': self.period, 'ch': self.channels,
                'desc': self.desc, 'seq': self.seq, 'scan': self.scan, 'scan0': self.scan0, 'msg': self.msg,
                'err': self.err, 'func': self.func_state, 'bridge': False, 'bridgePort': 2625, 'ap': False,
                'ssid': 'MockNet', 'ip': '127.0.0.1', 'rssi': -55, 'heap': 30000, 'host': 'hydra'}

    def start(self, q):
        if self.args.no_instrument:
            self.err = 'MON 0: no response'
            return 'MON 0: no response'
        self.mode = q.get('mode', 'live')
        self.hscan = self.mode == 'multi' and q.get('method') == 'hscan'
        self.period = max(500, min(3600000, int(q.get('period', '1000'))))
        if self.hscan:
            self.period = max(1, round(self.period / 1000)) * 1000
        self.channels, self.funcs = [], {}
        for item in q.get('f', '').split(';'):
            if '|' in item:
                ch, cmd = item.split('|', 1)
                self.channels.append(int(ch))
                self.funcs[int(ch)] = cmd.split(',', 1)[1] if ',' in cmd else ''
        if self.mode != 'multi':
            self.channels = self.channels[:1]
            self.func_state = self.funcs.get(self.channels[0], '')
        try:
            self.desc = json.loads(q.get('desc', 'null'))
        except ValueError:
            self.desc = None
        self.scan += 1
        self.scan0 = self.scan
        self.err, self.msg, self.running = '', 'Started', True
        return ''


class Handler(BaseHTTPRequestHandler):
    dev = None

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype='application/json'):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        d = self.dev
        if u.path == '/':
            html = PAGE.read_text(encoding='utf-8')
            html = html.replace('{{LOGO}}', 'data:image/png;base64,' + base64.b64encode(LOGO.read_bytes()).decode())
            return self.send(200, html, 'text/html; charset=utf-8')
        with d.lock:
            if u.path == '/api/status':
                return self.send(200, json.dumps(d.status()))
            if u.path == '/api/readings':
                since = int(q.get('since', 0))
                items = [r for r in d.ring if r[0] > since][:200]
                more = bool(items) and items[-1][0] < d.seq
                return self.send(200, json.dumps({'now': millis(), 'seq': items[-1][0] if items else max(since, 0) if since <= d.seq else d.seq,
                                                  'more': more, 'r': [[s, t, c, (None if v is None else round(v, 9)), sc]
                                                                      for s, t, c, v, sc in items]}))
            if u.path == '/api/config':
                return self.send(200, d.config)
        self.send(404, '{"ok":false,"err":"not found"}')

    def do_POST(self):
        u = urlparse(self.path)
        body = self.rfile.read(int(self.headers.get('Content-Length', 0) or 0)).decode()
        q = {k: v[0] for k, v in parse_qs(body).items()}
        d = self.dev
        with d.lock:
            if u.path == '/api/start':
                err = d.start(q)
                return self.send(409 if err else 200, json.dumps({'ok': not err, 'err': err}))
            if u.path == '/api/stop':
                d.running, d.msg = False, 'Measurement stopped'
                return self.send(200, '{"ok":true}')
            if u.path == '/api/cmd':
                if d.running:
                    return self.send(409, '{"ok":false,"err":"stop the measurement first"}')
                c = q.get('c', '').strip().upper()
                out = '=>'
                if c == '*IDN?':
                    out = 'FLUKE,2625A,0,MOCK\n=>'
                elif c.startswith('FUNC?'):
                    out = 'VDC,AUTO\n=>'
                elif not c.split(' ')[0] in ('FUNC', 'MON', 'SCAN', 'INTVL', 'TRIGGER', 'LAST?', 'MON_VAL?', '*TRG'):
                    out = '?>'
                return self.send(200, json.dumps({'ok': True, 'out': out}))
            if u.path == '/api/config':
                d.config = body if body.startswith('{') else '{}'
                return self.send(200, '{"ok":true}')
            if u.path in ('/api/wifi', '/api/reboot'):
                return self.send(200, '{"ok":true}')
        self.send(404, '{"ok":false,"err":"not found"}')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--port', type=int, default=8080)
    ap.add_argument('--overload-every', type=int, default=0)
    ap.add_argument('--no-instrument', action='store_true')
    args = ap.parse_args()
    Handler.dev = Device(args)
    print(f'Mock Hydra Web on http://localhost:{args.port}', flush=True)
    ThreadingHTTPServer(('', args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
