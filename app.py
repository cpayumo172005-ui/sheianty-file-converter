"""
ESP32 Sound & Weather Dashboard (matches firmware v4.1)

Run on Raspberry Pi:
    python dashboard.py
Then open http://localhost:5000 or http://<your-pi-ip>:5000 in a browser.

Needs: pip install flask
"""
import math
import random
import re
import sys
import threading
import time
from collections import deque

from flask import Flask, Response, jsonify, request

try:
    import serial
except ImportError:
    serial = None

app = Flask(__name__)

# ---------------------------------------------------------------- settings --
PORT = "/dev/ttyUSB0"    # Default serial port on Linux/Raspberry Pi
BAUD = 115200
VERBOSE = False          # True = print every line received from the ESP32

MIN_DB, MAX_DB = 15, 75  # meter range. The firmware dB is RELATIVE
BUMP_MAX_HZ = 250        # IMPACT below this pitch  -> "bump"
SNAP_MIN_HZ = 2200       # IMPACT above this pitch  -> "snap", otherwise "clap"
PEAK_TO_RMS = 2.83       # used only to draw the clap trigger on the dB meter
CONNECTED_TIMEOUT = 8    # seconds without a report before showing "waiting"

# ------------------------------------------------------------------- state --
lock = threading.Lock()
ser = None
ser_lock = threading.Lock()

latest = {
    "pp": 0, "db": 0.0, "amb_db": 0.0, "hz": 0, "bands": [0, 0, 0, 0, 0],
    "low": 0, "crest": 0.0, "act": 0, "syl": 0, "audio": "QUIET",
    "max_rms": 0.0, "act_thr": 0.0,
    "rain_raw": 4095, "rain_pct": 0, "rain_label": "DRY", "trend": "steady",
    "drops": 0, "dry_sec": 0, "alert": False, "clip": False,
}
cal = {"clap_thr": None, "noise_rms": None, "noise_pp": None}
events = deque(maxlen=8)
event_counter = 0
last_update = 0.0
seq = 0
prev_pct = 0

# --------------------------------------------------------------- parsing ----
RE_PP = re.compile(r"PP:\s*(\d+)")
RE_DB = re.compile(r"([\d.]+)\s*dB\s*\(x")
RE_AMB = re.compile(r"Ambient:\s*([\d.]+)\s*dB")
RE_HZ = re.compile(r"~\s*(\d+)\s*Hz")
RE_BANDS = re.compile(r"bands:((?:\s+\S+=[\d.]+){5})")
RE_LOW = re.compile(r"low:\s*(\d+)%")
RE_CREST = re.compile(r"Crest:\s*([\d.]+)")
RE_ACT = re.compile(r"act:\s*(\d+)\s+syl:\s*(\d+)")
RE_AUDIO = re.compile(r"Audio:\s*([A-Za-z_]+)")
RE_RMS = re.compile(r"maxRms:\s*([\d.]+)\s+thr:\s*([\d.]+)")
RE_RAIN = re.compile(r"Rain:\s*(\d+)\s*\(\s*(\d+)\s*%\s*([A-Za-z]+)\s*,\s*([A-Za-z]+)\s*\)")
RE_DROPS = re.compile(r"Drops:\s*(\d+)")
RE_DRY = re.compile(r"DryFor:\s*(\d+)")
RE_CAL = {
    "clap_thr": re.compile(r"clapThr=(\d+)"),
    "noise_rms": re.compile(r"noiseRms=([\d.]+)"),
    "noise_pp": re.compile(r"noisePP=(\d+)"),
}

def add_event(kind):
    global event_counter
    event_counter += 1
    events.appendleft({"id": event_counter, "type": kind, "ts": time.time()})

def impact_kind(hz):
    if hz and hz < BUMP_MAX_HZ:
        return "bump"
    if hz >= SNAP_MIN_HZ:
        return "snap"
    return "clap"

def update(d):
    global last_update, seq, prev_pct
    with lock:
        prev_audio = latest["audio"]
        if "trend" not in d:
            delta = d["rain_pct"] - prev_pct
            d["trend"] = "wetter" if delta > 5 else "drying" if delta < -5 else "steady"
        prev_pct = d["rain_pct"]
        latest.update(d)

        audio = d["audio"]
        if audio == "IMPACT":
            add_event(impact_kind(d["hz"]))
        elif audio == "LOUD" and prev_audio != "LOUD":
            add_event("sound")
        if d.get("pattern") == 2:
            add_event("double")
        elif d.get("pattern", 0) >= 3:
            add_event("triple")

        last_update = time.time()
        seq += 1

