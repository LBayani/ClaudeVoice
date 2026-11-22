// CRT phosphor "robot eyes" engine. Written in a C-portable style:
// fixed-size typed arrays, no allocations per frame, plain math.
// The firmware port (cv_eyes.cc) mirrors this file 1:1.
"use strict";

const SW = 240, SH = 240;          // screen
const LW = 120, LH = 120;          // phosphor layer (2x upscaled -> chunky CRT pixels)

const STATES = ["idle", "listening", "thinking", "speaking", "connecting", "error", "sleep"];
const EMOTIONS = ["neutral", "happy", "sad", "angry", "surprised", "thinking", "sleepy"];

const STATUS_TEXT = {
  idle: "ГОТОВ", listening: "СЛУШАЮ", thinking: "ОБРАБОТКА", speaking: "ГОВОРЮ",
  connecting: "СОЕДИНЕНИЕ", error: "НЕТ СВЯЗИ", sleep: "ОЖИДАНИЕ",
};

function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }
function lerp(a, b, t) { return a + (b - a) * t; }
function approach(cur, target, rate, dt) { return cur + (target - cur) * (1 - Math.exp(-rate * dt)); }
function rnd(a, b) { return a + Math.random() * (b - a); }

// ---------------- palettes: intensity 0..255 -> RGB ----------------
function buildPalette(kind) {
  const p = new Uint8Array(256 * 3);
  for (let i = 0; i < 256; i++) {
    const f = i / 255;
    let r, g, b;
    if (kind === "green") { r = 40 * f * f + 200 * Math.pow(f, 6); g = 255 * Math.pow(f, 0.75); b = 80 * f * f + 170 * Math.pow(f, 6); }
    else if (kind === "red") { r = 255 * Math.pow(f, 0.7); g = 70 * f * f + 160 * Math.pow(f, 6); b = 40 * f * f + 120 * Math.pow(f, 6); }
    else { r = 255 * Math.pow(f, 0.55); g = 175 * Math.pow(f, 0.95) + 70 * Math.pow(f, 6); b = 30 * f * f + 150 * Math.pow(f, 6); } // amber
    p[i * 3] = clamp(r, 0, 255); p[i * 3 + 1] = clamp(g, 0, 255); p[i * 3 + 2] = clamp(b, 0, 255);
  }
  return p;
}

class EyesEngine {
  constructor() {
    this.phos = new Float32Array(LW * LH);     // phosphor layer with persistence
    this.text = new Uint8Array(SW * SH);       // full-res text/HUD mask (0..255)
    this.vign = new Float32Array(SW * SH);
    for (let y = 0; y < SH; y++) for (let x = 0; x < SW; x++) {
      const dx = (x - SW / 2) / (SW / 2), dy = (y - SH / 2) / (SH / 2);
      const r2 = dx * dx + dy * dy;
      this.vign[y * SW + x] = clamp(1.04 - 0.16 * r2 * r2 - 0.06 * r2, 0, 1);
    }
    this.rowShift = new Int8Array(SH);
    this.palettes = { amber: buildPalette("amber"), green: buildPalette("green"), red: buildPalette("red") };
    this.paletteName = "amber";
    this.style = "classic";     // "classic" | "circuit"
    this.decay = 0.55;          // phosphor persistence per 33 ms frame
    this.t = 0;

    this.state = "idle"; this.stateT = 0;
    this.emotion = "neutral"; this.emotionT = 0;
    this.statusText = STATUS_TEXT.idle;
    this.message = ""; this.messageShown = 0; this.messageRole = "system";
    this.battery = 76; this.charging = true;
    this.unit = "UNIT-07"; this.header = "VOICE INTERFACE"; this.bg = null; this.customPal = null; this.skinEyes = null;

    // controllers
    this.gx = 0; this.gy = 0; this.tgx = 0; this.tgy = 0; this.nextSaccade = 1;
    this.mx = 0; this.my = 0; this.nextMicro = 0.3;              // microsaccades
    this.blink = 0; this.blinkPhase = -1; this.nextBlink = 2; this.doubleBlink = false;
    this.pupil = 0.55; this.irisRot = 0; this.level = 0; this.levelTarget = 0;
    this.openU = 0.9; this.openL = 0.75; this.tilt = 0; this.happy = 0; this.tiltR = 0;
    this.bright = 1; this.glitch = 0;
    this.scope = new Float32Array(LW);   // oscilloscope history
  }

