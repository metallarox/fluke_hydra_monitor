// Fluke Hydra Web — ESP8266 (Wemos D1 mini / D1 mini Pro) firmware
//
// Drives a Fluke Hydra 2620A/2625A over RS-232 (MAX3232) and serves a web page
// that replicates the Fluke Hydra Monitor desktop app: readout, analog meter,
// chart, multi-channel acquisition, channel setup and a command console.
// It also exposes the serial line on a TCP port so the desktop app can connect
// over WiFi (socket://hydra.local:2625).
//
// https://github.com/metallarox/fluke_hydra_monitor  ·  IV3HGA
//
// Build: Arduino IDE (board "LOLIN(WEMOS) D1 mini Pro") or the Makefile/PlatformIO
// files in this folder. Settings are in config.h. No external libraries.

#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>
#include <ESP8266mDNS.h>
#include <DNSServer.h>
#include <ArduinoOTA.h>
#include <LittleFS.h>
#include <math.h>
#include "config.h"
#include "web_page.h"

#define FW_VERSION "1.0.0"

#if DEBUG_SERIAL1
#define DBG(...) Serial1.printf(__VA_ARGS__)
#else
#define DBG(...) do {} while (0)
#endif

HardwareSerial &HY = Serial;
ESP8266WebServer server(80);
DNSServer dns;
WiFiServer bridgeServer(BRIDGE_PORT);
WiFiClient bridgeClient;

// =============================================================== command queue
enum Kind : uint8_t {
  K_PLAIN,      // command, answer ignored
  K_IDN,        // *IDN?
  K_FUNCQ,      // FUNC? ch  -> shown as "instrument config"
  K_VALUE,      // reading for channel ch
  K_DISCARD,    // reading thrown away (Monitor just moved)
  K_DELAY,      // wait 'timeout' ms, no command
  K_SCANDONE,   // end of one scan / cycle
  K_CONSOLE     // raw command typed in the web console
};

struct Step {
  char cmd[48];
  uint16_t timeout;
  uint8_t kind;
  int8_t ch;
  bool setup;       // a failure stops the session
  uint16_t gen;     // session generation: results of an old session are ignored
};

const int QN = 100;
Step q[QN];
int qHead = 0, qCount = 0;

// current transaction
bool busy = false, inDelay = false;
Step cur;
uint32_t txStart = 0, delayUntil = 0;
char rx[384];
int rxLen = 0;

// =============================================================== readings ring
struct Reading {
  uint32_t seq;
  uint32_t t;       // millis() of the reading
  float v;          // NaN = overload
  uint16_t scan;
  int8_t ch;
};
const int RN = 600;
Reading ring[RN];
uint32_t lastSeq = 0;
uint16_t scanId = 0;

// =============================================================== session
enum Mode : uint8_t { M_LIVE, M_MONITOR, M_TRIGGER, M_MULTI };
struct Session {
  bool running = false;
  uint8_t mode = M_LIVE;
  bool hscan = false;
  uint32_t periodMs = 1000;
  uint8_t nch = 0;
  int8_t ch[21];
  uint16_t scan0 = 0;     // first scan id of the session
} S;
uint16_t gen = 1;
uint32_t nextAt = 0;
uint8_t errors = 0;
const uint8_t MAX_RETRY = 3;
bool monitoring = false;
String sessDesc = "null";     // JSON describing the session, written by the page

// =============================================================== instrument / status
bool instOk = false;
String idn = "";
String statusMsg = "Starting";
String errMsg = "";
String funcState = "";
uint32_t lastIdnPoll = 0;
bool bridgeActive = false;
bool apMode = false;
String consoleOut;
bool consoleDone = false;

// prototypes (also needed by the Arduino IDE in a few cases)
void push(const char *cmd, uint8_t kind, int8_t ch, uint16_t timeout, bool setup);
void stopEngine(const String &why, bool isError);

// =============================================================== helpers
String jsonEsc(const String &s) {
  String o;
  o.reserve(s.length() + 8);
  for (unsigned i = 0; i < s.length(); i++) {
    char c = s[i];
    if (c == '"' || c == '\\') { o += '\\'; o += c; }
    else if (c == '\n') o += "\\n";
    else if (c == '\r') continue;
    else if ((uint8_t)c < 0x20) o += ' ';
    else o += c;
  }
  return o;
}

