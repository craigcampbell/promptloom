// The tapestry: every thread of work woven into one cloth, in three
// dimensions. Plain canvas and no library: the scene is small enough to
// project and paint back to front by hand, the way the graph runs its own
// physics. Loaded before app.js; it only calls app.js helpers at run time.
'use strict';

const TAP = {dx: 1, dz: 1.7, lift: 0.22, cap: 3};

// A stable 0..1 from a string, so a bead keeps its place between visits.
function tapHash(s) {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619);
  return ((h >>> 0) % 1000) / 1000;
}

const easeOut = t => 1 - Math.pow(1 - t, 3);

// Projects past the eighth would all share one grey; here each gets a hue of its own.
function tapColor(project) {
  const c = projectColor(project);
  return c === 'var(--other)' ? `hsl(${Math.round(tapHash(project) * 360)} 55% 58%)` : cssColor(c);
}

class Tapestry {
  constructor(canvas, data) {
    this.cv = canvas;
    this.ctx = canvas.getContext('2d');
    this.cam = {yaw: -0.7, pitch: 0.52, dist: 20, target: {x: 0, y: 0, z: 0}};
    this.goal = null;       // where the camera is easing to
    this.building = null;   // the weaving animation, while it runs
    this.reveal = 1;        // how much of the history is woven yet, 0..1
    this.selected = null;   // a bead
    this.thread = null;     // the thread being followed
    this.hover = null;
    this.dirty = true;
    this.onselect = () => {};
    this.onreveal = () => {};
    this.colors = {
      text: cssColor('var(--text-primary)'), muted: cssColor('var(--text-muted)'),
      border: cssColor('var(--border)'), bg: cssColor('var(--surface-1)'),
      accent: cssColor('var(--accent)'), shadow: cssColor('var(--text-primary)'),
    };
    this.resize();
    this.set(data);
    this.bind();
    const tick = () => {
      if (!document.body.contains(this.cv)) return; // the view changed
      if (this.animate() || this.dirty) { this.draw(); this.dirty = false; }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }

  resize() {
    const r = this.cv.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    this.w = r.width; this.h = r.height;
    this.cv.width = r.width * dpr; this.cv.height = r.height * dpr;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.focal = Math.min(this.w, this.h) * 1.15;
    this.dirty = true;
  }

  // Beads on strands: a column per day anything happened on (quiet stretches
  // take no room), a strand per project, a bead per prompt.
  set(data) {
    this.threads = data.threads;
    const beads = [];
    for (const t of data.threads)
      for (const s of t.sessions)
        for (const p of s.prompts)
          if (p.ts && !Number.isNaN(Date.parse(p.ts))) beads.push({thread: t, session: s, p, t: Date.parse(p.ts)});
    beads.sort((a, b) => a.t - b.t);
    beads.forEach((b, i) => { b.rank = i; });
    const days = [];
    const lanes = [];
    for (const b of beads) {
      const day = new Date(b.t).toDateString();
      if (days.at(-1) !== day) days.push(day);
      if (!lanes.includes(b.thread.project)) lanes.push(b.thread.project);
    }
    const dayAt = Object.fromEntries(days.map((d, i) => [d, i]));
    this.days = days; this.lanes = lanes;
    this.laneColor = lanes.map(tapColor);
    for (const b of beads) {
      const d = new Date(b.t);
      b.col = dayAt[d.toDateString()];
      b.row = lanes.indexOf(b.thread.project);
      b.x = (b.col + ((d.getHours() * 60 + d.getMinutes()) / 1440 - 0.5) * 0.8) * TAP.dx;
      b.z = (b.row + (tapHash(b.session.key + b.p.i) - 0.5) * 0.5) * TAP.dz;
      b.r = 0.07 + 0.05 * Math.min(2, Math.sqrt(b.p.n / 300));
      b.color = cssColor(modelColor(b.p.model));
    }
    this.beads = beads;
    this.byThread = {};
    for (const b of beads) (this.byThread[b.thread.id] ||= []).push(b);
    this.heights = new Float32Array(days.length * lanes.length);
    this.weave(1);
    this.fit();
  }

  // The cloth rises where the work was: a smoothed count of the beads woven so far.
  weave(reveal) {
    this.reveal = reveal;
    const D = this.days.length, L = this.lanes.length;
    const raw = new Float32Array(D * L);
    const n = Math.round(reveal * this.beads.length);
    for (let i = 0; i < n; i++) { const b = this.beads[i]; raw[b.col * L + b.row] += 0.22; }
    for (let c = 0; c < D; c++) for (let l = 0; l < L; l++) {
      let sum = 0, w = 0;
      for (let dc = -1; dc <= 1; dc++) for (let dl = -1; dl <= 1; dl++) {
        const cc = c + dc, ll = l + dl;
        if (cc < 0 || ll < 0 || cc >= D || ll >= L) continue;
        const k = !dc && !dl ? 2 : dc && dl ? 0.5 : 1;
        sum += raw[cc * L + ll] * k; w += k;
      }
      this.heights[c * L + l] = Math.min(TAP.cap, sum / w * 2);
    }
    this.shown = n;
    this.dirty = true;
    this.onreveal(n ? this.beads[n - 1] : null, n);
  }

  height(col, row) { return this.heights[col * this.lanes.length + row]; }

  // Frame the whole cloth: as close as keeps every corner of it in view.
  fit() {
    const D = this.days.length, L = this.lanes.length;
    const target = {x: (D - 1) * TAP.dx / 2, y: 0.6, z: (L - 1) * TAP.dz / 2};
    const goal = {yaw: -0.6, pitch: 0.62, dist: Math.max(D * TAP.dx, L * TAP.dz, 5) + 3, target};
    const corners = [];
    for (const x of [-1.3 * TAP.dx, D * TAP.dx])
      for (const y of [0, TAP.cap / 2])
        for (const z of [-0.6 * TAP.dz, (L - 0.3) * TAP.dz + 0.6]) corners.push([x, y, z]);
    const was = this.cam;
    this.cam = {...goal, target: {...target}};
    for (let i = 0; i < 4; i++) {
      let worst = 0;
      for (const [x, y, z] of corners) {
        const p = this.project(x, y, z);
        if (!p) { worst = 2; break; }
        worst = Math.max(worst, Math.abs(p[0] - this.w / 2) / (this.w / 2), Math.abs(p[1] - this.h / 2) / (this.h / 2));
      }
      this.cam.dist = Math.max(3, this.cam.dist * worst / 0.93);
    }
    goal.dist = this.cam.dist;
    this.cam = was;
    this.goal = goal;
  }

  // Watch it come together: beads land in the order they were typed.
  build(ms = 3800) {
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) { this.weave(1); return; }
    this.building = {start: performance.now(), ms};
  }

