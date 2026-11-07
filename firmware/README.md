# Hydra Web — ESP8266 firmware

A Wemos D1 mini (Pro) with a MAX3232 turns the Fluke Hydra 2620A/2625A into a network
instrument: open **http://hydra.local** from any browser, phone included, and you get the same
interface as the desktop app.

<p align="center">
  <img src="../docs/screenshots/web/web_live.png" width="49%" alt="LIVE mode in the browser">
  <img src="../docs/screenshots/web/web_multi.png" width="49%" alt="MULTI-CHANNEL in the browser">
</p>

## What it does

- **Same modes as the desktop app**: LIVE (analog meter), MONITOR (chart), Trigger + LAST?,
  MULTI-CHANNEL (Sweep through the Monitor, or experimental Hydra scan).
- **Same interface**: big readout with statistics, damped moving-coil meter with 1-3-10 auto-ranging and
  centre-zero, chart with smart autoscale / manual limits / Normalize, channel tiles, channel setup window.
- **The device measures, the page displays**: acquisition runs on the ESP8266, so it continues with the
  browser closed. Opening the page again (from any device) picks up the running session and the last
  600 readings kept on the device.
- **CSV export** from the page: all readings (long format) and, in multi-channel, one row per scan.
- **Command console** to send raw commands to the Hydra — handy to check the Hydra-scan syntax.
- **PC bridge**: the desktop app can connect over WiFi to `socket://hydra.local:2625`; while it is
  connected, web control is paused.
- WiFi setup from the page, access point fallback with captive portal, mDNS name, OTA updates.
- No external libraries: only the ESP8266 Arduino core. The page needs no internet (no CDN).

## Hardware

```
 Wemos D1 mini Pro            MAX3232 module (3.3 V)          Hydra RS-232 (DB9)
 ─────────────────            ──────────────────────          ──────────────────
 3V3  ─────────────────────── VCC
 GND  ─────────────────────── GND ─────────────────────────── pin 5  GND
 TX  (GPIO1)  ─────────────── T1IN      T1OUT ─────────────── RX of the Hydra
 RX  (GPIO3)  ─────────────── R1OUT     R1IN  ─────────────── TX of the Hydra
 5V  ── supply (USB or a 5 V step-down from the Hydra's 9–16 V DC supply)
```

- Power the MAX3232 from **3.3 V**, never 5 V: its outputs drive the ESP8266 pins directly.
- If the Hydra never answers (*Hydra: no answer* on the page), swap the two data lines on the DB9 side
  (null-modem).
- With `HYDRA_UART_SWAP 1` in `config.h` the Hydra goes on **D8 (TX, GPIO15)** and **D7 (RX, GPIO13)**:
  USB stays free for flashing and the boot messages of the ESP8266 do not reach the Hydra.
  With the default (TX/RX pins), disconnect the RX wire if an upload over USB fails.

## Build and flash

### Arduino IDE

1. *File → Preferences → Additional boards manager URLs*:
   `https://arduino.esp8266.com/stable/package_esp8266com_index.json`, then install **esp8266** (3.1.x)
   from the Boards Manager.
2. Open `firmware/hydra_web/hydra_web.ino`.
3. *Tools → Board*: **LOLIN(WEMOS) D1 mini Pro** (or *D1 mini*). Keep a *Flash Size* with a file system
   (the default, e.g. *16MB (FS:14MB OTA:~1019KB)*).
4. Edit `config.h` if you like (WiFi, pins), then **Upload**.

### PlatformIO

```bash
cd firmware
pio run -e d1_mini_pro -t upload        # or -e d1_mini
```

### Updating over WiFi

After the first USB flash, the board shows up as network port **hydra** in the Arduino IDE
(*Tools → Port*), or: `pio run -e d1_mini_pro -t upload --upload-port hydra.local`.

## First start

1. With no network configured, the board opens an access point **Hydra-XXXX**
   (password `hydra2625`, change it in `config.h`). Connect: the page opens by itself (captive portal),
   otherwise browse to **http://192.168.4.1**.
2. **WiFi…** → network name and password → *Save & restart*.
3. Back on your network, open **http://hydra.local** (or the IP from your router).

If the saved network is not reachable at boot, the access point comes back and the board keeps trying
to join the network in the background.

## Using the PC bridge

In the desktop app choose the port `socket://hydra.local:2625` (it is in the port list) and press
Connect. The web page shows a banner while the PC is connected; closing the desktop app frees the
line again.

## Editing the page

The page is `hydra_web/web/index.html` (plain HTML/CSS/JS). After editing it:

```bash
python3 firmware/build_web.py          # regenerates hydra_web/web_page.h (gzip)
```

To try changes without flashing, run the mock device and open http://localhost:8080:

```bash
python3 firmware/mock_server.py        # same API, simulated readings
```

## HTTP API

| Method | Path | Parameters / answer |
|---|---|---|
| GET | `/api/status` | device, instrument, session and WiFi state (JSON) |
| GET | `/api/readings?since=SEQ` | `{"now","seq","more","r":[[seq, t_ms, ch, value or null, scan],…]}`, at most 200 per call |
| POST | `/api/start` | `mode` (live, monitor, trigger, multi), `method` (sweep, hscan), `period` (ms), `f` (`ch\|FUNC …;…`), `desc` (JSON echoed back in status) |
| POST | `/api/stop` | — |
| POST | `/api/cmd` | `c`: one raw command (only while stopped) → `{"out": answer + prompt}` |
| GET/POST | `/api/config` | the channel setup (JSON, stored on the device) |
| POST | `/api/wifi` | `ssid`, `pass` → saves and restarts |
| POST | `/api/reboot` | — |

`t_ms` is the device's `millis()`; the page converts it with the `now` of the same answer.

## Notes

- Readings kept on the device: the last 600. The page keeps everything it receives (up to 200 000
  readings) until it is closed: export before closing it.
- The *Hydra scan* commands are in `config.h` (`HSCAN_*`); they have not yet been verified on a real
  unit. Use the console to check `INTVL?`, `TRIGGER?`, `SCAN?` and adjust them if the Hydra answers `?>`.
- Overrange is detected as |value| ≥ 9E+9 or an `OL` answer, like in the desktop app.