  // ---- skins (same format as voice_server/skins.py) ----
  applySkin(skin) {
    this.style = skin.base || "classic";
    this.customPal = null;
    if (skin.palette) {
      const pts = skin.palette.map(([p, c]) => [p, [1, 3, 5].map(i => parseInt(c.slice(i, i + 2), 16))]);
      const lut = new Uint8Array(256 * 3);
      for (let i = 0; i < 256; i++) {
        const f = i / 255; let col = pts[pts.length - 1][1];
        for (let k = 0; k < pts.length - 1; k++) {
          const [p0, c0] = pts[k], [p1, c1] = pts[k + 1];
          if (f <= p1) { const t = p1 === p0 ? 0 : Math.max(0, (f - p0) / (p1 - p0)); col = c0.map((v, j) => Math.round(v + (c1[j] - v) * t)); break; }
        }
        lut.set(col, i * 3);
      }
      this.customPal = lut;
    }
    this.bg = null;
    if (skin.background) {
      const raw = atob(skin.background);
      if (raw.length === LW * LH) { this.bg = new Uint8Array(LW * LH); for (let i = 0; i < raw.length; i++) this.bg[i] = raw.charCodeAt(i); }
    }
    if (skin.bgPixels) this.bg = skin.bgPixels;
    this.skinEyes = skin.eyes || null;
    this.fxOverride = skin.fx || null;
    this.unit = (skin.texts && skin.texts.unit) || "UNIT-07";
    this.header = (skin.texts && skin.texts.header) || "VOICE INTERFACE";
    this.circuit = null;
  }

  setState(s) {
    if (s === this.state) return;
    this.state = s; this.stateT = 0;
    this.statusText = STATUS_TEXT[s] || s.toUpperCase();
    this.glitch = 0.18;                       // short CRT glitch on every transition
    this.nextSaccade = 0;
    if (s === "listening" || s === "error") this.blinkPhase = 0; // reactive blink
  }
  setEmotion(e) { this.emotion = e; this.emotionT = 0; }
  setMessage(role, text) { this.messageRole = role; this.message = text || ""; this.messageShown = 0; }
  setLevel(v) { this.levelTarget = clamp(v, 0, 1); }