  animate() {
    let moving = false;
    if (this.building) {
      const t = Math.min(1, (performance.now() - this.building.start) / this.building.ms);
      this.weave(easeOut(t));
      if (t >= 1) this.building = null;
      moving = true;
    }
    if (this.goal) {
      const c = this.cam, g = this.goal, k = 0.14;
      c.yaw += (g.yaw - c.yaw) * k; c.pitch += (g.pitch - c.pitch) * k; c.dist += (g.dist - c.dist) * k;
      c.target.x += (g.target.x - c.target.x) * k; c.target.y += (g.target.y - c.target.y) * k; c.target.z += (g.target.z - c.target.z) * k;
      const left = Math.abs(g.yaw - c.yaw) + Math.abs(g.pitch - c.pitch) + Math.abs(g.dist - c.dist) / 10 +
        Math.hypot(g.target.x - c.target.x, g.target.y - c.target.y, g.target.z - c.target.z);
      if (left < 0.002) { Object.assign(c, {yaw: g.yaw, pitch: g.pitch, dist: g.dist}); Object.assign(c.target, g.target); this.goal = null; }
      moving = true;
    }
    return moving;
  }

  // World to screen: turn by the camera's yaw and pitch about its target, then
  // look along +z from `dist` away. Returns [x, y, depth, scale] or null when behind.
  project(x, y, z) {
    const c = this.cam;
    x -= c.target.x; y -= c.target.y; z -= c.target.z;
    const cy = Math.cos(c.yaw), sy = Math.sin(c.yaw), cp = Math.cos(c.pitch), sp = Math.sin(c.pitch);
    const x1 = x * cy - z * sy, z1 = x * sy + z * cy;
    const y2 = y * cp - z1 * sp, z2 = y * sp + z1 * cp;
    const depth = z2 + c.dist;
    if (depth < 0.2) return null;
    const f = this.focal / depth;
    return [this.w / 2 + x1 * f, this.h / 2 - y2 * f, depth, f];
  }