bool parseNumber(const char *s, float &out) {
  for (const char *p = s; *p; p++) {
    if (strchr("+-.0123456789", *p)) {
      char *end;
      double d = strtod(p, &end);
      if (end != p && (isdigit(*(end - 1)) || *(end - 1) == '.')) {
        out = (fabs(d) >= 9e9) ? NAN : (float)d;     // Hydra overrange: huge value
        return true;
      }
    }
  }
  return false;
}

void clearQueue() { qHead = 0; qCount = 0; }

void push(const char *cmd, uint8_t kind, int8_t ch, uint16_t timeout, bool setup) {
  if (qCount >= QN) return;
  Step &s = q[(qHead + qCount) % QN];
  strncpy(s.cmd, cmd, sizeof(s.cmd) - 1);
  s.cmd[sizeof(s.cmd) - 1] = 0;
  s.kind = kind;
  s.ch = ch;
  s.timeout = timeout;
  s.setup = setup;
  s.gen = gen;
  qCount++;
}

void storeReading(int8_t ch, float v) {
  lastSeq++;
  Reading &r = ring[lastSeq % RN];
  r.seq = lastSeq;
  r.t = millis();
  r.v = v;
  r.ch = ch;
  r.scan = scanId;
}

// =============================================================== engine
bool chanInSession(int8_t ch) {
  for (int i = 0; i < S.nch; i++) if (S.ch[i] == ch) return true;
  return false;
}

// funcs: "ch|FUNC ...;ch|FUNC ..." built by the page
String startEngine(uint8_t mode, bool hscan, uint32_t periodMs, const String &funcs) {
  if (bridgeActive) return "PC bridge in use";
  gen++;
  clearQueue();
  S.running = false;
  S.mode = mode;
  S.hscan = (mode == M_MULTI) && hscan;
  S.periodMs = periodMs;
  S.nch = 0;
  errors = 0;
  errMsg = "";
  monitoring = false;

  push("MON 0", K_PLAIN, -1, 3500, true);
  push("SCAN 0", K_PLAIN, -1, 3500, true);
  int start = 0;
  while (start < (int)funcs.length() && S.nch < 21) {
    int end = funcs.indexOf(';', start);
    if (end < 0) end = funcs.length();
    String item = funcs.substring(start, end);
    start = end + 1;
    int bar = item.indexOf('|');
    if (bar < 1) continue;
    int ch = item.substring(0, bar).toInt();
    String cmd = item.substring(bar + 1);
    if (ch < 0 || ch > 20 || !cmd.startsWith("FUNC ") || chanInSession(ch)) continue;
    S.ch[S.nch++] = ch;
    push(cmd.c_str(), K_PLAIN, ch, 3500, true);
  }
  if (S.nch == 0) return "no channel";
  if (mode != M_MULTI) {
    S.nch = 1;
    char b[24];
    snprintf(b, sizeof(b), "FUNC? %d", S.ch[0]);
    push(b, K_FUNCQ, S.ch[0], 3500, true);
    if (mode == M_LIVE || mode == M_MONITOR) {
      snprintf(b, sizeof(b), "MON 1,%d", S.ch[0]);
      push(b, K_PLAIN, S.ch[0], 3500, true);
      monitoring = true;
    }
  } else if (S.hscan) {
    uint32_t sec = max<uint32_t>(1, (periodMs + 500) / 1000);
    S.periodMs = sec * 1000;
    char b[40];
    snprintf(b, sizeof(b), HSCAN_START_1, (unsigned)(sec / 3600), (unsigned)(sec / 60 % 60), (unsigned)(sec % 60));
    push(b, K_PLAIN, -1, 3500, true);
    push(HSCAN_START_2, K_PLAIN, -1, 3500, true);
    push(HSCAN_START_3, K_PLAIN, -1, 3500, true);
  }
  S.running = true;
  scanId++;
  S.scan0 = scanId;
  nextAt = millis() + (S.hscan ? 1000 : 0);     // with Hydra scan, let the first scan finish
  statusMsg = "Started";
  return "";
}

void stopEngine(const String &why, bool isError) {
  bool wasHscan = S.running && S.hscan;
  S.running = false;
  gen++;
  clearQueue();
  if (!bridgeActive) {
    push("MON 0", K_PLAIN, -1, 3500, false);
    if (wasHscan || isError) push("SCAN 0", K_PLAIN, -1, 3500, false);
  }
  monitoring = false;
  if (isError) errMsg = why;
  statusMsg = why;
}