  // ---------------- behaviour ----------------
  update(dt) {
    this.t += dt; this.stateT += dt; this.emotionT += dt;
    const s = this.state;

    // gaze: saccades to new fixation points, behaviour depends on state
    this.nextSaccade -= dt;
    if (this.nextSaccade <= 0) {
      if (s === "listening") { this.tgx = rnd(-0.12, 0.12); this.tgy = rnd(-0.08, 0.1); this.nextSaccade = rnd(0.8, 2.0); }
      else if (s === "thinking") { this.tgx = rnd(0.35, 0.75) * (Math.random() < 0.7 ? 1 : -1); this.tgy = rnd(-0.75, -0.35); this.nextSaccade = rnd(0.5, 1.4); }
      else if (s === "speaking") { this.tgx = rnd(-0.25, 0.25); this.tgy = rnd(-0.1, 0.15); this.nextSaccade = rnd(0.6, 1.8); }
      else if (s === "sleep") { this.tgx = 0; this.tgy = 0.3; this.nextSaccade = 3; }
      else if (s === "connecting") { this.tgx = 0; this.tgy = 0; this.nextSaccade = 1; }
      else { // idle: look around, sometimes far
        const far = Math.random() < 0.25;
        this.tgx = far ? rnd(-0.9, 0.9) : rnd(-0.35, 0.35); this.tgy = far ? rnd(-0.6, 0.6) : rnd(-0.2, 0.25);
        this.nextSaccade = rnd(1.2, 4.0);
      }
    }
    this.gx = approach(this.gx, this.tgx, 28, dt); this.gy = approach(this.gy, this.tgy, 28, dt);
    this.nextMicro -= dt;
    if (this.nextMicro <= 0) { this.mx = rnd(-0.04, 0.04); this.my = rnd(-0.03, 0.03); this.nextMicro = rnd(0.25, 0.7); }

    // blink: fast close, short hold, slower open; occasional double blink
    this.nextBlink -= dt;
    if (this.blinkPhase < 0 && this.nextBlink <= 0 && s !== "sleep") { this.blinkPhase = 0; this.doubleBlink = Math.random() < 0.18; }
    if (this.blinkPhase >= 0) {
      this.blinkPhase += dt;
      const p = this.blinkPhase, c = 0.07, h = 0.04, o = 0.13;
      this.blink = p < c ? p / c : p < c + h ? 1 : p < c + h + o ? 1 - (p - c - h) / o : 0;
      if (p >= c + h + o) {
        this.blinkPhase = -1; this.blink = 0;
        if (this.doubleBlink) { this.doubleBlink = false; this.nextBlink = 0.12; }
        else this.nextBlink = s === "listening" ? rnd(3, 6) : rnd(2, 6.5);
      }
    }

    // audio level (smoothed); prototype fakes speech envelope when no real level comes
    this.level = approach(this.level, this.levelTarget, this.levelTarget > this.level ? 30 : 9, dt);

    // targets per state
    let pupilT = 0.55, openU = 0.9, openL = 0.72, rotSpeed = 0.25, bright = 0.9, tilt = 0, happy = 0, tiltR = 0;
    if (s === "listening") { pupilT = 0.85 + 0.15 * this.level; openU = 1.02; openL = 0.8; rotSpeed = 0.5; bright = 1; tiltR = 0.06; }
    if (s === "thinking") { pupilT = 0.38 + 0.08 * Math.sin(this.t * 6); openU = 0.78; openL = 0.66; rotSpeed = 2.6; bright = 0.95; }
    if (s === "speaking") { pupilT = 0.55 + 0.35 * this.level; openU = 0.92; openL = 0.74; rotSpeed = 0.6 + 1.5 * this.level; bright = 0.95 + 0.05 * this.level; }
    if (s === "connecting") { pupilT = 0.3; openU = 0.7; openL = 0.6; rotSpeed = 5; bright = 0.8; }
    if (s === "error") { pupilT = 0.25; openU = 0.55; openL = 0.5; rotSpeed = 0; bright = 0.85; tilt = 0.25; }
    if (s === "sleep") { pupilT = 0.4; openU = 0.12 + 0.03 * Math.sin(this.t * 0.8); openL = 0.1; rotSpeed = 0.05; bright = 0.45 + 0.1 * Math.sin(this.t * 0.8); }

    const e = this.emotion;
    if (e === "happy") { happy = 1; openU = Math.min(openU, 0.9); }
    if (e === "sad") { tilt = -0.35; openU *= 0.8; }
    if (e === "angry") { tilt = 0.45; openU *= 0.75; pupilT *= 0.7; }
    if (e === "surprised") { openU = 1.15; openL = 0.9; pupilT = 0.35; }
    if (e === "sleepy") { openU *= 0.45; }

    this.pupil = approach(this.pupil, pupilT, 6, dt);
    this.openU = approach(this.openU, openU, 10, dt);
    this.openL = approach(this.openL, openL, 10, dt);
    this.tilt = approach(this.tilt, tilt, 8, dt);
    this.happy = approach(this.happy, happy, 8, dt);
    this.tiltR = approach(this.tiltR, tiltR, 3, dt);
    this.bright = approach(this.bright, bright, 4, dt);
    this.irisRot += rotSpeed * dt;
    this.glitch = Math.max(0, this.glitch - dt);
    if (s === "error" && Math.random() < dt * 1.5) this.glitch = 0.12;

    // typewriter
    if (this.messageShown < this.message.length) this.messageShown = Math.min(this.message.length, this.messageShown + dt * 45);

    // oscilloscope history
    for (let i = 0; i < LW - 1; i++) this.scope[i] = this.scope[i + 1];
    const amp = (s === "speaking" || s === "listening") ? this.level : 0;
    this.scope[LW - 1] = amp * (Math.sin(this.t * 47) * 0.6 + Math.sin(this.t * 83 + 1) * 0.4) * (0.6 + 0.4 * Math.random());
  }

