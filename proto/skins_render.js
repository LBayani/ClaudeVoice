// Extra skin renderers for EyesEngine (all draw phosphor intensity 0..255 into the 120x120 layer,
// so every skin can be recolored by its palette). The firmware port (cv_skins.cc) mirrors this file.
"use strict";

// Per-base post-processing: scan = brightness of odd rows (1 = off), noise amplitude, flicker,
// vignette, decay = trail persistence, smooth = bilinear upscale instead of chunky pixels, hud = text layout.
const SKIN_FX = {
  classic: {scan: 0.68, noise: 7, flicker: 1, vign: 1, decay: 0.55, smooth: 0, hud: "crt"},
  circuit: {scan: 0.68, noise: 7, flicker: 1, vign: 1, decay: 0.55, smooth: 0, hud: "circuit"},
  pixel:   {scan: 0.82, noise: 0, flicker: 0, vign: 1, decay: 0.0,  smooth: 0, hud: "pixel"},
  hud:     {scan: 0.8,  noise: 3, flicker: 1, vign: 1, decay: 0.35, smooth: 1, hud: "hud"},
  orb:     {scan: 1,    noise: 0, flicker: 0, vign: 0, decay: 0.55, smooth: 1, hud: "minimal"},
  pet:     {scan: 1,    noise: 0, flicker: 0, vign: 0, decay: 0.0,  smooth: 1, hud: "minimal"},
  minimal: {scan: 1,    noise: 0, flicker: 0, vign: 0, decay: 0.3,  smooth: 1, hud: "minimal"},
  eye:     {scan: 1,    noise: 0, flicker: 0, vign: 1, decay: 0.0,  smooth: 1, hud: "minimal"},
};

const sstep = (a, b, x) => { const t = clamp((x - a) / (b - a), 0, 1); return t * t * (3 - 2 * t); };
function sdRoundRect(px, py, hw, hh, r) {
  const qx = Math.abs(px) - hw + r, qy = Math.abs(py) - hh + r;
  return Math.hypot(Math.max(qx, 0), Math.max(qy, 0)) + Math.min(Math.max(qx, qy), 0) - r;
}