  draw() {
    const ctx = this.ctx, w = this.w, h = this.h;
    ctx.clearRect(0, 0, w, h);
    const D = this.days.length, L = this.lanes.length;
    if (!this.beads.length) {
      ctx.fillStyle = this.colors.muted; ctx.font = '13px system-ui, sans-serif'; ctx.textAlign = 'center';
      ctx.fillText('Nothing woven yet. Use any AI tool and the work shows up here.', w / 2, h / 2);
      return;
    }
    const P = (x, y, z) => this.project(x, y, z);
    const far = this.cam.dist * 2.4;
    const fog = d => Math.max(0.15, Math.min(1, 1.4 - d / far));
    const items = [];
    const focus = this.thread;

    // The strands: a ribbon per project, a cell per day, alternately lighter
    // and darker so it reads as woven; the cloth rises where the work was.
    const edgeH = (c, l) => (this.height(Math.max(0, c - 1), l) + this.height(Math.min(D - 1, c), l)) / 2;
    for (let l = 0; l < L; l++) {
      const z0 = (l - 0.42) * TAP.dz, z1 = (l + 0.42) * TAP.dz;
      const dimmed = focus && focus.project !== this.lanes[l];
      for (let c = 0; c < D; c++) {
        const x0 = (c - 0.5) * TAP.dx, x1 = (c + 0.5) * TAP.dx;
        const h0 = edgeH(c, l), h1 = edgeH(c + 1, l);
        const q = [P(x0, h0, z0), P(x1, h1, z0), P(x1, h1, z1), P(x0, h0, z1)];
        if (q.some(p => !p)) continue;
        const d = (q[0][2] + q[2][2]) / 2;
        const slope = h1 - h0;
        const alpha = (0.16 + ((c + l) % 2) * 0.08 + Math.min(0.4, this.height(c, l) * 0.14) + slope * 0.25) * fog(d) * (dimmed ? 0.35 : 1);
        items.push({d, f: () => {
          ctx.globalAlpha = Math.max(0.05, Math.min(0.9, alpha));
          ctx.fillStyle = this.laneColor[l];
          ctx.beginPath(); ctx.moveTo(q[0][0], q[0][1]);
          for (let i = 1; i < 4; i++) ctx.lineTo(q[i][0], q[i][1]);
          ctx.closePath(); ctx.fill();
        }});
      }
    }
    // The weft: a thin line across the strands for every day.
    for (let c = 0; c < D; c++) {
      const pts = [];
      for (let l = 0; l < L; l++) {
        const a = P(c * TAP.dx, this.height(c, l) + 0.01, (l - 0.42) * TAP.dz), b = P(c * TAP.dx, this.height(c, l) + 0.01, (l + 0.42) * TAP.dz);
        if (a && b) pts.push(a, b);
      }
      if (pts.length < 2) continue;
      const d = pts.reduce((s, p) => s + p[2], 0) / pts.length;
      items.push({d: d - 0.02, f: () => {
        ctx.globalAlpha = 0.5 * fog(d); ctx.strokeStyle = this.colors.border; ctx.lineWidth = 1;
        ctx.beginPath();
        for (let i = 0; i < pts.length; i += 2) { ctx.moveTo(pts[i][0], pts[i][1]); ctx.lineTo(pts[i + 1][0], pts[i + 1][1]); }
        ctx.stroke();
      }});
    }

    // The threads: each line of work runs through its beads in order, lifting
    // off the cloth between them; dashed where the work moved to another session.
    for (const t of this.threads) {
      const beads = (this.byThread[t.id] || []).filter(b => b.rank < this.shown);
      if (beads.length < 2) continue;
      const lit = focus === t, dim = focus && !lit;
      const color = this.laneColor[beads[0].row];
      for (let i = 0; i < beads.length - 1; i++) {
        const a = beads[i], b = beads[i + 1];
        const ay = this.height(a.col, a.row) + TAP.lift + a.r, by = this.height(b.col, b.row) + TAP.lift + b.r;
        const span = Math.hypot(b.x - a.x, b.z - a.z);
        const rise = 0.25 + 0.22 * Math.min(4, span);
        const pts = [];
        const n = span < 0.6 ? 3 : 8;
        for (let k = 0; k <= n; k++) {
          const u = k / n, m = 1 - u;
          const x = a.x * m + b.x * u, z = a.z * m + b.z * u;
          const y = ay * m + by * u + rise * 4 * u * m;
          const p = P(x, y, z);
          if (!p) { pts.length = 0; break; }
          pts.push(p);
        }
        if (!pts.length) continue;
        const d = (pts[0][2] + pts.at(-1)[2]) / 2 - 0.03;
        const moved = a.session !== b.session;
        items.push({d, f: () => {
          ctx.globalAlpha = (lit ? 0.95 : dim ? 0.12 : 0.55) * fog(d);
          ctx.strokeStyle = color;
          ctx.lineWidth = Math.max(0.8, Math.min(5, (lit ? 0.055 : 0.03) * pts[0][3]));
          ctx.setLineDash(moved ? [6, 5] : []);
          ctx.lineCap = 'round';
          ctx.beginPath(); ctx.moveTo(pts[0][0], pts[0][1]);
          for (let k = 1; k < pts.length; k++) ctx.lineTo(pts[k][0], pts[k][1]);
          ctx.stroke(); ctx.setLineDash([]);
        }});
      }
    }

    // The beads, with a shadow on the cloth so their height reads.
    for (let i = 0; i < this.beads.length; i++) {
      const b = this.beads[i];
      if (i >= this.shown) { b.sx = null; continue; }
      const floor = this.height(b.col, b.row);
      const p = P(b.x, floor + TAP.lift + b.r, b.z);
      if (!p) { b.sx = null; continue; }
      const [sx, sy, d, f] = p;
      const r = Math.max(1.5, b.r * f);
      b.sx = sx; b.sy = sy; b.sr = r; b.sd = d;
      const dim = focus && b.thread !== focus;
      const picked = b === this.selected, over = b === this.hover;
      const s = P(b.x, floor + 0.01, b.z);
      if (s) items.push({d: s[2] - 0.01, f: () => {
        ctx.globalAlpha = 0.16 * fog(d) * (dim ? 0.3 : 1); ctx.fillStyle = this.colors.shadow;
        ctx.beginPath(); ctx.ellipse(s[0], s[1], r * 1.1, r * 0.45, 0, 0, Math.PI * 2); ctx.fill();
      }});
      items.push({d, f: () => {
        ctx.globalAlpha = (dim ? 0.22 : 1) * fog(d);
        ctx.fillStyle = b.color;
        ctx.beginPath(); ctx.arc(sx, sy, r, 0, Math.PI * 2); ctx.fill();
        // a highlight, so it reads as a bead and not a dot
        ctx.globalAlpha = 0.45 * fog(d) * (dim ? 0.3 : 1); ctx.fillStyle = '#fff';
        ctx.beginPath(); ctx.arc(sx - r * 0.3, sy - r * 0.32, r * 0.32, 0, Math.PI * 2); ctx.fill();
        if (picked || over) {
          ctx.globalAlpha = 1; ctx.strokeStyle = picked ? this.colors.accent : this.colors.text; ctx.lineWidth = picked ? 2.5 : 1.5;
          ctx.beginPath(); ctx.arc(sx, sy, r + 3.5, 0, Math.PI * 2); ctx.stroke();
        }
      }});
    }

    items.sort((a, b) => b.d - a.d);
    for (const it of items) it.f();
    ctx.globalAlpha = 1;

    // Labels in screen space: the project on each strand, a date now and then.
    ctx.font = '600 12px system-ui, sans-serif';
    ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
    let lastY = -Infinity;  // strands too close together on screen share a label
    for (let l = 0; l < L; l++) {
      const p = P(-0.9 * TAP.dx, this.height(0, l) + 0.05, l * TAP.dz);
      if (!p || Math.abs(p[1] - lastY) < 13) continue;
      lastY = p[1];
      const dim = focus && focus.project !== this.lanes[l];
      ctx.globalAlpha = dim ? 0.35 : 1;
      ctx.lineWidth = 3; ctx.strokeStyle = this.colors.bg; ctx.fillStyle = this.laneColor[l];
      ctx.strokeText(this.lanes[l], p[0], p[1]); ctx.fillText(this.lanes[l], p[0], p[1]);
    }
    ctx.globalAlpha = 1;
    ctx.font = '11px system-ui, sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    const every = Math.max(1, Math.ceil(D / Math.max(3, Math.floor(w / 140))));
    for (let c = 0; c < D; c += every) {
      const p = P(c * TAP.dx, 0, (L - 0.4) * TAP.dz + 0.35);
      if (!p) continue;
      const day = new Date(this.days[c]).toLocaleDateString([], {month: 'short', day: 'numeric'});
      ctx.lineWidth = 3; ctx.strokeStyle = this.colors.bg; ctx.fillStyle = this.colors.muted;
      ctx.strokeText(day, p[0], p[1]); ctx.fillText(day, p[0], p[1]);
    }
    ctx.textBaseline = 'alphabetic';
  }