  // ---------------- rendering ----------------
  put(x, y, v) {
    x = Math.round(x); y = Math.round(y);
    if (x < 0 || y < 0 || x >= LW || y >= LH) return;
    const i = y * LW + x; if (v > this.phos[i]) this.phos[i] = v;
  }

  drawEye(cx, cy, R, side) {
    const gx = (this.gx + this.mx) * R * 0.42, gy = (this.gy + this.my) * R * 0.34;
    const b = this.blink;
    // emotion tilt mirrored per eye: positive = inner corner down (angry)
    const tilt = this.tilt * side;
    const ou = lerp(this.openU, -this.openL * 0.85, b), ol = this.openL * (1 - 0.25 * b);
    const Ri = R * 0.74, Rp = Ri * (0.25 + 0.45 * this.pupil);
    const x0 = Math.floor(cx - R - 3), x1 = Math.ceil(cx + R + 3), y0 = Math.floor(cy - R - 3), y1 = Math.ceil(cy + R + 3);
    const segs = 12, rot = this.irisRot * side;
    for (let y = y0; y <= y1; y++) {
      for (let x = x0; x <= x1; x++) {
        const dx = x - cx, dy = y - cy, r = Math.sqrt(dx * dx + dy * dy);
        let v = 0;
        // housing: thin outer ring, always visible
        const hr = Math.abs(r - (R + 1.5));
        if (hr < 1.0) v = (1 - hr) * 80;
        if (r > R) { if (v > 0) this.put(x, y, v * this.bright); continue; }
        // shutters (lids)
        const nx = dx / R, curve = 1 - 0.38 * nx * nx;
        const up = -R * ou * curve + tilt * dx * 0.38 - this.tiltR * R * side;
        const lo = R * ol * curve - this.happy * R * 0.55 * (1 - nx * nx) - this.tiltR * R * side;
        const du = dy - up, dl = lo - dy;
        if (du < -0.9 || dl < -0.9) {
          // closed shutter plate: faint horizontal hatching
          if (y % 3 === 0) v = Math.max(v, 22);
          if (v > 0) this.put(x, y, v * this.bright);
          continue;
        }
        // lid edges: bright shutter lines
        const edge = Math.min(Math.abs(du), Math.abs(dl));
        if (edge < 0.9) v = Math.max(v, 240 * (1 - edge / 0.9));
        if (du >= 0 && dl >= 0) {
          v = Math.max(v, 10);                                // dark glass
          const ix = dx - gx, iy = dy - gy, ir = Math.sqrt(ix * ix + iy * iy);
          if (ir < Ri + 1.5) {
            const ia = Math.atan2(iy, ix);
            // outer iris ring
            const d1 = Math.abs(ir - Ri); if (d1 < 1.4) v = Math.max(v, 225 * (1 - d1 / 1.4));
            // segmented aperture ring (rotates, brightens with voice)
            if (ir > Rp + 2 && ir < Ri - 2) {
              const f = ((ia + rot) / (Math.PI * 2) * segs % 1 + 1) % 1;
              const glow = 70 + 110 * this.level;
              if (f < 0.55) v = Math.max(v, glow * (0.75 + 0.25 * (ir - Rp) / (Ri - Rp)));
            }
            // pupil: black lens, bright rim, glint
            if (ir < Rp) v = 4;
            const dp = Math.abs(ir - Rp); if (dp < 1.1) v = Math.max(v, 200 * (1 - dp / 1.1));
            const glx = ix + Rp * 0.42, gly = iy + Rp * 0.42;
            if (glx * glx + gly * gly < 2.4) v = 255;
          }
        }
        if (v > 0) this.put(x, y, v * this.bright);
      }
    }
  }

  drawScope() {
    const y0 = 86, amp = 9;
    for (let x = 18; x < LW - 18; x++) {
      const w = Math.sin((x - 18) / (LW - 36) * Math.PI);           // fade at ends
      const y = Math.round(y0 + this.scope[x] * amp * w);
      this.put(x, y, 150 * this.bright);
      const yp = Math.round(y0 + this.scope[x - 1] * amp * w);      // connect samples
      for (let k = Math.min(y, yp) + 1; k < Math.max(y, yp); k++) this.put(x, k, 120 * this.bright);
    }
  }