void scheduleCycle() {
  char b[40];
  if (S.mode == M_LIVE || S.mode == M_MONITOR) {
    push("MON_VAL?", K_VALUE, S.ch[0], 2500, false);
  } else if (S.mode == M_TRIGGER) {
    push("*TRG", K_PLAIN, S.ch[0], 8000, false);
    snprintf(b, sizeof(b), "LAST? %d", S.ch[0]);
    push(b, K_VALUE, S.ch[0], 8000, false);
  } else if (!S.hscan) {                      // multi, sweep through the Monitor
    for (int i = 0; i < S.nch; i++) {
      int8_t ch = S.ch[i];
      snprintf(b, sizeof(b), "MON 1,%d", ch);
      push(b, K_PLAIN, ch, 3500, false);
      push("", K_DELAY, ch, SWEEP_SETTLE_MS, false);
      push("MON_VAL?", K_DISCARD, ch, 2500, false);   // may still belong to the previous channel
      push("MON_VAL?", K_VALUE, ch, 2500, false);
    }
    monitoring = true;
  } else {                                    // multi, Hydra-paced scan
    for (int i = 0; i < S.nch; i++) {
      snprintf(b, sizeof(b), HSCAN_READ, S.ch[i]);
      push(b, K_VALUE, S.ch[i], 3500, false);
    }
  }
  push("", K_SCANDONE, -1, 0, false);
}

void finishStep(bool ok, const char *prompt, const String &payload) {
  if (cur.kind == K_CONSOLE) {
    consoleOut = payload.length() ? payload + "\n" + prompt : String(prompt);
    consoleDone = true;
    return;
  }
  if (cur.kind == K_IDN) {
    instOk = ok;
    if (ok) idn = payload;
    return;
  }
  if (cur.gen != gen) return;                 // result of a stopped session
  if (!ok) {
    String why = String(cur.cmd) + ": " +
                 (prompt[0] == 0 ? "no response" : prompt[0] == '?' ? "syntax error (?>)" : "execution error (!>)");
    if (prompt[0] == 0) instOk = false;
    if (!S.running) return;
    if (cur.setup) { stopEngine(why, true); return; }
    errors++;
    statusMsg = "Read failed (" + String(errors) + "/" + String(MAX_RETRY) + "): " + why;
    // skip what is left of this channel in this scan
    while (qCount && q[qHead].ch == cur.ch && q[qHead].kind != K_SCANDONE) { qHead = (qHead + 1) % QN; qCount--; }
    if (errors >= MAX_RETRY) stopEngine(why, true);
    return;
  }
  instOk = true;
  if (cur.kind == K_FUNCQ) {
    funcState = payload;
  } else if (cur.kind == K_VALUE) {
    float v;
    if (!parseNumber(payload.c_str(), v)) {
      String up = payload;
      up.toUpperCase();
      if (up.indexOf("OL") >= 0) v = NAN;
      else {
        cur.setup = false;
        finishStep(false, "!", payload);    // counts as a failed read
        statusMsg = "Non-numeric reading: " + payload;
        return;
      }
    }
    storeReading(cur.ch, v);
    errors = 0;
    if (S.running && S.mode != M_MULTI) statusMsg = "Measuring";
  }
}

// Extract the answer: lines without the prompt and without the command echo.
void completeTransaction(const char *prompt) {
  rx[rxLen] = 0;
  String all(rx), payload;
  int start = 0;
  while (start < (int)all.length()) {
    int end = all.indexOf('\n', start);
    if (end < 0) end = all.length();
    String line = all.substring(start, end);
    start = end + 1;
    line.trim();
    if (!line.length() || line == "=>" || line == "?>" || line == "!>") continue;
    if (line.equalsIgnoreCase(cur.cmd)) continue;
    if (payload.length()) payload += '\n';
    payload += line;
  }
  bool ok = prompt[0] == '=';
  finishStep(ok, prompt, payload);
}

