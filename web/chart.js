// Steel's chart engine: one canvas, several panes, time on the x axis so gaps show as gaps.
// Layers: candles, volume (buy/sell), delta and CVD (from the tape), a session VWAP anchored at
// the UTC day, liquidation markers, the model's resolved calls where they were made, and the
// live forecast fan ahead of the last candle. Wheel zooms around the cursor, drag pans, double
// click resets, arrows step. No build step: a plain class on window.
(function () {
  const TF_LABEL = s => s >= 86400 ? `${s / 86400}d` : s >= 3600 ? `${s / 3600}h` : s >= 60 ? `${s / 60}m` : `${s}s`;
  const fmtK = v => { const a = Math.abs(v); return a >= 1e9 ? (v / 1e9).toFixed(2) + "B" : a >= 1e6 ? (v / 1e6).toFixed(2) + "M" : a >= 1e3 ? (v / 1e3).toFixed(1) + "k" : v.toFixed(0); };
  const fmtP = v => v == null || !isFinite(v) ? "·" : v >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 2 }) : v >= 1 ? v.toFixed(4) : v.toPrecision(4);
  const fmtT = (t, tf) => { const d = new Date(t * 1000); const hh = String(d.getUTCHours()).padStart(2, "0"), mm = String(d.getUTCMinutes()).padStart(2, "0"); return tf >= 86400 ? d.toISOString().slice(5, 10) : (tf >= 3600 ? `${d.toISOString().slice(5, 10)} ${hh}:00` : `${hh}:${mm}`); };

  class SteelChart {
    constructor(canvas, opts = {}) {
      this.canvas = canvas; this.opts = opts; this.C = opts.palette || {};
      this.layers = Object.assign({ volume: true, delta: true, cvd: true, vwap: true, liq: true, calls: true, forecast: true, grid: true, footprint: true, bubbles: true, levels: true, profile: true, tpo: false, vwapBands: true, heatmap: false, liqmap: false, positioning: false, gex: false,
        ema: false, bollinger: false, supertrend: false, ichimoku: false, sessions: false, structure: false, pvsra: false, rsi: false, macd: false, oi: false, oiZ: false, premium: false, fills: true, transfers: true }, opts.layers || {});
      this.prem = null;
      this.ind = {};                                                   // indicator series cached per data payload
      this.hm = null; this.lm = null; this.liqBand = 0; this.pos = null; this.opt = null;              // the order-book heatmap; the modeled liquidation map (band 0 = all)                                                 // the order-book heatmap payload
      this.levels = null; this.anchor = null;                       // period levels from /api/levels; the anchored VWAP's start time (shift-click)
      this.data = null; this.view = null; this.cursor = null; this.drag = null; this.readout = opts.readout || null;
      this.fp = null; this.fpByT = new Map(); this.cvdBand = 0;      // the footprint payload, by candle time; CVD from size band index upward (0 = all)
      this.onView = opts.onView || null;
      canvas.addEventListener("wheel", e => this.onWheel(e), { passive: false });
      canvas.addEventListener("mousedown", e => { const r = canvas.getBoundingClientRect(), ray = this.rayAt(e.clientY - r.top); if (ray) { this.rayDrag = ray; return; } this.drag = { x: e.clientX, from: this.view.from, to: this.view.to }; });
      addEventListener("mousemove", e => this.onMove(e));
      addEventListener("mouseup", () => { if (this.rayDrag && this.ticket && this.onTicketChange) this.onTicketChange({ ...this.ticket }); this.rayDrag = null; this.drag = null; });
      canvas.addEventListener("mouseleave", () => { this.cursor = null; this.draw(); });
      canvas.addEventListener("dblclick", () => { this.resetView(); this.draw(); });
      canvas.addEventListener("click", e => { if (!e.shiftKey || !this.view) return; const r = canvas.getBoundingClientRect(); const { padL, plotW } = this.geom(r.width); const t = this.view.from + (e.clientX - r.left - padL) / plotW * (this.view.to - this.view.from); this.anchor = this.anchor && Math.abs(this.anchor - t) < this.data.tf ? null : t; this.draw(); });
      canvas.tabIndex = 0;
      canvas.addEventListener("keydown", e => { if (!this.view) return; const span = this.view.to - this.view.from, step = span * 0.1;
        if (e.key === "ArrowLeft") { this.view.from -= step; this.view.to -= step; } else if (e.key === "ArrowRight") { this.view.from += step; this.view.to += step; }
        else if (e.key === "+" || e.key === "=") { this.view.from += span * 0.15; } else if (e.key === "-") { this.view.from -= span * 0.15; } else return;
        e.preventDefault(); this.draw(); });
      if (window.ResizeObserver) new ResizeObserver(() => this.draw()).observe(canvas);
    }
    setData(d) {
      const keepView = this.data && this.view && this.data.pair === d.pair && this.data.tf === d.tf && !this.followLatest;
      this.data = d; this.ind = {};
      const c = d.candles || [];
      if (!keepView || !this.view) this.resetView();
      else if (c.length && this.view.to >= this._lastT() - d.tf) { const span = this.view.to - this.view.from; this.view.to = c[c.length - 1].t + d.tf * 4; this.view.from = this.view.to - span; }
      this.draw();
    }
    setLevels(lv) { this.levels = lv; this.draw(); }
    setHeatmap(hm) { this.hm = hm; this.draw(); }
    setLiqmap(lm) { this.lm = lm; this.draw(); }
    setPositioning(p) { this.pos = p; this.draw(); }
    setOptions(o) { this.opt = o; this.draw(); }
    setPremium(p) { this.prem = p; this.draw(); }
    setTicket(t) { this.ticket = t; this.draw(); }
    rayAt(y) {                                                     // which ticket ray sits within 6 px of a canvas y
      if (!this.ticket || !this.view || !this.data) return null; const r = this.canvas.getBoundingClientRect(); const G = this.geom(r.width, r.height), price = G.panes.find(p => p.id === "price"); if (!price) return null;
      const { lo, hi } = this.lastRange || {}; if (lo == null) return null; const ys = v => price.top + (hi - v) / (hi - lo) * (price.bottom - price.top);
      for (const k of ["limit", "stop", "target"]) { const v = this.ticket[k]; if (v && Math.abs(ys(v) - y) <= 6) return k; } return null;
    }
    priceAt(y) { const r = this.canvas.getBoundingClientRect(); const G = this.geom(r.width, r.height), price = G.panes.find(p => p.id === "price"); const { lo, hi } = this.lastRange || {}; if (!price || lo == null) return null; return hi - (y - price.top) / (price.bottom - price.top) * (hi - lo); }
    setCustom(name, data) { this.custom = this.custom || {}; if (data) this.custom[name] = data; else delete this.custom[name]; this.draw(); }
    customOn() { return Object.entries(this.custom || {}).filter(([k]) => this.layers["cus_" + k]); }
    setFootprint(fp) { this.fp = fp; this.fpByT = new Map(); if (fp && fp.candles) fp.candles.forEach(c => this.fpByT.set(c.t, c)); this.draw(); }
    _delta(c) { const f = this.fpByT.get(c.t); if (this.cvdBand > 0 && f && f.cvd_bands) return f.cvd_bands.slice(this.cvdBand).reduce((a, b) => a + b, 0); return (c.vb || 0) - (c.vs || 0); }
    _lastT() { const c = this.data && this.data.candles; return c && c.length ? c[c.length - 1].t : 0; }
    resetView() {
      const d = this.data; if (!d) return; const c = d.candles || [];
      const n = Math.min(c.length, this.opts.visible || 140);
      const last = c.length ? c[c.length - 1].t : Date.now() / 1000;
      // room ahead of the last candle for the forecast fan: its horizon, but never more than a third of the window, so a short tape is not all fan
      const horizon = this.layers.forecast && d.forecast ? (d.forecast.horizon_s || 0) : 0;
      const ahead = Math.max(d.tf * 4, Math.min(horizon, n * d.tf * 0.5));
      this.view = { from: last - n * d.tf, to: last + ahead };
      this.followLatest = true;
    }
    onWheel(e) {
      if (!this.view) return; e.preventDefault();
      const r = this.canvas.getBoundingClientRect(), x = e.clientX - r.left;
      const { padL, plotW } = this.geom(r.width); const f = Math.max(0, Math.min(1, (x - padL) / plotW));
      const span = this.view.to - this.view.from, k = e.deltaY > 0 ? 1.15 : 1 / 1.15;
      const at = this.view.from + f * span, ns = Math.max(this.data.tf * 10, Math.min(span * k, this.data.tf * 5000));
      this.view.from = at - f * ns; this.view.to = at + (1 - f) * ns; this.followLatest = false; this.draw();
    }
    onMove(e) {
      const r = this.canvas.getBoundingClientRect();
      if (this.rayDrag && this.ticket) { const p = this.priceAt(e.clientY - r.top); if (p && p > 0) { this.ticket[this.rayDrag] = p; this.ticket.edited = true; this.draw(); } return; }
      if (this.ticket && !this.drag) this.canvas.style.cursor = this.rayAt(e.clientY - r.top) ? "ns-resize" : "";
      if (this.drag && this.view) { const { plotW } = this.geom(r.width); const dt = (e.clientX - this.drag.x) / plotW * (this.drag.to - this.drag.from);
        this.view.from = this.drag.from - dt; this.view.to = this.drag.to - dt; this.followLatest = false; this.draw(); return; }
      if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) { if (this.cursor) { this.cursor = null; this.draw(); } return; }
      this.cursor = { x: e.clientX - r.left, y: e.clientY - r.top }; this.draw();
    }
    geom(w, h) {
      const padL = 10, padR = 72, padT = 8, padB = 20;
      const L = this.layers, panes = [];
      const cus = this.customOn().filter(([, v]) => v.pane === "own" && v.series);
      const sub = (L.delta || L.cvd ? 1 : 0) + (L.volume ? 1 : 0) + (L.positioning ? 1 : 0) + (L.rsi ? 1 : 0) + (L.macd ? 1 : 0) + (L.oi ? 1 : 0) + (L.premium ? 1 : 0) + cus.length;
      const subH = Math.max(36, Math.min(90, (h || 300) * (sub >= 4 ? 0.1 : sub >= 3 ? 0.14 : 0.16)));
      const bottom = (h || 300) - padB;
      let y = bottom;
      cus.slice().reverse().forEach(([k]) => { panes.unshift({ id: "cus:" + k, top: y - subH, bottom: y }); y -= subH + 6; });
      if (L.premium) { panes.unshift({ id: "prem", top: y - subH, bottom: y }); y -= subH + 6; }
      if (L.oi) { panes.unshift({ id: "oi", top: y - subH, bottom: y }); y -= subH + 6; }
      if (L.macd) { panes.unshift({ id: "macd", top: y - subH, bottom: y }); y -= subH + 6; }
      if (L.rsi) { panes.unshift({ id: "rsi", top: y - subH, bottom: y }); y -= subH + 6; }
      if (L.positioning) { panes.unshift({ id: "pos", top: y - subH, bottom: y }); y -= subH + 6; }
      if (L.delta || L.cvd) { panes.unshift({ id: "flow", top: y - subH, bottom: y }); y -= subH + 6; }
      if (L.volume) { panes.unshift({ id: "vol", top: y - subH, bottom: y }); y -= subH + 6; }
      panes.unshift({ id: "price", top: padT, bottom: y });
      return { padL, padR, padT, padB, plotW: w - padL - padR, panes, sub };
    }
    draw() {
      const cv = this.canvas, r = cv.getBoundingClientRect(); if (!r.width || !r.height) return;
      const dpr = window.devicePixelRatio || 1, w = r.width | 0, h = r.height | 0;
      if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
      const ctx = cv.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
      const C = this.C, d = this.data; ctx.font = "11px ui-monospace, Menlo, monospace"; ctx.textBaseline = "middle";
      if (!d || !(d.candles || []).length) { ctx.fillStyle = C.muted || "#888"; ctx.fillText(d ? "no prints in this window yet" : "loading…", 14, 24); return; }
      const G = this.geom(w, h), { padL, plotW } = G, V = this.view, span = V.to - V.from, tf = d.tf;
      const xs = t => padL + (t - V.from) / span * plotW;
      const all = d.candles, vis = all.filter(c => c.t + tf >= V.from && c.t <= V.to);
      const price = G.panes.find(p => p.id === "price");
      // price range over the visible candles, the forecast fan and the calls
      let lo = Infinity, hi = -Infinity;
      vis.forEach(c => { lo = Math.min(lo, c.l); hi = Math.max(hi, c.h); });
      const fc = this.layers.forecast && d.forecast && d.forecast.q ? d.forecast : null;
      if (fc) { const f = fc.q.map(q => fc.price * Math.exp(q / 1e4)); lo = Math.min(lo, f[0]); hi = Math.max(hi, f[4]); }
      if (!isFinite(lo)) { lo = all[all.length - 1].l; hi = all[all.length - 1].h; }
      const pad = (hi - lo || lo * 1e-3) * 0.06; lo -= pad; hi += pad;
      const ys = v => price.top + (hi - v) / (hi - lo) * (price.bottom - price.top);
      this.lastRange = { lo, hi };
      const cw = Math.max(1, Math.min(tf / span * plotW * 0.7, this.layers.footprint && this.fpByT.size ? 72 : 18));   // wide enough to read a footprint
      // grid and axes
      if (this.layers.grid) {
        ctx.strokeStyle = C.grid || "#2a2f3a"; ctx.lineWidth = 1; ctx.fillStyle = C.muted || "#888";
        for (let k = 0; k <= 4; k++) { const v = lo + (hi - lo) * k / 4, y = Math.round(ys(v)) + 0.5; ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + plotW, y); ctx.stroke(); ctx.textAlign = "left"; ctx.fillText(fmtP(v), padL + plotW + 6, y); }
        const ticks = Math.max(3, Math.floor(plotW / 110)); const step = this._niceStep(span / ticks);
        for (let t = Math.ceil(V.from / step) * step; t < V.to; t += step) { const x = Math.round(xs(t)) + 0.5; ctx.strokeStyle = C.grid || "#2a2f3a"; ctx.beginPath(); ctx.moveTo(x, price.top); ctx.lineTo(x, h - G.padB); ctx.stroke(); ctx.textAlign = "center"; ctx.fillText(fmtT(t, step), x, h - 8); }
      }
      // the order-book heatmap: resting USD per price row per column, log-scaled; bids blue, asks orange
      if (this.layers.heatmap && this.hm && this.hm.cols && this.hm.cols.length && this.hm.max > 0) {
        const H = this.hm, bk = H.bucket, lg = v => Math.log1p(v) / Math.log1p(H.max), colW = Math.max(1, H.tf / span * plotW);
        H.cols.forEach(col => { if (col.t + H.tf < V.from || col.t > V.to) return; const x = xs(col.t);
          col.cells.forEach(([L, sd, usd]) => { const p0 = L * bk; if (p0 + bk < lo || p0 > hi) return; const y1 = ys(p0 + bk), y2 = ys(p0), a = 0.08 + 0.8 * lg(usd);
            ctx.fillStyle = sd > 0 ? `rgba(90,150,255,${a.toFixed(3)})` : `rgba(255,150,70,${a.toFixed(3)})`; ctx.fillRect(x, y1, colW + 0.5, Math.max(1, y2 - y1)); }); });
        ctx.fillStyle = C.muted || "#888"; ctx.textAlign = "left"; ctx.fillText(`book heatmap · ${H.quote || ""} · rows of ${fmtP(bk)} · brightest ${fmtK(H.max)} resting`, padL + 4, price.bottom - 6);
      }
      // the modeled liquidation map: the evolution as cells per bar (longs below price in red, shorts above in green), the current clusters as a profile at the right edge
      if (this.layers.liqmap && this.lm && this.lm.bucket) {
        const M = this.lm, bk = M.bucket, sel = this.liqBand, states = M.states || [];
        let mx = 1; states.forEach(st => st.cells.forEach(([, L, , v]) => { if (!sel || L === sel) mx = Math.max(mx, v); }));
        const colW = Math.max(1, M.tf / span * plotW), lg = v => Math.log1p(v) / Math.log1p(mx);
        states.forEach(st => { if (st.t + M.tf < V.from || st.t > V.to) return; const x = xs(st.t);
          st.cells.forEach(([bucket, L, side, v]) => { if (sel && L !== sel) return; const p0 = bucket * bk; if (p0 + bk < lo || p0 > hi) return; const a = 0.1 + 0.75 * lg(v);
            ctx.fillStyle = side < 0 ? `rgba(240,90,90,${a.toFixed(3)})` : `rgba(80,210,120,${a.toFixed(3)})`; ctx.fillRect(x, ys(p0 + bk), colW + 0.5, Math.max(1, ys(p0) - ys(p0 + bk))); }); });
        const now = (M.now || []).filter(([, L]) => !sel || L === sel), pm = Math.max(1, ...now.map(c => c[3])), pw = Math.min(120, plotW * 0.15);
        now.forEach(([bucket, L, side, v]) => { const p0 = bucket * bk; if (p0 + bk < lo || p0 > hi) return; ctx.fillStyle = side < 0 ? "rgba(240,90,90,.7)" : "rgba(80,210,120,.7)"; ctx.fillRect(padL + plotW - pw * v / pm, ys(p0 + bk), pw * v / pm, Math.max(1, ys(p0) - ys(p0 + bk) - 1)); });
        ctx.fillStyle = C.muted || "#888"; ctx.textAlign = "right"; ctx.fillText(`${M.calibration && M.calibration.events ? `${M.calibration.explained} of ${M.calibration.events} real liquidations on a modeled cluster (${Math.round((M.calibration.share_usd || 0) * 100)}% by USD) · ` : ""}liquidation map (modeled) · longs $${fmtK(M.total ? M.total.long : 0)} below · shorts $${fmtK(M.total ? M.total.short : 0)} above${sel ? ` · ${sel}x only` : ""}`, padL + plotW - 4, price.top + 22);
      }
      // dealer gamma by strike: bars at the strike (positive gamma to the right in blue, negative in orange), the zero-gamma and max-pain levels
      if (this.layers.gex && this.opt && this.opt.by_strike && this.opt.by_strike.length) { const O = this.opt, mx = Math.max(1, ...O.by_strike.map(s => Math.abs(s.gex))), pw = Math.min(160, plotW * 0.2), cx = padL + plotW * 0.5;
        O.by_strike.forEach(s => { if (s.strike < lo || s.strike > hi) return; const y = ys(s.strike), w2 = pw * Math.abs(s.gex) / mx; ctx.fillStyle = s.gex >= 0 ? "rgba(122,162,247,.35)" : "rgba(255,150,70,.35)"; ctx.fillRect(s.gex >= 0 ? cx : cx - w2, y - 2, w2, 4); });
        [["zero_gamma", "zero γ", C.yellow || "#e0af68"], ["max_pain", "max pain", C.magenta || "#ff79c6"]].forEach(([k, label, col]) => { const v = O[k]; if (!v || v < lo || v > hi) return; const y = Math.round(ys(v)) + 0.5; ctx.strokeStyle = col; ctx.setLineDash([3, 5]); ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + plotW, y); ctx.stroke(); ctx.setLineDash([]); ctx.fillStyle = col; ctx.textAlign = "left"; ctx.font = "9px ui-monospace, monospace"; ctx.fillText(label, padL + 4, y - 6); ctx.font = "11px ui-monospace, Menlo, monospace"; });
        ctx.fillStyle = C.muted || "#888"; ctx.textAlign = "left"; ctx.fillText(`GEX by strike (${O.asset}) · total ${fmtK(O.total)} per 1% · ${O.n} contracts · Deribit`, padL + 4, price.top + 34); }
      const I = window.SteelInd, closes = all.map(c => c.c);
      // session boxes (Asia, London, New York in UTC) with their highs and lows
      if (this.layers.sessions && I) { I.sessions(all).forEach(sx => { if (sx.t1 < V.from || sx.t0 > V.to) return; const x0 = xs(Math.max(sx.t0, V.from)), x1 = xs(Math.min(sx.t1, V.to)); ctx.fillStyle = sx.color; ctx.fillRect(x0, ys(Math.min(hi, sx.hi)), x1 - x0, ys(Math.max(lo, sx.lo)) - ys(Math.min(hi, sx.hi)));
          ctx.fillStyle = C.muted || "#888"; ctx.textAlign = "left"; ctx.font = "9px ui-monospace, monospace"; ctx.fillText(sx.name, x0 + 3, ys(Math.min(hi, sx.hi)) + 9); ctx.font = "11px ui-monospace, Menlo, monospace"; }); }
      // market structure: swing points, breaks of structure and changes of character, order blocks, fair value gaps
      if (this.layers.structure && I && all.length > 12) { const st = this.ind.structure || (this.ind.structure = I.structure(all));
        st.fvg.forEach(g => { const c0 = all[g.i]; if (!c0 || c0.t + tf < V.from) return; const x0 = xs(c0.t), x1 = padL + plotW; ctx.fillStyle = g.dir > 0 ? "rgba(60,200,100,.10)" : "rgba(220,80,80,.10)"; ctx.fillRect(x0, ys(g.hi), Math.min(x1, x0 + plotW * 0.25) - x0, ys(g.lo) - ys(g.hi)); });
        st.ob.forEach(o => { const c0 = all[o.i]; if (!c0 || c0.t + tf < V.from) return; const x0 = xs(c0.t); ctx.strokeStyle = o.dir > 0 ? (C.up || "#2a2") : (C.down || "#d33"); ctx.setLineDash([3, 3]); ctx.strokeRect(x0, ys(o.hi), Math.min(padL + plotW, x0 + plotW * 0.35) - x0, ys(o.lo) - ys(o.hi)); ctx.setLineDash([]); ctx.fillStyle = C.muted || "#888"; ctx.textAlign = "left"; ctx.font = "9px ui-monospace, monospace"; ctx.fillText("OB", x0 + 2, ys(o.hi) + 9); ctx.font = "11px ui-monospace, Menlo, monospace"; });
        st.breaks.forEach(b => { const c0 = all[b.i]; if (!c0 || c0.t + tf < V.from || c0.t > V.to) return; const y = ys(b.px), x1 = xs(c0.t + tf / 2); ctx.strokeStyle = b.dir > 0 ? (C.up || "#2a2") : (C.down || "#d33"); ctx.beginPath(); ctx.moveTo(x1 - 40, y); ctx.lineTo(x1, y); ctx.stroke(); ctx.fillStyle = ctx.strokeStyle; ctx.textAlign = "left"; ctx.font = "9px ui-monospace, monospace"; ctx.fillText(b.kind, x1 + 2, y + (b.dir > 0 ? -4 : 10)); ctx.font = "11px ui-monospace, Menlo, monospace"; });
        ctx.fillStyle = C.muted || "#888"; st.swingHighs.forEach(i => { const c0 = all[i]; if (c0.t + tf < V.from || c0.t > V.to) return; ctx.beginPath(); ctx.arc(xs(c0.t + tf / 2), ys(c0.h) - 5, 2, 0, Math.PI * 2); ctx.fill(); }); st.swingLows.forEach(i => { const c0 = all[i]; if (c0.t + tf < V.from || c0.t > V.to) return; ctx.beginPath(); ctx.arc(xs(c0.t + tf / 2), ys(c0.l) + 5, 2, 0, Math.PI * 2); ctx.fill(); }); }
      // period levels: horizontal lines with a label at the right edge
      if (this.layers.levels && this.levels && this.levels.levels) { const col = { day: C.aqua || "#2ac3de", prev: C.yellow || "#e0af68", week: C.violet || "#bb9af7", month: C.magenta || "#ff79c6" };
        const labels = [];
        this.levels.levels.forEach(l => { if (l.px < lo || l.px > hi) return; const y = Math.round(ys(l.px)) + 0.5; ctx.strokeStyle = col[l.kind] || (C.muted || "#888"); ctx.setLineDash(l.kind === "day" ? [] : [6, 4]); ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + plotW, y); ctx.stroke(); ctx.setLineDash([]); labels.push({ y, text: l.label, col: col[l.kind] || (C.muted || "#888") }); });
        // labels at the same height merge into one line; neighbours closer than a line are pushed apart so every label stays readable
        labels.sort((a, b) => a.y - b.y); const merged = []; labels.forEach(l => { const last = merged[merged.length - 1]; if (last && Math.abs(last.y - l.y) < 2) { last.text += " · " + l.text; } else merged.push({ ...l }); });
        for (let i = 1; i < merged.length; i++) if (merged[i].y - merged[i - 1].y < 11) merged[i].y = merged[i - 1].y + 11;
        ctx.font = "9px ui-monospace, monospace"; ctx.textAlign = "right"; merged.forEach(l => { ctx.fillStyle = l.col; ctx.fillText(l.text, padL + plotW - 4, l.y - 6); }); ctx.font = "11px ui-monospace, Menlo, monospace"; }
      // volume profile over the visible candles (the footprint's rows when loaded, else each candle's volume spread over its range) and the TPO count, drawn against the right edge
      if ((this.layers.profile || this.layers.tpo) && vis.length) {
        const bk = this.fp && this.fp.bucket ? this.fp.bucket : (hi - lo) / 60, acc = new Map(), tpo = new Map(), key = p => Math.floor(p / bk);
        vis.forEach(c => { const f = this.fpByT.get(c.t); if (f && f.rows && f.rows.length) f.rows.forEach(r => acc.set(key(r.px), (acc.get(key(r.px)) || 0) + r.a + r.b));
          else { const n = Math.max(1, Math.round((c.h - c.l) / bk)), each = ((c.vb || 0) + (c.vs || 0) || (c.v || 0) * c.c) / n; for (let i = 0; i < n; i++) acc.set(key(c.l + i * bk), (acc.get(key(c.l + i * bk)) || 0) + each); }
          for (let k = key(c.l); k <= key(c.h); k++) tpo.set(k, (tpo.get(k) || 0) + 1); });
        const rows = [...acc.entries()].sort((a, b) => a[0] - b[0]); const mx = Math.max(1, ...rows.map(r => r[1])); const pw = Math.min(140, plotW * 0.18);
        let poc = null, tot = 0; rows.forEach(r => { tot += r[1]; if (!poc || r[1] > poc[1]) poc = r; });
        let va = null; if (poc && tot) { const idx = rows.findIndex(r => r === poc); let lo2 = idx, hi2 = idx, got = poc[1]; while (got < 0.7 * tot && (lo2 > 0 || hi2 < rows.length - 1)) { const up = hi2 < rows.length - 1 ? rows[hi2 + 1][1] : -1, dn = lo2 > 0 ? rows[lo2 - 1][1] : -1; if (up >= dn) { hi2++; got += up; } else { lo2--; got += dn; } } va = [rows[lo2][0], rows[hi2][0]]; }
        if (this.layers.profile) rows.forEach(([k, v]) => { const y1 = ys((k + 1) * bk), y2 = ys(k * bk), inVa = va && k >= va[0] && k <= va[1]; ctx.fillStyle = k === poc[0] ? (C.yellow || "#e0af68") : inVa ? "rgba(122,162,247,.45)" : "rgba(122,162,247,.2)"; ctx.fillRect(padL + plotW - pw * v / mx, y1, pw * v / mx, Math.max(1, y2 - y1 - 1)); });
        if (this.layers.tpo) { const tm = Math.max(1, ...tpo.values()); [...tpo.entries()].forEach(([k, v]) => { const y1 = ys((k + 1) * bk), y2 = ys(k * bk); ctx.fillStyle = "rgba(255,255,255,.12)"; ctx.fillRect(padL, y1, pw * 0.6 * v / tm, Math.max(1, y2 - y1 - 1)); }); ctx.fillStyle = C.muted || "#888"; ctx.textAlign = "left"; ctx.fillText("TPO", padL + 4, price.top + 10); }
        if (this.layers.profile && poc) { ctx.fillStyle = C.muted || "#888"; ctx.textAlign = "right"; ctx.fillText(`VPVR · POC ${fmtP((poc[0] + 0.5) * bk)}${va ? ` · VA ${fmtP(va[0] * bk)}–${fmtP((va[1] + 1) * bk)}` : ""}`, padL + plotW - 4, price.top + 10); }
      }
      // VWAP: anchored at the UTC day of each candle, from the tape's dollar volume
      if (this.layers.vwap) {
        let day = null, pv = 0, vv = 0; ctx.strokeStyle = C.yellow || "#e0af68"; ctx.lineWidth = 1.2; ctx.beginPath(); let started = false;
        all.forEach(c => { const dd = Math.floor(c.t / 86400); if (dd !== day) { day = dd; pv = 0; vv = 0; started = false; }
          const usd = (c.vb || 0) + (c.vs || 0), base = c.v || 0; if (base > 0 && usd > 0) { pv += usd; vv += base; } else { pv += ((c.h + c.l + c.c) / 3) * (c.v || 0); vv += c.v || 0; }
          if (vv <= 0 || c.t + tf < V.from || c.t > V.to) return; const v = pv / vv, x = xs(c.t + tf / 2), y = ys(v);
          if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y); });
        ctx.stroke();
        // ±1σ and ±2σ bands around the day's VWAP (volume-weighted), and the anchored VWAP from a shift-clicked time
        const vw = (fromT) => { let pv = 0, vv = 0, pv2 = 0; const pts = []; all.forEach(c => { if (c.t < fromT) return; const base = c.v || 0, usd = (c.vb || 0) + (c.vs || 0), px = base > 0 && usd > 0 ? usd / base : (c.h + c.l + c.c) / 3; if (!(base > 0)) return; pv += px * base; vv += base; pv2 += px * px * base; const m = pv / vv, sd = Math.sqrt(Math.max(0, pv2 / vv - m * m)); pts.push([c.t + tf / 2, m, sd]); }); return pts; };
        if (this.layers.vwapBands) { const dayStart = Math.floor(all[all.length - 1].t / 86400) * 86400, pts = vw(dayStart).filter(p => p[0] + tf >= V.from && p[0] <= V.to);
          [[1, "rgba(224,175,104,.35)"], [2, "rgba(224,175,104,.18)"]].forEach(([k, col]) => { ctx.strokeStyle = col; ctx.lineWidth = 1; [1, -1].forEach(sg => { ctx.beginPath(); pts.forEach((p, i) => { const y = ys(p[1] + sg * k * p[2]); i ? ctx.lineTo(xs(p[0]), y) : ctx.moveTo(xs(p[0]), y); }); ctx.stroke(); }); }); }
        if (this.anchor) { const pts = vw(this.anchor); ctx.strokeStyle = C.magenta || "#ff79c6"; ctx.lineWidth = 1.4; ctx.beginPath(); pts.forEach((p, i) => { i ? ctx.lineTo(xs(p[0]), ys(p[1])) : ctx.moveTo(xs(p[0]), ys(p[1])); }); ctx.stroke(); const x0 = xs(this.anchor); ctx.setLineDash([2, 3]); ctx.beginPath(); ctx.moveTo(x0, price.top); ctx.lineTo(x0, price.bottom); ctx.stroke(); ctx.setLineDash([]); ctx.fillStyle = C.magenta || "#ff79c6"; ctx.textAlign = "left"; ctx.fillText("AVWAP", x0 + 3, price.top + 10); }
      }
      // candles (PVSRA colors the climax and above-average volume candles)
      const pv = this.layers.pvsra && I ? (this.ind.pvsra || (this.ind.pvsra = I.pvsra(all))) : null;
      vis.forEach(c => { const x = xs(c.t + tf / 2), up = c.c >= c.o; const i = pv ? all.indexOf(c) : -1; const col = pv && pv[i] ? ({ lime: "#8bff5a", red: "#ff4d4d", blue: "#5aa0ff", violet: "#c68bff" })[pv[i]] : null;
        ctx.strokeStyle = ctx.fillStyle = col || (up ? (C.up || "#2a2") : (C.down || "#d33")); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(x, ys(c.h)); ctx.lineTo(x, ys(c.l)); ctx.stroke();
        const y1 = ys(Math.max(c.o, c.c)), y2 = ys(Math.min(c.o, c.c)); ctx.fillRect(x - cw / 2, y1, cw, Math.max(1, y2 - y1)); });
      // overlays over the candles: an EMA ribbon, Bollinger bands, the supertrend line, the Ichimoku cloud
      if (I && (this.layers.ema || this.layers.bollinger || this.layers.supertrend || this.layers.ichimoku)) { const line = (ser, col, w = 1, shift = 0) => { ctx.strokeStyle = col; ctx.lineWidth = w; ctx.beginPath(); let st = false; ser.forEach((v, i) => { const c = all[i]; if (v == null || !c) return; const x = xs(c.t + tf / 2 + shift * tf); if (c.t + tf + shift * tf < V.from || c.t + shift * tf > V.to) return; st ? ctx.lineTo(x, ys(v)) : ctx.moveTo(x, ys(v)); st = true; }); ctx.stroke(); };
        if (this.layers.ema) [[12, "#7aa2f7"], [26, "#2ac3de"], [50, "#e0af68"], [200, "#ff79c6"]].forEach(([n, col]) => line(this.ind["ema" + n] || (this.ind["ema" + n] = I.ema(closes, n)), col));
        if (this.layers.bollinger) { const m = this.ind.sma20 || (this.ind.sma20 = I.sma(closes, 20)), sd = this.ind.std20 || (this.ind.std20 = I.std(closes, 20)); line(m, "rgba(200,200,220,.6)"); line(m.map((v, i) => v == null ? null : v + 2 * sd[i]), "rgba(200,200,220,.35)"); line(m.map((v, i) => v == null ? null : v - 2 * sd[i]), "rgba(200,200,220,.35)"); }
        if (this.layers.supertrend) { const stv = this.ind.st || (this.ind.st = I.supertrend(all, 10, 3)); ctx.lineWidth = 1.5; let prev = null; stv.forEach((v, i) => { const c = all[i]; if (c.t + tf < V.from || c.t > V.to) { prev = v; return; } const x = xs(c.t + tf / 2); ctx.strokeStyle = v.dir > 0 ? (C.up || "#2a2") : (C.down || "#d33"); if (prev && prev.dir === v.dir) { ctx.beginPath(); ctx.moveTo(xs(all[i - 1].t + tf / 2), ys(prev.line)); ctx.lineTo(x, ys(v.line)); ctx.stroke(); } prev = v; }); }
        if (this.layers.ichimoku) { const ic = this.ind.ichi || (this.ind.ichi = I.ichimoku(all)); line(ic.tenkan, "#2ac3de"); line(ic.kijun, "#e0af68"); ctx.fillStyle = "rgba(122,162,247,.12)"; let st = false; ctx.beginPath(); ic.spanA.forEach((v, i) => { const c = all[i]; if (v == null) return; const x = xs(c.t + tf / 2 + ic.shift * tf); st ? ctx.lineTo(x, ys(v)) : ctx.moveTo(x, ys(v)); st = true; }); for (let i = ic.spanB.length - 1; i >= 0; i--) { const v = ic.spanB[i], c = all[i]; if (v == null) continue; ctx.lineTo(xs(c.t + tf / 2 + ic.shift * tf), ys(v)); } ctx.closePath(); ctx.fill(); } }
      // footprint: bid x ask per price bucket inside each candle (numbers when the candle is wide, bars when narrow); POC and value area; stacked imbalances
      if (this.layers.footprint && this.fp && this.fp.bucket && this.fpByT.size) {
        const bk = this.fp.bucket, rowH = Math.abs(ys(lo + bk) - ys(lo)), wide = cw >= 34 && rowH >= 9;
        vis.forEach(c => { const f = this.fpByT.get(c.t); if (!f || !f.rows || !f.rows.length) return; const x = xs(c.t + tf / 2);
          const mx = Math.max(1, ...f.rows.map(r => Math.max(r.a, r.b)));
          const imbs = new Map((f.imb || []).map(i => [i.px + ":" + i.side, true]));
          f.rows.forEach(r => { if (r.px < lo || r.px > hi) return; const y = ys(r.px + bk / 2), hh = Math.max(2, Math.abs(ys(r.px + bk) - ys(r.px)) - 1);
            const inVa = f.va && r.px >= f.va[0] && r.px <= f.va[1];
            if (r.px === f.poc) { ctx.fillStyle = C.labelBg || "rgba(255,255,255,.08)"; ctx.fillRect(x - cw / 2 - 1, y - hh / 2, cw + 2, hh); }
            if (wide) {
              const half = cw / 2 - 2;
              ctx.fillStyle = imbs.get(r.px + ":-1") ? (C.down || "#d33") : (inVa ? "rgba(220,80,80,.32)" : "rgba(220,80,80,.16)"); ctx.fillRect(x - half, y - hh / 2, half * Math.min(1, r.b / mx), hh);
              ctx.fillStyle = imbs.get(r.px + ":1") ? (C.up || "#2a2") : (inVa ? "rgba(60,200,100,.32)" : "rgba(60,200,100,.16)"); ctx.fillRect(x, y - hh / 2, half * Math.min(1, r.a / mx), hh);
              ctx.fillStyle = C.ink || "#eee"; ctx.font = "9px ui-monospace, monospace"; ctx.textAlign = "right"; ctx.fillText(fmtK(r.b), x - 2, y); ctx.textAlign = "left"; ctx.fillText(fmtK(r.a), x + 2, y); ctx.font = "11px ui-monospace, Menlo, monospace";
            } else {
              const half = Math.max(1, cw / 2);
              ctx.fillStyle = imbs.get(r.px + ":-1") ? (C.down || "#d33") : "rgba(220,80,80,.35)"; ctx.fillRect(x - half * Math.min(1, r.b / mx), y - hh / 2, half * Math.min(1, r.b / mx), hh);
              ctx.fillStyle = imbs.get(r.px + ":1") ? (C.up || "#2a2") : "rgba(60,200,100,.35)"; ctx.fillRect(x, y - hh / 2, half * Math.min(1, r.a / mx), hh);
            } });
          if (f.va && wide) { ctx.strokeStyle = C.muted || "#888"; ctx.setLineDash([2, 2]); ctx.strokeRect(x - cw / 2 - 1, ys(f.va[1] + bk), cw + 2, ys(f.va[0]) - ys(f.va[1] + bk)); ctx.setLineDash([]); } });
      }
      // bubbles: the large prints, where they printed, sized by notional
      if (this.layers.bubbles && this.fpByT.size) vis.forEach(c => { const f = this.fpByT.get(c.t); if (!f || !f.big) return; const ref = this.fp.bubble_usd || 1e5;
        f.big.forEach(p => { const x = xs(p.t), y = ys(p.px), rr = 3 + 7 * Math.min(3, Math.sqrt(p.usd / ref)); ctx.fillStyle = p.side > 0 ? "rgba(60,200,100,.35)" : "rgba(220,80,80,.35)"; ctx.strokeStyle = p.side > 0 ? (C.up || "#2a2") : (C.down || "#d33");
          ctx.beginPath(); ctx.arc(x, y, rr, 0, Math.PI * 2); ctx.fill(); ctx.stroke(); if (rr >= 9) { ctx.fillStyle = C.ink || "#eee"; ctx.textAlign = "center"; ctx.font = "9px ui-monospace, monospace"; ctx.fillText(fmtK(p.usd), x, y); ctx.font = "11px ui-monospace, Menlo, monospace"; } }); });
      // liquidations: triangles at the candle, shorts above (forced buys), longs below (forced sells), sized by USD
      if (this.layers.liq && d.liq && d.liq.longs) {
        const mx = Math.max(1, ...d.liq.longs, ...d.liq.shorts);
        d.liq.longs.forEach((usd, i) => { if (!usd) return; const t = d.liq.t0 + i * d.liq.bar_s; if (t + tf < V.from || t > V.to) return; const c = all.find(k => k.t === t); const yb = c ? ys(c.l) + 6 : price.bottom - 6;
          const s = 3 + 9 * Math.sqrt(usd / mx); ctx.fillStyle = C.downFill || "rgba(220,60,60,.6)"; const x = xs(t + tf / 2); ctx.beginPath(); ctx.moveTo(x, yb); ctx.lineTo(x - s, yb + s * 1.4); ctx.lineTo(x + s, yb + s * 1.4); ctx.closePath(); ctx.fill(); });
        d.liq.shorts.forEach((usd, i) => { if (!usd) return; const t = d.liq.t0 + i * d.liq.bar_s; if (t + tf < V.from || t > V.to) return; const c = all.find(k => k.t === t); const yt = c ? ys(c.h) - 6 : price.top + 6;
          const s = 3 + 9 * Math.sqrt(usd / mx); ctx.fillStyle = C.upFill || "rgba(40,200,90,.6)"; const x = xs(t + tf / 2); ctx.beginPath(); ctx.moveTo(x, yt); ctx.lineTo(x - s, yt - s * 1.4); ctx.lineTo(x + s, yt - s * 1.4); ctx.closePath(); ctx.fill(); });
      }
      // the model's resolved calls: a marker where each was made, filled when it hit, hollow when it missed
      if (this.layers.calls && d.calls) d.calls.forEach(k => { if (!k.side || k.t < V.from || k.t > V.to) return; const c = all.find(c => k.t >= c.t && k.t < c.t + tf); if (!c) return;
        const x = xs(k.t), y = k.side > 0 ? ys(c.l) - 14 : ys(c.h) + 14; ctx.strokeStyle = ctx.fillStyle = k.side > 0 ? (C.up || "#2a2") : (C.down || "#d33"); ctx.lineWidth = 1.5;
        ctx.beginPath(); if (k.side > 0) { ctx.moveTo(x, y - 6); ctx.lineTo(x - 5, y + 3); ctx.lineTo(x + 5, y + 3); } else { ctx.moveTo(x, y + 6); ctx.lineTo(x - 5, y - 3); ctx.lineTo(x + 5, y - 3); } ctx.closePath(); k.hit ? ctx.fill() : ctx.stroke(); });
      // the paper book's fills (and the live mirrors with their slippage) where they happened, and the transfer waits as a band on the time axis
      if (this.layers.transfers && d.transfers) d.transfers.forEach(tr => { if (!tr.sent || tr.lands_at < V.from || tr.sent > V.to) return; const xa = Math.max(padL, xs(tr.sent)), xb = Math.min(padL + plotW, xs(tr.lands_at));
        ctx.fillStyle = tr.landed ? (C.fanOuter || "rgba(90,140,255,.15)") : (C.yellowSoft || "rgba(224,175,104,.25)"); ctx.fillRect(xa, price.bottom - 10, Math.max(2, xb - xa), 8);
        ctx.fillStyle = C.muted || "#888"; ctx.textAlign = "left"; ctx.fillText(`${tr.landed ? "moved" : "in flight"} $${Math.round(tr.amount).toLocaleString()} → ${tr.to} (${tr.rail || tr.asset || ""})`, xa + 3, price.bottom - 18); });
      if (this.layers.fills && d.fills) d.fills.forEach(f => { if (f.t < V.from || f.t > V.to || !f.price) return; const x = xs(f.t), y = ys(f.price); if (!isFinite(y)) return;
        const buy = /buy|open long|close short/.test(f.side), liq = /liquidated/.test(f.side); ctx.strokeStyle = ctx.fillStyle = liq ? (C.yellow || "#e0af68") : buy ? (C.up || "#2a2") : (C.down || "#d33"); ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(x, y, 5, 0, Math.PI * 2); ctx.stroke(); ctx.beginPath(); ctx.moveTo(x - 9, y); ctx.lineTo(x + 9, y); ctx.stroke();
        ctx.fillStyle = C.ink || "#eee"; ctx.textAlign = "left"; ctx.fillText(`${f.side} $${Math.round(f.notional).toLocaleString()}`, x + 8, y - 9); });
      if (this.layers.fills && d.mirrors) d.mirrors.forEach(m => { if (m.t < V.from || m.t > V.to || !m.price) return; const x = xs(m.t), y = ys(m.price); if (!isFinite(y)) return;
        ctx.strokeStyle = m.ok ? (C.blue || "#5a8cff") : (C.red || "#f7768e"); ctx.lineWidth = 1.5; ctx.setLineDash([2, 2]); ctx.beginPath(); ctx.arc(x, y, 8, 0, Math.PI * 2); ctx.stroke(); ctx.setLineDash([]);
        ctx.fillStyle = C.muted || "#888"; ctx.textAlign = "left"; ctx.fillText(m.ok ? `live ${m.slippage_bps != null ? (m.slippage_bps >= 0 ? "+" : "") + m.slippage_bps.toFixed(1) + " bps slip" : ""}` : "live refused", x + 10, y + 10); });
      // the order ticket's rays: the limit (entry), the stop, the target and the liquidation price; drag the first three
      if (this.ticket) { const T = this.ticket, rays = [["limit", T.limit, C.blue || "#5a8cff", `${T.side > 0 ? "buy" : T.side < 0 ? "sell" : "entry"} ${T.qty ? Number(T.qty).toPrecision(4) : ""} @`], ["stop", T.stop, C.red || "#f7768e", "stop"], ["target", T.target, C.green || "#5fd38d", "target"], ["liq", T.liq, C.yellow || "#e0af68", "liquidation"]];
        rays.forEach(([k, v, col, label]) => { if (!v || v < lo || v > hi) return; const y = Math.round(ys(v)) + 0.5; ctx.strokeStyle = col; ctx.lineWidth = k === "liq" ? 1 : 1.5; ctx.setLineDash(k === "liq" ? [2, 4] : [8, 4]); ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + plotW, y); ctx.stroke(); ctx.setLineDash([]);
          ctx.fillStyle = col; ctx.fillRect(padL + plotW - 2, y - 8, 2, 16); ctx.textAlign = "left"; ctx.fillStyle = C.ink || "#eee"; const txt = `${label} ${v.toLocaleString(undefined, { maximumFractionDigits: v >= 100 ? 2 : 5 })}${k !== "limit" && T.limit ? ` (${((v / T.limit - 1) * 1e4 * (T.side > 0 ? 1 : -1)).toFixed(0)} bps)` : ""}`; ctx.fillRect(padL + 4, y - 8, ctx.measureText(txt).width + 8, 15); ctx.fillStyle = C.bg || "#0b0f14"; ctx.fillStyle = "#0b0f14"; ctx.fillText(txt, padL + 8, y); }); }
      // the forecast fan ahead of the last candle, with the longer horizons' medians as ticks on the right
      if (fc) { const last = all[all.length - 1], x0 = xs(last.t + tf), x1 = xs(last.t + tf + (fc.horizon_s || tf * 12)); const f = fc.q.map(q => fc.price * Math.exp(q / 1e4)), y0 = ys(fc.price);
        ctx.fillStyle = C.fanOuter || "rgba(90,140,255,.15)"; ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, ys(f[4])); ctx.lineTo(x1, ys(f[0])); ctx.closePath(); ctx.fill();
        ctx.fillStyle = C.fanInner || "rgba(90,140,255,.3)"; ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, ys(f[3])); ctx.lineTo(x1, ys(f[1])); ctx.closePath(); ctx.fill();
        ctx.strokeStyle = C.blue || "#5a8cff"; ctx.setLineDash([4, 3]); ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, ys(f[2])); ctx.stroke(); ctx.setLineDash([]);
        ctx.fillStyle = C.ink || "#eee"; ctx.textAlign = "left"; ctx.fillText(`${fc.q[2] >= 0 ? "+" : ""}${fc.q[2].toFixed(0)} bps · P↑ ${Math.round((fc.p_up || 0) * 100)}%${fc.action && fc.action !== "HOLD" ? " · " + fc.action : ""}`, Math.min(x1 + 4, w - 70), ys(f[2]));
        if (fc.horizons) Object.entries(fc.horizons).forEach(([k, hz], j) => { if (!hz || !hz.q) return; const m = fc.price * Math.exp(hz.q[2] / 1e4), y = ys(m); if (!isFinite(y) || y < price.top || y > price.bottom) return;
          ctx.strokeStyle = C.violet || "#bb9af7"; ctx.setLineDash([2, 3]); ctx.beginPath(); ctx.moveTo(x1 - 8, y); ctx.lineTo(x1 + 8, y); ctx.stroke(); ctx.setLineDash([]);
          ctx.fillStyle = C.violet || "#bb9af7"; ctx.textAlign = "left"; ctx.fillText(`${k} ${hz.q[2] >= 0 ? "+" : ""}${hz.q[2].toFixed(0)}`, Math.min(x1 + 10, w - 60), y + (j % 2 ? 10 : -10)); });
        if (fc.reasons && fc.reasons.length) { ctx.fillStyle = C.muted || "#888"; ctx.textAlign = "right"; ctx.fillText(fc.reasons.slice(0, 3).join(" · ").slice(0, 90), padL + plotW - 4, price.top + 10); } }
      // sub panes
      const vol = G.panes.find(p => p.id === "vol"), flow = G.panes.find(p => p.id === "flow");
      if (vol) { const mx = Math.max(1e-9, ...vis.map(c => c.v || 0)); vis.forEach(c => { const x = xs(c.t + tf / 2), hh = (c.v || 0) / mx * (vol.bottom - vol.top - 10);
          if (c.vb || c.vs) { const tot = c.vb + c.vs || 1, bh = hh * c.vb / tot, sh = hh * c.vs / tot; ctx.fillStyle = C.upSoft || "rgba(40,200,90,.5)"; ctx.fillRect(x - cw / 2, vol.bottom - bh, cw, bh); ctx.fillStyle = C.downSoft || "rgba(220,60,60,.5)"; ctx.fillRect(x - cw / 2, vol.bottom - bh - sh, cw, sh); }
          else { ctx.fillStyle = c.c >= c.o ? (C.upSoft || "#2a26") : (C.downSoft || "#d336"); ctx.fillRect(x - cw / 2, vol.bottom - hh, cw, hh); } });
        ctx.fillStyle = C.muted || "#888"; ctx.textAlign = "left"; ctx.fillText("volume · buy / sell", padL + 4, vol.top + 8); }
      if (flow) { const deltas = all.map(c => this._delta(c)); let cum = 0; const cvd = deltas.map(v => (cum += v));
        const dm = Math.max(1e-9, ...vis.map(c => Math.abs(this._delta(c)))); const mid = (flow.top + flow.bottom) / 2;
        if (this.layers.delta) all.forEach((c, i) => { if (c.t + tf < V.from || c.t > V.to) return; const x = xs(c.t + tf / 2), v = deltas[i], bh = Math.abs(v) / dm * (flow.bottom - flow.top - 12) / 2; ctx.fillStyle = v >= 0 ? (C.upSoft || "#2a26") : (C.downSoft || "#d336"); ctx.fillRect(x - cw / 2, v >= 0 ? mid - bh : mid, cw, bh); });
        if (this.layers.cvd) { const vi = all.map((c, i) => [c, cvd[i]]).filter(([c]) => c.t + tf >= V.from && c.t <= V.to); if (vi.length > 1) { const lo2 = Math.min(...vi.map(x => x[1])), hi2 = Math.max(...vi.map(x => x[1])), sp2 = (hi2 - lo2) || 1;
          ctx.strokeStyle = C.aqua || "#2ac3de"; ctx.lineWidth = 1.3; ctx.beginPath(); vi.forEach(([c, v], i) => { const x = xs(c.t + tf / 2), y = flow.top + 6 + (hi2 - v) / sp2 * (flow.bottom - flow.top - 12); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }); ctx.stroke(); } }
        ctx.fillStyle = C.muted || "#888"; ctx.textAlign = "left"; ctx.fillText(`${this.layers.delta ? "delta " : ""}${this.layers.delta && this.layers.cvd ? "· " : ""}${this.layers.cvd ? "CVD " : ""}(USD, from the tape${this.cvdBand > 0 && this.fp ? ", prints from $" + this.fp.bands[this.cvdBand] + " up" : ""})`, padL + 4, flow.top + 8); }
      // the operator's Python indicators (steel/indicators.py): a line or histogram in its own pane, a line on the price pane, or markers on the candles
      this.customOn().forEach(([name, ind]) => {
        const col = ind.color || C.aqua || "#2ac3de", pal = [col, C.yellow || "#e0af68", C.violet || "#bb9af7", C.red || "#f7768e", C.green || "#5fd38d"];
        const inWin = t => t >= V.from - tf && t <= V.to + tf;
        if (ind.kind === "marker" && ind.markers) {
          ind.markers.forEach(m => { if (!inWin(m.t)) return; const x = xs(m.t - tf / 2), y = ys(m.v); if (!isFinite(y)) return; ctx.fillStyle = m.text === "down" ? (C.red || "#f7768e") : m.text === "up" ? (C.green || "#5fd38d") : col;
            ctx.beginPath(); if (m.text === "down") { ctx.moveTo(x, y - 10); ctx.lineTo(x - 5, y - 18); ctx.lineTo(x + 5, y - 18); } else { ctx.moveTo(x, y + 10); ctx.lineTo(x - 5, y + 18); ctx.lineTo(x + 5, y + 18); } ctx.closePath(); ctx.fill();
            if (m.text && m.text !== "up" && m.text !== "down") { ctx.fillStyle = C.muted || "#888"; ctx.textAlign = "center"; ctx.fillText(m.text, x, y + 24); } });
          return;
        }
        if (!ind.series) return;
        const names = Object.keys(ind.series), pts = names.map(k => ind.t.map((t, i) => [t, ind.series[k][i]]).filter(([t, v]) => v != null && inWin(t)));
        if (ind.pane === "price") {
          names.forEach((k, j) => { ctx.strokeStyle = pal[j % pal.length]; ctx.lineWidth = 1.2; ctx.beginPath(); let st = false; pts[j].forEach(([t, v]) => { const x = xs(t - tf / 2), y = ys(v); if (!isFinite(y)) return; st ? ctx.lineTo(x, y) : ctx.moveTo(x, y); st = true; }); ctx.stroke(); });
          return;
        }
        const pane = G.panes.find(p => p.id === "cus:" + name); if (!pane) return;
        const vals = pts.flat().map(([, v]) => v); if (!vals.length) { ctx.fillStyle = C.muted || "#888"; ctx.textAlign = "left"; ctx.fillText(`${name}: no values yet`, padL + 4, pane.top + 8); return; }
        let lo2 = Math.min(...vals), hi2 = Math.max(...vals); if (ind.kind === "hist") { lo2 = Math.min(0, lo2); hi2 = Math.max(0, hi2); } if (hi2 === lo2) { hi2 += 1; lo2 -= 1; }
        const yv = v => pane.top + 6 + (hi2 - v) / (hi2 - lo2) * (pane.bottom - pane.top - 12);
        ctx.strokeStyle = C.grid || "#2a2f3a"; ctx.setLineDash([2, 3]); ctx.beginPath(); const y0 = yv(lo2 < 0 && hi2 > 0 ? 0 : lo2); ctx.moveTo(padL, y0); ctx.lineTo(padL + plotW, y0); ctx.stroke(); ctx.setLineDash([]);
        names.forEach((k, j) => {
          const colj = pal[j % pal.length];
          if (ind.kind === "hist") { const bw = Math.max(1, (plotW / Math.max(1, span / (ind.t.length > 1 ? ind.t[1] - ind.t[0] : tf))) * 0.7); pts[j].forEach(([t, v]) => { const x = xs(t - (ind.t.length > 1 ? (ind.t[1] - ind.t[0]) / 2 : tf / 2)); ctx.fillStyle = v >= 0 ? (C.green || "#5fd38d") : (C.red || "#f7768e"); const yy = yv(v); ctx.fillRect(x - bw / 2, Math.min(yy, y0), bw, Math.abs(y0 - yy) || 1); }); }
          else { ctx.strokeStyle = colj; ctx.lineWidth = 1.3; ctx.beginPath(); let st = false; pts[j].forEach(([t, v]) => { const x = xs(t - tf / 2), y = yv(v); st ? ctx.lineTo(x, y) : ctx.moveTo(x, y); st = true; }); ctx.stroke(); }
        });
        const last = pts.map(p => p.length ? p[p.length - 1][1] : null);
        ctx.fillStyle = C.muted || "#888"; ctx.textAlign = "left"; ctx.fillText(`${name}${ind.doc ? " · " + ind.doc.slice(0, 60) : ""} · ${names.map((k, j) => (names.length > 1 ? k + " " : "") + (last[j] != null ? (Math.abs(last[j]) >= 1000 ? last[j].toFixed(0) : last[j].toFixed(2)) : "·")).join(" ")}`, padL + 4, pane.top + 8);
        ctx.textAlign = "right"; ctx.fillText(hi2.toFixed(Math.abs(hi2) >= 100 ? 0 : 2), padL + plotW + G.padR - 4, pane.top + 8); ctx.fillText(lo2.toFixed(Math.abs(lo2) >= 100 ? 0 : 2), padL + plotW + G.padR - 4, pane.bottom - 6);
      });
      // indicator panes: RSI, MACD, open interest and funding (optionally as a 50-bar z-score)
      const paneLine = (pane, ser, col, lo2, hi2, w = 1) => { const sp = (hi2 - lo2) || 1; ctx.strokeStyle = col; ctx.lineWidth = w; ctx.beginPath(); let st = false; ser.forEach((v, i) => { const c = all[i]; if (v == null || !c || c.t + tf < V.from || c.t > V.to) return; const x = xs(c.t + tf / 2), y = pane.top + 6 + (hi2 - v) / sp * (pane.bottom - pane.top - 12); st ? ctx.lineTo(x, y) : ctx.moveTo(x, y); st = true; }); ctx.stroke(); };
      const rsiP = G.panes.find(p => p.id === "rsi"); if (rsiP && I) { const r = this.ind.rsi || (this.ind.rsi = I.rsi(closes, 14)); [30, 70].forEach(lv => { const y = rsiP.top + 6 + (100 - lv) / 100 * (rsiP.bottom - rsiP.top - 12); ctx.strokeStyle = C.grid || "#2a2f3a"; ctx.setLineDash([2, 3]); ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + plotW, y); ctx.stroke(); ctx.setLineDash([]); }); paneLine(rsiP, r, C.violet || "#bb9af7", 0, 100, 1.3); ctx.fillStyle = C.muted || "#888"; ctx.textAlign = "left"; ctx.fillText(`RSI 14 · ${r[r.length - 1] != null ? r[r.length - 1].toFixed(0) : "·"}`, padL + 4, rsiP.top + 8); }
      const macdP = G.panes.find(p => p.id === "macd"); if (macdP && I) { const m = this.ind.macd || (this.ind.macd = I.macd(closes)); const vals = m.macd.concat(m.signal, m.hist).filter(v => v != null && isFinite(v)); const lo2 = Math.min(...vals), hi2 = Math.max(...vals), mid = macdP.top + 6 + (hi2 - 0) / ((hi2 - lo2) || 1) * (macdP.bottom - macdP.top - 12);
        m.hist.forEach((v, i) => { const c = all[i]; if (c.t + tf < V.from || c.t > V.to) return; const x = xs(c.t + tf / 2), y = macdP.top + 6 + (hi2 - v) / ((hi2 - lo2) || 1) * (macdP.bottom - macdP.top - 12); ctx.fillStyle = v >= 0 ? (C.upSoft || "#2a26") : (C.downSoft || "#d336"); ctx.fillRect(x - cw / 2, Math.min(y, mid), cw, Math.abs(y - mid)); });
        paneLine(macdP, m.macd, C.blue || "#5a8cff", lo2, hi2); paneLine(macdP, m.signal, C.yellow || "#e0af68", lo2, hi2); ctx.fillStyle = C.muted || "#888"; ctx.textAlign = "left"; ctx.fillText("MACD 12 26 9", padL + 4, macdP.top + 8); }
      const oiP = G.panes.find(p => p.id === "oi"); if (oiP) { const H = (this.pos && this.pos.rows) || []; const byT = new Map(H.map(r => [r.t, r.oi])); let oiSer = all.map(c => byT.get(c.t) ?? null);
        if (this.layers.oiZ && I) oiSer = I.zscore(oiSer, 50); const vals = oiSer.filter(v => v != null); if (vals.length > 1) { paneLine(oiP, oiSer, C.yellow || "#e0af68", Math.min(...vals), Math.max(...vals), 1.3); ctx.fillStyle = C.muted || "#888"; ctx.textAlign = "left"; ctx.fillText(`open interest${this.layers.oiZ ? " · z-score 50" : ""} · ${fmtK(vals[vals.length - 1])}`, padL + 4, oiP.top + 8); } else { ctx.fillStyle = C.muted || "#888"; ctx.textAlign = "left"; ctx.fillText("open interest pane needs the positioning feed (turn positioning on)", padL + 4, oiP.top + 8); } }
      // premiums: the perp basis to spot and each venue's premium to the reference venue, in bps, from the tape's per-venue VWAPs
      const premP = G.panes.find(p => p.id === "prem"); if (premP) { const P = this.prem; if (P && P.basis_bps) { const ts = all.map(c => c.t), idx = t => Math.round((t - P.t0) / P.tf); const basis = all.map(c => P.basis_bps[idx(c.t)] ?? null);
          const cb = P.premium_bps.coinbase ? all.map(c => P.premium_bps.coinbase[idx(c.t)] ?? null) : null; const vals = basis.concat(cb || []).filter(v => v != null);
          if (vals.length > 1) { const lo2 = Math.min(0, ...vals), hi2 = Math.max(0, ...vals); const y0 = premP.top + 6 + (hi2 - 0) / ((hi2 - lo2) || 1) * (premP.bottom - premP.top - 12); ctx.strokeStyle = C.grid || "#2a2f3a"; ctx.beginPath(); ctx.moveTo(padL, y0); ctx.lineTo(padL + plotW, y0); ctx.stroke();
            paneLine(premP, basis, C.yellow || "#e0af68", lo2, hi2, 1.3); if (cb) paneLine(premP, cb, C.aqua || "#2ac3de", lo2, hi2, 1.1);
            const lb = basis.filter(v => v != null).slice(-1)[0], lc = cb ? cb.filter(v => v != null).slice(-1)[0] : null; ctx.fillStyle = C.muted || "#888"; ctx.textAlign = "left"; ctx.fillText(`perp basis to spot ${lb != null ? lb.toFixed(1) + " bps" : "·"} (yellow)${cb ? ` · Coinbase premium to ${P.reference} ${lc != null ? lc.toFixed(1) + " bps" : "·"} (cyan)` : ""}`, padL + 4, premP.top + 8); }
          else { ctx.fillStyle = C.muted || "#888"; ctx.textAlign = "left"; ctx.fillText("premiums build from the tape's per-venue prices (a day's reach)", padL + 4, premP.top + 8); } }
        else { ctx.fillStyle = C.muted || "#888"; ctx.textAlign = "left"; ctx.fillText("premiums: loading", padL + 4, premP.top + 8); } }
      // net positioning: net longs (green) and net shorts (red) accumulated over the loaded window, from OI-confirmed flow; absorption bars marked on the price pane
      const pos = G.panes.find(p => p.id === "pos");
      if (pos && this.pos && this.pos.rows && this.pos.rows.length) { const P = this.pos, ts = P.rows.map(r => r.t), vi = ts.map((t, i) => [t, i]).filter(([t]) => t + P.tf >= V.from && t <= V.to);
        if (vi.length > 1) { const vals = vi.flatMap(([, i]) => [P.net_long[i], P.net_short[i]]), lo2 = Math.min(0, ...vals), hi2 = Math.max(0, ...vals), sp2 = (hi2 - lo2) || 1; const yy = v => pos.top + 6 + (hi2 - v) / sp2 * (pos.bottom - pos.top - 12);
          ctx.strokeStyle = C.grid || "#2a2f3a"; ctx.beginPath(); ctx.moveTo(padL, yy(0)); ctx.lineTo(padL + plotW, yy(0)); ctx.stroke();
          [[P.net_long, C.up || "#2a2"], [P.net_short, C.down || "#d33"]].forEach(([ser, col]) => { ctx.strokeStyle = col; ctx.lineWidth = 1.3; ctx.beginPath(); vi.forEach(([t, i], k) => { const x = xs(t + P.tf / 2), y = yy(ser[i]); k ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }); ctx.stroke(); });
          vi.forEach(([t, i]) => { if (!P.absorption[i]) return; const c = all.find(k => k.t === t); if (!c) return; const x = xs(t + tf / 2); ctx.strokeStyle = C.yellow || "#e0af68"; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.arc(x, ys(c.c), Math.max(4, cw / 2 + 2), 0, Math.PI * 2); ctx.stroke(); });
          ctx.fillStyle = C.muted || "#888"; ctx.textAlign = "left"; ctx.fillText(`net positioning · longs ${fmtK(P.net_long[P.net_long.length - 1])} · shorts ${fmtK(P.net_short[P.net_short.length - 1])} (OI-confirmed, from ${P.oi_samples} OI samples) · circles: absorption`, padL + 4, pos.top + 8); }
        else { ctx.fillStyle = C.muted || "#888"; ctx.textAlign = "left"; ctx.fillText("net positioning builds as open interest is sampled per bar", padL + 4, pos.top + 8); } }
      else if (pos) { ctx.fillStyle = C.muted || "#888"; ctx.textAlign = "left"; ctx.fillText("net positioning: waiting for the perp venues' open interest", padL + 4, pos.top + 8); }
      // crosshair and readout
      if (this.cursor) { const { x, y } = this.cursor; const t = V.from + (x - padL) / plotW * span; const c = all.find(k => t >= k.t && t < k.t + tf);
        ctx.strokeStyle = C.muted || "#888"; ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(x + 0.5, price.top); ctx.lineTo(x + 0.5, h - G.padB); ctx.moveTo(padL, y + 0.5); ctx.lineTo(padL + plotW, y + 0.5); ctx.stroke(); ctx.setLineDash([]);
        if (y >= price.top && y <= price.bottom) { const pv = hi - (y - price.top) / (price.bottom - price.top) * (hi - lo); ctx.fillStyle = C.labelBg || "#000"; ctx.fillRect(padL + plotW + 2, y - 8, G.padR - 4, 16); ctx.fillStyle = C.ink || "#eee"; ctx.textAlign = "left"; ctx.fillText(fmtP(pv), padL + plotW + 6, y); }
        ctx.fillStyle = C.labelBg || "#000"; const lbl = fmtT(t, tf); ctx.fillRect(x - 34, h - 16, 68, 14); ctx.fillStyle = C.ink || "#eee"; ctx.textAlign = "center"; ctx.fillText(lbl, x, h - 9);
        if (this.readout) { const f = c && this.fpByT.get(c.t); this.readout.textContent = c ? `${new Date(c.t * 1000).toISOString().slice(0, 16).replace("T", " ")}Z  O ${fmtP(c.o)}  H ${fmtP(c.h)}  L ${fmtP(c.l)}  C ${fmtP(c.c)}  vol ${fmtK(c.v)}${c.vb || c.vs ? `  buy $${fmtK(c.vb)} sell $${fmtK(c.vs)} Δ ${fmtK(c.vb - c.vs)}` : ""}  ${c.n} prints` +
          (f ? `  │ Δ range ${fmtK(f.delta_min)}…${fmtK(f.delta_max)}  POC ${fmtP(f.poc)}  VA ${fmtP(f.va && f.va[0])}–${fmtP(f.va && f.va[1])}  imbalances ${(f.imb || []).length}  max print $${fmtK(f.max_print)}${f.liq_long || f.liq_short ? `  liq L $${fmtK(f.liq_long)} S $${fmtK(f.liq_short)}` : ""}${f.ob_imb_05 != null ? `  book imb ${(f.ob_imb_05 * 100).toFixed(0)}%` : ""}` : "") : ""; } }
      else if (this.readout) { const c = all[all.length - 1]; this.readout.textContent = `${d.pair} · ${TF_LABEL(tf)} · ${vis.length} of ${all.length} candles from the ${d.source}${d.venues && d.venues.length ? " · " + d.venues.join(", ") : ""} · last ${fmtP(c.c)}`; }
    }
    _niceStep(s) { const steps = [60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400, 172800, 604800]; return steps.find(x => x >= s) || 604800 * Math.ceil(s / 604800); }
  }
  window.SteelChart = SteelChart; window.steelTfLabel = TF_LABEL;
})();