Object.assign(EyesEngine.prototype, {
  // ---------------- ORB: modern voice assistant ----------------
  drawOrb() {
    const s = this.state, e = this.emotion, L = this.level, t = this.t;
    const cx = 60 + (this.gx + this.mx) * 5, cy = 50 + (this.gy + this.my) * 3
      + (e === "happy" ? Math.sin(t * 7) * 1.5 : 0) + (e === "sad" ? 4 : 0);
    let R = 20 * (1 + 0.05 * Math.sin(t * 1.7)) * (1 + 0.3 * L);
    if (s === "listening") R *= 1.1; if (s === "sleep") R *= 0.55; if (s === "thinking") R *= 0.9;
    if (e === "surprised") R *= 1.15; if (this.blink > 0) R *= 1 - 0.12 * this.blink;
    const spike = e === "angry" ? 0.13 : 0.04, br = this.bright * (s === "error" ? 0.6 + 0.4 * Math.random() : 1);
    const ext = R * 2.4;
    for (let y = Math.floor(cy - ext); y <= cy + ext; y++) for (let x = Math.floor(cx - ext); x <= cx + ext; x++) {
      const dx = x - cx, dy = y - cy, d = Math.sqrt(dx * dx + dy * dy), a = Math.atan2(dy, dx);
      const rr = R * (1 + spike * Math.sin(3 * a + t * 1.3) + 0.035 * Math.sin(5 * a - t * 2.1) + 0.09 * L * Math.sin(7 * a + t * 11));
      const q = d / rr;
      let v = 235 * Math.exp(-q * q * 2.4) + 60 * Math.exp(-q * 1.5);          // core + soft halo
      if (q < 0.45) v += 40 * (1 - q / 0.45);                                   // hot centre
      v += 18 * Math.sin(a * 2 + t * 0.7 + q * 3) * Math.exp(-q * q * 3);      // slow inner swirl
      if (v > 2) this.put(x, y, v * br);
    }
    // ripples: listening / speaking
    if (s === "listening" || s === "speaking") {
      for (let k = 0; k < 3; k++) {
        const p = (t * (s === "speaking" ? 0.9 : 0.55) + k / 3) % 1, rad = R * (1.15 + p * 1.1);
        this.ringPx(cx, cy, rad, 1.1, (1 - p) * (90 + 120 * L) * br);
      }
    }
    if (s === "thinking" || s === "connecting") {                               // orbiting comets
      for (let k = 0; k < 2; k++) {
        const ang = t * 3.2 + k * Math.PI;
        for (let j = 0; j < 22; j++) {
          const aa = ang - j * 0.06, rad = R * 1.45;
          this.dot(cx + Math.cos(aa) * rad, cy + Math.sin(aa) * rad, 1.6, (1 - j / 22) * 220 * br);
        }
      }
    }
  },

  // ---------------- PET: soft expressive robot eyes ----------------
  drawPet() {
    const s = this.state, e = this.emotion, L = this.level;
    const gx = (this.gx + this.mx) * 11, gy = (this.gy + this.my) * 7;
    for (const side of [-1, 1]) {
      let w = 12.5, h = 15, cy = 48 + gy;
      const cx = 60 + side * 23 + gx;
      if (s === "listening") { w *= 1.05; h *= 1.08 + 0.06 * L; }
      if (s === "speaking") { h *= 1 - 0.1 * L; w *= 1 + 0.05 * L; }
      if (e === "surprised") { h *= 1.22; w *= 0.95; }
      if (e === "sleepy" || s === "sleep") { h *= 0.32; cy += 7; }
      if (s === "thinking" && side === 1) h *= 0.75;                           // one-eye squint
      if (gx * side > 0) { const k = 1 + 0.1 * Math.abs(gx) / 11; w *= k; h *= k; } // near eye bigger
      h *= 1 - 0.93 * this.blink;
      const r = Math.min(w, h) * 0.62;
      const inner = -side;                                                       // direction of the nose
      for (let y = Math.floor(cy - h - 2); y <= cy + h + 2; y++) for (let x = Math.floor(cx - w - 2); x <= cx + w + 2; x++) {
        const dx = x - cx, dy = y - cy;
        let a = 1 - sstep(-0.8, 0.8, sdRoundRect(dx, dy, w, h, r));
        if (a <= 0) continue;
        const nx = clamp(dx / w, -1, 1), inn = (nx * inner + 1) / 2;           // 0 outer .. 1 inner
        // emotion "lids": cut the shape from above / below
        let top = -h - 1, bot = h + 1;
        if (e === "angry") top = -h + h * 1.1 * inn;
        if (e === "sad") top = -h + h * 1.0 * (1 - inn);
        if (e === "happy") bot = h - h * 1.35 * (1 - nx * nx);
        a *= sstep(top - 0.8, top + 0.8, dy) * (1 - sstep(bot - 0.8, bot + 0.8, dy));
        const shade = 0.82 + 0.18 * (1 - (dy + h) / (2 * h));                   // lighter at the top
        if (a > 0.01) this.put(x, y, 235 * a * shade * this.bright);
      }
    }
    if (s === "speaking" || e === "happy") {                                     // small mouth
      const mh = s === "speaking" ? 1 + L * 4 : 1.2, mw = e === "happy" ? 8 : 6;
      for (let y = 78 - 6; y <= 78 + 6; y++) for (let x = 60 - mw - 2; x <= 60 + mw + 2; x++) {
        const a = 1 - sstep(-0.8, 0.8, sdRoundRect(x - 60 - gx * 0.3, y - 78 - gy * 0.3, mw, mh, Math.min(mw, mh)));
        if (a > 0.01) this.put(x, y, 200 * a * this.bright);
      }
    }
  },

  // ---------------- MINIMAL: two dots and a living line ----------------
  drawMinimal() {
    const s = this.state, e = this.emotion, L = this.level, t = this.t;
    const gx = (this.gx + this.mx) * 6, gy = (this.gy + this.my) * 4;
    let rad = 4.5 * (s === "listening" ? 1.15 + 0.25 * L : 1);
    if (e === "surprised") rad *= 1.3;
    for (const side of [-1, 1]) {
      const cx = 60 + side * 17 + gx, cy = 50 + gy, ry = rad * (1 - 0.85 * this.blink) * (e === "sleepy" || s === "sleep" ? 0.3 : 1);
      for (let y = Math.floor(cy - rad - 2); y <= cy + rad + 2; y++) for (let x = Math.floor(cx - rad - 2); x <= cx + rad + 2; x++) {
        const q = Math.hypot((x - cx) / rad, (y - cy) / Math.max(0.6, ry));
        const a = 1 - sstep(0.85, 1.15, q);
        if (a > 0.01) this.put(x, y, 240 * a * this.bright);
      }
    }
    // the line: smile / frown / voice wave / loading dots
    const y0 = 70 + gy * 0.5;
    if (s === "thinking" || s === "connecting") {
      for (let k = 0; k < 3; k++) { const b = 0.5 + 0.5 * Math.sin(t * 6 - k * 0.9); this.dot(52 + k * 8 + gx * 0.5, y0, 1.5 + b, (120 + 120 * b) * this.bright); }
      return;
    }
    for (let x = 42; x <= 78; x += 0.5) {
      const u = (x - 60) / 18, env = Math.cos(u * Math.PI / 2);
      let y = y0;
      if (e === "happy") y += 4 * (1 - u * u); if (e === "sad") y -= 3 * (1 - u * u);
      if (s === "speaking" || s === "listening") y += Math.sin(x * 0.55 - t * 14) * L * 6 * env;
      this.dot(x + gx * 0.5, y, 1.1, 200 * env * this.bright + 30);
    }
  },

  // ---------------- HUD: holographic AI core ----------------
  drawHudCore() {
    const s = this.state, L = this.level, t = this.t, rot = this.irisRot;
    const cx = 60 + (this.gx + this.mx) * 4, cy = 54 + (this.gy + this.my) * 3, br = this.bright;
    const grow = s === "listening" ? 2 + L * 3 : L * 2;
    for (let y = Math.floor(cy - 44); y <= cy + 44; y++) for (let x = Math.floor(cx - 44); x <= cx + 44; x++) {
      const dx = x - cx, dy = y - cy, d = Math.sqrt(dx * dx + dy * dy);
      if (d > 43) continue;
      const a = Math.atan2(dy, dx), f = (v) => ((v / (Math.PI * 2)) % 1 + 1) % 1;
      let v = 0;
      if (d < 7 + L * 2) v = 240 - d * 8;                                                    // core
      if (Math.abs(d - 10) < 0.7) v = Math.max(v, 150);
      if (Math.abs(d - 16) < 1 && f(a + rot) * 24 % 1 < 0.6) v = Math.max(v, 140 + 80 * L);  // dashed ring
      if (d > 21 && d < 25 && f(a - rot * 0.5) * 36 % 1 < 0.18) v = Math.max(v, f(a - rot * 0.5) * 12 % 1 < 0.08 ? 220 : 110);
      if (Math.abs(d - (31 + grow)) < 1.3) {                                                  // three arcs
        const p = f(a + rot * 0.8);
        if (p < 0.14 || (p > 0.33 && p < 0.55) || (p > 0.7 && p < 0.78)) v = Math.max(v, 210);
      }
      if (Math.abs(d - 39) < 0.6) v = Math.max(v, 70);
      if (s === "speaking") { const rw = 12.5 + L * 5 * Math.sin(10 * a + t * 25); if (Math.abs(d - rw) < 0.8) v = Math.max(v, 230); }
      if (s === "thinking" || s === "connecting") {                                           // radar sweep
        const diff = (((t * 1.6) - a / (Math.PI * 2)) % 1 + 1) % 1;
        if (d > 10 && d < 39 && diff < 0.18) v = Math.max(v, (1 - diff / 0.18) * 120);
      }
      if (v > 0) this.put(x, y, v * br);
    }
    for (let k = 0; k < 4; k++) {                                                             // markers
      const a = k * Math.PI / 2 + rot * 0.2;
      this.dot(cx + Math.cos(a) * 41.5, cy + Math.sin(a) * 41.5, 1.4, 200 * br);
    }
  },

  // ---------------- PIXEL: 8-bit game eyes ----------------
  drawPixel() {
    const s = this.state, e = this.emotion, L = this.level, B = 6;   // 20x20 blocks of 6 px
    const block = (bx, by, v) => { for (let y = 0; y < B - 1; y++) for (let x = 0; x < B - 1; x++) this.put(bx * B + x, by * B + y, v); };
    const shapes = {
      open: ["0110", "1111", "1111", "1111", "0110"], happy: ["0000", "0110", "1001", "0000", "0000"],
      sad: ["0000", "0000", "1111", "1111", "0110"], angryL: ["1000", "1100", "1110", "1111", "0110"],
      angryR: ["0001", "0011", "0111", "1111", "0110"], closed: ["0000", "0000", "0000", "1111", "0000"],
      wide: ["1111", "1111", "1111", "1111", "1111"],
    };
    const px = Math.round(clamp((this.gx + this.mx) * 1.6, -1, 1)), py = Math.round(clamp((this.gy + this.my) * 1.6, -1, 1));
    for (const side of [-1, 1]) {
      let sh = shapes.open;
      if (e === "happy") sh = shapes.happy; else if (e === "sad") sh = shapes.sad;
      else if (e === "angry") sh = side < 0 ? shapes.angryL : shapes.angryR; else if (e === "surprised") sh = shapes.wide;
      if (this.blink > 0.5 || e === "sleepy" || s === "sleep") sh = shapes.closed;
      const ox = (side < 0 ? 4 : 12), oy = 5;
      const pupil = sh === shapes.open || sh === shapes.wide || sh === shapes.sad || sh === shapes.angryL || sh === shapes.angryR;
      for (let r = 0; r < 5; r++) for (let c = 0; c < 4; c++) {
        if (sh[r][c] !== "1") continue;
        const isPupil = pupil && (c === 1 + px || c === 2 + px) && (r === 2 + py || r === 3 + py) && r >= 1;
        block(ox + c, oy + r, isPupil ? 40 : 230 * this.bright);
      }
    }
    // mouth
    const mouth = s === "speaking" ? (L > 0.55 ? ["0111110", "1000001", "0111110"] : L > 0.2 ? ["0111110", "0111110"] : ["0111110"])
      : e === "happy" ? ["1000001", "0111110"] : e === "sad" ? ["0111110", "1000001"] : s === "thinking" ? ["0101010"] : ["0011100"];
    mouth.forEach((row, r) => [...row].forEach((ch, c) => { if (ch === "1") block(6 + c, 13 + r, 210 * this.bright); }));
  },

  // ---------------- EYE: one realistic eye ----------------
  drawRealEye() {
    const s = this.state, e = this.emotion, t = this.t;
    const cx = 60, cy = 50, A = 40, Bv = 23;
    let open = 1; if (s === "sleep" || e === "sleepy") open = 0.28; if (e === "surprised") open = 1.22; if (s === "listening") open = 1.08;
    open *= 1 - this.blink; const lower = e === "happy" ? 0.55 : 1;
    const Ri = 14, gx = (this.gx + this.mx) * A * 0.42, gy = (this.gy + this.my) * Bv * 0.4;
    const pr = Ri * (0.26 + 0.28 * this.pupil);
    const tilt = e === "angry" ? 0.25 : e === "sad" ? -0.2 : 0;
    for (let y = Math.floor(cy - Bv * 1.4 - 4); y <= cy + Bv * 1.3; y++) for (let x = cx - A - 3; x <= cx + A + 3; x++) {
      const dx = x - cx, dy = y - cy, nx = dx / A;
      if (Math.abs(nx) > 1) continue;
      const shape = Math.pow(1 - nx * nx, 0.62);
      const up = -Bv * open * shape + tilt * dx * 0.3, lo = Bv * 0.95 * lower * shape;
      const du = dy - up, dl = lo - dy;
      // eyelashes above the upper lid
      if (du < 0 && du > -5 && Math.abs(nx) < 0.9) {
        const lash = ((x + 400) % 4 === 0) && du > -4 * (1 - Math.abs(nx) * 0.7);
        if (lash) this.put(x + Math.round(nx * 2 * (du / 4)), y, 110 * this.bright);
      }
      if (Math.abs(du) < 0.9 && Math.abs(nx) < 0.98) this.put(x, y, 150 * this.bright);   // lid rim
      if (du < 0 || dl < 0) continue;
      let v = 150 * (1 - 0.55 * (nx * nx + (dy / Bv) * (dy / Bv)));                          // sclera shading
      v *= 0.55 + 0.45 * sstep(0, 6, du);                                                    // lid shadow
      const ix = dx - gx, iy = dy - gy, ir = Math.hypot(ix, iy);
      if (ir < Ri) {
        const a = Math.atan2(iy, ix), rn = ir / Ri;
        const fib = 0.55 + 0.45 * (0.5 + 0.5 * Math.sin(a * 23 + Math.sin(a * 7) * 2 + rn * 6))
                         * (0.65 + 0.35 * Math.sin(a * 41 + rn * 11));
        let iv = (70 + 120 * fib) * (0.75 + 0.5 * Math.exp(-Math.pow((rn - 0.5) / 0.12, 2)));
        if (rn > 0.86) iv *= 1 - (rn - 0.86) / 0.14 * 0.75;                                   // limbal ring
        if (ir < pr) iv = 6; else if (ir < pr + 1) iv = Math.min(iv, 40);
        v = iv * (0.6 + 0.4 * sstep(0, 7, du));
      }
      const h1 = Math.hypot(ix + Ri * 0.38, iy + Ri * 0.42), h2 = Math.hypot(ix - Ri * 0.3, iy - Ri * 0.38);
      if (h1 < 3.2) v = Math.max(v, 255 * (1 - h1 / 3.2) + 120);
      if (h2 < 1.4) v = Math.max(v, 200);
      this.put(x, y, v * this.bright);
    }
  },

  // helpers
  dot(cx, cy, r, v) {
    for (let y = Math.floor(cy - r - 1); y <= cy + r + 1; y++) for (let x = Math.floor(cx - r - 1); x <= cx + r + 1; x++) {
      const a = 1 - sstep(r - 0.6, r + 0.6, Math.hypot(x - cx, y - cy));
      if (a > 0.01) this.put(x, y, v * a);
    }
  },
  ringPx(cx, cy, r, w, v) {
    const n = Math.ceil(r * 6.4);
    for (let i = 0; i < n; i++) { const a = i / n * Math.PI * 2; this.put(Math.round(cx + Math.cos(a) * r), Math.round(cy + Math.sin(a) * r), v); }
  },

  // ---------------- text layouts ----------------
  drawHudMinimal() {
    this.text.fill(0);
    const hi = 235, dim = 120;
    const STATUS = {idle: "", listening: "слушаю", thinking: "думаю", speaking: "", connecting: "подключение", error: "нет связи", sleep: ""};
    const st = this.notifLeft > 0 ? this.notif : STATUS[this.state];
    if (st) this.drawText(st, (SW - st.length * 8) / 2, 8, dim);
    const shown = this.message.slice(0, Math.floor(this.messageShown));
    const lines = this.wrap((this.messageRole === "user" ? "" : "") + shown, 26).slice(-2);
    lines.forEach((l, i) => this.drawText(l, (SW - l.length * 8) / 2, 206 + i * 14, this.messageRole === "user" ? dim : hi));
  },
  drawHudHolo() {
    this.text.fill(0);
    const hi = 230, dim = 130;
    this.drawText(this.header.slice(0, 18), 6, 4, hi);
    this.drawText(this.unit, 6, 16, dim);
    const right = (this.charging ? "+" : "") + this.battery + "%";
    this.drawText(right, SW - 6 - right.length * 8, 4, dim);
    const EN = {idle: "STANDBY", listening: "LISTENING", thinking: "ANALYSIS", speaking: "OUTPUT", connecting: "LINK", error: "NO SIGNAL", sleep: "SLEEP"};
    this.drawText(EN[this.state], SW - 6 - EN[this.state].length * 8, 16, dim);
    if (!this.hexH || this.t - this.hexHT > 0.4) { this.hexHT = this.t; this.hexH = [0, 1, 2, 3].map(() => (Math.random() * 65536 | 0).toString(16).toUpperCase().padStart(4, "0")); }
    this.hexH.forEach((h, i) => { this.drawText(h, 4, 70 + i * 13, 90); this.drawText(h.split("").reverse().join(""), SW - 36, 70 + i * 13, 90); });
    const shown = this.message.slice(0, Math.floor(this.messageShown));
    this.wrap(shown, 28).slice(-2).forEach((l, i) => this.drawText(l, 6, 208 + i * 13, this.messageRole === "user" ? dim : hi));
  },
  drawHudPixel() {
    this.text.fill(0);
    const EN = {idle: "READY", listening: "LISTEN", thinking: "WAIT...", speaking: "TALK", connecting: "LOADING", error: "GAME OVER", sleep: "ZZZ"};
    const st = EN[this.state]; this.drawText(st, (SW - st.length * 8) / 2, 4, 200);
    const shown = this.message.slice(0, Math.floor(this.messageShown));
    this.wrap(shown, 28).slice(-2).forEach((l, i) => this.drawText(l, 6, 208 + i * 13, 230));
  },
});