void processQueue() {
  if (bridgeActive) return;
  uint32_t now = millis();
  if (!busy) {
    if (S.running && qCount == 0 && (int32_t)(now - nextAt) >= 0) {
      scheduleCycle();
      nextAt += S.periodMs;
      if ((int32_t)(nextAt - now) < 0) nextAt = now + S.periodMs;    // fell behind: restart from now
    }
    if (!qCount) return;
    cur = q[qHead];
    qHead = (qHead + 1) % QN;
    qCount--;
    if (cur.kind == K_SCANDONE) { scanId++; return; }
    if (cur.kind == K_DELAY) { inDelay = true; busy = true; delayUntil = now + cur.timeout; return; }
    while (HY.available()) HY.read();
    rxLen = 0;
    HY.print(cur.cmd);
    HY.print('\r');
    txStart = now;
    busy = true;
    return;
  }
  if (inDelay) {
    if ((int32_t)(now - delayUntil) >= 0) { busy = false; inDelay = false; }
    return;
  }
  while (HY.available()) {
    char c = HY.read();
    if (c == '\r') continue;
    if (rxLen < (int)sizeof(rx) - 1) rx[rxLen++] = c;
  }
  // prompt at the end of the buffer (ignoring trailing spaces/newlines)?
  int e = rxLen;
  while (e > 0 && (rx[e - 1] == '\n' || rx[e - 1] == ' ')) e--;
  if (e >= 2 && rx[e - 1] == '>' && (rx[e - 2] == '=' || rx[e - 2] == '?' || rx[e - 2] == '!')) {
    char prompt[3] = {rx[e - 2], '>', 0};
    busy = false;
    completeTransaction(prompt);
  } else if (now - txStart > cur.timeout) {
    busy = false;
    finishStep(false, "", "");
  }
}

// =============================================================== PC bridge
void bridgeLoop() {
  if (bridgeServer.hasClient()) {
    WiFiClient c = bridgeServer.accept();
    if (bridgeActive && bridgeClient.connected()) {
      c.print("busy\r\n");
      c.stop();
    } else {
      if (S.running) stopEngine("PC bridge connected", false);
      clearQueue();
      busy = inDelay = false;
      bridgeClient = c;
      bridgeClient.setNoDelay(true);
      bridgeActive = true;
      while (HY.available()) HY.read();
      statusMsg = "PC bridge connected";
      DBG("bridge on\n");
    }
  }
  if (!bridgeActive) return;
  if (!bridgeClient.connected()) {
    bridgeActive = false;
    bridgeClient.stop();
    statusMsg = "PC bridge closed";
    lastIdnPoll = 0;
    DBG("bridge off\n");
    return;
  }
  uint8_t buf[128];
  size_t n;
  while ((n = bridgeClient.available()) > 0) {
    n = bridgeClient.read(buf, min(n, sizeof(buf)));
    HY.write(buf, n);
  }
  while ((n = HY.available()) > 0) {
    n = HY.readBytes(buf, min(n, sizeof(buf)));
    bridgeClient.write(buf, n);
  }
}

// =============================================================== HTTP API
const char *modeName(uint8_t m) {
  return m == M_LIVE ? "live" : m == M_MONITOR ? "monitor" : m == M_TRIGGER ? "trigger" : "multi";
}

void sendJson(int code, const String &body) {
  server.sendHeader("Cache-Control", "no-store");
  server.send(code, "application/json", body);
}

void handleRoot() {
  server.sendHeader("Content-Encoding", "gzip");
  server.sendHeader("Cache-Control", "no-cache");
  server.send_P(200, "text/html", (PGM_P)WEB_PAGE_GZ, WEB_PAGE_GZ_LEN);
}

void handleStatus() {
  String j;
  j.reserve(700 + sessDesc.length());
  j = "{\"fw\":\"" FW_VERSION "\",\"now\":" + String(millis());
  j += ",\"inst\":" + String(instOk ? "true" : "false");
  j += ",\"idn\":\"" + jsonEsc(idn) + "\"";
  j += ",\"run\":" + String(S.running ? "true" : "false");
  j += ",\"mode\":\"" + String(modeName(S.mode)) + "\"";
  j += ",\"method\":\"" + String(S.hscan ? "hscan" : "sweep") + "\"";
  j += ",\"period\":" + String(S.periodMs);
  j += ",\"ch\":[";
  for (int i = 0; i < S.nch; i++) { if (i) j += ','; j += String(S.ch[i]); }
  j += "],\"desc\":" + sessDesc;
  j += ",\"seq\":" + String(lastSeq);
  j += ",\"scan\":" + String(scanId);
  j += ",\"scan0\":" + String(S.scan0);
  j += ",\"msg\":\"" + jsonEsc(statusMsg) + "\"";
  j += ",\"err\":\"" + jsonEsc(errMsg) + "\"";
  j += ",\"func\":\"" + jsonEsc(funcState) + "\"";
  j += ",\"bridge\":" + String(bridgeActive ? "true" : "false");
  j += ",\"bridgePort\":" + String(BRIDGE_PORT);
  j += ",\"ap\":" + String(apMode ? "true" : "false");
  j += ",\"ssid\":\"" + jsonEsc(WiFi.SSID()) + "\"";
  j += ",\"ip\":\"" + (WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString() : WiFi.softAPIP().toString()) + "\"";
  j += ",\"rssi\":" + String(WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : 0);
  j += ",\"heap\":" + String(ESP.getFreeHeap());
  j += ",\"host\":\"" HOSTNAME "\"}";
  sendJson(200, j);
}