def parse_human(line):
    m_pp, m_rain = RE_PP.search(line), RE_RAIN.search(line)
    if not (m_pp and m_rain):
        return None
    g = lambda rx, default, cast=float: cast(rx.search(line).group(1)) if rx.search(line) else default
    bands = [0.0] * 5
    mb = RE_BANDS.search(line)
    if mb:
        bands = [float(v) for _, v in re.findall(r"(\S+)=([\d.]+)", mb.group(1))]
    ma = RE_ACT.search(line)
    mr = RE_RMS.search(line)
    return {
        "pp": int(m_pp.group(1)),
        "db": g(RE_DB, 0.0),
        "amb_db": g(RE_AMB, 0.0),
        "hz": g(RE_HZ, 0, int),
        "bands": bands,
        "low": g(RE_LOW, 0, int),
        "crest": g(RE_CREST, 0.0),
        "act": int(ma.group(1)) if ma else 0,
        "syl": int(ma.group(2)) if ma else 0,
        "audio": RE_AUDIO.search(line).group(1) if RE_AUDIO.search(line) else "QUIET",
        "max_rms": float(mr.group(1)) if mr else 0.0,
        "act_thr": float(mr.group(2)) if mr else 0.0,
        "rain_raw": int(m_rain.group(1)),
        "rain_pct": int(m_rain.group(2)),
        "rain_label": m_rain.group(3).upper(),
        "trend": m_rain.group(4).lower(),
        "drops": g(RE_DROPS, 0, int),
        "dry_sec": g(RE_DRY, 0, int),
        "alert": "[ALERT" in line,
        "clip": "[CLIPPING" in line,
    }

def parse_line(line):
    try:
        if line.startswith("[CAL]"):
            with lock:
                for key, rx in RE_CAL.items():
                    m = rx.search(line)
                    if m:
                        cal[key] = float(m.group(1))
        elif ">>> DOUBLE" in line:
            with lock:
                add_event("double")
        elif ">>> TRIPLE" in line:
            with lock:
                add_event("triple")
        elif "PP:" in line and "Rain:" in line:
            d = parse_human(line)
            if d:
                update(d)
    except Exception as e:
        print(f"Parsing error: {e} on line: {line}")

# ------------------------------------------------------------ data sources --
def send_cmd(ch):
    with ser_lock:
        if ser is None:
            return False
        try:
            ser.write(ch.encode())
            return True
        except Exception:
            return False

def read_serial(port):
    global ser
    while True:
        try:
            s = serial.Serial(port, BAUD, timeout=1)
            with ser_lock:
                ser = s
            print(f"Connected to ESP32 on USB {port}")
            t_conn, asked = time.time(), 0.0
            while True:
                line = s.readline().decode("utf-8", errors="ignore").strip()
                if line:
                    if VERBOSE:
                        print(f"ESP32 Serial: {line}")
                    parse_line(line)
                if cal["clap_thr"] is None and time.time() - t_conn > 5 and time.time() - asked > 5:
                    asked = time.time()
                    send_cmd("d")
        except Exception as e:
            with ser_lock:
                ser = None
            time.sleep(3)

def run_demo():
    print("Demo mode: generating fake sensor data")
    parse_line("[CAL] fs=8000Hz baseline=2048 noisePP=14 noiseRms=2.8 (36.7 dB) clapThr=192 clapRel=96 voiceRatio=1.8 floor=0.8")
    t0, dry, prev, fired = time.time(), 0, 0, False
    while True:
        t = time.time() - t0
        c = t % 30
        if c < 8 or 16 <= c < 20 or c >= 25:
            audio, pp, rms, hz, act, syl = "QUIET", random.randint(8, 18), random.uniform(2, 3.5), random.randint(100, 500), 0, 0
        elif c < 15:
            audio, pp, rms, hz, act, syl = "VOICE", random.randint(120, 400), random.uniform(25, 60), random.randint(150, 400), 25, 4
        elif c < 16:
            audio, pp, rms, hz, act, syl = "IMPACT", random.randint(1500, 2800), random.uniform(300, 700), random.choice([120, 1200, 3000]), 3, 1
        else:
            audio, pp, rms, hz, act, syl = "NOISE_STEADY", random.randint(50, 80), random.uniform(15, 20), 300, 40, 0

        r = t % 48
        pct = 0 if r < 8 else 12 if r < 16 else 45 if r < 28 else 82 if r < 40 else 3
        label = "DRY" if pct < 5 else "MIST" if pct < 30 else "WET" if pct < 65 else "SOAKED"
        trend = "wetter" if pct - prev > 5 else "drying" if pct - prev < -5 else "steady"
        prev = pct
        dry = dry + 1 if pct < 5 else 0
        raw = int(4095 - pct * 33)
        db = 20 * math.log10(max(rms / 2048.0, 0.0001)) + 90
        b = [rms * random.uniform(0.2, 1.2) for _ in range(5)]
        line = (
            f"PP:{pp} | {db:.1f} dB (x{rms / 2.8:.1f}) | Ambient:37.0 dB | ~{hz}Hz | bands:"
            + "".join(f" {n}={v:.1f}" for n, v in zip(["150", "300", "600", "1.2k", "2.4k"], b))
            + f" | low:{random.randint(40, 90)}% | Crest:{random.uniform(1.5, 5):.1f} | act:{act} syl:{syl} | Audio:{audio} | "
            + f"maxRms:{rms:.1f} thr:5.0 | "
            + f"Rain:{raw} ({pct}% {label}, {trend}) | Drops:{random.randint(0, 3) if pct >= 30 else 0} | DryFor:{dry}s"
        )
        if audio == "IMPACT" and pct >= 30:
            line += " [ALERT: loud event + liquid]"
        parse_line(line)
        time.sleep(1)