  drawText(str, x, y, v) {
    for (const ch of str) {
      const g = CV_FONT.g[ch.codePointAt(0)] || CV_FONT.g[63];
      for (let r = 0; r < CV_FONT.h; r++) {
        const bits = g[r]; if (!bits) continue;
        for (let c = 0; c < CV_FONT.w; c++) if (bits & (0x80 >> c)) {
          const px = x + c, py = y + r;
          if (px >= 0 && py >= 0 && px < SW && py < SH) this.text[py * SW + px] = v;
        }
      }
      x += CV_FONT.w;
    }
  }

  wrap(str, cols) {
    const words = str.split(/\s+/), lines = []; let cur = "";
    for (const w of words) {
      if ((cur + " " + w).trim().length > cols) { if (cur) lines.push(cur); cur = w; } else cur = (cur + " " + w).trim();
    }
    if (cur) lines.push(cur);
    return lines;
  }

  drawHud() {
    this.text.fill(0);
    const dim = 150, hi = 235;
    // header
    this.drawText(this.unit, 6, 4, dim);
    const bars = Math.round(this.battery / 25), batt = "█".repeat(bars) + "·".repeat(4 - bars);
    this.drawText((this.charging ? "+" : " ") + batt + " " + this.battery + "%", SW - 6 - 8 * 10, 4, dim);
    for (let x = 6; x < SW - 6; x += 3) this.text[18 * SW + x] = 90;
    // status line
    const cursor = (Math.floor(this.t * 2.5) % 2) ? "█" : " ";
    let status = "> " + this.statusText;
    if (this.state === "thinking" || this.state === "connecting") status += ".".repeat(Math.floor(this.t * 3) % 4);
    this.drawText(status + cursor, 6, 194, hi);
    for (let x = 6; x < SW - 6; x += 3) this.text[190 * SW + x] = 90;
    // message (typewriter, last 2 lines)
    const shown = this.message.slice(0, Math.floor(this.messageShown));
    const prefix = this.messageRole === "user" ? "> " : this.messageRole === "assistant" ? "" : "";
    const lines = this.wrap(prefix + shown, 28).slice(-2);
    lines.forEach((l, i) => this.drawText(l, 6, 210 + i * 13, this.messageRole === "user" ? dim : hi));
  }

  // ================= "circuit face" style =================
  sline(buf, x0, y0, x1, y1, v) {             // static-layer line (integer DDA)
    const n = Math.max(Math.abs(x1 - x0), Math.abs(y1 - y0), 1);
    for (let i = 0; i <= n; i++) {
      const x = Math.round(x0 + (x1 - x0) * i / n), y = Math.round(y0 + (y1 - y0) * i / n);
      if (x >= 0 && y >= 0 && x < LW && y < LH && v > buf[y * LW + x]) buf[y * LW + x] = v;
    }
  }
  sring(buf, cx, cy, r, v) {
    for (let y = Math.floor(cy - r - 2); y <= cy + r + 2; y++) for (let x = Math.floor(cx - r - 2); x <= cx + r + 2; x++) {
      const d = Math.abs(Math.hypot(x - cx, y - cy) - r);
      if (d < 0.8 && x >= 0 && y >= 0 && x < LW && y < LH) { const w = v * (1 - d / 0.8); if (w > buf[y * LW + x]) buf[y * LW + x] = w; }
    }
  }
  buildCircuit() {
    const b = new Float32Array(LW * LH), v = 75, pad = (x, y) => this.sring(b, x, y, 1.6, 110);
    const E = [[34, 44], [86, 44]], R = 16;
    for (const [cx, cy] of E) {
      const s = cx < 60 ? -1 : 1;                 // outward direction
      this.sring(b, cx, cy, R + 4, 90);           // housing
      this.sring(b, cx, cy, R + 6.5, 40);
      // top bus to the centre connector
      this.sline(b, cx, cy - R - 6, cx, 18, v); this.sline(b, cx, 18, 60, 18, v);
      // outward traces with right-angle bends and pads
      const ox = cx + s * (R + 6);
      this.sline(b, ox, cy - 6, cx + s * 27, cy - 6, v); this.sline(b, cx + s * 27, cy - 6, cx + s * 27, 22, v); pad(cx + s * 27, 21);
      this.sline(b, ox, cy + 5, cx + s * 30, cy + 5, v); this.sline(b, cx + s * 30, cy + 5, cx + s * 30, 58, v); pad(cx + s * 30, 59);
      this.sline(b, cx + s * 14, cy + R + 2, cx + s * 14, 64, v); this.sline(b, cx + s * 14, 64, cx + s * 22, 64, v); pad(cx + s * 23, 64);
      // small rivets on the housing
      for (let k = 0; k < 4; k++) { const a = (k / 4) * Math.PI * 2 + 0.6; pad(cx + Math.cos(a) * (R + 9), cy + Math.sin(a) * (R + 9)); }
    }
    // centre connector + nose bracket
    for (let x = 55; x <= 65; x++) { b[16 * LW + x] = 110; b[20 * LW + x] = 110; }
    this.sline(b, 55, 16, 55, 20, 110); this.sline(b, 65, 16, 65, 20, 110);
    this.sline(b, 57, 20, 57, 58, v); this.sline(b, 63, 20, 63, 58, v);
    this.sline(b, 57, 58, 53, 62, v); this.sline(b, 63, 58, 67, 62, v); this.sline(b, 53, 62, 67, 62, v);
    pad(52, 62); pad(68, 62);
    // mouth brackets
    this.sline(b, 22, 70, 22, 84, v); this.sline(b, 22, 70, 26, 70, v); this.sline(b, 22, 84, 26, 84, v);
    this.sline(b, 98, 70, 98, 84, v); this.sline(b, 94, 70, 98, 70, v); this.sline(b, 94, 84, 98, 84, v);
    return b;
  }