void handleReadings() {
  uint32_t since = server.hasArg("since") ? strtoul(server.arg("since").c_str(), nullptr, 10) : 0;
  uint32_t first = lastSeq > RN - 1 ? lastSeq - (RN - 1) : 1;
  if (since + 1 > first) first = since + 1;
  const uint32_t LIMIT = 200;
  uint32_t last = lastSeq;
  bool more = false;
  if (last >= first && last - first + 1 > LIMIT) { last = first + LIMIT - 1; more = true; }
  server.setContentLength(CONTENT_LENGTH_UNKNOWN);
  server.sendHeader("Cache-Control", "no-store");
  server.send(200, "application/json", "");
  String chunk = "{\"now\":" + String(millis()) + ",\"seq\":" + String(last) +
                 ",\"more\":" + (more ? "true" : "false") + ",\"r\":[";
  bool firstItem = true;
  for (uint32_t s = first; s <= last && last >= first; s++) {
    const Reading &r = ring[s % RN];
    if (r.seq != s) continue;
    char b[64];
    if (isnan(r.v)) snprintf(b, sizeof(b), "%s[%u,%u,%d,null,%u]", firstItem ? "" : ",", (unsigned)r.seq, (unsigned)r.t, r.ch, (unsigned)r.scan);
    else snprintf(b, sizeof(b), "%s[%u,%u,%d,%.7g,%u]", firstItem ? "" : ",", (unsigned)r.seq, (unsigned)r.t, r.ch, (double)r.v, (unsigned)r.scan);
    chunk += b;
    firstItem = false;
    if (chunk.length() > 1200) { server.sendContent(chunk); chunk = ""; }
  }
  chunk += "]}";
  server.sendContent(chunk);
  server.sendContent("");
}

void handleStart() {
  String m = server.arg("mode");
  uint8_t mode = m == "monitor" ? M_MONITOR : m == "trigger" ? M_TRIGGER : m == "multi" ? M_MULTI : M_LIVE;
  uint32_t period = server.arg("period").toInt();
  if (period < 500) period = 500;
  if (period > 3600000UL) period = 3600000UL;
  String desc = server.arg("desc");
  sessDesc = (desc.startsWith("{") && desc.endsWith("}") && desc.length() < 1500) ? desc : String("null");
  String err = startEngine(mode, server.arg("method") == "hscan", period, server.arg("f"));
  if (err.length()) { sendJson(409, "{\"ok\":false,\"err\":\"" + jsonEsc(err) + "\"}"); return; }
  sendJson(200, "{\"ok\":true}");
}

void handleStop() {
  if (S.running) stopEngine("Measurement stopped", false);
  sendJson(200, "{\"ok\":true}");
}

void handleCmd() {
  if (bridgeActive) { sendJson(409, "{\"ok\":false,\"err\":\"PC bridge in use\"}"); return; }
  if (S.running) { sendJson(409, "{\"ok\":false,\"err\":\"stop the measurement first\"}"); return; }
  String c = server.arg("c");
  c.trim();
  if (!c.length() || c.length() > 46) { sendJson(400, "{\"ok\":false,\"err\":\"bad command\"}"); return; }
  consoleDone = false;
  consoleOut = "";
  push(c.c_str(), K_CONSOLE, -1, 4000, false);
  uint32_t t0 = millis();
  while (!consoleDone && millis() - t0 < 6000) { processQueue(); yield(); }
  if (!consoleDone) consoleOut = "(no response)";
  sendJson(200, "{\"ok\":true,\"out\":\"" + jsonEsc(consoleOut) + "\"}");
}

void handleConfigGet() {
  File f = LittleFS.open("/channels.json", "r");
  if (!f) { sendJson(200, "{}"); return; }
  server.sendHeader("Cache-Control", "no-store");
  server.streamFile(f, "application/json");
  f.close();
}

void handleConfigPost() {
  String body = server.arg("plain");
  if (!body.startsWith("{") || body.length() > 6000) { sendJson(400, "{\"ok\":false}"); return; }
  File f = LittleFS.open("/channels.json", "w");
  if (f) { f.print(body); f.close(); }
  sendJson(200, "{\"ok\":true}");
}