# ------------------------------------------------------------------ routes --
@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")

@app.route("/api/data", methods=["POST"])
def api_data():
    """Receives Wi-Fi HTTP POST requests directly from the ESP32."""
    line = request.data.decode("utf-8", errors="ignore").strip()
    if line:
        if VERBOSE:
            print(f"ESP32 Wi-Fi: {line}")
        parse_line(line)
        return jsonify({"status": "ok"}), 200
    return jsonify({"status": "error", "message": "empty line"}), 400

@app.route("/data")
def data():
    with lock:
        thr_db = None
        if cal["clap_thr"]:
            thr_db = round(20 * math.log10(max(cal["clap_thr"] / PEAK_TO_RMS, 0.5) / 2048.0) + 90, 1)
        out = dict(latest)
        out.update({
            "connected": (time.time() - last_update) < CONNECTED_TIMEOUT,
            "seq": seq,
            "min_db": MIN_DB, "max_db": MAX_DB,
            "thresh_db": thr_db,
            "clap_thr": cal["clap_thr"],
            "events": list(events),
        })
    return jsonify(out)

@app.route("/cmd", methods=["POST"])
def cmd():
    ch = str((request.get_json(silent=True) or {}).get("cmd", ""))
    if ch not in {"c", "w", "+", "-", "v", "V", "f", "F", "d"}:
        return jsonify({"ok": False, "error": "unknown command"}), 400
    if ch == "c":
        with lock:
            cal["clap_thr"] = None
    ok = send_cmd(ch)
    return jsonify({"ok": ok})