  drawEyeCircuit(cx, cy, R, side) {
    const gx = (this.gx + this.mx) * R * 0.32, gy = (this.gy + this.my) * R * 0.28;
    const b = this.blink, tilt = this.tilt * side;
    const ou = lerp(this.openU, -this.openL * 0.85, b) * 1.1, ol = this.openL * (1 - 0.25 * b) * 1.25;
    const Rc = R * (0.2 + 0.16 * this.pupil);          // bright pupil core
    const rot = this.irisRot * side;
    for (let y = Math.floor(cy - R - 2); y <= cy + R + 2; y++) {
      for (let x = Math.floor(cx - R - 2); x <= cx + R + 2; x++) {
        const dx = x - cx, dy = y - cy, r = Math.sqrt(dx * dx + dy * dy);
        if (r > R + 1.5) continue;
        const nx = clamp(dx / R, -1, 1), curve = 1 - 0.3 * nx * nx;
        const up = -R * ou * curve + tilt * dx * 0.38 - this.tiltR * R * side;
        const lo = R * ol * curve - this.happy * R * 0.6 * (1 - nx * nx) - this.tiltR * R * side;
        const du = dy - up, dl = lo - dy;
        let v = 0;
        if (du < -0.9 || dl < -0.9) { if (y % 2 === 0 && r < R) this.put(x, y, 26 * this.bright); continue; }
        const edge = Math.min(Math.abs(du), Math.abs(dl));
        if (edge < 0.9 && r < R + 0.5) v = 230 * (1 - edge / 0.9);
        if (du >= 0 && dl >= 0) {
          const a = Math.atan2(dy, dx);
          // thick rotating outer arcs (3 x 80 deg)
          if (r > R - 3 && r < R) {
            const f = ((a + rot) / (Math.PI * 2) * 3 % 1 + 1) % 1;
            if (f < 0.62) v = Math.max(v, 150 + 80 * this.level);
          }
          // thin static ring + counter-rotating dashes
          const d2 = Math.abs(r - R * 0.72); if (d2 < 0.8) v = Math.max(v, 140);
          // pupil cluster follows the gaze
          const ix = dx - gx, iy = dy - gy, ir = Math.sqrt(ix * ix + iy * iy);
          if (ir < Rc + 3.2) {
            if (ir > Rc && ir < Rc + 1.6) v = 6;                       // dark gap
            const dr = Math.abs(ir - (Rc + 2.4)); if (dr < 0.9) v = Math.max(v, 200);
            if (ir <= Rc) v = 215 + 40 * (1 - ir / Rc);                 // glowing core
          }
          if (r < R - 3 && v < 12) v = 12;
        }
        if (v > 0) this.put(x, y, v * this.bright);
      }
    }
  }