void handleWifiPost() {
  String ssid = server.arg("ssid"), pass = server.arg("pass");
  if (!ssid.length() || ssid.length() > 32 || pass.length() > 64) { sendJson(400, "{\"ok\":false}"); return; }
  File f = LittleFS.open("/wifi.txt", "w");
  if (f) { f.print(ssid + "\n" + pass + "\n"); f.close(); }
  sendJson(200, "{\"ok\":true}");
  delay(300);
  ESP.restart();
}

void handleReboot() {
  sendJson(200, "{\"ok\":true}");
  delay(300);
  ESP.restart();
}

void handleNotFound() {
  if (apMode) {                         // captive portal: send everything to the page
    server.sendHeader("Location", "http://" + WiFi.softAPIP().toString() + "/");
    server.send(302, "text/plain", "");
    return;
  }
  sendJson(404, "{\"ok\":false,\"err\":\"not found\"}");
}

// =============================================================== WiFi
void startWifi() {
  String ssid = WIFI_SSID, pass = WIFI_PASSWORD;
  File f = LittleFS.open("/wifi.txt", "r");
  if (f) {
    String s = f.readStringUntil('\n'), p = f.readStringUntil('\n');
    f.close();
    s.trim();
    p.trim();
    if (s.length()) { ssid = s; pass = p; }
  }
  WiFi.persistent(false);
  WiFi.hostname(HOSTNAME);
  if (ssid.length()) {
    WiFi.mode(WIFI_STA);
    WiFi.begin(ssid.c_str(), pass.c_str());
    uint32_t t0 = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) { delay(100); }
  }
  if (WiFi.status() != WL_CONNECTED) {
    // access point; with a saved network the STA keeps retrying in the background
    WiFi.mode(ssid.length() ? WIFI_AP_STA : WIFI_AP);
    char apName[32];
    snprintf(apName, sizeof(apName), "%s%04X", AP_SSID_PREFIX, (unsigned)(ESP.getChipId() & 0xFFFF));
    WiFi.softAP(apName, AP_PASSWORD);
    dns.start(53, "*", WiFi.softAPIP());
    apMode = true;
    DBG("AP %s\n", apName);
  }
  MDNS.begin(HOSTNAME);
  MDNS.addService("http", "tcp", 80);
}

// =============================================================== setup / loop
void setup() {
#if DEBUG_SERIAL1
  Serial1.begin(115200);
#endif
  HY.setRxBufferSize(512);
  HY.begin(HYDRA_BAUD, SERIAL_8N1);
#if HYDRA_UART_SWAP
  HY.swap();
#endif
  LittleFS.begin();
  startWifi();

  server.on("/", HTTP_GET, handleRoot);
  server.on("/api/status", HTTP_GET, handleStatus);
  server.on("/api/readings", HTTP_GET, handleReadings);
  server.on("/api/start", HTTP_POST, handleStart);
  server.on("/api/stop", HTTP_POST, handleStop);
  server.on("/api/cmd", HTTP_POST, handleCmd);
  server.on("/api/config", HTTP_GET, handleConfigGet);
  server.on("/api/config", HTTP_POST, handleConfigPost);
  server.on("/api/wifi", HTTP_POST, handleWifiPost);
  server.on("/api/reboot", HTTP_POST, handleReboot);
  server.onNotFound(handleNotFound);
  server.begin();
  bridgeServer.begin();
  bridgeServer.setNoDelay(true);

  ArduinoOTA.setHostname(HOSTNAME);
  if (strlen(OTA_PASSWORD)) ArduinoOTA.setPassword(OTA_PASSWORD);
  ArduinoOTA.onStart([]() { if (S.running) stopEngine("Firmware update", false); });
  ArduinoOTA.begin();

  // Ctrl-C cancels any half-received command (and the boot noise on TX)
  delay(200);
  HY.write((uint8_t)0x03);
  HY.flush();
  delay(300);
  while (HY.available()) HY.read();
  push("*IDN?", K_IDN, -1, 3500, false);
  lastIdnPoll = millis();
  statusMsg = "Ready";
}

void loop() {
  server.handleClient();
  if (apMode) dns.processNextRequest();
  MDNS.update();
  ArduinoOTA.handle();
  bridgeLoop();
  // keep the identification fresh while idle (also detects a disconnected cable)
  if (!S.running && !bridgeActive && !busy && qCount == 0 && millis() - lastIdnPoll > 10000) {
    push("*IDN?", K_IDN, -1, 3500, false);
    lastIdnPoll = millis();
  }
  processQueue();
}
