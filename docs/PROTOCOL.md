# RS-232 protocol and software architecture

## Line settings

9600 baud, 8N1, no flow control. Commands are ASCII terminated by `CR`.

## Prompts

Every command is answered by an optional response line followed by a prompt:

| Prompt | Meaning | Software behaviour |
|---|---|---|
| `=>` | command OK | response returned |
| `?>` | syntax error | `RuntimeError: … syntax error (?>)` |
| `!>` | execution error | `RuntimeError: … execution error (!>)` |

If the Hydra echoes commands (ECHO ON), the echoed line is removed from the response.
A command without a prompt within the timeout raises `TimeoutError`.

## Commands used

| Command | When | Purpose |
|---|---|---|
| `Ctrl-C` (0x03) | on connect | cancel any half-received command, then flush input |
| `*IDN?` | on connect | identification string shown in the header |
| `SCAN 0` | before configuring | stop scanning |
| `MON 0` | before configuring, on stop, disconnect and exit | disable Monitor |
| `FUNC ch,VDC,range` | configure | V DC (range AUTO, 1…4) |
| `FUNC ch,VAC,range` | configure | V AC |
| `FUNC ch,OHMS,range,wires` | configure | resistance, 2 or 4 wires (range AUTO, 1…6) |
| `FUNC ch,FREQ,range` | configure | frequency (range AUTO, 1…5) |
| `FUNC ch,TEMP,type` | configure | thermocouple, type J K E T N R S B C |
| `FUNC ch,TEMP,PT,wires` | configure | Pt100 RTD |
| `FUNC ch,OFF` | configure | channel off |
| `FUNC? ch` | after configuring | read back the configuration (shown in the UI) |
| `MON 1,ch` | start LIVE / MONITOR | enable Monitor on the channel |
| `MON_VAL?` | every period | read the Monitor value (timeout 2.5 s) |
| `*TRG` + `LAST? ch` | every period, Trigger mode | trigger and read the last scan value |
| `MON 1,ch` + `MON_VAL?` ×2 | every channel, MULTI / Sweep | move the Monitor, discard the first value, read |
| `INTVL h,m,s` · `TRIGGER 1` · `SCAN 1` | start, MULTI / Hydra scan (**unverified**) | instrument-paced scanning |
| `LAST? ch` | every channel, MULTI / Hydra scan | last scanned value of the channel |

The Hydra-scan strings are the constants `HYDRA_SCAN_START`, `HYDRA_SCAN_STOP` and
`HYDRA_SCAN_READ` at the top of `fluke_hydra_monitor.py`; adjust them if your unit answers `?>`.

Readings are parsed with a regular expression for a signed float with optional exponent.
Values with magnitude ≥ 9E+9, or responses containing `OL`, are treated as overload (NaN).

## Architecture

```
 Tk main thread                          worker thread (owns the serial port)
 ───────────────                         ─────────────────────────────────────
 buttons ──► jobs queue  ───────────────► connect / disconnect / configure /
                                          start / stop / quit
                                          periodic read (fixed-step schedule)
 _poll() every 100 ms ◄── events queue ◄── connected, configured, reading,
   ├─ readout, stats, CSV                  status, error
   ├─ analog meter (LIVE / Trigger)
   └─ chart redraw every 300 ms (MONITOR)
```

- Only the worker touches the port, so commands never interleave.
- The worker remembers which `(channel, FUNC command)` is loaded; **Start** reconfigures only if it changed.
- Reads are scheduled as `next += period`; if the schedule falls behind it restarts from now.
- Errors during periodic reads are retried up to `MAX_RETRY` (3) times before the run is stopped.
- In MULTI-CHANNEL the worker reads **one channel per loop iteration**, so Stop and window close
  are handled between channels even during a long sweep. A channel whose read fails is skipped
  for that scan (empty in the CSV) and counts towards the retry limit.
- Serial reads return as soon as bytes are available (`read(in_waiting or 1)`), so a command
  costs only the instrument's response time.
- On window close the GUI enqueues `quit` and waits up to 3 s for the worker to send `MON 0` and close the port.

## Analog meter model

The needle position *x* (fraction of full scale) follows

    x'' = ωn² (target − x) − 2 ζ ωn x'      ωn = 13 rad/s, ζ = 0.55

integrated at 8 ms steps and drawn at ~60 fps while it moves. At the end stops
(−5.5 % / +105.5 %) the velocity is reversed and reduced to 25 % (bounce).
