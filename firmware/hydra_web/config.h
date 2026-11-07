// Fluke Hydra Web — user settings
// Edit, then build and flash. WiFi credentials can also be set later from the web page.
#pragma once

// ---- WiFi -----------------------------------------------------------------
// Network to join at boot. Leave empty to configure it from the web page
// (connect to the access point below and open http://192.168.4.1).
#define WIFI_SSID      ""
#define WIFI_PASSWORD  ""

// Access point started when the network above cannot be joined (or is not set).
// The name gets the last 4 hex digits of the chip id: e.g. "Hydra-3F2A".
#define AP_SSID_PREFIX "Hydra-"
#define AP_PASSWORD    "hydra2625"     // at least 8 characters

#define HOSTNAME       "hydra"         // http://hydra.local (mDNS) and OTA name
#define OTA_PASSWORD   ""              // empty = OTA without password

// ---- Serial link to the Hydra (through the MAX3232) -------------------------
// 0 = hardware TX/RX pins (GPIO1/GPIO3). Disconnect the MAX3232 RX line while
//     flashing over USB if the upload fails.
// 1 = swapped UART0: TX on D8 (GPIO15), RX on D7 (GPIO13). USB stays free for
//     flashing and the boot messages do not reach the Hydra.
#define HYDRA_UART_SWAP 0
#define HYDRA_BAUD      9600

// Optional debug log on Serial1 (TX only, pin D4 / GPIO2, 115200 baud).
#define DEBUG_SERIAL1   0

// ---- PC bridge --------------------------------------------------------------
// TCP port that exposes the Hydra serial line to the desktop app:
//   python3 fluke_hydra_monitor.py  ->  port  socket://hydra.local:2625
// While a PC is connected, the web page shows the bridge as busy.
#define BRIDGE_PORT     2625

// ---- Hydra-paced scanning (method "Hydra scan") ------------------------------
// Same syntax as HYDRA_SCAN_* in the desktop app; not yet verified on a real unit.
#define HSCAN_START_1   "INTVL %u,%u,%u"      // hours, minutes, seconds
#define HSCAN_START_2   "TRIGGER 1"
#define HSCAN_START_3   "SCAN 1"
#define HSCAN_READ      "LAST? %d"

#define SWEEP_SETTLE_MS 300   // after moving the Monitor to a new channel