# -------------------------------------------------------------------- page --
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sound &amp; Weather Station</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #E9F0F3; --card: #FFFFFF; --ink: #19303D; --muted: #5E7482;
    --line: #D6E1E7; --sound: #E9853D; --sound-soft: #FCEBDD;
    --water: #2F7FA8; --ok: #3F9E7A; --alert: #C8473B; --radius: 24px;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; }
  body { font-family: "Nunito", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--ink); padding: 28px clamp(16px, 4vw, 48px) 48px; line-height: 1.45; }
  header { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; max-width: 1120px; margin: 0 auto 22px; }
  h1 { font-size: clamp(1.5rem, 3vw, 2rem); font-weight: 800; margin: 0; letter-spacing: -0.01em; }
  .right { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .status { display: inline-flex; align-items: center; gap: 8px; background: var(--card); border: 1px solid var(--line); padding: 7px 14px; border-radius: 999px; font-weight: 600; font-size: .9rem; color: var(--muted); }
  .status i { width: 9px; height: 9px; border-radius: 50%; background: #B8C6CF; transition: background .4s; }
  .status.on i { background: var(--ok); }
  .status.on { color: var(--ink); }
  button.btn { font: inherit; font-weight: 700; font-size: .85rem; color: var(--ink); background: var(--card); border: 1px solid var(--line); padding: 7px 14px; border-radius: 999px; cursor: pointer; }
  button.btn:hover { background: #F3F7F9; }
  button.btn.sent { background: var(--sound-soft); }

  .banner { max-width: 1120px; margin: 0 auto 18px; padding: 14px 20px; border-radius: 16px; background: #FBE3DF; border: 1.5px solid #E7A59D; color: #7B221A; font-weight: 700; display: none; }
  .banner.show { display: flex; gap: 12px; align-items: center; animation: nudge 1.6s ease-in-out infinite; }
  .banner.clip { background: #FFF3D6; border-color: #E8C873; color: #6B4E06; margin-bottom: 18px; }
  @keyframes nudge { 50% { transform: translateY(-2px); } }

  main { max-width: 1120px; margin: 0 auto; display: grid; grid-template-columns: 1fr 1fr; gap: 22px; }
  @media (max-width: 860px) { main { grid-template-columns: 1fr; } }
  .card { background: var(--card); border-radius: var(--radius); padding: 28px; position: relative; overflow: hidden; box-shadow: 0 1px 2px rgba(25,48,61,.06), 0 12px 32px -16px rgba(25,48,61,.22); min-height: 500px; display: flex; flex-direction: column; }
  .card h2 { margin: 0; font-size: 1rem; font-weight: 700; color: var(--muted); }

  .headline { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin: 10px 0 4px; }
  .big { font-size: clamp(2rem, 4vw, 2.6rem); font-weight: 800; letter-spacing: -0.02em; }
  .db { font-size: 1.1rem; font-weight: 700; color: var(--muted); font-variant-numeric: tabular-nums; }
  .hint { color: var(--muted); margin: 0 0 18px; }
  .hint.lvl { margin: -12px 0 18px; font-size: .82rem; font-weight: 600; font-variant-numeric: tabular-nums; }

  .meter { position: relative; height: 26px; border-radius: 13px; background: #EEF3F5; margin-top: 14px; }
  .meter-fill { position: absolute; inset: 0; border-radius: 13px; background: linear-gradient(90deg, #7CC5A6 0%, #E7C95A 55%, #E9853D 78%, #D9534A 100%); clip-path: inset(0 100% 0 0 round 13px); }
  .peak { position: absolute; top: 4px; bottom: 4px; width: 4px; border-radius: 2px; background: var(--ink); opacity: .55; left: 0; }
  .trigger { position: absolute; top: -8px; bottom: -8px; width: 0; border-left: 2px dashed var(--ink); left: 0; transition: left .4s; display: none; }
  .trigger span { position: absolute; top: 100%; left: 50%; transform: translateX(-50%); margin-top: 4px; font-size: .78rem; font-weight: 700; white-space: nowrap; }
  .scale { display: flex; justify-content: space-between; margin-top: 28px; font-size: .8rem; color: var(--muted); font-weight: 600; }
  .ctrl-inline { display: flex; gap: 6px; align-items: center; }

  canvas.history { width: 100%; height: 80px; display: block; margin-top: 6px; }

  .bands { display: flex; gap: 10px; align-items: flex-end; height: 64px; margin-top: 14px; }
  .bands div { flex: 1; text-align: center; font-size: .72rem; font-weight: 700; color: var(--muted); display: flex; flex-direction: column; justify-content: flex-end; height: 100%; }
  .bands b { display: block; width: 100%; background: var(--sound); border-radius: 6px 6px 2px 2px; opacity: .85; transition: height .5s; min-height: 3px; height: 3px; margin-bottom: 4px; }

  .row-label { display: flex; justify-content: space-between; align-items: center; margin-top: 18px; font-weight: 700; color: var(--muted); font-size: .9rem; }
  .chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; min-height: 36px; }
  .chip { display: inline-flex; gap: 7px; align-items: center; background: var(--sound-soft); color: #8A4310; padding: 6px 12px; border-radius: 999px; font-weight: 700; font-size: .9rem; animation: pop .35s ease-out; }
  .chip small { font-weight: 600; opacity: .7; }
  .empty { color: var(--muted); font-size: .92rem; align-self: center; }
  @keyframes pop { from { transform: scale(.7); opacity: 0; } }

  .burst { position: absolute; left: 50%; top: 42%; transform: translate(-50%, -50%) scale(.6); font-size: 3.4rem; font-weight: 800; pointer-events: none; opacity: 0; text-align: center; color: var(--sound); text-shadow: 0 2px 0 #fff, 0 0 24px #fff; }
  .burst.go { animation: burst 1.3s ease-out; }
  @keyframes burst { 0% { opacity: 0; transform: translate(-50%,-50%) scale(.5); } 15% { opacity: 1; transform: translate(-50%,-50%) scale(1.1); } 70% { opacity: 1; } 100% { opacity: 0; transform: translate(-50%,-70%) scale(1); } }
  .card.flash::after { content: ""; position: absolute; inset: 0; border-radius: var(--radius); box-shadow: inset 0 0 0 4px var(--sound); animation: ring 1s ease-out forwards; pointer-events: none; }
  @keyframes ring { to { opacity: 0; } }

  .weather { transition: background-color 1.6s ease; background-color: #F6EBD0; }
  .weather[data-state="mist"] { background-color: #DCE8EE; }
  .weather[data-state="wet"] { background-color: #BCD1DF; }
  .weather[data-state="soaked"] { background-color: #8FAABD; }
  .weather canvas { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }
  .sun { position: absolute; right: -40px; top: -40px; width: 220px; height: 220px; border-radius: 50%; background: radial-gradient(circle, #FFE9A8 0%, rgba(255,233,168,.55) 40%, rgba(255,233,168,0) 70%); opacity: 0; transition: opacity 1.6s; }
  .weather[data-state="dry"] .sun { opacity: 1; }
  .w-top, .w-panel { position: relative; z-index: 1; }
  .w-top h2 { color: #3F5563; }
  .w-state { display: flex; align-items: center; gap: 14px; margin-top: 10px; }
  .w-icon { font-size: 3rem; line-height: 1; }
  .w-text { color: #34505F; font-weight: 600; margin: 6px 0 0; max-width: 28ch; }
  .w-panel { margin-top: auto; background: rgba(255,255,255,.72); backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px); border-radius: 18px; padding: 18px 20px; }
  .bar { height: 12px; border-radius: 6px; background: rgba(25,48,61,.1); overflow: hidden; margin: 8px 0 14px; }
  .bar > div { height: 100%; width: 0; border-radius: 6px; background: var(--water); transition: width 1s ease; }
  .facts { display: flex; justify-content: space-between; gap: 12px; font-weight: 700; }
  .facts small { display: block; font-weight: 600; color: var(--muted); }
  .pct-line { display: flex; justify-content: space-between; font-weight: 700; }

  @media (prefers-reduced-motion: reduce) { .banner.show, .chip, .burst.go, .card.flash::after { animation: none; } }
</style>
</head>
<body>
<header>
  <h1>Sound &amp; Weather Station</h1>
  <div class="right">
    <button class="btn" data-cmd="c" title="Keep the room quiet and the rain board dry">Recalibrate</button>
    <button class="btn" data-cmd="w" title="Press while the rain board is fully wet">Set wet level</button>
    <button class="btn" data-cmd="f" title="Detect quieter sounds (lowers the activity floor)">Sensitivity +</button>
    <button class="btn" data-cmd="F" title="Ignore quieter sounds (raises the activity floor)">Sensitivity −</button>
    <div class="status" id="status"><i></i><span>Waiting for sensor…</span></div>
  </div>
</header>

<div class="banner" id="banner" role="status"><span style="font-size:1.4rem">⚠️</span><span>Heads up: water was detected at the same time as a loud noise.</span></div>
<div class="banner clip" id="clip" role="status"><span style="font-size:1.4rem">📉</span><span>The microphone is clipping. Turn the gain pot on the sound module down a little.</span></div>

<main>
  <section class="card" id="scard">
    <h2>🔊 Sound</h2>
    <div class="headline">
      <div class="big" id="s-word">Listening…</div>
      <div class="db"><span id="s-db">--</span> dB</div>
    </div>
    <p class="hint" id="s-hint">Waiting for the microphone.</p>
    <p class="hint lvl" id="s-lvl"></p>

    <div class="meter" aria-label="Current sound level">
      <div class="meter-fill" id="fill"></div>
      <div class="peak" id="peak"></div>
      <div class="trigger" id="trig"><span>Clap trigger</span></div>
    </div>
    <div class="scale">
      <span>Quiet</span>
      <span class="ctrl-inline">Trigger
        <button class="btn" data-cmd="-" title="More sensitive">−</button>
        <button class="btn" data-cmd="+" title="Less sensitive">+</button>
      </span>
      <span>Very loud</span>
    </div>

    <canvas class="history" id="hist"></canvas>

    <div class="bands" id="bands">
      <div><b></b>150</div><div><b></b>300</div><div><b></b>600</div><div><b></b>1.2k</div><div><b></b>2.4k</div>
    </div>

    <div class="row-label"><span>Recent sudden sounds</span><span id="pitch" style="font-weight:600"></span></div>
    <div class="chips" id="chips"><span class="empty">Clap, snap, or knock to try it.</span></div>
    <div class="burst" id="burst"></div>
  </section>

  <section class="card weather" id="wcard" data-state="dry">
    <div class="sun"></div>
    <canvas id="rain"></canvas>
    <div class="w-top">
      <h2>🌧️ Moisture</h2>
      <div class="w-state"><span class="w-icon" id="w-icon">☀️</span><span class="big" id="w-word">All dry</span></div>
      <p class="w-text" id="w-text">No moisture on the sensor.</p>
    </div>
    <div class="w-panel">
      <div class="pct-line"><span>How wet</span><span id="w-pct">0%</span></div>
      <div class="bar"><div id="w-bar"></div></div>
      <div class="facts">
        <div><small>Right now</small><span id="w-trend">Holding steady</span></div>
        <div style="text-align:center"><small>Drops this second</small><span id="w-drops">0</span></div>
        <div style="text-align:right"><small>Time since last moisture</small><span id="w-dry">--</span></div>
      </div>
    </div>
  </section>
</main>

<script>
(() => {
  const $ = id => document.getElementById(id);
  const lerp = (a, b, k) => a + (b - a) * k;
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ======================= SOUND ======================= */
  const EVENT_INFO = {
    clap:   { icon: "👏", label: "Clap" },
    snap:   { icon: "✨", label: "Snap" },
    bump:   { icon: "💥", label: "Bump" },
    sound:  { icon: "⚡", label: "Loud sound" },
    double: { icon: "👏👏", label: "Double clap" },
    triple: { icon: "🔔", label: "Triple tap" },
  };
  const AUDIO_INFO = {
    QUIET:          ["Quiet", "Calm, background-level sound."],
    AMBIENT:        ["Background noise", "A steady hum is raising the baseline."],
    SOUND:          ["Some sound", "Small sounds, nothing sharp."],
    VOICE:          ["Voices", "Sounds like someone talking."],
    TONE:           ["Steady tone", "A single pitch, like a hum, whistle or beeper."],
    NOISE_STEADY:   ["Steady noise", "Constant noise, like a fan or running water."],
    IMPACT:         ["Impact", "A sharp bang, clap or knock."],
    LOUD:           ["Loud", "A loud sound just went off."],
    LOUD_SUSTAINED: ["Very loud", "It has been loud for the whole second."],
  };

  let minDb = 15, maxDb = 75, targetDb = 0, shownDb = 0, peakDb = 0, peakT = 0;
  const fill = $("fill"), peak = $("peak"), trig = $("trig");
  const frac = v => Math.max(0, Math.min(1, (v - minDb) / (maxDb - minDb)));

  let lastFrame = performance.now();
  function meterFrame(now) {
    const dt = Math.min(0.1, (now - lastFrame) / 1000); lastFrame = now;
    shownDb = targetDb > shownDb ? lerp(shownDb, targetDb, 1 - Math.exp(-dt * 12))
                                 : lerp(shownDb, targetDb, 1 - Math.exp(-dt * 2.5));
    if (shownDb >= peakDb) { peakDb = shownDb; peakT = now; }
    else if (now - peakT > 1500) peakDb = Math.max(shownDb, peakDb - dt * 12);
    fill.style.clipPath = `inset(0 ${100 - frac(shownDb) * 100}% 0 0 round 13px)`;
    peak.style.left = `calc(${frac(peakDb) * 100}% - 2px)`;
    requestAnimationFrame(meterFrame);
  }
  requestAnimationFrame(meterFrame);

  const hist = $("hist"), hctx = hist.getContext("2d");
  const samples = [], MAX_SAMPLES = 120;
  function drawHistory() {
    const dpr = devicePixelRatio || 1, w = hist.clientWidth, h = hist.clientHeight;
    if (hist.width !== Math.round(w * dpr)) { hist.width = w * dpr; hist.height = h * dpr; }
    hctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    hctx.clearRect(0, 0, w, h);
    if (samples.length < 2) return;
    const x = i => i / (MAX_SAMPLES - 1) * w;
    const y = v => h - 4 - frac(v) * (h - 10);
    const off = MAX_SAMPLES - samples.length;
    hctx.beginPath();
    samples.forEach((s, i) => { const px = x(i + off), py = y(s); i ? hctx.lineTo(px, py) : hctx.moveTo(px, py); });
    hctx.strokeStyle = "#E9853D"; hctx.lineWidth = 2.5; hctx.lineJoin = "round"; hctx.stroke();
    hctx.lineTo(x(MAX_SAMPLES - 1), h); hctx.lineTo(x(off), h); hctx.closePath();
    const g = hctx.createLinearGradient(0, 0, 0, h);
    g.addColorStop(0, "rgba(233,133,61,.28)"); g.addColorStop(1, "rgba(233,133,61,0)");
    hctx.fillStyle = g; hctx.fill();
  }

  function showBands(b) {
    const bars = $("bands").querySelectorAll("b");
    b.forEach((v, i) => { if (bars[i]) bars[i].style.height = Math.max(3, Math.min(1, Math.log10(1 + v) / 2.7) * 52) + "px"; });
  }

  let lastEventId = null;
  function showEvents(events) {
    if (lastEventId === null) lastEventId = events.length ? events[0].id : 0;
    const fresh = events.filter(e => e.id > lastEventId);
    if (fresh.length) {
      lastEventId = events[0].id;
      const info = EVENT_INFO[fresh[0].type] || EVENT_INFO.sound;
      const burst = $("burst");
      burst.innerHTML = `${info.icon}<br>${info.label}!`;
      burst.classList.remove("go"); void burst.offsetWidth; burst.classList.add("go");
      const card = $("scard");
      card.classList.remove("flash"); void card.offsetWidth; card.classList.add("flash");
    }
    if (!events.length) return;
    $("chips").innerHTML = events.map(e => {
      const info = EVENT_INFO[e.type] || EVENT_INFO.sound;
      const t = new Date(e.ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
      return `<span class="chip">${info.icon} ${info.label} <small>${t}</small></span>`;
    }).join("");
  }

  /* ======================= RAIN ======================= */
  const wcard = $("wcard"), rc = $("rain"), rctx = rc.getContext("2d");
  const MAXP = 360;
  const parts = Array.from({ length: MAXP }, () => ({ x: Math.random() * 600, y: Math.random() * 600, t: Math.random(), ph: Math.random() * 6.28 }));
  const ripples = [];
  const TARGETS = {
    dry:    { n: 0,   vmin: 20,  vmax: 40,  len: 0.1, w: 2.0, alpha: 0,   wind: 0,    sway: 0,  pool: 0,  splash: 0 },
    mist:   { n: 45,  vmin: 10,  vmax: 30,  len: 0.1, w: 2.6, alpha: 0.5, wind: 6,    sway: 16, pool: 0,  splash: 0 },
    wet:    { n: 120, vmin: 340, vmax: 480, len: 14,  w: 1.5, alpha: 0.55, wind: -45, sway: 0,  pool: 5,  splash: 0.3 },
    soaked: { n: 340, vmin: 720, vmax: 980, len: 28,  w: 1.9, alpha: 0.7, wind: -130, sway: 0,  pool: 15, splash: 1 },
  };
  const cur = { ...TARGETS.dry };
  let W = 0, H = 0, rainTarget = TARGETS.dry, lastT = performance.now();

  function resizeRain() {
    const dpr = devicePixelRatio || 1;
    W = wcard.clientWidth; H = wcard.clientHeight;
    rc.width = W * dpr; rc.height = H * dpr;
    rctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  new ResizeObserver(resizeRain).observe(wcard);
  resizeRain();

  function rainFrame(now) {
    const dt = Math.min(0.05, (now - lastT) / 1000); lastT = now;
    const k = 1 - Math.exp(-dt * 1.6);
    for (const key in rainTarget) cur[key] = lerp(cur[key], rainTarget[key], k);
    const slow = reduceMotion ? 0.4 : 1;
    rctx.clearRect(0, 0, W, H);
    if (cur.n > 0.05) {
      rctx.lineCap = "round"; rctx.lineWidth = cur.w;
      const count = Math.min(MAXP, Math.ceil(cur.n * (reduceMotion ? 0.35 : 1)));
      const time = now / 1000;
      for (let i = 0; i < count; i++) {
        const p = parts[i];
        const speed = lerp(cur.vmin, cur.vmax, p.t) * slow;
        p.y += speed * dt;
        p.x += (cur.wind * (0.6 + 0.4 * p.t) + Math.sin(time * 0.9 + p.ph) * cur.sway) * dt * slow;
        if (p.x < -30) p.x += W + 60; else if (p.x > W + 30) p.x -= W + 60;
        if (p.y > H) {
          if (cur.splash > 0.25 && Math.random() < cur.splash && ripples.length < 50)
            ripples.push({ x: p.x, y: H - 3 - Math.random() * cur.pool * 0.6, r: 1, a: 0.5 });
          p.y = -Math.random() * 40; p.x = Math.random() * W; p.t = Math.random();
        }
        const a = cur.alpha * (0.45 + 0.55 * p.t) * Math.min(1, cur.n - i);
        if (a <= 0.01) continue;
        const dx = speed ? cur.wind * slow / speed * cur.len : 0;
        rctx.strokeStyle = `rgba(32,86,128,${a.toFixed(3)})`;
        rctx.beginPath(); rctx.moveTo(p.x, p.y); rctx.lineTo(p.x + dx, p.y + cur.len); rctx.stroke();
      }
    }
    for (let i = ripples.length - 1; i >= 0; i--) {
      const r = ripples[i]; r.r += 38 * dt; r.a -= 1.1 * dt;
      if (r.a <= 0) { ripples.splice(i, 1); continue; }
      rctx.strokeStyle = `rgba(32,86,128,${r.a.toFixed(3)})`; rctx.lineWidth = 1.2;
      rctx.beginPath(); rctx.ellipse(r.x, r.y, r.r, r.r * 0.35, 0, 0, 6.283); rctx.stroke();
    }
    if (cur.pool > 0.3) {
      const t = now / 1000;
      rctx.beginPath(); rctx.moveTo(0, H);
      for (let x = 0; x <= W; x += 12) rctx.lineTo(x, H - cur.pool + Math.sin(x / 38 + t * 2) * Math.min(3, cur.pool * 0.25));
      rctx.lineTo(W, H); rctx.closePath();
      rctx.fillStyle = "rgba(47,127,168,.28)"; rctx.fill();
    }
    requestAnimationFrame(rainFrame);
  }
  requestAnimationFrame(rainFrame);

  const WEATHER = {
    dry:    { icon: "☀️", word: "All dry", text: "No moisture on the sensor." },
    mist:   { icon: "🌫️", word: "Misty",   text: "A few light droplets on the sensor." },
    wet:    { icon: "🌧️", word: "Wet",     text: "Steady rain is falling on the sensor." },
    soaked: { icon: "⛈️", word: "Soaked",  text: "Heavy downpour. The sensor is drenched." },
  };
  const TREND = { wetter: "Getting wetter", drying: "Drying out", steady: "Holding steady" };
  function duration(s) {
    if (s < 60) return s + " sec";
    if (s < 3600) return Math.floor(s / 60) + " min " + (s % 60) + " sec";
    return Math.floor(s / 3600) + " hr " + Math.floor((s % 3600) / 60) + " min";
  }

  /* ======================= POLLING ======================= */
  let currentState = "", lastSeq = -1;
  async function poll() {
    try {
      const d = await (await fetch("/data")).json();
      const st = $("status");
      st.classList.toggle("on", d.connected);
      st.lastElementChild.textContent = d.connected ? "Sensor connected" : "Waiting for sensor…";

      minDb = d.min_db; maxDb = d.max_db;
      targetDb = d.connected ? d.db : minDb;
      if (d.thresh_db !== null) {
        trig.style.display = "block";
        trig.style.left = frac(d.thresh_db) * 100 + "%";
      }
      const [word, hint] = d.connected ? (AUDIO_INFO[d.audio] || [d.audio, ""]) : ["Listening…", "Waiting for the microphone."];
      $("s-word").textContent = word; $("s-hint").textContent = hint;
      $("s-db").textContent = d.connected ? Math.round(d.db) : "--";
      $("s-lvl").textContent = (d.connected && d.act_thr > 0)
        ? `Level ${d.max_rms.toFixed(1)} · counts as sound above ${d.act_thr.toFixed(1)}`
        : "";
      $("clip").classList.toggle("show", d.connected && d.clip);
      if (d.seq !== lastSeq) {
        lastSeq = d.seq;
        samples.push(d.db);
        if (samples.length > MAX_SAMPLES) samples.shift();
        drawHistory();
        showBands(d.bands);
        $("pitch").textContent = d.hz ? `loudest pitch ≈ ${d.hz} Hz` : "";
      }
      showEvents(d.events);

      const state = (d.rain_label || "DRY").toLowerCase();
      if (state !== currentState && WEATHER[state]) {
        currentState = state;
        const w = WEATHER[state];
        wcard.dataset.state = state;
        rainTarget = TARGETS[state];
        $("w-icon").textContent = w.icon; $("w-word").textContent = w.word; $("w-text").textContent = w.text;
      }
      $("w-pct").textContent = d.rain_pct + "%";
      $("w-bar").style.width = d.rain_pct + "%";
      $("w-trend").textContent = TREND[d.trend] || "Holding steady";
      $("w-drops").textContent = d.drops;
      $("w-dry").textContent = state === "dry" ? duration(d.dry_sec) : "Moisture present";
      $("banner").classList.toggle("show", d.alert);
    } catch (e) {
      $("status").classList.remove("on");
      $("status").lastElementChild.textContent = "Dashboard server not reachable";
    }
  }
  setInterval(poll, 250);
  poll();

  document.querySelectorAll("[data-cmd]").forEach(b => b.addEventListener("click", async () => {
    await fetch("/cmd", { method: "POST", headers: { "Content-Type": "application/json" },
                          body: JSON.stringify({ cmd: b.dataset.cmd }) });
    b.classList.add("sent"); setTimeout(() => b.classList.remove("sent"), 600);
  }));
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--demo" in sys.argv:
        threading.Thread(target=run_demo, daemon=True).start()
    else:
        if serial is not None:
            threading.Thread(target=read_serial, args=(args[0] if args else PORT,), daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False)