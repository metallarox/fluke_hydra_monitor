Everything to bring a **Fluke Hydra 2620A/2625A** back to life on a modern bench: a Linux desktop app, a WiFi web interface on a Wemos D1 mini, and a printed mount for the unused GPIB opening.

## Downloads

| File | What |
|---|---|
| `fluke_hydra_monitor.py` | Desktop app 1.2.0 (Python 3 + Tkinter): readout, analog meter, chart, multi-channel, CSV. Accepts `socket://hydra.local:2625` to work over WiFi |
| `hydra_web_1.1.0_d1mini_pro_D7-D8.bin` | **Hydra Web** firmware, Hydra on **D8 (TX) / D7 (RX)** — recommended wiring, USB stays free |
| `hydra_web_1.1.0_d1mini_pro_TX-RX.bin` | Same firmware with the Hydra on the TX/RX pins |
| `hydra_wemos_plate.stl` | GPIB plate, print-oriented (stands on the tongue, supports from the build plate) |
| `hydra_wemos_plate_cover.stl` | Cover, print-oriented (no supports) |
| `hydra_wemos_plate.step` | Plate + cover for FreeCAD or any CAD |
| `hydra_wemos_plate.FCMacro` | Parametric model (FreeCAD macro / CadQuery script) |

Flash a firmware with `esptool.py write_flash 0x0 <file>.bin`; then join the **Hydra-XXXX** WiFi (password `hydra2625`) and open http://192.168.4.1.

## Highlights

- **Desktop app**: LIVE (damped moving-coil meter with 1-3-10 auto-ranging), MONITOR (chart with smart autoscale), Trigger, **MULTI-CHANNEL** (sweep or Hydra scan), CSV, IV3HGA logo, serial simulator.
- **Hydra Web** (ESP8266): the same interface in any browser or phone at http://hydra.local, acquisition running on the board, channel setup, command console, **serial debug page** with plain-words diagnosis and loopback test, TCP bridge for the desktop app, OTA updates.
- **GPIB plate**: 12 mm outside the panel, tongue shaped like the slot holds the mini MAX3232, Wemos outside under a low cover. [Rotate it in 3D](https://github.com/metallarox/fluke_hydra_monitor/blob/main/hardware/gpib_plate/3D.md).

Check the caliper measurements listed in [hardware/gpib_plate](https://github.com/metallarox/fluke_hydra_monitor/tree/main/hardware/gpib_plate) before printing. The *Hydra scan* command syntax is not yet verified on a real unit.

73 de IV3HGA