  drawMouth() {
    const y0 = 77, x0 = 27, x1 = 93, talk = (this.state === "speaking" || this.state === "listening") ? this.level : 0;
    for (let x = x0; x <= x1; x += 1) {
      const env = Math.pow(Math.max(0, Math.sin(Math.PI * (x - x0) / (x1 - x0))), 1.3);
      const idle = 0.6 + 0.4 * Math.sin(this.t * 3 + x * 0.4);
      const h = Math.round(env * (talk * 9 * (0.35 + 0.65 * Math.random()) + idle * 0.8));
      for (let k = -h; k <= h; k++) this.put(x, y0 + k, (225 - Math.abs(k) * 10) * this.bright);
      this.put(x, y0, 120 * this.bright);
    }
  }

  drawSpectrum() {
    if (!this.spec) this.spec = new Float32Array(6);
    const talk = (this.state === "speaking" || this.state === "listening") ? this.level : 0.05;
    for (let i = 0; i < 6; i++) {
      const target = talk * (1 - i / 9) * (0.3 + 0.7 * Math.random()) + 0.06;
      this.spec[i] += (target - this.spec[i]) * 0.35;
      const h = Math.round(this.spec[i] * 18), x = 3 + i * 3;
      for (let k = 0; k < h; k++) { this.put(x, 87 - k, 170 * this.bright); this.put(x + 1, 87 - k, 170 * this.bright); }
    }
    for (let x = 2; x < 20; x++) this.put(x, 88, 90 * this.bright);
  }

  drawHudCircuit() {
    this.text.fill(0);
    const dim = 150, hi = 235;
    const EN = { idle: "IDLE", listening: "LISTENING", thinking: "PROCESSING", speaking: "SPEAKING",
                 connecting: "LINKING", error: "NO SIGNAL", sleep: "STANDBY" };
    this.drawText((this.header + ": " + this.unit).slice(0, 28), 6, 2, hi);
    let st = "STATUS: " + EN[this.state];
    if (this.state === "thinking" || this.state === "connecting") st += ".".repeat(Math.floor(this.t * 3) % 4);
    this.drawText(st, 6, 14, dim);
    this.drawText((this.charging ? "+" : "") + this.battery + "%", SW - 6 - 8 * 4, 14, dim);
    // right data column
    if (!this.hex || this.t - this.hexT > 0.35) {
      this.hexT = this.t;
      this.hex = [0, 1, 2].map(() => (Math.random() * 65536 | 0).toString(16).toUpperCase().padStart(4, "0"));
    }
    this.drawText("DIAG", 198, 124, dim);
    this.hex.forEach((h, i) => this.drawText(h, 198, 137 + i * 12, (this.state === "thinking" ? hi : 110)));
    const EMO = { neutral: "NEUTRAL", happy: "CONFIDENT", sad: "SUBDUED", angry: "HOSTILE", surprised: "ALERT",
                  thinking: "ANALYTIC", sleepy: "LOW POWER" };
    this.drawText("EMOTIVE: " + EMO[this.emotion], 6, 177, dim);
    this.drawText("NET:" + (this.state === "error" ? "LOST" : "OK"), SW - 6 - 8 * 7, 177, dim);
    for (let x = 6; x < SW - 6; x += 3) this.text[190 * SW + x] = 90;
    const shown = this.message.slice(0, Math.floor(this.messageShown));
    const cursor = (Math.floor(this.t * 2.5) % 2) ? "█" : "";
    const lines = this.wrap((this.messageRole === "user" ? "> " : "") + shown, 28).slice(-3);
    if (!lines.length) lines.push("");
    lines[lines.length - 1] += cursor;
    lines.forEach((l, i) => this.drawText(l, 6, 195 + i * 13, this.messageRole === "user" ? dim : hi));
  }