  hit(sx, sy) {
    let best = null, bd = Infinity;
    for (let i = 0; i < this.shown; i++) {
      const b = this.beads[i];
      if (b.sx == null) continue;
      const d = Math.hypot(b.sx - sx, b.sy - sy);
      if (d < b.sr + 4 && (d < bd || (best && Math.abs(d - bd) < 2 && b.sd < best.sd))) { best = b; bd = d; }
    }
    return best;
  }

  bind() {
    const cv = this.cv;
    let drag = null, moved = false;
    const pos = e => { const r = cv.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
    cv.onpointerdown = e => {
      const [sx, sy] = pos(e);
      moved = false;
      cv.setPointerCapture(e.pointerId);
      drag = {sx, sy, pan: e.button !== 0 || e.shiftKey, yaw: this.cam.yaw, pitch: this.cam.pitch, target: {...this.cam.target}};
      this.goal = null; this.stop();
      cv.classList.add('grabbing');
    };
    cv.onpointermove = e => {
      const [sx, sy] = pos(e);
      if (drag) {
        const dx = sx - drag.sx, dy = sy - drag.sy;
        if (Math.hypot(dx, dy) > 3) moved = true;
        if (drag.pan) {
          const c = this.cam, f = this.focal / c.dist;
          const cy = Math.cos(c.yaw), sy2 = Math.sin(c.yaw), cp = Math.cos(c.pitch), sp = Math.sin(c.pitch);
          const right = [cy, 0, -sy2], up = [-sy2 * sp, cp, -cy * sp];
          c.target.x = drag.target.x - right[0] * dx / f + up[0] * dy / f;
          c.target.y = drag.target.y - right[1] * dx / f + up[1] * dy / f;
          c.target.z = drag.target.z - right[2] * dx / f + up[2] * dy / f;
        } else {
          this.cam.yaw = drag.yaw - dx * 0.006;
          this.cam.pitch = Math.max(0.06, Math.min(1.5, drag.pitch + dy * 0.006));
        }
        this.dirty = true;
      } else {
        const b = this.hit(sx, sy);
        if (b !== this.hover) { this.hover = b; this.dirty = true; }
        cv.style.cursor = b ? 'pointer' : 'grab';
        if (b) showTip(e, `<div class="t">${esc(b.thread.name)}</div><div class="r">${esc(b.session.title)}</div>` +
          `<div class="r">${esc(b.p.model || 'model unknown')} · ${when(b.p.ts)}</div><div class="r">${esc(b.p.text.slice(0, 140))}${b.p.text.length > 140 ? '…' : ''}</div>`);
        else hideTip();
      }
    };
    cv.onpointerup = e => {
      const [sx, sy] = pos(e);
      cv.classList.remove('grabbing');
      if (drag && !moved && e.button === 0) {
        const b = this.hit(sx, sy);
        if (b) this.select(b); else this.clear();
      }
      drag = null;
    };
    cv.onpointerleave = () => { hideTip(); if (this.hover) { this.hover = null; this.dirty = true; } };
    cv.oncontextmenu = e => e.preventDefault();
    cv.onwheel = e => {
      e.preventDefault();
      this.goal = null;
      this.cam.dist = Math.min(80, Math.max(2.5, this.cam.dist * Math.exp(e.deltaY * 0.0012)));
      this.dirty = true;
    };
    window.addEventListener('resize', () => { if (document.body.contains(cv)) { this.resize(); this.draw(); } });
  }

  // Follow a thread: frame it, pick up at its first bead.
  follow(thread, at = null) {
    const beads = (this.byThread[thread.id] || []).filter(b => b.rank < this.shown);
    this.thread = thread;
    if (!beads.length) { this.selected = null; this.dirty = true; this.onselect(null, thread); return; }
    const xs = beads.map(b => b.x), zs = beads.map(b => b.z);
    const span = Math.max(Math.max(...xs) - Math.min(...xs), (Math.max(...zs) - Math.min(...zs)) * 1.3);
    const bead = at || beads[0];
    this.selected = bead;
    this.goal = {yaw: this.cam.yaw, pitch: Math.min(this.cam.pitch, 0.6), dist: Math.max(4.5, span * 0.9 + 4),
      target: {x: bead.x, y: this.height(bead.col, bead.row) + TAP.lift, z: bead.z}};
    this.dirty = true;
    this.onselect(bead, thread);
  }

  select(bead) {
    const sameThread = this.thread === bead.thread;
    this.thread = bead.thread;
    this.selected = bead;
    const target = {x: bead.x, y: this.height(bead.col, bead.row) + TAP.lift, z: bead.z};
    this.goal = {yaw: this.cam.yaw, pitch: this.cam.pitch, dist: sameThread ? this.cam.dist : Math.min(this.cam.dist, 7), target};
    this.dirty = true;
    this.onselect(bead, bead.thread);
  }

  clear() {
    this.stop();
    if (!this.thread && !this.selected) return;
    this.thread = null; this.selected = null; this.dirty = true;
    this.onselect(null, null);
  }

  // The next or previous bead along the thread being followed.
  step(dir) {
    if (!this.thread) return false;
    const beads = (this.byThread[this.thread.id] || []).filter(b => b.rank < this.shown);
    const i = beads.indexOf(this.selected);
    const next = beads[i + dir];
    if (!next) return false;
    this.select(next);
    return true;
  }

  // Walk the thread bead by bead until it ends or something interrupts.
  play() {
    this.stop();
    this.playing = setInterval(() => { if (!this.step(1) || !document.body.contains(this.cv)) this.stop(); }, 1700);
    this.onselect(this.selected, this.thread);
  }

  stop() {
    if (!this.playing) return;
    clearInterval(this.playing);
    this.playing = 0;
    this.onselect(this.selected, this.thread);
  }
}

// The view: the scene, a list of threads to follow, and a card for the bead in hand.
async function tapestryView(params) {
  const data = await api('tapestry');
  main.innerHTML = `
    <div class="row"><h1 class="grow">Tapestry</h1>
      <span class="muted small">drag to turn · shift-drag to move · scroll to zoom · click a bead · ← → walk the thread</span>
      <button id="tap-build" title="Watch the history come together again">▶ Weave it again</button>
      <button id="tap-fit" title="Frame the whole cloth">Whole cloth</button></div>
    <p class="muted small">Every prompt is a bead on its project's strand, time runs left to right, and the cloth rises where the work was busiest. A line of work runs through its beads; pick one to follow it.</p>
    <div class="tapestry">
      <canvas id="tap"></canvas>
      <div class="tap-threads" id="tap-threads"></div>
      <div class="tap-side" id="tap-side" hidden></div>
      <div class="tap-scrub"><input type="range" id="tap-time" min="0" max="1000" value="1000" title="How far the history is woven"><span class="small muted" id="tap-when"></span></div>
    </div>`;
  const scene = new Tapestry($('#tap'), data);
  const side = $('#tap-side'), list = $('#tap-threads'), scrub = $('#tap-time');
  const byId = Object.fromEntries(data.threads.map(t => [t.id, t]));
  const last = t => Math.max(0, ...t.sessions.flatMap(s => s.prompts.map(p => Date.parse(p.ts) || 0)));
  const ordered = data.threads.filter(t => scene.byThread[t.id]?.length).sort((a, b) => last(b) - last(a));
  const fullText = {};  // thread id -> "session key#i" -> whole prompt text

  const renderList = () => {
    list.innerHTML = `<div class="small muted">Threads, newest first</div>` + ordered.map(t =>
      `<div class="t${scene.thread === t ? ' on' : ''}" data-thread="${esc(t.id)}"><span class="swatch" style="background:${tapColor(t.project)}"></span><span class="grow">${esc(t.name)}</span><span class="muted">${scene.byThread[t.id].length}</span></div>`).join('');
    list.querySelectorAll('[data-thread]').forEach(el => { el.onclick = () => scene.follow(byId[el.dataset.thread]); });
  };
  const renderSide = async (bead, thread) => {
    renderList();
    if (!bead) {
      side.hidden = !thread;
      if (thread) side.innerHTML = `<div class="small muted">Thread</div><h3>${esc(thread.name)}</h3><p class="small muted">Nothing of it is woven yet; move the slider forward.</p>`;
      return;
    }
    const beads = scene.byThread[thread.id].filter(b => b.rank < scene.shown);
    const at = beads.indexOf(bead);
    const key = `${bead.session.key}#${bead.p.i}`;
    const text = fullText[thread.id]?.[key] ?? bead.p.text;
    side.hidden = false;
    side.innerHTML = `<div class="small muted"><a href="#/threads?open=${encodeURIComponent(thread.id)}">${esc(thread.name)}</a> · bead ${at + 1} of ${beads.length}</div>
      <h3>${esc(bead.session.title)}</h3>
      <div class="meta">${toolChip(bead.session.harness)} ${bead.p.model ? chip('model:' + bead.p.model) : ''} · ${when(bead.p.ts)}</div>
      <div class="text small" id="tap-text">${esc(text)}${text.length === 160 && bead.p.n > 160 ? '…' : ''}</div>
      <div class="row small" style="margin-top:8px">
        <button data-step="-1" title="Previous bead (←)"${at > 0 ? '' : ' disabled'}>←</button>
        <button data-step="1" title="Next bead (→)"${at < beads.length - 1 ? '' : ' disabled'}>→</button>
        <button data-play>${scene.playing ? '■ Stop' : '▶ Follow'}</button>
        <span class="grow"></span>
        <a href="${sessionHref(bead.session.key)}#p${bead.p.i}">Open</a>
        <a href="#/compose?idea=${encodeURIComponent(text)}">Reuse</a>
      </div>`;
    side.querySelectorAll('[data-step]').forEach(b => { b.onclick = () => scene.step(+b.dataset.step); });
    $('[data-play]', side).onclick = () => { if (scene.playing) scene.stop(); else scene.play(); };
    if (!fullText[thread.id]) {
      fullText[thread.id] = {};
      try {
        const d = await api('tapestry?thread=' + encodeURIComponent(thread.id));
        const t = d.threads.find(x => x.id === thread.id);
        for (const s of t?.sessions || []) for (const p of s.prompts) fullText[thread.id][`${s.key}#${p.i}`] = p.text;
        if (scene.selected === bead && $('#tap-text')) $('#tap-text').textContent = fullText[thread.id][key] ?? text;
      } catch { /* the snippet will do */ }
    }
  };
  scene.onselect = renderSide;
  scene.onreveal = (bead, n) => {
    scrub.value = Math.round(scene.reveal * 1000);
    $('#tap-when').textContent = bead ? `${n} of ${scene.beads.length} prompts, up to ${when(bead.p.ts)}` : 'nothing yet';
  };
  scrub.oninput = () => { scene.building = null; scene.weave(+scrub.value / 1000); if (scene.selected && scene.selected.rank >= scene.shown) scene.clear(); };
  $('#tap-build').onclick = () => { scene.clear(); scene.build(); };
  $('#tap-fit').onclick = () => { scene.clear(); scene.fit(); };
  const keys = e => {
    if (!document.body.contains(scene.cv)) { document.removeEventListener('keydown', keys); return; }
    if (e.target.matches('input, textarea, select') || $('dialog[open]')) return;
    if (e.key === 'ArrowRight') { scene.stop(); if (scene.step(1)) e.preventDefault(); }
    else if (e.key === 'ArrowLeft') { scene.stop(); if (scene.step(-1)) e.preventDefault(); }
    else if (e.key === 'Escape') scene.clear();
    else if (e.key === ' ' && scene.thread) { e.preventDefault(); if (scene.playing) scene.stop(); else scene.play(); }
  };
  document.addEventListener('keydown', keys);
  renderList();
  scene.onreveal(scene.beads.at(-1) || null, scene.beads.length);
  const want = params.get('thread') && byId[params.get('thread')];
  if (want) scene.follow(want);
  else scene.build();
}
