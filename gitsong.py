#!/usr/bin/env python3
"""gitsong: turn a GitHub contribution graph into a song.

    python gitsong.py <github-user>                 -> out/<user>.wav, .svg, .html
    python gitsong.py <user> --bpm 110 --key A      -> faster, in A minor
    python gitsong.py <user> --json data.json       -> from a saved GraphQL response

Every week of the year is one beat. The seven days of that week are played inside the
beat as a fast arpeggio, Sunday lowest, Saturday highest, on the tones of the current
chord. Busier days hit harder. Busy weeks bring in bass, kick, hats and snare. Empty
weeks fall silent except for the pad, so you can hear the gaps in your year.

Standard library only. Python 3.9+.
"""
import argparse
import array
import html
import json
import math
import random
import re
import sys
import urllib.request
import wave
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

SR = 44100
NOTES = {"C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3, "E": 4, "F": 5, "F#": 6,
         "Gb": 6, "G": 7, "G#": 8, "Ab": 8, "A": 9, "A#": 10, "Bb": 10, "B": 11}
# i - VI - III - VII in a minor key, four weeks per chord.
PROGRESSION = [(0, 3, 7), (8, 12, 15), (3, 7, 10), (10, 14, 17)]
CHORD_NAMES = ["i", "VI", "III", "VII"]


@dataclass
class Day:
    date: date
    count: int
    level: int      # 0-4, like GitHub's own shading
    week: int = 0
    row: int = 0    # 0 = Sunday
    t: float = 0.0  # when it plays, in seconds