  render(out /* Uint8ClampedArray RGBA SW*SH */) {
    const fx = Object.assign({}, (typeof SKIN_FX !== "undefined" && SKIN_FX[this.style]) || {scan: 0.68, noise: 7, flicker: 1, vign: 1, decay: this.decay, smooth: 0, hud: "crt"}, this.fxOverride || {});
    if (this.style === "classic" || this.style === "circuit") fx.decay = this.decay;   // slider in the prototype
    // phosphor persistence
    for (let i = 0; i < this.phos.length; i++) this.phos[i] *= fx.decay;
    if (this.bg) { const b = this.bg, k = this.bright; for (let i = 0; i < b.length; i++) if (b[i] * k > this.phos[i]) this.phos[i] = b[i] * k; }
    const E = this.skinEyes;
    const extra = {orb: "drawOrb", pet: "drawPet", minimal: "drawMinimal", hud: "drawHudCore", pixel: "drawPixel", eye: "drawRealEye"}[this.style];
    if (extra) {
      this[extra]();
      ({minimal: () => this.drawHudMinimal(), hud: () => this.drawHudHolo(), pixel: () => this.drawHudPixel()})[fx.hud]();
      return this.composite(out, fx);
    }
    if (this.style === "circuit") {
      if (!this.circuit) this.circuit = this.buildCircuit();
      const c = this.circuit, k = this.bright;
      for (let i = 0; i < c.length; i++) if (c[i] * k > this.phos[i]) this.phos[i] = c[i] * k;
      if (E) { this.drawEyeCircuit(E.lx, E.ly, E.r, -1); this.drawEyeCircuit(E.rx, E.ry, E.r, 1); }
      else { this.drawEyeCircuit(34, 44, 16, -1); this.drawEyeCircuit(86, 44, 16, 1); }
      this.drawMouth();
      this.drawSpectrum();
      this.drawHudCircuit();
    } else {
      if (E) { this.drawEye(E.lx, E.ly, E.r, -1); this.drawEye(E.rx, E.ry, E.r, 1); }
      else { this.drawEye(33, 50, 22, -1); this.drawEye(87, 50, 22, 1); }
      this.drawScope();
      this.drawHud();
    }
    this.composite(out, fx);
  }

  composite(out, fx) {
    // glitch: horizontal row displacement bands
    this.rowShift.fill(0);
    if (this.glitch > 0) {
      const bands = 2 + (Math.random() * 3 | 0);
      for (let k = 0; k < bands; k++) {
        const y = Math.random() * SH | 0, h = 2 + (Math.random() * 10 | 0), s = (Math.random() * 14 - 7) | 0;
        for (let j = y; j < Math.min(SH, y + h); j++) this.rowShift[j] = s;
      }
    }
    const pal = this.state === "error" ? this.palettes.red : (this.customPal || this.palettes[this.paletteName]);
    const flicker = fx.flicker ? 0.965 + 0.035 * Math.random() : 1;
    const P = this.phos;
    for (let Y = 0; Y < SH; Y++) {
      const scan = (Y & 1) ? fx.scan : 1.0, sh = this.rowShift[Y];
      const fy = clamp(Y * 0.5 - 0.25, 0, LH - 1), y0 = fy | 0, y1 = Math.min(LH - 1, y0 + 1), wy = fy - y0;
      for (let X = 0; X < SW; X++) {
        const sx = clamp(X + sh, 0, SW - 1);
        let v;
        if (fx.smooth) {   // bilinear: soft modern look instead of chunky CRT pixels
          const fxx = clamp(sx * 0.5 - 0.25, 0, LW - 1), x0 = fxx | 0, x1 = Math.min(LW - 1, x0 + 1), wx = fxx - x0;
          v = (P[y0 * LW + x0] * (1 - wx) + P[y0 * LW + x1] * wx) * (1 - wy) + (P[y1 * LW + x0] * (1 - wx) + P[y1 * LW + x1] * wx) * wy;
        } else v = P[(Y >> 1) * LW + (sx >> 1)];
        const tv = this.text[Y * SW + sx]; if (tv > v) v = tv;
        v = v * scan * (fx.vign ? this.vign[Y * SW + X] : 1) * flicker + (fx.noise ? Math.random() * fx.noise : 0);
        const c = clamp(v | 0, 0, 255) * 3, o = (Y * SW + X) * 4;
        out[o] = pal[c]; out[o + 1] = pal[c + 1]; out[o + 2] = pal[c + 2]; out[o + 3] = 255;
      }
    }
  }
}