# --------------------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------------------
def fetch_user(user):
    """Read the public contribution calendar. No token needed."""
    url = f"https://github.com/users/{user}/contributions"
    req = urllib.request.Request(url, headers={"User-Agent": "gitsong"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read().decode("utf-8", "replace")
    except Exception as e:
        sys.exit(f"could not fetch {url}: {e}")
    counts = {}
    for m in re.finditer(r'<tool-tip[^>]*\bfor="([^"]+)"[^>]*>([^<]*)</tool-tip>', text):
        n = re.match(r"\s*([\d,]+) contribution", m.group(2))
        counts[m.group(1)] = int(n.group(1).replace(",", "")) if n else 0
    days = []
    for tag in re.findall(r"<td[^>]*ContributionCalendar-day[^>]*>", text):
        a = dict(re.findall(r'([\w-]+)="([^"]*)"', tag))
        if "data-date" not in a:
            continue
        days.append(Day(date.fromisoformat(a["data-date"]), counts.get(a.get("id"), 0),
                        int(a.get("data-level", 0))))
    if not days:
        sys.exit(f"no contribution calendar found for '{user}'")
    return sorted(days, key=lambda d: d.date)


def load_json(path):
    """A saved GraphQL response: user.contributionsCollection.contributionCalendar."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    cal = raw.get("data", raw)["user"]["contributionsCollection"]["contributionCalendar"]
    days = [Day(date.fromisoformat(d["date"]), d["contributionCount"], 0)
            for w in cal["weeks"] for d in w["contributionDays"]]
    nonzero = sorted(d.count for d in days if d.count)
    cuts = [nonzero[min(len(nonzero) - 1, int(p * len(nonzero)))] for p in (.25, .5, .75)] \
        if nonzero else [0, 0, 0]
    for d in days:
        d.level = 0 if not d.count else 1 + sum(d.count > c for c in cuts)
    return sorted(days, key=lambda d: d.date)


def to_weeks(days):
    first = days[0].date - timedelta(days=(days[0].date.weekday() + 1) % 7)   # a Sunday
    weeks = []
    for d in days:
        d.week, d.row = (d.date - first).days // 7, (d.date.weekday() + 1) % 7
        while len(weeks) <= d.week:
            weeks.append([None] * 7)
        weeks[d.week][d.row] = d
    return weeks


# --------------------------------------------------------------------------------------
# Synth. Wavetables plus envelopes, mixed into plain float lists.
# --------------------------------------------------------------------------------------
TN = 2048
MASK = TN - 1


def table(harmonics):
    t = [sum(a * math.sin(2 * math.pi * k * i / TN) for k, a in harmonics) for i in range(TN)]
    peak = max(abs(x) for x in t)
    return [x / peak for x in t]


SINE = table([(1, 1)])
PLUCK = table([(1, 1), (2, .45), (3, .22), (4, .1), (6, .05)])
SAW = table([(k, 1 / k) for k in range(1, 10)])
BASS = table([(1, 1), (2, .35), (3, .12)])


def mtof(m):
    return 440.0 * 2 ** ((m - 69) / 12)


class Mix:
    def __init__(self, seconds):
        n = int(seconds * SR) + 1
        self.n = n
        self.L, self.R = [0.0] * n, [0.0] * n        # dry bus
        self.EL, self.ER = [0.0] * n, [0.0] * n      # bus that goes through the echo

    def tone(self, t, freq, dur, amp, pan=0.0, wav=PLUCK, attack=.004, decay=6.0,
             release=.03, echo=False):
        start, n = int(t * SR), int(dur * SR)
        n = min(n, self.n - start)
        if n <= 0:
            return
        ang = (pan + 1) * math.pi / 4
        gl, gr = amp * math.cos(ang), amp * math.sin(ang)
        L, R = (self.EL, self.ER) if echo else (self.L, self.R)
        inc = freq * TN / SR
        k = math.exp(-decay / SR)
        a_n, r_n = max(1, int(attack * SR)), max(1, int(release * SR))
        phase, env = 0.0, 1.0
        for i in range(n):
            s = wav[int(phase) & MASK] * env
            if i < a_n:
                s *= i / a_n
            left = n - i
            if left < r_n:
                s *= left / r_n
            j = start + i
            L[j] += s * gl
            R[j] += s * gr
            phase += inc
            env *= k

    def kick(self, t, amp):
        start, n = int(t * SR), int(.32 * SR)
        n = min(n, self.n - start)
        phase, L, R = 0.0, self.L, self.R
        for i in range(n):
            x = i / SR
            f = 45 + 110 * math.exp(-x * 28)
            phase += f / SR
            s = math.sin(2 * math.pi * phase) * math.exp(-x * 11) * amp
            L[start + i] += s
            R[start + i] += s

    def noise(self, t, amp, dur, decay, tone=0.0, bright=True, pan=0.0):
        start, n = int(t * SR), int(dur * SR)
        n = min(n, self.n - start)
        ang = (pan + 1) * math.pi / 4
        gl, gr = math.cos(ang), math.sin(ang)
        prev, L, R, rnd = 0.0, self.L, self.R, random.random
        for i in range(n):
            x = i / SR
            w = rnd() * 2 - 1
            s = (w - prev) if bright else w      # first difference = crude high-pass
            prev = w
            if tone:
                s = s * .6 + math.sin(2 * math.pi * tone * x) * .5
            s *= amp * math.exp(-x * decay)
            L[start + i] += s * gl
            R[start + i] += s * gr

    def finish(self, beat):
        """Ping-pong echo on the echo bus, sum, soft clip, normalise."""
        d, fb = int(beat * .75 * SR), .38
        EL, ER = self.EL, self.ER
        for i in range(d, self.n):
            EL[i] += ER[i - d] * fb
            ER[i] += EL[i - d] * fb
        L = [a + b for a, b in zip(self.L, EL)]
        R = [a + b for a, b in zip(self.R, ER)]
        peak = max(max(map(abs, L)), max(map(abs, R))) or 1.0
        g, th = 1.25 / peak, math.tanh(1.25)
        L = [math.tanh(x * g) / th * .9 for x in L]
        R = [math.tanh(x * g) / th * .9 for x in R]
        return L, R


# --------------------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------------------
def compose(weeks, bpm, key):
    random.seed(0)
    beat = 60.0 / bpm
    t0, tail = beat, 5.0
    end = t0 + len(weeks) * beat
    mix = Mix(end + tail)
    base = 60 + NOTES[key]
    if base > 65:
        base -= 12

    # Pad: one chord per four weeks, louder when those weeks were busy.
    for b in range(0, len(weeks), 4):
        block = [d.level for w in weeks[b:b + 4] for d in w if d]
        avg = sum(block) / max(1, len(block))
        chord = PROGRESSION[(b // 4) % 4]
        for k, iv in enumerate(chord):
            mix.tone(t0 + b * beat, mtof(base - 12 + iv), 4 * beat + 1.0, .028 + .03 * avg,
                     pan=(k - 1) * .45, wav=SAW, attack=.7, decay=.35, release=.9)

    for w, week in enumerate(weeks):
        chord = PROGRESSION[(w // 4) % 4]
        tw = t0 + w * beat
        levels = [d.level if d else 0 for d in week]
        total, active, top = sum(levels), sum(1 for x in levels if x), max(levels)
        for d in week:
            if not d or not d.level:
                if d:
                    d.t = tw + d.row * beat / 7
                continue
            d.t = tw + d.row * beat / 7
            note = base + chord[d.row % 3] + 12 * (d.row // 3)
            amp = (0, .17, .24, .31, .38)[d.level]
            pan = -.7 + d.row * (1.4 / 6)
            mix.tone(d.t, mtof(note), .5 + .08 * d.level, amp, pan, PLUCK,
                     decay=7.5 - d.level, echo=True)
            if d.level == 4:                        # the best days shimmer an octave up
                mix.tone(d.t + .004, mtof(note + 12), .45, amp * .35, -pan, SINE,
                         decay=6, echo=True)
        if total:
            mix.tone(tw, mtof(base - 24 + chord[0]), beat * .92, .2 + .2 * min(1, total / 12),
                     0, BASS, attack=.006, decay=2.2, release=.06)
        if total >= 6:
            mix.kick(tw, .55)
        if active >= 3:
            mix.noise(tw + beat / 2, .09, .05, 70, pan=.3)
        if top == 4 and w % 2:
            mix.noise(tw, .22, .18, 22, tone=185, bright=False, pan=-.1)

    # Outro: the tonic, rolled slowly, left to ring.
    for k, iv in enumerate((0, 3, 7, 12, 15, 19)):
        mix.tone(end + k * beat / 5, mtof(base + iv), 3.8, .22, -.6 + k * .24, PLUCK,
                 decay=1.3, echo=True)
    mix.tone(end, mtof(base - 24), 3.5, .3, 0, BASS, decay=1.2, release=.4)

    L, R = mix.finish(beat)
    return L, R, {"beat": beat, "t0": t0, "end": end, "duration": len(L) / SR}


def write_wav(path, L, R):
    pcm = array.array("h", [0]) * (2 * len(L))
    pcm[0::2] = array.array("h", (int(x * 32767) for x in L))
    pcm[1::2] = array.array("h", (int(x * 32767) for x in R))
    if sys.byteorder == "big":
        pcm.byteswap()
    with wave.open(str(path), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes(pcm.tobytes())


def loudness(L, R, bins):
    step = max(1, len(L) // bins)
    out = []
    for b in range(bins):
        seg = range(b * step, min(len(L), (b + 1) * step), 8)
        out.append(math.sqrt(sum(L[i] * L[i] + R[i] * R[i] for i in seg) / max(1, len(seg))))
    peak = max(out) or 1
    return [round(x / peak, 3) for x in out]


# --------------------------------------------------------------------------------------
# Visuals
# --------------------------------------------------------------------------------------
LEVELS = ["#1a1d29", "#3a2a66", "#6a35b8", "#b84bd8", "#ff79e1"]


def render_svg(user, weeks, info, wave_bins, total, bpm, key):
    step, cell, pad = 14, 11, 28
    gw = len(weeks) * step - 3
    W = gw + 2 * pad
    gy, wy = 88, 88 + 7 * step + 22
    wh = 46
    H = wy + wh + 56
    T = info["duration"]
    beat, t0 = info["beat"], info["t0"]
    flash = .45 / T * 100
    sweep = (info["end"] - t0 + beat) / T * 100
    x_of = lambda t: pad + (t - t0) / beat * step     # noqa: E731

    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" '
         f'height="{H}" role="img" aria-label="gitsong for {html.escape(user)}">',
         f"<title>gitsong: {html.escape(user)}'s year on GitHub as a song</title>",
         "<style>",
         ".t{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}",
         ".h{font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;font-weight:700}",
         ".f{opacity:0;transform-box:fill-box;transform-origin:center;"
         f"animation:hit {T:.2f}s linear infinite}}",
         f"@keyframes hit{{0%{{opacity:1;transform:scale(1.9)}}{flash:.2f}%{{opacity:0;"
         "transform:scale(1)}100%{opacity:0}}",
         f".p{{animation:sweep {T:.2f}s linear infinite}}",
         f"@keyframes sweep{{0%{{transform:translateX(0);opacity:1}}{sweep:.2f}%"
         f"{{transform:translateX({x_of(info['end'] + beat) - pad:.1f}px);opacity:1}}"
         f"{sweep + .5:.2f}%{{opacity:0}}100%{{opacity:0}}}}",
         "@media (prefers-reduced-motion:reduce){.f,.p{animation:none}}",
         "</style>",
         f'<rect width="{W}" height="{H}" rx="18" fill="#0b0c12"/>',
         f'<text x="{pad}" y="44" class="h" font-size="22" fill="#f4f1ff">gitsong '
         f'<tspan fill="#ff79e1">@{html.escape(user)}</tspan></text>',
         f'<text x="{W - pad}" y="44" class="t" font-size="11" fill="#8b86a8" '
         f'text-anchor="end">{total:,} CONTRIBUTIONS · {T:.0f}s · {key} MINOR · {bpm} BPM</text>',
         f'<text x="{pad}" y="66" class="t" font-size="11" fill="#5f5a7a">one week per beat, '
         f'one note per day</text>']

    flashes = []
    for w, week in enumerate(weeks):
        for d in week:
            if not d:
                continue
            x, y = pad + w * step, gy + d.row * step
            o.append(f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="2.5" '
                     f'fill="{LEVELS[d.level]}"/>')
            if d.level:
                flashes.append(f'<rect class="f" style="animation-delay:{d.t:.3f}s" x="{x}" '
                               f'y="{y}" width="{cell}" height="{cell}" rx="2.5" fill="#fff"/>')
    o += flashes

    # Loudness of the finished track, drawn as a mirrored strip.
    bw = (W - 2 * pad) / len(wave_bins)
    mid = wy + wh / 2
    bars = "".join(f'<rect x="{pad + i * bw:.1f}" y="{mid - max(1, v * wh / 2):.1f}" '
                   f'width="{max(1, bw - 1):.1f}" height="{max(2, v * wh):.1f}" rx="1"/>'
                   for i, v in enumerate(wave_bins))
    o.append(f'<g fill="#2b2840">{bars}</g>')
    o.append(f'<g class="p"><rect x="{x_of(0):.1f}" y="{gy - 8}" width="2" '
             f'height="{wy + wh - gy + 16}" rx="1" fill="#ff79e1"/></g>')
    o.append(f'<text x="{pad}" y="{H - 24}" class="t" font-size="11" fill="#5f5a7a">'
             f'made with github.com/madhavbuilds/gitsong</text>')
    lx = W - pad - 5 * 14
    o.append(f'<text x="{lx - 8}" y="{H - 24}" class="t" font-size="10" fill="#5f5a7a" '
             f'text-anchor="end">quiet</text>')
    for i, c in enumerate(LEVELS):
        o.append(f'<rect x="{lx + i * 14}" y="{H - 34}" width="11" height="11" rx="2.5" '
                 f'fill="{c}"/>')
    o.append("</svg>")
    return "".join(o)


def render_html(user, weeks, info, wave_bins, total, bpm, key, audio_name):
    cells = [[d.week, d.row, d.level, d.count, d.date.isoformat(), round(d.t, 4)]
             for w in weeks for d in w if d]
    data = {"user": user, "total": total, "bpm": bpm, "key": key, "beat": info["beat"],
            "t0": info["t0"], "end": info["end"], "duration": info["duration"],
            "weeks": len(weeks), "cells": cells, "wave": wave_bins, "audio": audio_name,
            "levels": LEVELS, "chords": CHORD_NAMES}
    return PLAYER.replace("__DATA__", json.dumps(data, separators=(",", ":"))) \
                 .replace("__USER__", html.escape(user))


PLAYER = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>gitsong · @__USER__</title>
<meta name="description" content="A year of GitHub contributions, played as a song.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Inter:wght@400;600;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#07080d;--card:#0e0f17;--line:#1d1e2b;--ink:#f4f1ff;--dim:#8b86a8;--faint:#5f5a7a;--pink:#ff79e1;--violet:#b84bd8}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 Inter,system-ui,sans-serif}
main{max-width:980px;margin:0 auto;padding:48px 16px 64px}
.kicker{font:600 11px/1 'JetBrains Mono',monospace;letter-spacing:.14em;text-transform:uppercase;color:var(--pink)}
h1{font-size:clamp(40px,8vw,84px);line-height:.95;letter-spacing:-.04em;margin:14px 0 12px;font-weight:800}
h1 span{background:linear-gradient(90deg,var(--violet),var(--pink));-webkit-background-clip:text;background-clip:text;color:transparent}
.lede{color:var(--dim);max-width:560px;margin:0 0 32px}
.card{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:22px}
.top{display:flex;align-items:center;gap:16px;margin-bottom:18px;flex-wrap:wrap}
button{all:unset;cursor:pointer;width:56px;height:56px;border-radius:50%;background:var(--pink);display:grid;place-items:center;box-shadow:0 0 40px #ff79e155;flex:none}
button:focus-visible{outline:2px solid var(--ink);outline-offset:3px}
button svg{width:22px;height:22px;fill:#14051a}
.who{font-weight:600;font-size:18px}
.meta{font:12px 'JetBrains Mono',monospace;color:var(--faint)}
.now{margin-left:auto;text-align:right;font:12px 'JetBrains Mono',monospace;color:var(--dim);min-width:150px}
.now b{display:block;color:var(--ink);font-size:20px;font-weight:600}
canvas{display:block;width:100%;touch-action:none;cursor:pointer}
.how{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-top:20px}
.how div{border:1px solid var(--line);border-radius:14px;padding:14px 16px;color:var(--dim);font-size:13.5px}
.how strong{display:block;color:var(--ink);margin-bottom:4px;font-size:14px}
pre{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;overflow:auto;font:13px 'JetBrains Mono',monospace;color:var(--ink)}
h2{font-size:22px;letter-spacing:-.02em;margin:40px 0 10px}
a{color:var(--pink)}
footer{margin-top:40px;color:var(--faint);font:12px 'JetBrains Mono',monospace}
</style>
</head>
<body>
<main>
<div class="kicker">gitsong · a year of commits, out loud</div>
<h1>@__USER__'s year,<br><span>played as a song.</span></h1>
<p class="lede">Every week of the contribution graph is one beat. Every day in it is one note. Busy days hit harder, busy weeks bring in the drums, and the empty weeks go quiet.</p>

<div class="card">
  <div class="top">
    <button id="play" aria-label="Play"><svg viewBox="0 0 24 24" id="icon"><path d="M7 4.5v15l13-7.5z"/></svg></button>
    <div><div class="who">@__USER__</div><div class="meta" id="meta"></div></div>
    <div class="now" id="now"><b>&nbsp;</b>press play</div>
  </div>
  <canvas id="c" aria-label="Contribution graph with a playhead"></canvas>
</div>

<div class="how">
  <div><strong>Week = beat</strong>Sunday to Saturday plays as a fast arpeggio inside each beat, low to high.</div>
  <div><strong>Day = note</strong>Each day is a tone of the current chord. Shade sets how hard it hits.</div>
  <div><strong>Busy = loud</strong>Busy weeks add bass, kick and hats. Your best days shimmer an octave up.</div>
  <div><strong>Quiet = quiet</strong>Weeks off leave only the pad. You can hear every break you took.</div>
</div>

<h2>Make yours</h2>
<pre>git clone https://github.com/madhavbuilds/gitsong
cd gitsong
python gitsong.py your-github-username</pre>
<p class="lede">No dependencies, no token. It writes a WAV, an animated SVG for your README and a page like this one.</p>
<footer>made with <a href="https://github.com/madhavbuilds/gitsong">gitsong</a> by <a href="https://madhavpanchal.com/">madhav panchal</a></footer>
</main>
<audio id="a" preload="auto"></audio>
<script>
const D = __DATA__;
const a = document.getElementById('a'); a.src = D.audio;
const cv = document.getElementById('c'), cx = cv.getContext('2d');
const btn = document.getElementById('play'), icon = document.getElementById('icon');
const now = document.getElementById('now');
document.getElementById('meta').textContent =
  `${D.total.toLocaleString()} contributions · ${Math.round(D.duration)}s · ${D.key} minor · ${D.bpm} bpm`;
const PLAY = 'M7 4.5v15l13-7.5z', PAUSE = 'M6 4h4.5v16H6zM13.5 4H18v16h-4.5z';
let W = 0, H = 0, step = 0, cell = 0, gy = 0, wy = 0, wh = 0, dpr = 1;
function layout() {
  dpr = window.devicePixelRatio || 1;
  W = cv.clientWidth;
  step = W / D.weeks; cell = Math.max(2, step * .8);
  gy = 6; wy = gy + 7 * step + 18; wh = Math.max(40, step * 4);
  H = wy + wh + 6;
  cv.style.height = H + 'px'; cv.width = W * dpr; cv.height = H * dpr;
  cx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
function rr(x, y, w, h, r) { cx.beginPath(); cx.roundRect ? cx.roundRect(x, y, w, h, r) : cx.rect(x, y, w, h); cx.fill(); }
const xAt = t => (t - D.t0) / D.beat * step;
function draw() {
  const t = a.currentTime || 0;
  cx.clearRect(0, 0, W, H);
  let current = null;
  for (const [w, r, lv, n, date, ct] of D.cells) {
    const x = w * step + (step - cell) / 2, y = gy + r * step + (step - cell) / 2;
    cx.fillStyle = D.levels[lv]; rr(x, y, cell, cell, cell * .22);
    const age = t - ct;
    if (lv && t > 0 && age >= 0 && age < .6) {
      const k = 1 - age / .6, g = cell * (1 + .9 * k);
      cx.save(); cx.globalAlpha = k; cx.shadowColor = '#ff79e1'; cx.shadowBlur = 18 * k;
      cx.fillStyle = '#fff'; rr(x + (cell - g) / 2, y + (cell - g) / 2, g, g, g * .25); cx.restore();
    }
    if (age >= 0 && age < D.beat / 7) current = [n, date];
  }
  const bw = W / D.wave.length, mid = wy + wh / 2, prog = t / D.duration;
  D.wave.forEach((v, i) => {
    cx.fillStyle = i / D.wave.length < prog ? '#b84bd8' : '#26233a';
    const h = Math.max(2, v * wh); rr(i * bw, mid - h / 2, Math.max(1, bw - 1), h, 1);
  });
  if (t > 0 && t < D.end + D.beat) {
    const x = xAt(t); cx.fillStyle = '#ff79e1'; cx.shadowColor = '#ff79e1'; cx.shadowBlur = 12;
    rr(x - 1, 0, 2, wy - 8, 1); cx.shadowBlur = 0;
  }
  if (current) {
    const d = new Date(current[1] + 'T00:00:00');
    now.innerHTML = `<b>${current[0]} contribution${current[0] === 1 ? '' : 's'}</b>` +
      d.toLocaleDateString('en', { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });
  } else if (t >= D.end) now.innerHTML = '<b>fin.</b>that was the year';
  requestAnimationFrame(draw);
}
btn.onclick = () => a.paused ? a.play() : a.pause();
a.onplay = () => { icon.innerHTML = `<path d="${PAUSE}"/>`; btn.setAttribute('aria-label', 'Pause'); };
a.onpause = a.onended = () => { icon.innerHTML = `<path d="${PLAY}"/>`; btn.setAttribute('aria-label', 'Play'); };
cv.addEventListener('pointerdown', e => {
  const r = cv.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
  a.currentTime = y > wy - 10 ? x / W * D.duration : Math.max(0, D.t0 + x / step * D.beat);
  if (a.paused) a.play();
});
addEventListener('resize', layout); layout(); draw();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Turn a GitHub contribution graph into a song.")
    ap.add_argument("user", help="GitHub username")
    ap.add_argument("--json", help="read a saved GraphQL contributionCalendar instead")
    ap.add_argument("--bpm", type=int, default=96, help="tempo, one week per beat (96)")
    ap.add_argument("--key", default="D", choices=sorted(NOTES), help="minor key (D)")
    ap.add_argument("--out", default="out", help="output folder (out)")
    args = ap.parse_args()

    days = load_json(args.json) if args.json else fetch_user(args.user)
    weeks = to_weeks(days)
    total = sum(d.count for d in days)
    print(f"@{args.user}: {total:,} contributions over {len(weeks)} weeks. Composing...")

    L, R, info = compose(weeks, args.bpm, args.key)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^\w.-]", "_", args.user)
    write_wav(out / f"{stem}.wav", L, R)
    bins = loudness(L, R, len(weeks) * 3)
    (out / f"{stem}.svg").write_text(
        render_svg(args.user, weeks, info, bins, total, args.bpm, args.key), encoding="utf-8")
    (out / f"{stem}.html").write_text(
        render_html(args.user, weeks, info, bins, total, args.bpm, args.key, f"{stem}.wav"),
        encoding="utf-8")
    print(f"{info['duration']:.1f}s of audio -> {out}/{stem}.wav, {stem}.svg, {stem}.html")


if __name__ == "__main__":
    main()
