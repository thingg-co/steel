(() => {
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
// The canvas palette is read from the stylesheet's tokens, so the charts follow the theme; it is
// re-read when the header toggle flips the theme (applyTheme) and every canvas repaints.
function palette() {
  const cs = getComputedStyle(document.documentElement), v = n => cs.getPropertyValue(n).trim();
  return { blue: v("--blue"), orange: v("--orange"), aqua: v("--aqua"), yellow: v("--yellow"), magenta: v("--magenta"), violet: v("--violet"),
    good: v("--good"), warning: v("--warning"), serious: v("--serious"), critical: v("--critical"),
    ink: v("--ink"), ink2: v("--ink-2"), muted: v("--muted"), grid: v("--grid"), axis: v("--axis"), surface: v("--surface"),
    up: v("--up"), down: v("--down"), upSoft: v("--up-soft"), downSoft: v("--down-soft"), upFill: v("--up-fill"), downFill: v("--down-fill"),
    fanOuter: v("--fan-outer"), fanInner: v("--fan-inner"), signal: v("--signal"), labelBg: v("--label-bg"),
    heatUp: v("--heat-up") || "12,163,12", heatDown: v("--heat-down") || "208,59,59", onColor: v("--on-color") || "rgba(255,255,255,0.92)" };
}
let C = palette();
let SERIES = [C.blue, C.orange, C.aqua, C.yellow, C.magenta, C.violet];
function currentTheme() {
  return document.documentElement.dataset.theme || (window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
}
function applyTheme(next) {
  if (next) { document.documentElement.dataset.theme = next; try { localStorage.setItem("steel.theme", next); } catch (e) { /* storage may be unavailable */ } }
  C = palette(); SERIES = [C.blue, C.orange, C.aqua, C.yellow, C.magenta, C.violet];
  const b = $("#theme-toggle");
  if (b) { const dark = currentTheme() === "dark"; b.textContent = dark ? "☀" : "☾"; b.title = dark ? "Switch to light mode" : "Switch to dark mode"; b.setAttribute("aria-label", b.title); }
  if (typeof redrawCharts === "function") requestAnimationFrame(() => { redrawCharts(); if (typeof renderAll === "function" && state.config) renderAll(); });
}
const CHANNELS = {
  feed: "tick counts per bar, backfill, reconnects",
  bars: "closed bars: close, return, trades, volume",
  features: "standardized inputs at the latest bar",
  model: "forecast quantiles, P(up), regime gate, attention",
  policy: "how each forecast became BUY, SELL or HOLD",
  learn: "matured labels, training steps, loss",
  news: "headless-browser skim: headlines, tone, ideas",
  signals: "Reddit crypto chatter, perp positioning, DVOL, and the whole-market scan",
  operator: "input you inject from the dashboard, and the nudges it applies",
  system: "checkpoints, settings, lifecycle",
};
const CONTROL_FIELDS = [
  ["score_threshold", "score threshold", 0.01], ["prob_margin", "P(up) margin", 0.01], ["cost_bps", "cost floor (bps)", 0.1],
  ["max_size", "max size", 0.05], ["lr", "learning rate", 0.0001], ["steps_per_label", "steps per label", 1],
  ["min_labels", "labels before trusted", 1], ["news_minutes", "news every (min)", 1],
  ["kelly_fraction", "Kelly fraction", 0.01], ["move_floor_bps", "move floor (bps)", 1], ["min_hit_rate", "min hit rate", 0.01],
  ["deep_backfill_days", "backfill depth (days)", 1], ["train_epochs", "background training (passes/day)", 0.5],
  ["skill_min_n", "calls before a name trades", 1], ["confirm_bars", "bars to confirm a signal", 1],
  ["min_hold_bars", "min hold, spot (bars)", 1], ["short_min_hold_bars", "min hold, short (bars)", 1],
  ["max_spread_bps", "max spread (bps)", 1], ["min_dollar_volume", "min daily $ volume", 100000],
  ["short_leverage", "short leverage (x)", 0.5], ["long_leverage", "long leverage (x)", 0.5], ["short_max_frac", "max margin per short (frac)", 0.005], ["perp_fee_bps", "perp taker fee (bps, 0 = venue)", 0.1], ["spot_fee_bps", "spot taker fee (bps, 0 = venue)", 0.1],
  ["stop_loss_bps", "stop loss (bps)", 10], ["take_profit_bps", "take profit (bps, 0 off)", 10], ["max_hold_bars", "max hold (bars, 0 off)", 1],
  ["exit_cooldown_bars", "cooldown after exit (bars)", 1], ["daily_loss_limit", "daily loss limit (frac)", 0.005], ["size_by_coverage", "size by coverage (0/1)", 1],
];

let wsUp = false;
const state = { config: null, status: {}, controls: {}, prices: {}, bars: {}, latest: {}, gate: [], outcomes: {}, portfolio: null,
  metrics: {}, history: { loss: [], hit: [], coverage: [], pnl: [] }, log: [], news: null,
  sources: [], news_sources: [], providers: {}, classes: {},
  signals: null, signal_providers: [], brief: null, paper: null, overlay: { enabled: true }, keys: [], muted: [], classes: {}, universe: [] };
const cards = {};
const chartRO = ("ResizeObserver" in window) ? new ResizeObserver(entries => {
  for (const e of entries) { const c = cards[e.target.dataset.sym]; if (c && e.target.clientWidth > 0) drawChart(c); }
}) : null;
const consoles = {};
const lastPrices = {};
let ws, retry = 1000, drawQueued = false;
let audioCtx = null, soundOn = false;
let demoLoading = /[?&#]loading\b/i.test(location.href);
const prevAction = {};
try { soundOn = localStorage.getItem("steel.sound") === "1"; } catch (e) { /* off unless this browser turned it on before */ }

function beep(freq, dur, when = 0, type = "sine", gain = 0.14) {
  if (!soundOn || !audioCtx) return;
  const t = audioCtx.currentTime + when;
  const osc = audioCtx.createOscillator(), g = audioCtx.createGain();
  osc.type = type; osc.frequency.value = freq;
  g.gain.setValueAtTime(0.0001, t);
  g.gain.exponentialRampToValueAtTime(gain, t + 0.01);
  g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
  osc.connect(g); g.connect(audioCtx.destination);
  osc.start(t); osc.stop(t + dur + 0.02);
}

function ringEvent(kind) {
  if (kind === "buy") { beep(660, 0.12); beep(990, 0.16, 0.12, "sine", 0.16); }
  else if (kind === "sell") { beep(440, 0.12); beep(300, 0.18, 0.12, "sine", 0.16); }
  else if (kind === "flip") { beep(520, 0.09); beep(400, 0.09, 0.1); beep(300, 0.14, 0.2); }
}

function checkAlerts() {
  if (!state.config) return;
  const mutedSet = new Set(state.muted || []);
  state.config.symbols.forEach(sym => {
    const L = state.latest[sym];
    if (!L || mutedSet.has(sym)) return;
    const now = L.trusted && L.side ? L.action : "HOLD";
    const was = prevAction[sym];
    if (was !== undefined && now !== was && now !== "HOLD") {
      const flipped = L.overlay && L.overlay.some(n => /contrarian flip/.test(n));
      ringEvent(flipped ? "flip" : now === "BUY" ? "buy" : "sell");
      flashCard(sym);
    }
    prevAction[sym] = now;
  });
}

function flashCard(sym) {
  const card = cards[sym];
  if (!card) return;
  card.el.classList.add("flash");
  setTimeout(() => card.el.classList.remove("flash"), 1400);
}

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const safeUrl = u => /^https?:\/\//i.test(u || "") ? u : "#";
const fmtPrice = v => v == null ? "·" : v >= 100 ? v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : v >= 1 ? v.toFixed(3) : v.toFixed(5);
const fmtBps = v => v == null ? "·" : (v >= 0 ? "+" : "") + v.toFixed(1);
const fmtPct = v => v == null ? "·" : (v * 100).toFixed(1) + "%";
const fmtTime = t => new Date(t * 1000).toLocaleTimeString([], { hour12: false });
const fmtDur = s => s < 60 ? `${s | 0}s` : s < 3600 ? `${(s / 60) | 0}m ${(s % 60) | 0}s` : `${(s / 3600) | 0}h ${((s % 3600) / 60) | 0}m`;
const base = sym => sym.split("-")[0];
// coin quantities are fractional (0.0123 BTC, 1.2e6 PEPE): six significant digits, no trailing zeros
const fmtQty = v => { if (v == null) return "·"; const n = Number(v); if (!isFinite(n)) return "·";
  return Math.abs(n) >= 1e6 ? n.toLocaleString(undefined, { maximumFractionDigits: 0 }) : String(parseFloat(n.toPrecision(6))); };
const fmtRate = (v, d = 4) => v == null ? "·" : (v >= 0 ? "+" : "") + (Number(v) * 100).toFixed(d) + "%";

// Canvas helpers ------------------------------------------------------------------

function ctx2d(canvas) {
  const r = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(10, r.width | 0), h = Math.max(10, r.height | 0);
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

function ema(a, p) { const k = 2 / (p + 1); let e; return a.map((v, i) => (e = i ? v * k + e * (1 - k) : v)); }

function drawChart(card) {
  if (!card.canvas.clientWidth) return;   // not laid out yet (cold first paint / hidden tab) -- a later redraw handles it
  const sym = card.sym;
  const bars = (state.bars[sym] || []).slice(-((state.config && state.config.chart_bars) || 160));
  const { ctx, w, h } = ctx2d(card.canvas);
  ctx.font = "10px system-ui, sans-serif";
  if (bars.length < 2) { ctx.fillStyle = C.muted; ctx.fillText("waiting for bars", 10, 20); return; }
  const cfg = state.config, H = cfg.horizon, L = state.latest[sym];
  const padL = 8, padR = 8, padT = 10, padB = 16;
  const n = bars.length;
  const plotW = w - padL - padR;
  const fanW = Math.max(0.2 * plotW, 64);
  const dx = (plotW - fanW) / (n - 1);
  const xs = i => Math.min(padL + i * dx, padL + plotW);
  const closes = bars.map(b => b.c);
  const upC = C.up, downC = C.down;
  const cw = Math.max(1, Math.min(dx * 0.68, 9));

  // panes: price on top, a volume strip, then MACD below (shared x axis, no overlap)
  const macdH = 46, volPaneH = 26, gap = 8, axisY = h - padB;
  const mB = axisY, mT = mB - macdH;
  const vB = mT - gap, vT = vB - volPaneH;
  const pT = padT, pB = vT - gap;

  const live = state.prices[sym] && state.prices[sym].price;
  const marketLive = true;                            // crypto never closes: the live price is always current
  let lo = Math.min(...bars.map(b => b.l)), hi = Math.max(...bars.map(b => b.h));
  if (live && marketLive) { lo = Math.min(lo, live); hi = Math.max(hi, live); }
  let fan = null;
  if (L && L.q) { fan = L.q.map(q => L.price * Math.exp(q / 1e4)); }   // fan drawn but clipped; wide model bands must not crush the candles
  const span = (hi - lo) || closes[n - 1] * 1e-4;
  lo -= span * 0.06; hi += span * 0.06;
  const ys = v => pT + (hi - v) / (hi - lo) * (pB - pT);

  // price grid + labels
  ctx.strokeStyle = C.grid; ctx.lineWidth = 1; ctx.textBaseline = "middle"; ctx.textAlign = "left";
  for (let k = 0; k < 3; k++) {
    const v = lo + (hi - lo) * (0.14 + 0.36 * k), y = Math.round(ys(v)) + 0.5;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    const label = fmtPrice(v), tw = ctx.measureText(label).width;
    ctx.fillStyle = C.labelBg; ctx.fillRect(padL, y - 13, tw + 4, 12);
    ctx.fillStyle = C.muted; ctx.fillText(label, padL + 2, y - 7);
  }

  // volume pane (its own strip between price and MACD, so it never overlaps the candles)
  const maxVol = bars.reduce((mx, b) => Math.max(mx, b.v || 0), 0);
  if (maxVol > 0) {
    bars.forEach((b, i) => {
      const v = b.v || 0; if (v <= 0) return;
      const vh = (v / maxVol) * volPaneH;
      ctx.fillStyle = b.c >= b.o ? C.upSoft : C.downSoft;
      ctx.fillRect(xs(i) - cw / 2, vB - vh, cw, Math.max(1, vh));
    });
    ctx.strokeStyle = C.grid; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(padL, vB + 0.5); ctx.lineTo(xs(n - 1), vB + 0.5); ctx.stroke();
    ctx.fillStyle = C.muted; ctx.textBaseline = "alphabetic"; ctx.textAlign = "left";
    ctx.fillText("vol", padL + 1, vT + 8);
  }

  // forecast fan: straight wedges from the last close to the quantiles at the horizon
  const xLast = xs(n - 1), xEnd = padL + plotW;
  const anchor = L ? L.price : closes[n - 1];
  if (fan) {
    ctx.save();
    ctx.beginPath(); ctx.rect(padL, pT, w - padL - padR, pB - pT); ctx.clip();   // keep the fan inside the price pane
    const wedge = (a, b, color) => { ctx.beginPath(); ctx.moveTo(xLast, ys(anchor)); ctx.lineTo(xEnd, ys(a)); ctx.lineTo(xEnd, ys(b)); ctx.closePath(); ctx.fillStyle = color; ctx.fill(); };
    wedge(fan[0], fan[4], C.fanOuter);
    wedge(fan[1], fan[3], C.fanInner);
    // fat-tail forecasts flag their outer edges in orange: big move, direction unclear
    if (L.fat_tails) {
      ctx.setLineDash([2, 3]); ctx.strokeStyle = C.signal; ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.moveTo(xLast, ys(anchor)); ctx.lineTo(xEnd, ys(fan[0])); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(xLast, ys(anchor)); ctx.lineTo(xEnd, ys(fan[4])); ctx.stroke();
    }
    // median tinted by the model's call
    ctx.setLineDash([4, 3]); ctx.lineWidth = 1.5;
    ctx.strokeStyle = L.action === "BUY" ? upC : L.action === "SELL" ? downC : C.blue;
    ctx.beginPath(); ctx.moveTo(xLast, ys(anchor)); ctx.lineTo(xEnd, ys(fan[2])); ctx.stroke(); ctx.setLineDash([]);
    ctx.restore();
    const my = Math.max(pT + 8, Math.min(pB - 2, ys(fan[2]) - 9));   // keep the median label on-pane
    ctx.fillStyle = C.ink2; ctx.textAlign = "right"; ctx.fillText(`${fmtBps(L.q[2])} bps`, xEnd - 2, my); ctx.textAlign = "left";
  }

  // candlesticks -- but a quote-only feed (CoinGecko aggregate prices) has no intrabar OHLC at all, so
  // its bars are flat (o=h=l=c) and would render as meaningless dashes; fall back to a close line there.
  // Only there: a thin name has many flat bars too (carry-forward bars in a quiet five minutes), and
  // those are honest dashes next to real candles.
  const flat = bars.reduce((k, b) => k + (b.o === b.h && b.h === b.l && b.l === b.c ? 1 : 0), 0);
  if (flat < bars.length * 0.95) {
    bars.forEach((b, i) => {
      const x = xs(i), col = b.c >= b.o ? upC : downC;
      ctx.strokeStyle = col; ctx.fillStyle = col; ctx.lineWidth = 1;
      const wx = Math.round(x) + 0.5;
      ctx.beginPath(); ctx.moveTo(wx, ys(b.h)); ctx.lineTo(wx, ys(b.l)); ctx.stroke();
      const yo = ys(b.o), yc = ys(b.c);
      ctx.fillRect(x - cw / 2, Math.min(yo, yc), cw, Math.max(1, Math.abs(yc - yo)));
    });
  } else {
    ctx.strokeStyle = C.blue; ctx.lineWidth = 2; ctx.lineJoin = "round";
    ctx.beginPath(); bars.forEach((b, i) => (i ? ctx.lineTo(xs(i), ys(b.c)) : ctx.moveTo(xs(i), ys(b.c)))); ctx.stroke();
  }
  if (live && marketLive) {   // the live-price dot on the last candle
    ctx.fillStyle = C.ink; ctx.beginPath(); ctx.arc(xLast, ys(live), 4, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = C.surface; ctx.lineWidth = 2; ctx.stroke();
  }

  // the fan's track record: each matured forecast draws a diamond at its predicted median on
  // the bar where it resolved, with a stem to the close it was judged against. Green/red by
  // direction hit; hollow when the label landed outside the calibrated band.
  const outs = (state.outcomes && state.outcomes[sym]) || [];
  if (outs.length && cfg.horizon && cfg.bar_seconds) {
    const tsIdx = new Map(bars.map((b, i) => [Math.round(b.t), i]));
    outs.forEach(o => {
      const i = tsIdx.get(Math.round(o.t + cfg.horizon * cfg.bar_seconds));
      if (i == null) return;
      const realized = bars[i].c;
      const origin = realized / Math.exp((o.y || 0) / 1e4);       // close the forecast was made from
      const pred = origin * Math.exp((o.q50 || 0) / 1e4);
      const x = xs(i), yp = ys(pred), yr = ys(realized);
      const col = o.hit === true ? upC : o.hit === false ? downC : C.muted;
      ctx.globalAlpha = 0.45; ctx.strokeStyle = col; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, yp); ctx.lineTo(x, yr); ctx.stroke();
      ctx.globalAlpha = 0.9; ctx.fillStyle = col; ctx.strokeStyle = col;
      ctx.beginPath(); ctx.moveTo(x, yp - 3); ctx.lineTo(x + 3, yp); ctx.lineTo(x, yp + 3); ctx.lineTo(x - 3, yp); ctx.closePath();
      o.inside === false ? ctx.stroke() : ctx.fill();
      ctx.globalAlpha = 1;
    });
  }

  // MACD pane (12/26/9)
  const ef = ema(closes, 12), es = ema(closes, 26);
  const mline = closes.map((_, i) => ef[i] - es[i]);
  const sigl = ema(mline, 9);
  const hist = mline.map((v, i) => v - sigl[i]);
  let mmax = 1e-9;
  for (let i = 0; i < n; i++) mmax = Math.max(mmax, Math.abs(mline[i]), Math.abs(sigl[i]), Math.abs(hist[i]));
  mmax *= 1.15;
  const ym = v => (mT + mB) / 2 - (v / mmax) * (macdH / 2);
  const hw = Math.max(1, Math.min(dx * 0.6, 7));
  hist.forEach((v, i) => { const x = xs(i), y0 = ym(0), y1 = ym(v); ctx.fillStyle = v >= 0 ? C.upSoft : C.downSoft; ctx.fillRect(x - hw / 2, Math.min(y0, y1), hw, Math.max(1, Math.abs(y1 - y0))); });
  const zeroY = Math.round(ym(0)) + 0.5;
  ctx.strokeStyle = C.grid; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(padL, zeroY); ctx.lineTo(xLast, zeroY); ctx.stroke();
  const drawLine = (arr, color) => { ctx.strokeStyle = color; ctx.lineWidth = 1.3; ctx.lineJoin = "round"; ctx.beginPath(); for (let i = 0; i < n; i++) { const x = xs(i), y = ym(arr[i]); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); } ctx.stroke(); };
  drawLine(mline, C.blue);
  drawLine(sigl, C.signal);
  ctx.textBaseline = "alphabetic"; ctx.textAlign = "left";
  let lx = padL + 1;
  ctx.fillStyle = C.blue; ctx.fillText("MACD", lx, mT + 9); lx += ctx.measureText("MACD").width + 6;
  ctx.fillStyle = C.signal; ctx.fillText("signal", lx, mT + 9); lx += ctx.measureText("signal").width + 6;
  ctx.fillStyle = C.muted; ctx.fillText("12·26·9", lx, mT + 9);

  // x axis + now marker — show dates when the window spans more than a day, so a multi-day
  // chart doesn't read as a few minutes (times of day alone repeat every 24h)
  const spanH = (bars[n - 1].t - bars[0].t) / 3600;
  const fmtAxis = ts => { const d = new Date(ts * 1000), hm = d.toTimeString().slice(0, 5);
    return spanH > 20 ? `${d.getMonth() + 1}/${d.getDate()} ${hm}` : d.toTimeString().slice(0, 8); };
  const mid = (n - 1) >> 1;
  ctx.fillStyle = C.muted; ctx.textBaseline = "alphabetic";
  ctx.textAlign = "left"; ctx.fillText(fmtAxis(bars[0].t), padL, h - 4);
  ctx.textAlign = "center"; ctx.fillText(fmtAxis(bars[mid].t), xs(mid), h - 4);
  ctx.textAlign = "right"; ctx.fillText(fmtAxis(bars[n - 1].t), xLast, h - 4);
  ctx.fillText(`+${Math.round(H * cfg.bar_seconds / 60)}m`, xEnd, h - 4); ctx.textAlign = "left";
  ctx.strokeStyle = C.axis; ctx.setLineDash([2, 3]); ctx.beginPath(); ctx.moveTo(Math.round(xLast) + 0.5, pT); ctx.lineTo(Math.round(xLast) + 0.5, mB); ctx.stroke(); ctx.setLineDash([]);

  card.geom = { padL, dx, n, bars };
  if (card.hoverX != null) {
    const i = Math.max(0, Math.min(n - 1, Math.round((card.hoverX - padL) / dx)));
    const x = xs(i);
    ctx.strokeStyle = C.ink2; ctx.lineWidth = 1; ctx.setLineDash([2, 3]);
    ctx.beginPath(); ctx.moveTo(x, pT); ctx.lineTo(x, mB); ctx.stroke(); ctx.setLineDash([]);
    const b = bars[i], prev = i ? bars[i - 1].c : b.o;
    ctx.fillStyle = b.c >= b.o ? upC : downC; ctx.beginPath(); ctx.arc(x, ys(b.c), 3.5, 0, Math.PI * 2); ctx.fill();
    showTooltip(card.tipX, card.tipY, `${fmtTime(b.t)}\nO ${fmtPrice(b.o)}  H ${fmtPrice(b.h)}\nL ${fmtPrice(b.l)}  C ${fmtPrice(b.c)}\n${fmtBps(Math.log(b.c / prev) * 1e4)} bps  vol ${b.v.toPrecision(3)}\nMACD ${mline[i].toFixed(3)}  sig ${sigl[i].toFixed(3)}`);
  }
}

function drawSpark(sel, values, { color = C.blue, fmt = v => v.toFixed(2), ref = null } = {}) {
  const canvas = $(sel);
  const { ctx, w, h } = ctx2d(canvas);
  const vals = (values || []).filter(v => v != null);
  ctx.font = "11px system-ui, sans-serif"; ctx.textBaseline = "alphabetic";
  if (vals.length < 2) { ctx.fillStyle = C.muted; ctx.fillText("collecting", 6, 16); return; }
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (ref != null) { lo = Math.min(lo, ref); hi = Math.max(hi, ref); }
  if (hi === lo) { hi += 1e-6; lo -= 1e-6; }
  const padT = 14, padB = 6, padR = 52;
  const xs = i => 4 + i / (vals.length - 1) * (w - padR - 4);
  const ys = v => padT + (hi - v) / (hi - lo) * (h - padT - padB);
  if (ref != null) {
    ctx.strokeStyle = C.axis; ctx.setLineDash([3, 3]); ctx.beginPath();
    ctx.moveTo(4, Math.round(ys(ref)) + 0.5); ctx.lineTo(w - padR, Math.round(ys(ref)) + 0.5); ctx.stroke(); ctx.setLineDash([]);
  }
  ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.lineJoin = "round";
  ctx.beginPath(); vals.forEach((v, i) => i ? ctx.lineTo(xs(i), ys(v)) : ctx.moveTo(xs(i), ys(v))); ctx.stroke();
  const last = vals[vals.length - 1];
  ctx.fillStyle = color; ctx.beginPath(); ctx.arc(xs(vals.length - 1), ys(last), 3.5, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = C.ink; ctx.font = "12px ui-monospace, Menlo, monospace"; ctx.textBaseline = "middle";
  ctx.fillText(fmt(last), w - padR + 6, ys(last));
  ctx.fillStyle = C.muted; ctx.font = "10px system-ui, sans-serif"; ctx.textBaseline = "alphabetic";
  ctx.fillText(fmt(hi), 4, 10); ctx.fillText(fmt(lo), 4, h - 1);
}

const tooltip = $("#tooltip");
function showTooltip(x, y, text) {
  tooltip.textContent = text; tooltip.style.display = "block";
  const r = tooltip.getBoundingClientRect();
  tooltip.style.left = Math.min(x + 14, window.innerWidth - r.width - 8) + "px";
  tooltip.style.top = Math.max(4, y - r.height - 12) + "px";
}
function hideTooltip() { tooltip.style.display = "none"; }

// Dashboard -----------------------------------------------------------------------

function buildCards() {
  const root = $("#cards"); root.innerHTML = "";
  if (chartRO) chartRO.disconnect();
  for (const k of Object.keys(cards)) delete cards[k];
  state.config.symbols.forEach(sym => {
    const el = document.createElement("section"); el.className = "card"; el.dataset.sym = sym;
    el.innerHTML = `<header><span class="sym">${esc(sym)}</span><span class="spread num"></span><span class="price num">·</span></header>
      <div class="via" title="active data source for this symbol"></div>
      <div class="signal"><span class="badge hold">■ HOLD</span><span class="size num"></span><span class="warm" hidden>warming up</span></div>
      <div class="chart-wrap"><canvas class="chart"></canvas></div>
      <div class="stats"><div><label>median</label><b class="q50">·</b></div><div><label>10-90 band</label><b class="band">·</b></div>
        <div><label>P(up)</label><b class="pup">·</b></div><div><label>P(down)</label><b class="pdown">·</b></div></div>
      <div class="pmeter" title="right = P(up), left = P(down); the gap between is P(flat)"><i class="up"></i><i class="dn"></i></div>
      <div class="outcomes" title="matured forecasts, newest right: green hit, red miss, outlined = acted on"></div>
      <div class="why"></div>
      <div class="strategy"></div>`;
    root.appendChild(el);
    const card = cards[sym] = { sym, el, canvas: $("canvas", el), hoverX: null };
    card.canvas.dataset.sym = sym;
    if (chartRO) chartRO.observe(card.canvas);
    card.canvas.addEventListener("mousemove", e => { card.hoverX = e.offsetX; card.tipX = e.clientX; card.tipY = e.clientY; drawChart(card); });
    card.canvas.addEventListener("mouseleave", () => { card.hoverX = null; hideTooltip(); drawChart(card); });
    el.addEventListener("click", e => { if (!e.target.closest("canvas, a, button, input")) openDetail(sym); });
  });
}

// only the top cards render; the rest wait behind "show more". A page is as many cards as fill whole
// rows at the current width (about 50), so the last row is never ragged; each "more" adds one page.
let cardPages = 1;
function cardCols() {
  const root = $("#cards"); if (!root) return 1;
  const cols = getComputedStyle(root).gridTemplateColumns.split(" ").filter(x => x && x !== "0px").length;
  return Math.max(1, cols);
}
function cardPageSize() { const c = cardCols(); return Math.max(c, Math.floor(50 / c) * c); }
function applyCardLimit(order) {
  order = order || [...document.querySelectorAll("#cards .card")].map(c => c.dataset.sym);
  const mutedSet = new Set(state.muted || []);
  const page = cardPageSize(), limit = cardPages * page;
  let shown = 0, total = 0;
  order.forEach(sym => {
    const c = cards[sym]; if (!c || mutedSet.has(sym)) return;
    total++;
    const beyond = shown >= limit;
    c.el.classList.toggle("beyond", beyond);
    if (!beyond) shown++;
  });
  const wrap = $("#more-wrap");
  if (wrap) {
    wrap.hidden = total <= shown;
    $("#more-cards").textContent = `Show ${Math.min(page, total - shown)} more`;
    $("#more-meta").textContent = `${shown} of ${total} cards, most urgent first`;
  }
  redrawCharts();
}
document.addEventListener("DOMContentLoaded", () => { const b = $("#more-cards"); if (b) b.onclick = () => { cardPages += 1; applyCardLimit(); }; });
let cardsResizeT = null;
addEventListener("resize", () => { clearTimeout(cardsResizeT); cardsResizeT = setTimeout(() => { if (state.config) applyCardLimit(); }, 150); });

// ---- symbol detail overlay: everything the dashboard knows about one name ----
let detailSym = null, detailCard = null, pendingDetail = null;
function openDetail(sym, pushHistory = true) {
  if (!state.config) { pendingDetail = sym; return; }          // before the first snapshot: open once the data is here
  const tab = document.body.dataset.view || "market";
  if (pushHistory && location.hash !== `#${tab}/${sym}`) history.pushState(null, "", `#${tab}/${encodeURIComponent(sym)}`);
  detailSym = sym;
  const d = $("#detail"); d.hidden = false;
  if (!detailCard) {
    const cv = $("#dcanvas");
    detailCard = { sym, el: d, canvas: cv, hoverX: null };
    cv.addEventListener("mousemove", e => { detailCard.hoverX = e.offsetX; detailCard.tipX = e.clientX; detailCard.tipY = e.clientY; drawChart(detailCard); });
    cv.addEventListener("mouseleave", () => { detailCard.hoverX = null; hideTooltip(); drawChart(detailCard); });
    addEventListener("resize", () => { if (detailSym) drawChart(detailCard); });
  }
  detailCard.sym = sym; detailCard.hoverX = null;
  renderDetail();
  const hasBars = (state.bars[sym] || []).length > 1;
  $("#detail .dchart").hidden = !hasBars;
  if (hasBars) requestAnimationFrame(() => drawChart(detailCard));
}
function closeDetail(pushHistory = true) {
  detailSym = null; hideTooltip(); $("#detail").hidden = true;
  if (pushHistory && location.hash.includes("/")) history.pushState(null, "", location.hash.split("/")[0]);   // forward keeps the detail, back reopens it
}
function renderDetail() {
  const sym = detailSym; if (!sym) return;
  const L = state.latest[sym] || {}, P = state.prices[sym] || {}, cfg = state.config || {};
  const bps = v => v == null ? "·" : `${v >= 0 ? "+" : ""}${Number(v).toFixed(0)} bps`;
  const pct = v => v == null ? "·" : `${(v * 100).toFixed(0)}%`;
  const num = (v, d = 2) => v == null || v === "" || Number.isNaN(Number(v)) ? "·" : Number(v).toFixed(d);
  const sgn = v => v > 0 ? "good" : v < 0 ? "bad" : "";
  const row = (k, v, cls = "") => `<dt>${esc(k)}</dt><dd class="${cls}">${v}</dd>`;
  const bar = (v, scale = 1) => { const x = Math.max(-1, Math.min(1, (v || 0) / scale)); const w = Math.abs(x) * 50;
    return `<span class="dbar"><i class="${x < 0 ? "neg" : "pos"}" style="left:${x < 0 ? 50 - w : 50}%;width:${w}%"></i></span>${num(v)}`; };
  const sect = (title, inner, wide = false) => `<section class="dsec${wide ? " wide" : ""}"><h4>${esc(title)}</h4>${inner || '<div class="dempty">nothing yet</div>'}</section>`;
  const kpi = (label, val, sub = "", cls = "") => `<div class="dkpi ${cls}"><label>${esc(label)}</label><b>${val}</b>${sub ? `<small>${sub}</small>` : ""}</div>`;
  const d = $("#detail");
  // header
  $(".dsym", d).textContent = sym;
  const idn = (state.identity || {})[sym];
  const idText = idn && idn.name ? `${idn.name}${idn.rank ? ` #${idn.rank}` : ""}${idn.ambiguous ? (idn.resolved ? " (ticker shared" + (idn.others && idn.others.length ? " with " + idn.others.join(", ") : "") + ")" : " ⚠ ticker shared, unresolved on this venue") : ""}` : "";
  $(".dmeta", d).textContent = state.latest[sym] ? [state.classes[sym], state.providers[sym] || state.status.providers?.[sym], L.trusted ? "trusted" : "warming up", idText].filter(Boolean).join(" · ") : "on the radar, not modeled";
  const rr0 = ((state.signals && state.signals.market && state.signals.market.radar) || []).find(r => r.symbol === sym);
  const price = P.price ?? L.price ?? (rr0 && rr0.price); $(".dprice", d).textContent = fmtPrice(price);
  const bars = state.bars[sym] || []; const chg = bars.length && bars[0].o ? (price / bars[0].o - 1) * 100 : null;
  const dchg = $(".dchg", d); dchg.textContent = chg == null ? "" : `${chg >= 0 ? "+" : ""}${chg.toFixed(2)}% over ${bars.length} bars`; dchg.className = "dchg num " + (chg > 0 ? "up" : chg < 0 ? "down" : "");
  const badge = $(".dbadge", d); badge.textContent = L.action || "HOLD"; badge.className = "dbadge " + (L.action === "BUY" ? "buy" : L.action === "SELL" ? "sell" : "");
  if (!$("#d-ticket", d)) { const b = document.createElement("button"); b.type = "button"; b.className = "mini"; b.id = "d-ticket"; b.textContent = "Ticket"; b.title = "an order ticket for this call, to place by hand"; badge.after(b); }
  $("#d-ticket", d).onclick = () => showTicket(sym);
  $(".dwhy", d).textContent = L.why || (state.latest[sym] ? "no forecast yet" : "Steel does not model this name; what follows is what the market scan knows about it.");
  // KPIs
  const q = L.q || [];
  $(".dkpis", d).innerHTML =
    kpi("median", bps(q[2]), (state.status && state.status.target && state.status.target.mode === "relative") ? `1-hour move vs ${(state.status.target.index || []).join(", ")}` : "1-hour expected move", sgn(q[2])) +
    kpi("10–90 band", q.length ? `${(q[4] - q[0]).toFixed(0)} bps` : "·", q.length ? `${bps(q[0])} … ${bps(q[4])}` : "") +
    kpi("P(up)", pct(L.p_up), `raw ${pct(L.p_raw)}`, L.p_up > 0.5 ? "good" : "") +
    kpi("P(down)", pct(L.p_down), `raw ${pct(L.p_down_raw)}`, L.p_down > 0.5 ? "bad" : "") +
    kpi("score", num(L.score), "move ÷ uncertainty", sgn(L.score)) +
    kpi("size", L.size ? `${(L.size * 100).toFixed(1)}%` : "0%", "of the paper book") +
    Object.entries(L.horizons || {}).map(([h, v]) => kpi(`${h} median`, bps(v.q[2]), `${bps(v.q[0])} … ${bps(v.q[4])} · P(up) ${pct(v.p_up)} (raw, not traded)`, sgn(v.q[2]))).join("");
  // sections
  const qr = L.q_raw || [];
  const fc = row("calibrated quantiles", q.length ? q.map(x => x.toFixed(0)).join(" / ") : "·") + row("raw quantiles", qr.length ? qr.map(x => x.toFixed(0)).join(" / ") : "·")
    + row("middle-50% width", bps(L.iqr)) + row("band scale", num(L.band_scale)) + row("P(up) scaling", num(L.p_scale))
    + row("fat tails", L.fat_tails ? "yes — big move expected, direction unclear" : "no")
    + row("funding", L.funding != null ? `${fmtRate(L.funding)} per 8h` : "·") + (L.side < 0 && L.liq ? row("liquidation", `${fmtPrice(L.liq)} (${state.controls.short_leverage || "?"}x isolated)`) : "")
    + (L.beta && typeof L.beta === "object" ? row("index loadings", Object.entries(L.beta).map(([k, v]) => `${esc(base(k))} ${num(v)}`).join(" · ")) : "") + (L.hedge != null ? row("index hedge", `${(L.hedge * 100).toFixed(1)}% of the book`) : "")
    + (L.base_action && L.base_action !== L.action ? row("model said", esc(L.base_action)) : "")
    + (L.overlay && L.overlay.length ? row("overlay", esc(L.overlay.join("; "))) : "") + (L.muted ? row("muted", "yes") : "");
  const outs = state.outcomes[sym] || [];
  const hits = outs.filter(o => o.hit === true).length, judged = outs.filter(o => o.hit != null).length;
  const cov = outs.filter(o => o.covered != null), covered = cov.filter(o => o.covered).length;
  const tr = outs.length ? row("matured forecasts", outs.length) + row("direction hit rate", judged ? `${hits}/${judged} (${(hits / judged * 100).toFixed(0)}%)` : "·", judged ? (hits / judged >= 0.5 ? "good" : "bad") : "")
    + row("band coverage", cov.length ? `${covered}/${cov.length} (${(covered / cov.length * 100).toFixed(0)}%)` : "·")
    + row("last error", bps((outs[outs.length - 1].y ?? 0) - (outs[outs.length - 1].q50 ?? 0))) : "";
  const spread = P.bid && P.ask ? `${((P.ask - P.bid) / ((P.ask + P.bid) / 2) * 1e4).toFixed(1)} bps` : "·";
  const lb = bars[bars.length - 1];
  const pf = row("bid / ask", P.bid && P.ask ? `${fmtPrice(P.bid)} / ${fmtPrice(P.ask)}` : "·") + row("spread", spread)
    + row("bars held", `${bars.length} × ${cfg.bar_seconds ? cfg.bar_seconds / 60 : "?"} min`)
    + (lb ? row("last bar", `O ${fmtPrice(lb.o)} H ${fmtPrice(lb.h)} L ${fmtPrice(lb.l)} C ${fmtPrice(lb.c)}`) + row("last volume", Number(lb.v).toLocaleString()) : "");
  const sg = state.signals && state.signals.per_asset && state.signals.per_asset[sym];
  let sig = "";
  if (sg) {
    const f = sg.feat || {};
    sig = row("reddit tone", bar(f.social_sent)) + row("reddit attention", bar(f.social_attn))
      + row("reddit mentions", `${sg.mentions ?? 0}${sg.mention_chg ? ` (${fmtRate(sg.mention_chg, 0)} vs 24h ago)` : ""}`)
      + row("funding", sg.funding_rate != null ? `${fmtRate(sg.funding_rate)} per 8h` : "·")
      + row("open interest", sg.oi != null ? `$${(sg.oi / 1e6).toFixed(0)}M (${fmtRate(sg.oi_chg_raw, 1)} since the last scan)` : "·")
      + row("long/short accounts", sg.longshort_raw != null ? num(sg.longshort_raw) : "·")
      + row("basis (mark vs index)", sg.basis_raw != null ? fmtRate(sg.basis_raw, 3) : "·")
      + row("DVOL", sg.dvol_raw != null ? num(sg.dvol_raw, 0) : "·") + row("fear & greed", bar(f.fng)) + row("breadth", bar(f.breadth))
      + row("BTC dominance", bar(f.btc_dom)) + row("stablecoin flow", bar(f.stable_flow)) + row("crowding", num(L.crowding, 3));
  }
  const nw = state.news && state.news.per_asset && state.news.per_asset[sym];
  const b0 = sym.split("-")[0];
  const heads = ((state.news && state.news.headlines) || []).filter(h => (h.assets || []).some(a => a === sym || a === b0)).sort((x, y) => (y.ts || 0) - (x.ts || 0));
  let news = "";
  if (nw) news += `<dl>${row("mentions", `${nw.mentions ?? 0} scored · ${nw.generic ?? 0} generic`)}${row("tone", bar(nw.sentiment))}${row("attention", bar(nw.attention))}</dl>`;
  if (heads.length) {
    const age = t => { const m = Math.max(0, (Date.now() / 1000 - t) / 60); return m < 60 ? `${m.toFixed(0)}m` : m < 1440 ? `${(m / 60).toFixed(0)}h` : `${(m / 1440).toFixed(0)}d`; };
    news += `<ul class="dnews">${heads.slice(0, 12).map(h => `<li><span class="dtone ${h.sentiment > 0.2 ? "pos" : h.sentiment < -0.2 ? "neg" : ""}">●</span>` +
      (h.url ? `<a href="${esc(h.url)}" target="_blank" rel="noopener">${esc(h.title)}</a>` : esc(h.title)) +
      ` <small>${esc(h.source || "")}${h.ts ? ` · ${age(h.ts)}` : ""}${h.new ? " · new" : ""}</small></li>`).join("")}</ul>`;
  } else if (nw && nw.top && nw.top.length) {
    news += `<ul class="dnews">${nw.top.slice(0, 6).map(h => `<li>${esc(typeof h === "string" ? h : (h.title || h.text || ""))}</li>`).join("")}</ul>`;
  }
  let pos = "";
  const pp = ((state.paper && state.paper.positions) || []).find(x => x.sym === sym || x.symbol === sym);
  if (pp) pos += row("paper", esc(Object.entries(pp).filter(([k]) => !["sym", "symbol"].includes(k)).map(([k, v]) => `${k} ${typeof v === "number" ? num(v) : v}`).join(", ")));
  ((state.portfolio && state.portfolio.accounts) || []).forEach(a => (a.positions || []).filter(x => x.symbol === sym).forEach(x => { pos += row(esc(a.name || a.id || "account"), `${fmtQty(x.qty)} ${esc(x.asset || base(sym))} = ${x.marked ? fmtUSD(x.value) : "unpriced"}`); }));
  const dl = inner => inner ? `<dl>${inner}</dl>` : "";
  $(".dbody", d).innerHTML =
    sect("Forecast detail", dl(fc)) + sect("Track record", dl(tr)) + sect("Price & feed", dl(pf)) +
    sect("Signals", dl(sig)) + sect("Positions", dl(pos)) + sect("News", news, true);
}
document.addEventListener("keydown", e => { if (e.key === "Escape" && detailSym) closeDetail(); });
// any ticker anywhere (Market watch tiles, radar rows, category chips, portfolio and paper rows, brief) opens the detail
document.addEventListener("click", e => {
  if (!e.target.closest) return;
  if (e.target.closest("button, a, input, label, #cards .card, #detail")) return;
  const t = e.target.closest("[data-sym]"); if (t) openDetail(t.dataset.sym);
});
document.addEventListener("DOMContentLoaded", () => {
  const d = $("#detail"); if (!d) return;
  $(".dclose", d).onclick = closeDetail;
  d.addEventListener("click", e => { if (e.target === d) closeDetail(); });
});

function updatePrice(sym) {
  const card = cards[sym]; if (!card) return;
  const p = state.prices[sym] || {};
  const el = $(".price", card.el), prev = lastPrices[sym];
  el.textContent = fmtPrice(p.price);
  if (prev != null && p.price !== prev) { el.classList.toggle("up", p.price > prev); el.classList.toggle("down", p.price < prev); }
  lastPrices[sym] = p.price;
  $(".spread", card.el).textContent = p.bid && p.ask ? `spread ${((p.ask - p.bid) / ((p.ask + p.bid) / 2) * 1e4).toFixed(1)} bps` : "";
}

function updateVia(sym) {
  const card = cards[sym]; if (!card) return;
  const via = $(".via", card.el);
  const pid = state.providers[sym];
  const src = state.sources.find(x => x.id === pid);
  const cls = state.classes[sym] || "";
  if (!pid) { via.className = "via none"; via.innerHTML = `${cls} · <b>no live source</b>`; }
  else { via.className = "via" + (pid === "sim" ? " sim" : ""); via.innerHTML = `${cls} · via <b>${esc(src ? src.name : pid)}</b>`; }
}

function updateCard(sym) {
  if (sym === detailSym) renderDetail();
  const card = cards[sym]; if (!card) return;
  updatePrice(sym); updateVia(sym);
  const L = state.latest[sym];
  if (L) {
    const badge = $(".badge", card.el);
    badge.className = "badge " + L.action.toLowerCase();
    badge.textContent = (L.action === "BUY" ? "▲ " : L.action === "SELL" ? "▼ " : "■ ") + L.action;
    $(".size", card.el).textContent = L.side ? `size ${L.size.toFixed(2)}` : "";
    $(".warm", card.el).hidden = !!L.trusted;
    $(".q50", card.el).textContent = `${fmtBps(L.q[2])} bps`;
    $(".band", card.el).textContent = `${fmtBps(L.q[0])} to ${fmtBps(L.q[4])}`;
    $(".pup", card.el).textContent = L.p_up.toFixed(2);
    $(".pdown", card.el).textContent = (L.p_down != null ? L.p_down.toFixed(2) : "·");
    const up = $(".pmeter i.up", card.el), dn = $(".pmeter i.dn", card.el);
    if (up) up.style.width = `${Math.max(0, Math.min(1, L.p_up)) * 50}%`;
    if (dn) dn.style.width = `${Math.max(0, Math.min(1, L.p_down || 0)) * 50}%`;
    $(".why", card.el).textContent = L.why;
    const strat = $(".strategy", card.el);
    if (L.crowding != null) {
      const crowd = L.crowding, hot = crowd > 0.15, cold = crowd < -0.15;
      const acted = L.base_action && L.base_action !== L.action;
      strat.classList.toggle("acted", !!(acted || (L.overlay && L.overlay.length)));
      let txt = `crowding <b class="crowd-chip">${crowd >= 0 ? "+" : ""}${crowd.toFixed(2)}</b>`;
      if (L.funding != null) txt += ` · funding <b class="crowd-chip">${fmtRate(L.funding, 3)}</b>`;
      if (acted) txt += ` · overlay: ${esc(L.base_action)}→<b>${esc(L.action)}</b>`;
      if (L.overlay && L.overlay.length) txt += ` — ${esc(L.overlay[0])}`;
      strat.innerHTML = txt;
    } else strat.textContent = "";
  }
  const outs = (state.outcomes[sym] || []).slice(-40);
  $(".outcomes", card.el).innerHTML = outs.map(o =>
    `<i class="${o.hit === true ? "hit" : o.hit === false ? "miss" : ""}${o.side ? " acted" : ""}" title="${fmtTime(o.t)}: realized ${fmtBps(o.y)} bps vs median ${fmtBps(o.q50)}${o.pnl != null ? `, paper ${fmtBps(o.pnl)} bps` : ""}"></i>`).join("");
  drawChart(card);
}

function renderTiles() {
  const m = state.metrics || {}, cfg = state.config;
  const f2 = v => v != null ? v.toFixed(2) : "·";
  const tiles = [
    { label: "labels learned", value: m.labels ?? 0, small: m.trusted ? `${m.live_labels} live, trusted${m.trusted_by === "held-out" ? " on held-out history" : ""}` : `${m.live_labels ?? 0} live, need ${cfg.min_labels}${m.holdout_hit != null ? ` · held-out ${Math.round(m.holdout_hit * 100)}% on ${m.holdout_calls} calls` : ""}` },
    { label: "training steps", value: m.steps ?? 0 },
    { label: "train pinball", value: f2(m.pinball), small: "bps, in sample" },
    { label: "live pinball", value: f2(m.live_pinball), small: "bps, out of sample" },
    { label: "held-out pinball", value: f2(m.holdout_pinball), small: "bps, 5% never trained on" },
    { label: "hit rate", value: fmtPct(m.hit_rate), small: m.decisions ? `n=${m.decisions}` : "", cls: m.hit_rate > 0.55 ? "good" : (m.hit_rate < 0.45 && m.decisions > 50) ? "bad" : "" },
    { label: "10-90 coverage", value: fmtPct(m.coverage), small: `raw ${fmtPct(m.coverage_raw)}` },
    { label: "band scale", value: m.band_scale != null ? m.band_scale.toFixed(2) + "x" : "·", small: "widened to hit 80%" },
    { label: "P(up) scaling", value: f2(m.p_scale), small: "1 = as forecast" },
    { label: "paper P&L", value: fmtBps(m.pnl_bps), small: m.suggestions ? `${m.suggestions} calls, ${fmtPct(m.win_rate)} won` : "no calls yet", cls: m.pnl_bps > 0 ? "good" : m.pnl_bps < 0 ? "bad" : "" },
  ];
  $("#tiles").innerHTML = tiles.map(t => `<div class="tile ${t.cls || ""}"><label>${t.label}</label><b class="num">${t.value ?? "·"}</b>${t.small ? `<small>${t.small}</small>` : ""}</div>`).join("");
}

function renderSparks() {
  const h = state.history;
  drawSpark("#spark-loss", h.loss, { color: C.orange, fmt: v => v.toFixed(2) });
  drawSpark("#spark-hit", h.hit, { color: C.aqua, ref: 0.5, fmt: v => (v * 100).toFixed(0) + "%" });
  drawSpark("#spark-coverage", h.coverage, { color: C.blue, ref: 0.8, fmt: v => (v * 100).toFixed(0) + "%" });
}

function renderGate() {
  const g = state.gate || [];
  $("#gate").innerHTML = g.length ? g.map((v, k) =>
    `<div class="g"><span>E${k + 1}</span><div class="bar"><i style="width:${(v * 100).toFixed(1)}%;background:${SERIES[k % SERIES.length]}"></i></div><span>${v.toFixed(2)}</span></div>`).join("")
    : `<span class="why">no forecast yet</span>`;
}

function renderArch() {
  const c = state.config, m = c.model;
  $("#arch").innerHTML = `SteelNet, <b>${m.params.toLocaleString()}</b> parameters<br>` +
    `${c.symbols.length} assets x <b>${c.window}</b> bars of <b>${c.bar_seconds}s</b>, ${c.features.length} features each<br>` +
    `causal conv dilations <b>${m.dilations.join(",")}</b>, receptive field ${m.receptive_field} bars<br>` +
    `cross-asset attention with <b>${m.heads}</b> heads, <b>${m.experts}</b> regime experts<br>` +
    `quantiles <b>${c.quantiles.join("/")}</b> of the <b>${c.horizon}</b>-bar (${c.horizon * c.bar_seconds}s) return<br>` +
    `feed <b>${esc(state.status.feed || "?")}</b>, news skim every <b>${state.controls.news_minutes}</b> min`;
}

// a leading ticker in a log line becomes a click-through to its detail
function linkTicker(text) {
  const m = /^([A-Z][A-Z0-9.\-]{0,7})\b/.exec(text || "");
  const sym = m && state.config && (state.config.symbols || []).find(x => x === m[1] || x.split("-")[0] === m[1]);
  if (!sym) return esc(text);
  return `<span class="tk" data-sym="${esc(sym)}">${esc(m[1])}</span>${esc(text.slice(m[1].length))}`;
}
// compact trade label: "Buy", "Sell", "Open short", "Close short", "Liquidated"; the venue note rides in the title
function tradeLabel(t) {
  const side = t.side || "";
  return side.charAt(0).toUpperCase() + side.slice(1);
}
const tradeUp = side => side === "buy" || side === "close short";
function renderLog() {
  $("#log").innerHTML = state.log.slice().reverse().map(l =>
    `<li class="${esc(l.kind)}"><span class="t">${fmtTime(l.t)}</span><span>${linkTicker(l.text)}</span></li>`).join("")
    || `<li class="empty"><span>no resolved suggestions yet</span></li>`;
}

function renderHolds() {
  const holds = state.holds || [];
  const el = $("#holds-list");
  if (!el) return;
  el.closest(".row").hidden = !holds.length;          // nothing blocked: no panel
  el.innerHTML = holds.slice(0, 8).map(r => `<li><span class="reason">${esc(r.reason)}</span><span class="count">${r.n}</span></li>`).join("");
}

// What Steel is connected to right now: the venues delivering ticks (the simulator does not
// count), the signal providers that have reported and the exchange accounts that answered.
// The header pill carries the count; its title lists them and the UTC day.
function connectedServices() {
  const now = Date.now() / 1000;
  // a venue counts when its stream ticked in the last five minutes (the sources list, refreshed every
  // 30 s) or when it is serving a name right now (the provider map, pushed with every status frame)
  const serving = new Set(Object.values((state.status && state.status.providers) || {}).filter(p => p && p !== "sim"));
  const venues = (state.sources || []).filter(s => s.enabled && s.mechanism !== "simulated" &&
    ((s.ticks > 0 && s.last_tick && now - s.last_tick < 300) || serving.has(s.id)));
  const bad = /fail|error|invalid|stopped|no data|unavailable/i;
  const signals = (state.signal_providers || []).filter(p => p.enabled && p.status && !bad.test(p.status));
  const accounts = ((state.portfolio && state.portfolio.accounts) || []).filter(a => !a.error);
  return { venues, signals, accounts, n: venues.length + signals.length + accounts.length };
}
function marketState() {
  const c = connectedServices();
  const simOnly = (state.sources || []).some(s => s.id === "sim" && s.owned && s.owned.length) && !c.venues.length;
  const phase = (state.status && state.status.phase) || "";
  const label = c.venues.length ? `connected to ${c.n} ${c.n === 1 ? "service" : "services"}`
    : simOnly ? "simulator only" : (phase && phase !== "live") ? `${phase}\u2026 ${c.n ? `${c.n} signal${c.n === 1 ? "" : "s"} up` : ""}`.trim() : "connecting\u2026";
  const cls = c.venues.length ? "live" : simOnly ? "warn" : "closed";
  const parts = [];
  if (c.venues.length) parts.push(`venues: ${c.venues.map(v => v.name).join(", ")}`);
  if (c.signals.length) parts.push(`signals: ${c.signals.map(p => p.name).join(", ")}`);
  if (c.accounts.length) parts.push(`accounts: ${c.accounts.map(a => a.name).join(", ")}`);
  const ms = (state.status || {}).market_status || {};
  parts.push(`24/7 \u00b7 ${ms.weekend ? "weekend (thin tape) \u00b7 " : ""}${ms.day || new Date().toISOString().slice(0, 10)} UTC`);
  return { label, cls, title: parts.join("\n") };
}

// Quotes can keep every price moving while no candle forms; the engine flags that and the
// dashboard must not look healthy when it happens.
function renderStall(s) {
  let el = $("#stall");
  if (!el) {
    el = document.createElement("div"); el.id = "stall"; el.className = "stall"; el.hidden = true;
    document.body.prepend(el);
  }
  el.hidden = !s.stalled;
  if (s.stalled) el.textContent = `No candle for ${Math.round((s.last_bar_age || 0) / 60)} minutes: prices tick but no source is delivering trades, so nothing forecasts or trades.`;
}

function renderStatus() {
  const s = state.status || {};
  renderStall(s);
  const phase = $("#phase");   // the phase pill is gone from the header; the session pill and loading bar carry it
  if (phase) { phase.textContent = s.phase || "?"; phase.className = "pill " + (s.phase === "live" ? "live" : s.phase === "error" ? "err" : "warn"); }
  $("#feed").textContent = "feed: " + (s.feed || "?");
  $("#bars").textContent = s.bar_index ?? 0;
  $("#pending").textContent = s.pending ?? 0;
  $("#learning").classList.toggle("off", s.learning === false);
  const mk = $("#market");
  if (mk) {
    const ms = marketState();
    let fresh = 0;
    for (const v of Object.values(state.prices || {})) if (v && v.ts) fresh = Math.max(fresh, v.ts);
    $("#market-label").textContent = ms.label;       // the border carries the link state: pulsing = connected, red = down
    mk.className = "pill market-nav " + ms.cls + (wsUp ? " up" : " down");
    mk.title = (wsUp ? "dashboard link up; the border pulses with data" : "dashboard link down, reconnecting") + (fresh ? " \u00b7 last trade " + fmtTime(fresh) : "") + "\n" + ms.title;
  }
  updateLoading();
}

function seedSparks() {
  const f = $("#spark-field");
  if (!f || f.childElementCount) return;
  // ember colours are theme tokens (--ember-1..5), so a theme switch recolours the sparks live
  for (let i = 0; i < 30; i++) {
    const e = document.createElement("i");
    e.className = "ember";
    const dur = 1.8 + Math.random() * 2.8, size = 1 + Math.random() * 2.6;
    e.style.left = (Math.random() * 100) + "%";
    e.style.width = e.style.height = size.toFixed(1) + "px";
    e.style.setProperty("--dur", dur.toFixed(2) + "s");
    e.style.setProperty("--delay", (-Math.random() * dur).toFixed(2) + "s");
    e.style.setProperty("--drift", (Math.random() * 60 - 30).toFixed(0) + "px");
    e.style.setProperty("--hue", `var(--ember-${i % 5 + 1})`);
    f.appendChild(e);
  }
}

function updateLoading(offline) {
  const el = $("#loading");
  if (!el) return;
  seedSparks();
  if (demoLoading) {
    el.hidden = false;
    $("#loading-title").textContent = "Heating the forge…";
    $("#loading-sub").textContent = "loading-screen preview — click to dismiss";
    return;
  }
  const phase = offline ? "offline" : ((state.status && state.status.phase) || "starting");
  const busy = phase !== "live" && phase !== "error";
  el.hidden = !busy;
  if (!busy) return;
  const title = $("#loading-title"), sub = $("#loading-sub");
  const feed = state.status && state.status.feed;
  if (phase === "offline") { title.textContent = "Reforging…"; sub.textContent = "reconnecting to Steel"; }
  else if (phase === "starting") { title.textContent = "Forging Steel…"; sub.textContent = "waking up"; }
  else if (phase.indexOf("backfilling") === 0) { title.textContent = "Heating the forge…"; sub.textContent = "backfilling market history" + (feed && feed !== "none" ? ` from ${feed}` : ""); }
  else if (phase === "training") {
    title.textContent = "Hammering it out…";
    const wu = (state.status && state.status.warmup) || null, m = state.metrics || {};
    sub.textContent = wu && wu.total ? `training on history — ${wu.done} of ${wu.total} steps${wu.holdout != null ? ` · held-out ${wu.holdout.toFixed(1)} bps` : ""}` : `training on history — step ${m.steps || 0}`;
  }
  else { title.textContent = "Tempering the model…"; sub.textContent = phase; }
}

// Views are organized by what the operator looks at; each panel belongs to one of them. `vis`
// says whether a panel's view is the one on screen, so renderers skip work for hidden panels.
const VIEW_OF = { market: "market", dislocations: "market", watch: "market", radar: "market", news: "market", brief: "market",
  cards: "names", universe: "names", paper: "book", portfolio: "book", sources: "venues", signals: "venues", keys: "venues",
  backtest: "research", tiles: "research", arbreplay: "research", monitor: "research", controls: "ops", briefctl: "ops", console: "ops", exec: "ops" };
const LEGACY_VIEW = { dashboard: "market", consoles: "venues", paper: "book", portfolio: "book", backtest: "research", console: "ops", brief: "market" };
function vis(part) { return document.body.dataset.view === VIEW_OF[part]; }

function renderAll() {
  if (!state.config) return;
  renderStatus();
  syncControls();
  if (vis("cards")) { state.config.symbols.forEach(updateCard); reorderCards(true); renderUniverse(); }
  if (vis("tiles")) { renderTiles(); renderSparks(); renderGate(); renderArch(); renderLog(); renderBacktest(); }
  if (vis("arbreplay")) loadArbReplay(false);
  if (vis("monitor")) loadMonitor(false);
  if (vis("market")) { renderMarket(); renderDislocations(); renderWatch(); renderRadar(); renderBrief(); }
  renderNews();
  if (vis("sources")) { renderKeys(); renderSources(); renderSignals(); }
  renderVault();
  if (vis("controls")) { renderBriefCtl(); renderExec(); renderLive(); renderArb(); }
  if (vis("paper")) { renderPaper(); renderPortfolio(); }
  renderHolds();
}

// The vault: every key file is sealed under a master key Steel keeps. The recovery copy is shown
// once, here, until the operator says it is saved; after that nothing shows it again.
let vaultShown = false;
async function renderVault() {
  const vt = state.vault, box = $("#vault-box");
  if (box) {
    if (!vt) box.textContent = "";
    else box.innerHTML = `<span class="dot ${vt.available ? "on" : "off"}"></span> vault ${vt.available ? "on" : "unavailable"}: ${vt.sealed.length} key file${vt.sealed.length === 1 ? "" : "s"} sealed` +
      (vt.plaintext.length ? ` · ${vt.plaintext.length} plaintext (sealed at the next start)` : "") + (vt.backup_pending ? ` · <b>recovery key not yet saved</b>` : " · recovery key saved") +
      (vt.error ? ` · <span class="err">${esc(vt.error)}</span>` : "");
  }
  if (!vt || !vt.backup_pending || vaultShown) return;
  vaultShown = true;
  const r = await api("/api/vault/backup", {}); const res = await r.json();
  if (!res.key) return;
  const el = document.createElement("div"); el.className = "onboard"; el.id = "vault-modal";
  el.innerHTML = `<div class="onb-card"><div class="onb-body"><h2>Save Steel's recovery key</h2>` +
    `<p>Steel sealed its key files under a master key it keeps in <code>${esc(res.key_file)}</code>. This is the only copy you will ever see. Store it somewhere safe (a password manager): with it, <code>steel vault restore</code> brings every exchange key back on a new box; without it, a lost state directory means pasting every key again.</p>` +
    `<pre class="vault-key" id="vault-key">${esc(res.key)}</pre>` +
    `<div class="onb-actions"><button class="onb-ghost" id="vault-copy">Copy</button><button class="onb-primary" id="vault-ack">I have saved it</button></div></div></div>`;
  document.body.appendChild(el);
  $("#vault-copy").onclick = async () => { try { await navigator.clipboard.writeText(res.key); $("#vault-copy").textContent = "Copied"; } catch (e) { /* select by hand */ } };
  $("#vault-ack").onclick = async () => { if (!confirm("Once acknowledged, Steel never shows this key again. Saved it?")) return; await api("/api/vault/ack", {}); el.remove(); };
}

function renderLive() {
  const L = state.live, meta = $("#live-meta"), st = $("#live-stats"), ck = $("#live-check"), tb = $("#live-orders");
  if (!meta || !st) return;
  if (!L) { meta.textContent = "paper"; st.innerHTML = ""; return; }
  meta.textContent = L.live ? `LIVE since ${fmtTime(L.since)} · ${L.scale}x paper notional` : (L.demoted ? `paper (demoted: ${L.demoted})` : "paper");
  const c = L.checklist || { rows: [], eligible: false };
  st.innerHTML = [["live equity", fmtUSD(L.equity || 0)], ["peak", fmtUSD(L.peak_equity || 0)], ["fills mirrored", L.fills || 0],
    ["slippage", L.slippage_bps_mean != null ? `${L.slippage_bps_mean > 0 ? "+" : ""}${L.slippage_bps_mean} bps` : "·"],
    ["names expected", Object.keys(L.expected || {}).length], ["worst drift", Object.keys(L.drift || {}).length ? `${Math.round(Math.max(...Object.values(L.drift)) * 100)}%` : "·"]]
    .map(([k, val]) => `<div class="pstat"><label>${esc(k)}</label><b>${esc(String(val))}</b></div>`).join("") +
    `<div class="pstat"><button type="button" class="${L.live ? "danger" : ""}" id="live-toggle" data-action="${L.live ? "demote" : "promote"}">${L.live ? "Demote to paper" : (c.eligible ? "Promote to live" : "Promote (not eligible)")}</button></div>` +
    ((L.live || Object.keys(L.expected || {}).length) ? `<div class="pstat"><button type="button" class="danger" id="live-flatten">Flatten everything</button></div>` : "");
  if (ck) ck.innerHTML = (c.rows || []).map(r => `<span class="live-row ${r.ok ? "ok" : "no"}">${r.ok ? "✓" : "✗"} ${esc(r.name)}: <b>${esc(String(r.value ?? "·"))}</b> <small>${esc(r.need)}</small></span>`).join("");
  if (tb) tb.innerHTML = (L.orders || []).length ? `<div class="rrow head live"><span>when</span><span>venue</span><span>order</span><span>status</span><span>fill</span><span>note</span></div>` +
    L.orders.slice().reverse().map(o => `<div class="rrow live"><span>${fmtTime(o.t)}</span><span>${esc(o.venue)}</span><span>${esc(o.order.side)} ${fmtQty(o.order.qty)} ${esc(base(o.order.symbol))}</span><span class="${o.ok ? "up" : "down"}">${esc(o.status)}</span><span>${o.avg_price ? fmtPrice(o.avg_price) : "·"} <small>paper ${fmtPrice(o.paper_price)}</small></span><span>${esc(o.error || o.order.note || "")}</span></div>`).join("") : "";
}
document.addEventListener("click", async e => {
  const b = e.target.closest && e.target.closest("#live-flatten"); if (!b) return;
  if (!confirm("Flatten everything: cancel every open order, close every live position with reduce-only market orders, and demote to paper. Continue?")) return;
  const r = await api("/api/live", { action: "flatten", reason: "operator" });
  const res = await r.json(); if (res.error) alert(res.error); if (res.live_status) { state.live = res.live_status; renderLive(); }
});
document.addEventListener("click", async e => {
  const b = e.target.closest && e.target.closest("#live-toggle"); if (!b) return;
  const act = b.dataset.action;
  const eligible = state.live && state.live.checklist && state.live.checklist.eligible;
  let force = false;
  if (act === "promote" && !eligible) { if (!confirm("The checklist is not met. Promote anyway (force)? Real orders will be sent to the armed venues.")) return; force = true; }
  else if (act === "promote" && !confirm("Go live: every paper fill will be mirrored to the armed venues at the configured scale. Continue?")) return;
  const r = await api("/api/live", { action: act, force });
  const res = await r.json(); if (res.error) alert(res.error); if (res.live_status) { state.live = res.live_status; renderLive(); }
});

function renderArb() {
  const A = state.arb, meta = $("#arb-meta"), st = $("#arb-stats"), tb = $("#arb-recent");
  if (!meta || !st) return;
  if (!A) { meta.textContent = "off"; st.innerHTML = ""; return; }
  const s = A.stats || {};
  meta.textContent = A.enabled ? `ON · min edge ${A.min_edge_bps} bps · ${fmtUSD(A.max_order_usd)} a leg` : (A.switch ? "switched on, but execution is off" : "off");
  st.innerHTML = [["considered", s.considered || 0], ["pairs filled", s.filled_pairs || 0], ["mean edge", s.mean_edge_bps != null ? `${s.mean_edge_bps} bps` : "·"], ["refused", s.refused || 0],
    ["unhedged", s.unhedged || 0], ["open pairs", (A.open_pairs || []).length], ["closed pairs", s.closed_pairs || 0]]
    .map(([k, val]) => `<div class="pstat"><label>${esc(k)}</label><b>${esc(String(val))}</b></div>`).join("") +
    `<div class="pstat"><button type="button" class="${A.switch ? "danger" : ""}" id="arb-toggle" data-action="${A.switch ? "off" : "on"}">${A.switch ? "Switch off" : "Switch on"}</button></div>`;
  if (tb) tb.innerHTML = (A.recent || []).length ? `<div class="rrow head arb"><span>when</span><span>name</span><span>event</span><span>buy → sell</span><span>edge</span><span>note</span></div>` +
    A.recent.slice().reverse().map(r => `<div class="rrow arb"><span>${fmtTime(r.t)}</span><span class="sym">${esc(base(r.sym || ""))}</span><span>${esc(r.event)}</span><span>${esc(r.buy || "")} → ${esc(r.sell || "")}</span><span>${r.edge_bps != null ? `+${Number(r.edge_bps).toFixed(1)} bps` : "·"}</span><span>${esc(r.why || (r.legs ? r.legs.map(l => `${l.venue} ${l.status}${l.error ? " " + l.error : ""}`).join(" · ") : ""))}</span></div>`).join("") : "";
}
document.addEventListener("click", async e => {
  const b = e.target.closest && e.target.closest("#arb-toggle"); if (!b) return;
  if (b.dataset.action === "on" && !confirm("Switch the arbitrage executor on: both legs of every qualifying dislocation will be sent to the armed venues through the gate. Continue?")) return;
  const r = await api("/api/arb", { action: b.dataset.action });
  const res = await r.json(); if (res.arb) { state.arb = res.arb; renderArb(); }
});

function gpuLine() {
  const h = state.status && state.status.handoff; if (!h || !h.enabled) return "·";
  const next = h.next_start ? ` · peer at ${esc(h.next_start.slice(5, 16).replace("T", " "))}` : "";
  return `${esc(h.mode)}${h.pinned !== "auto" ? " (pinned)" : ""} <small>${esc(h.why || "")}${next} · epochs ${h.train_epochs} · floor ${h.memory_floor_gb} GB</small> ` +
    `<button type="button" class="mini" data-handoff="${h.pinned === "auto" ? (h.mode === "shared" ? "solo" : "shared") : "auto"}">${h.pinned === "auto" ? (h.mode === "shared" ? "pin solo" : "pin shared") : "auto"}</button>`;
}
document.addEventListener("click", async e => {
  const b = e.target.closest && e.target.closest("button[data-handoff]"); if (!b) return;
  const r = await api("/api/handoff", { mode: b.dataset.handoff }); const res = await r.json();
  if (res.handoff && state.status) { state.status.handoff = res.handoff; renderExec(); }
});
function renderExec() {
  const meta = $("#exec-meta"), st = $("#exec-stats"); if (!meta || !st) return;
  const e = state.execution;
  if (!e) { meta.textContent = "off"; st.innerHTML = `<div class="why">execution is off: STEEL_EXECUTION=1 and an armed venue with its own trade key turn it on; every cap must be set</div>`; return; }
  meta.textContent = e.killed ? `KILLED (${e.kill_reason})` : e.enabled ? `on · armed: ${(e.armed || []).join(", ") || "none"}` : "off";
  const c = e.caps || {};
  const ck = e.clock || {};
  const clock = ck.offset_s == null ? "unchecked" : `${ck.offset_s > 0 ? "+" : ""}${ck.offset_s.toFixed(2)} s${ck.ok ? "" : " OFF"}`;
  st.innerHTML = [["per order", c.order_usd], ["per name / day", c.name_usd], ["per venue / day", c.venue_usd], ["per day", c.day_usd], ["spent today", e.spent_today],
    ["exposure", e.exposure_usd], ["max exposure", c.exposure_frac != null ? `${Math.round(c.exposure_frac * 100)}% eq` : "·"], ["max leverage", c.leverage ? `${c.leverage}x` : "·"], ["box clock", clock],
    ["GPU", { html: gpuLine() }]]
    .map(([k, val]) => `<div class="pstat"><label>${esc(k)}</label><b>${val && val.html != null ? val.html : typeof val === "number" ? fmtUSD(val) : esc(String(val ?? "·"))}</b></div>`).join("") +
    `<div class="pstat"><button type="button" class="${e.killed ? "" : "danger"}" data-action="${e.killed ? "revive" : "kill"}" id="exec-kill">${e.killed ? "Lift kill switch" : "KILL SWITCH"}</button></div>`;
  const vs = Object.entries(e.venues || {});
  if (vs.length) st.innerHTML += `<div class="exec-venues">` + vs.map(([id, x]) => {
    const armed = (e.armed || []).includes(id);
    return `<span class="exec-venue${armed ? " armed" : ""}">${esc(id)} <small>${esc(x.venue)}${x.dry_run ? " · dry-run" : ""}${x.testnet ? " · testnet" : ""}${x.configured ? "" : " · no trade key"}</small>` +
      `<button type="button" class="mini" data-exec-arm="${esc(id)}" data-on="${armed ? "0" : "1"}">${armed ? "disarm" : "arm"}</button></span>`;
  }).join("") + `</div>`;
}
document.addEventListener("click", async e => {
  const b = e.target.closest && e.target.closest("button[data-exec-arm]"); if (!b) return;
  const r = await api("/api/execution", { action: b.dataset.on === "1" ? "arm" : "disarm", venue: b.dataset.execArm });
  const res = await r.json(); if (res.execution) { state.execution = res.execution; renderExec(); }
});

function scheduleDraw() {
  if (drawQueued || !vis("cards")) return;
  drawQueued = true;
  requestAnimationFrame(() => { drawQueued = false; Object.values(cards).forEach(drawChart); });
}

function redrawCharts() {
  if (vis("cards")) Object.values(cards).forEach(drawChart);
}

// Consoles + controls ----------------------------------------------------------------

function buildConsoles() {
  const grid = $("#console-grid"); grid.innerHTML = "";
  for (const k of Object.keys(consoles)) delete consoles[k];
  Object.entries(CHANNELS).forEach(([ch, desc]) => {
    const el = document.createElement("div");
    el.className = "console";                      // one height for every card: the grid reads as a grid
    el.innerHTML = `<header><span class="name">${ch}</span><span class="desc">${desc}</span><span class="rate">0/min</span>
      <button type="button" class="pause">pause</button><button type="button" class="clear">clear</button></header><div class="body"></div>`;
    grid.appendChild(el);
    const c = consoles[ch] = { el, body: $(".body", el), paused: false, times: [], rate: $(".rate", el) };
    $(".pause", el).onclick = e => {
      c.paused = !c.paused; el.classList.toggle("paused", c.paused);
      e.target.textContent = c.paused ? "resume" : "pause";
      if (!c.paused) c.body.scrollTop = c.body.scrollHeight;
    };
    $(".clear", el).onclick = () => { c.body.innerHTML = ""; };
  });
}

function appendTrace(ev, scroll = true) {
  const c = consoles[ev.ch]; if (!c) return;
  const atBottom = c.body.scrollHeight - c.body.scrollTop - c.body.clientHeight < 40;
  const line = document.createElement("div");
  line.className = "line " + (ev.lvl || "info");
  line.innerHTML = `<span class="t">${fmtTime(ev.t)}</span><span class="msg">${esc(ev.text)}</span>`;
  c.body.appendChild(line);
  while (c.body.childElementCount > 400) c.body.removeChild(c.body.firstChild);
  if (scroll) c.times.push(Date.now() / 1000);
  if (scroll && !c.paused && atBottom) c.body.scrollTop = c.body.scrollHeight;
}

// ---- unified console: a terminal-style firehose of every trace channel ----
const termFilters = new Set(Object.keys(CHANNELS));
let termFollow = true;
function appendTerm(ev) {
  const term = $("#term"); if (!term) return;
  const atBottom = term.scrollHeight - term.scrollTop - term.clientHeight < 60;
  const line = document.createElement("div");
  line.className = "tline " + (ev.lvl || "info");
  line.dataset.ch = ev.ch;
  line.innerHTML = `<span class="tt">${fmtTime(ev.t)}</span><span class="tch ch-${ev.ch}">${esc(ev.ch)}</span><span class="tmsg">${esc(ev.text)}</span>`;
  if (!termFilters.has(ev.ch)) line.style.display = "none";
  term.appendChild(line);
  while (term.childElementCount > 2500) term.removeChild(term.firstChild);
  if (termFollow && atBottom) term.scrollTop = term.scrollHeight;
}
function setTermFollow(on) {
  termFollow = on;
  const fb = $("#term-follow"); if (fb) { fb.classList.toggle("on", on); fb.textContent = on ? "following" : "paused"; }
  if (on) { const t = $("#term"); if (t) t.scrollTop = t.scrollHeight; }
}
(function initTerm() {
  const box = $("#term-filters"); if (!box) return;
  box.innerHTML = Object.keys(CHANNELS).map(ch =>
    `<button class="tchip ch-${ch} on" data-ch="${ch}" title="${esc(CHANNELS[ch])}">${ch}</button>`).join("");
  box.addEventListener("click", e => {
    const b = e.target.closest(".tchip"); if (!b) return;
    const ch = b.dataset.ch;
    termFilters.has(ch) ? termFilters.delete(ch) : termFilters.add(ch);
    b.classList.toggle("on", termFilters.has(ch));
    $$("#term .tline").forEach(l => { if (l.dataset.ch === ch) l.style.display = termFilters.has(ch) ? "" : "none"; });
  });
  $("#term-follow").addEventListener("click", () => setTermFollow(!termFollow));
  $("#term-clear").addEventListener("click", () => { $("#term").innerHTML = ""; });
  $("#term").addEventListener("scroll", () => {
    const t = $("#term"); const atBottom = t.scrollHeight - t.scrollTop - t.clientHeight < 60;
    if (atBottom !== termFollow) setTermFollow(atBottom);
  });
})();

function buildControls() {
  const form = $("#controls");
  const val = v => v == null ? "" : typeof v === "boolean" ? (v ? 1 : 0) : v;   // booleans show as 1/0 in a number input
  form.innerHTML = CONTROL_FIELDS.map(([k, label, step]) =>
    `<label>${label}<input name="${k}" type="number" step="${step}" value="${val(state.controls[k])}"></label>`).join("") +
    `<button type="submit">Apply</button>`;
  form.onsubmit = async e => {
    e.preventDefault();
    const set = {};
    CONTROL_FIELDS.forEach(([k]) => { const v = form.elements[k].value; if (v !== "" && Number(v) !== Number(state.controls[k])) set[k] = Number(v); });   // booleans compare as 1/0
    if (Object.keys(set).length) await control({ set });
  };
}

function syncControls() {
  const form = $("#controls");
  if (!form.elements.length) return;
  CONTROL_FIELDS.forEach(([k]) => { const inp = form.elements[k]; if (inp && document.activeElement !== inp) { const v = state.controls[k]; inp.value = v == null ? "" : typeof v === "boolean" ? (v ? 1 : 0) : v; } });
  const b = $("#btn-pause");
  b.textContent = state.controls.learning === false ? "Resume learning" : "Pause learning";
  b.dataset.action = state.controls.learning === false ? "resume" : "pause";
  $("#learning").classList.toggle("off", state.controls.learning === false);
}

// Every change goes through api(): it carries the operator token (asked for once, kept in this
// browser) and asks again when the server answers 401.
function opToken() { try { return localStorage.getItem("steel.token") || ""; } catch (e) { return ""; } }
async function api(path, body, method) {
  for (let attempt = 0; attempt < 2; attempt++) {
    const headers = { "Content-Type": "application/json" };
    const tok = opToken(); if (tok) headers["Authorization"] = "Bearer " + tok;
    const r = await fetch(path, { method: method || "POST", headers, body: body === undefined ? undefined : JSON.stringify(body) });
    if (r.status !== 401) return r;
    const t = prompt("Operator token (run `steel token` on the box):", "");
    if (!t) return r;
    try { localStorage.setItem("steel.token", t.trim()); } catch (e) { /* storage may be unavailable */ }
  }
  return fetch(path, { method: method || "POST" });
}
async function control(payload) {
  const r = await api("/api/control", payload);
  const res = await r.json();
  if (res.controls) { state.controls = res.controls; syncControls(); }
  if (res.metrics) { state.metrics = res.metrics; if (vis("tiles")) renderTiles(); }
  return res;
}

// --- inject input: a compose box that pipes a human note into the model ---
const injectFab = $("#inject-fab"), injectPop = $("#inject-pop"), injectText = $("#inject-text"), injectAck = $("#inject-ack");
function injectToggle(show) {
  injectPop.hidden = show === undefined ? !injectPop.hidden : !show;
  if (!injectPop.hidden) { injectText.focus(); }
}
async function injectSend() {
  const text = injectText.value.trim();
  if (!text) return;
  const res = await control({ action: "inject", text });
  if (res && res.applied && res.applied.length) {
    injectAck.textContent = `nudged ${res.applied.join(", ")} (${res.sentiment >= 0 ? "+" : ""}${res.sentiment})`;
  } else {
    injectAck.textContent = "logged to the operator console";
  }
  injectText.value = "";
  clearTimeout(injectSend._t); injectSend._t = setTimeout(() => { injectAck.textContent = ""; injectToggle(false); }, 2200);
}
if (injectFab) {
  injectFab.onclick = () => injectToggle();
  $("#inject-send").onclick = injectSend;
  injectText.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); injectSend(); }
    else if (e.key === "Escape") { injectToggle(false); }
  });
  document.addEventListener("keydown", e => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "i") { e.preventDefault(); injectToggle(); }
  });
  document.addEventListener("click", e => {
    if (!injectPop.hidden && !injectPop.contains(e.target) && e.target !== injectFab) injectToggle(false);
  });
}

$$(".actions button").forEach(b => b.onclick = () => {
  const a = b.dataset.action;
  if (a === "reset" && !confirm("Re-initialize the model weights and optimizer? The replay buffer is kept.")) return;
  control({ action: a });
});

function sourceToggle(kind, id, on, disabled, reason) {
  const wrap = document.createElement("label");
  wrap.className = "switch" + (disabled ? " disabled" : "");
  if (reason) wrap.title = reason;
  wrap.innerHTML = `<input type="checkbox" ${on ? "checked" : ""} ${disabled ? "disabled" : ""}><span class="track"></span><span class="thumb"></span>`;
  wrap.querySelector("input").onchange = e => {
    control({ action: kind === "news" ? "toggle_news" : "toggle_source", id, on: e.target.checked });
  };
  return wrap;
}

function applyMuted() {
  if (!state.config) return;
  const muted = new Set(state.muted || []);
  state.config.symbols.forEach(sym => { if (cards[sym]) cards[sym].el.classList.toggle("muted", muted.has(sym)); });
  lastOrder = ""; reorderCards(true);
}

function renderUniverse() {
  const box = $("#uni-list");
  if (!box || !state.config) return;
  const active = new Set(state.config.symbols || []);
  const universe = (state.universe && state.universe.length) ? state.universe : state.config.symbols;
  box.innerHTML = universe.map(sym => {
    const cls = (state.classes || {})[sym] || "";
    const on = active.has(sym);
    return `<div class="uni ${on ? "" : "muted"}"><span><span class="un tk" data-sym="${esc(sym)}">${esc(base(sym))}</span> <span class="uc">${esc(cls)}</span></span>` +
      `<span class="right"><label class="switch"><input type="checkbox" data-mute="${esc(sym)}" ${on ? "checked" : ""}><span class="track"></span><span class="thumb"></span></label>` +
      `<button type="button" class="rm" data-remove="${esc(sym)}" title="remove from universe">×</button></span></div>`;
  }).join("");
  const meta = $("#uni-meta");
  if (meta) meta.textContent = `${(state.config.symbols || []).length} active of ${universe.length} (cap ${state.config.max_universe || 64}) — mute to pause, remove to drop`;
  // class-level disable/enable
  const ct = $("#class-toggles");
  if (ct) {
    const universe2 = (state.universe && state.universe.length) ? state.universe : state.config.symbols;
    const classes = [...new Set(universe2.map(s => (state.classes || {})[s]).filter(Boolean))];
    ct.innerHTML = classes.map(cls => {
      const syms = universe2.filter(s => (state.classes || {})[s] === cls);
      const allMuted = syms.every(s => !active.has(s));
      return `<button type="button" class="${allMuted ? "" : "danger"}" data-class="${esc(cls)}" data-on="${allMuted ? "0" : "1"}">${allMuted ? "Enable" : "Disable"} ${esc(cls)}</button>`;
    }).join("");
  }
}

// ---- first-launch setup walkthrough (every key is optional) ----
const ONBOARD_KEY = "steel.onboarded";
let onbStep = 0;
let onbVenue = null;   // the venue picked on the keys step
function onboardSteps() {
  return [{ type: "intro" }, { type: "key" }, { type: "brief" }, { type: "done" }];
}
function onboardStart(force) {
  if (!force) { try { if (localStorage.getItem(ONBOARD_KEY)) return; } catch (e) { /* storage may be unavailable */ } }
  if (!state.keys || !state.keys.length) return;      // wait until the service list is known
  onbStep = 0; const el = $("#onboard"); if (el) { el.hidden = false; renderOnboard(); }
}
function onboardClose() {
  try { localStorage.setItem(ONBOARD_KEY, "1"); } catch (e) { /* storage may be unavailable */ }
  const el = $("#onboard"); if (el) el.hidden = true;
}
function onbNext() { onbStep++; if (onbStep >= onboardSteps().length) onboardClose(); else renderOnboard(); }
function onbBack() { if (onbStep > 0) { onbStep--; renderOnboard(); } }
function renderOnboard() {
  const steps = onboardSteps();
  onbStep = Math.max(0, Math.min(onbStep, steps.length - 1));
  const step = steps[onbStep];
  $("#onb-progress").innerHTML = steps.map((_, i) => `<span class="onb-dot${i === onbStep ? " on" : i < onbStep ? " done" : ""}"></span>`).join("");
  const body = $("#onboard-body");
  if (step.type === "intro") {
    body.innerHTML = `<h2>Welcome to Steel</h2>` +
      `<p>Steel runs out of the box on free, no-key market data (Coinbase and Kraken on, ${Math.max(0, (state.sources || []).length - 3)} more venues a toggle away). The only keys it can use are <b>read-only</b> exchange keys that show your balances on the Portfolio tab, and a CoinGecko demo key for a higher rate limit \u2014 <b>every key is optional</b>, and you can add them anytime on the Venues tab.</p>` +
      `<p class="onb-sub">Add one now if you like, or skip.</p>` +
      `<div class="onb-actions"><button class="onb-ghost" id="onb-skipall">Skip setup</button><button class="onb-primary" id="onb-go">Get started</button></div>`;
  } else if (step.type === "key") {
    const keys = (state.keys || []).slice().sort((a, b) => (a.venue === "data") - (b.venue === "data") || a.name.localeCompare(b.name));
    const k = keys.find(x => x.id === onbVenue) || keys.find(x => x.id === "coinbase") || keys[0];
    if (!k) { onbNext(); return; }
    onbVenue = k.id;
    const fields = k.fields.map(f =>
      `<div class="onb-field"><label>${esc(f.label)}${f.present ? ` <span class="cur">set: ${esc(f.masked)}</span>` : ""}</label>` +
      keyField(k, f, "", f.present ? "replace\u2026" : "paste key\u2026") + `</div>`).join("");
    const picker = `<select id="onb-venue">${keys.map(x => `<option value="${esc(x.id)}"${x.id === k.id ? " selected" : ""}>${esc(x.name)}${x.present ? " \u2713" : ""}</option>`).join("")}</select>`;
    body.innerHTML = `<div class="onb-step">read-only keys \u00b7 ${keys.length} venues</div>` +
      `<h2>${picker} ${k.present ? '<span class="onb-set">\u2713 set</span>' : '<span class="onb-opt">optional</span>'}</h2>` +
      `<p>${esc(k.note || "")}</p>` +
      `<a class="onb-link" href="${esc(safeUrl(k.url))}" target="_blank" rel="noopener noreferrer">Get a ${esc(k.name)} key \u2197</a>` +
      `<div class="onb-fields">${fields}</div>` +
      `<div class="onb-actions"><button class="onb-ghost" id="onb-back">Back</button><button class="onb-ghost" id="onb-skip">Skip</button><button class="onb-primary" id="onb-save">Save &amp; add another</button></div>`;
    const sel = $("#onb-venue"); if (sel) sel.onchange = () => { onbVenue = sel.value; renderOnboard(); };
  } else if (step.type === "brief") {
    body.innerHTML = `<h2>Narrative brief <span class="onb-opt">local \u00b7 optional</span></h2>` +
      `<p>Steel can write a plain-English market brief with a local LLM through <b>Ollama</b> \u2014 it runs entirely on your machine, no key and no cloud. If Ollama is installed and running, the brief works automatically.</p>` +
      `<a class="onb-link" href="https://ollama.com/download" target="_blank" rel="noopener noreferrer">Install Ollama \u2197</a>` +
      `<p class="onb-sub">You can switch the brief on and off anytime on the Ops tab.</p>` +
      `<div class="onb-actions"><button class="onb-ghost" id="onb-back">Back</button><button class="onb-primary" id="onb-go">Continue</button></div>`;
  } else {
    body.innerHTML = `<h2>You're all set</h2>` +
      `<p>Steel is already running on free sources and learning from live data. Add or change keys anytime from <b>Venues \u2192 API keys</b>.</p>` +
      `<div class="onb-actions"><button class="onb-primary" id="onb-go">Enter Steel</button></div>`;
  }
  const on = (id, fn) => { const b = $("#" + id); if (b) b.onclick = fn; };
  on("onb-go", onbNext); on("onb-skip", onbNext); on("onb-back", onbBack); on("onb-skipall", onboardClose);
  on("onb-save", async () => {
    const svc = onbVenue;
    if (svc && $$(`#onboard input[data-svc="${svc}"], #onboard textarea[data-svc="${svc}"]`).some(i => i.value.trim())) { await saveKey(svc); renderOnboard(); }
    else onbNext();
  });
}

// a key field: a one-line masked input, or a textarea for a multi-line value (a PEM private key)
function keyField(k, f, cls, placeholder) {
  const common = `data-svc="${esc(k.id)}" data-field="${esc(f.id)}" placeholder="${placeholder}" autocomplete="off" autocapitalize="off" spellcheck="false"`;
  return (f.multiline || f.id === "privateKey")
    ? `<textarea class="${cls} kpem" rows="4" ${common}></textarea>`
    : `<input type="text" class="${cls}" ${common}>`;
}
const VENUE_WORD = { spot: "spot", perp: "perps", mixed: "spot + perps", data: "data", trade: "TRADE key" };
function keyRow(k) {
  const fields = k.fields.map(f =>
    `<div class="kfield${(f.multiline || f.id === "privateKey") ? " wide" : ""}"><label>${esc(f.label)}${f.present ? ` <span class="cur">set: ${esc(f.masked)}</span>` : ""}</label>` +
    keyField(k, f, "kmask", f.present ? "replace…" : "paste…") + `</div>`).join("");
  return `<div class="keyrow"><div class="kh"><span class="dot ${k.present ? "on" : "off"}"></span>` +
    `<span class="kname">${esc(k.name)}</span>${k.venue ? `<span class="tag">${esc(VENUE_WORD[k.venue] || k.venue)}</span>` : ""}` +
    `<a class="klink" href="${esc(safeUrl(k.url))}" target="_blank" rel="noopener noreferrer">${k.present ? "manage ↗" : "get a key ↗"}</a></div>` +
    `<div class="knote">${esc(k.note || "")}</div>` +
    `<div class="kfields">${fields}<button type="button" class="ksave" data-svc="${esc(k.id)}">Save</button></div></div>`;
}
// the venues with a key saved (and the data key) stay open; the rest fold under "more venues", by name
let keysOpen = null;   // set of ids the operator opened from the folded list this session
function renderKeys() {
  const box = $("#key-list");
  if (!box) return;
  const keys = state.keys || [];
  if (!keysOpen) keysOpen = new Set();
  const shown = keys.filter(k => k.present || k.venue === "data" || keysOpen.has(k.id));
  const folded = keys.filter(k => !shown.includes(k) && k.venue !== "trade").sort((a, b) => a.name.localeCompare(b.name));
  const trade = keys.filter(k => !shown.includes(k) && k.venue === "trade").sort((a, b) => a.name.localeCompare(b.name));
  box.innerHTML = shown.map(keyRow).join("") +
    (folded.length ? `<div class="kmore"><span class="kmore-label">add a venue</span>` +
      folded.map(k => `<button type="button" class="kpick" data-open-key="${esc(k.id)}" title="${esc(k.note || "")}">${esc(k.name)}</button>`).join("") + `</div>` : "") +
    (trade.length ? `<div class="kmore"><span class="kmore-label">trade keys (execution only)</span>` +
      trade.map(k => `<button type="button" class="kpick" data-open-key="${esc(k.id)}" title="${esc(k.note || "")}">${esc(k.name.replace(" trade key", ""))}</button>`).join("") + `</div>` : "");
}

async function saveKey(service) {
  const values = {};
  const scope = ($("#onboard") && !$("#onboard").hidden) ? "#onboard " : "";
  $$(`${scope}input[data-svc="${service}"], ${scope}textarea[data-svc="${service}"]`).forEach(i => {
    if (!i.value.trim()) return;
    values[i.dataset.field] = i.tagName === "TEXTAREA" ? i.value : i.value.trim();   // a PEM goes as-is, newlines and all
  });
  if (!Object.keys(values).length) return;
  const r = await api("/api/keys", { service, values });
  const res = await r.json();
  if (res.keys) { state.keys = res.keys; renderKeys(); }
}

document.addEventListener("click", e => {
  const b = e.target.closest && e.target.closest("button.ksave");
  if (b) saveKey(b.dataset.svc);
  const pk = e.target.closest && e.target.closest("button[data-open-key]");
  if (pk) { if (!keysOpen) keysOpen = new Set(); keysOpen.add(pk.dataset.openKey); renderKeys(); const f = $(`#key-list [data-svc="${pk.dataset.openKey}"]`); if (f) f.focus(); }
  const cb = e.target.closest && e.target.closest("button[data-class]");
  if (cb) control({ action: "mute_class", class: cb.dataset.class, on: cb.dataset.on === "1" });
  const rm = e.target.closest && e.target.closest("button[data-remove]");
  if (rm) control({ action: "remove_symbols", symbols: [rm.dataset.remove] });
  if (e.target.closest && e.target.closest("button[data-action=add_symbol]")) {
    const inp = $("#uni-add-input"); const v = (inp.value || "").trim().toUpperCase();
    if (v) { control({ action: "add_symbols", symbols: v.split(/[ ,]+/).filter(Boolean) }); inp.value = ""; }
  }
  if (e.target.closest && e.target.closest("button[data-action=add_movers]")) control({ action: "add_movers", n: 20 });
  if (e.target.closest && e.target.closest("button[data-action=refresh_universe]")) control({ action: "refresh_universe" });
});
document.addEventListener("change", e => {
  if (e.target && e.target.dataset && e.target.dataset.mute)
    control({ action: "mute", symbols: [e.target.dataset.mute], on: !e.target.checked });
});

function renderSources() {
  const list = $("#source-list");
  if (!list) return;
  list.innerHTML = "";
  const on = state.sources.filter(s => s.enabled), off = state.sources.filter(s => !s.enabled).sort((a, b) => a.name.localeCompare(b.name));
  const sm = $("#sources-meta"); if (sm) sm.textContent = `${on.length} on of ${state.sources.length} venues`;
  on.concat(off).forEach((src, i) => {
    if (i === on.length && off.length) { const h = document.createElement("div"); h.className = "source-fold"; h.textContent = `${off.length} more ${off.length === 1 ? "venue" : "venues"}, off — toggle any`; list.appendChild(h); }
    const owning = src.owned && src.owned.length;
    const el = document.createElement("div");
    el.className = "source" + (src.enabled ? "" : " off") + (owning ? " owning" : "");
    const isSim = src.id === "sim";
    const capList = (a, n) => a.slice(0, n).map(base).join(", ") + (a.length > n ? ` +${a.length - n}` : "");
    const ownsText = owning ? `serving ${capList(src.owned, 8)}`
      : src.enabled ? (src.supported && src.supported.length ? "standby / backup" : "idle — no matching symbols") : "off";
    const noteErr = /fail|stopped|error|invalid|no API/i.test(src.note || "");
    const venue = src.venue || "spot";
    el.innerHTML = `<div></div><div>
        <div class="sname">${src.url ? `<a href="${esc(safeUrl(src.url))}" target="_blank" rel="noopener noreferrer">${esc(src.name)}</a>` : esc(src.name)}
          <span class="tag ${esc(venue)}" title="${venue === "perp" ? "a perpetual-futures venue: funding keeps its price near spot, but it is a derivative, so it ranks below the spot venues" : venue === "aggregate" ? "an index across venues: quotes only, never a candle" : ""}">${esc(venue)}</span>
          <span class="tag ${esc(src.mechanism)}">${esc(src.mechanism)}</span>
          <span class="tag" title="priority (lower wins)">P${src.priority}</span></div>
        <div class="meta">supports ${src.supported && src.supported.length ? capList(src.supported, 8) : "none of the active symbols"} · ${src.ticks.toLocaleString()} ticks</div>
        <div class="owns ${owning ? "" : "backup"}">${esc(ownsText)}</div>
        ${src.note ? `<div class="snote ${noteErr ? "err" : ""}">${esc(src.note)}</div>` : ""}
      </div>`;
    const disabled = isSim || (!src.enabled && (!src.supported || !src.supported.length));
    el.firstChild.replaceWith(sourceToggle("source", src.id, src.enabled, disabled,
      isSim ? "the simulator is the fallback provider" : (!src.supported || !src.supported.length) ? "no active symbols in this asset class" : ""));
    list.appendChild(el);
  });
  const nl = $("#news-source-list");
  if (nl) {
    nl.innerHTML = "";
    state.news_sources.forEach(src => {
      const el = document.createElement("div");
      el.className = "source" + (src.enabled ? "" : " off");
      el.innerHTML = `<div></div><div><div class="sname">${esc(src.name)}</div></div>`;
      el.firstChild.replaceWith(sourceToggle("news", src.id, src.enabled, false, ""));
      nl.appendChild(el);
    });
  }
}

const urgencyEMA = {};
function realizedVol(sym) {
  const b = (state.bars[sym] || []).slice(-30);
  if (b.length < 4) return 0;
  const r = [];
  for (let i = 1; i < b.length; i++) r.push(Math.log(b[i].c / b[i - 1].c) * 1e4);
  const mean = r.reduce((a, x) => a + x, 0) / r.length;
  return Math.sqrt(r.reduce((a, x) => a + (x - mean) ** 2, 0) / r.length);  // bps
}
function rawUrgency(sym) {
  const L = state.latest[sym] || {};
  const pa = state.signals && state.signals.per_asset && state.signals.per_asset[sym];
  const f = (pa && pa.feat) || {};
  const traded = (L.side && L.trusted) ? 1 : 0;
  // Primary signal is volatility — the model's expected 10-90 band plus recent realized vol,
  // both slow-moving — so the ranking reflects what is actually active, not tick noise.
  const band = L.q ? (L.q[4] - L.q[0]) : 0;
  const vol = Math.min(1, band / 50) * 0.6 + Math.min(1, realizedVol(sym) / 30) * 0.4;
  return 2.0 * vol + 1.2 * traded + 0.5 * Math.abs(L.crowding || 0) +
    0.35 * (f.social_attn || 0) + 0.3 * Math.min(1, Math.abs(L.funding || 0) * 1000);
}
function cardUrgency(sym) {
  const r = rawUrgency(sym);
  urgencyEMA[sym] = urgencyEMA[sym] == null ? r : 0.9 * urgencyEMA[sym] + 0.1 * r;
  return urgencyEMA[sym];
}

let lastOrder = "", lastReorder = 0;
function reorderCards(force) {
  if (!state.config || !vis("cards")) return;
  const now = Date.now();
  if (!force && now - lastReorder < 1500) return;
  lastReorder = now;
  const HYST = 0.15;
  const u = {};
  state.config.symbols.forEach(s => { if (cards[s]) u[s] = cardUrgency(s); });
  // Start from the current on-screen order and only let a card overtake its neighbour
  // when it clearly leads (hysteresis) — so near-ties don't shuffle every bar.
  const mutedSet = new Set(state.muted || []);
  let order = [...document.querySelectorAll("#cards .card")].map(c => c.dataset.sym).filter(x => cards[x] && !mutedSet.has(x));
  state.config.symbols.forEach(s => { if (cards[s] && !mutedSet.has(s) && !order.includes(s)) order.push(s); });
  for (let pass = 0, changed = true; changed && pass < 40; pass++) {
    changed = false;
    for (let i = 0; i < order.length - 1; i++) {
      if (u[order[i + 1]] > u[order[i]] + HYST) { const t = order[i]; order[i] = order[i + 1]; order[i + 1] = t; changed = true; }
    }
  }
  const key = order.join(",");
  if (key === lastOrder) return;
  const firstRun = lastOrder === "";
  lastOrder = key;
  const root = $("#cards");
  const syms = state.config.symbols.filter(s => cards[s]);
  // FLIP: record current positions, reorder the DOM, then animate each card from
  // its old position to its new one so the swap slides instead of jumping.
  const first = {};
  syms.forEach(s => { first[s] = cards[s].el.getBoundingClientRect(); });
  order.forEach(sym => { if (cards[sym]) root.appendChild(cards[sym].el); });
  applyCardLimit(order);
  if (firstRun) return;
  syms.forEach(s => {
    const el = cards[s].el, a = first[s], b = el.getBoundingClientRect();
    const dx = a.left - b.left, dy = a.top - b.top;
    if (!dx && !dy) return;
    el.style.transition = "none";
    el.style.transform = `translate(${dx}px, ${dy}px)`;
    el.style.zIndex = "2";
  });
  requestAnimationFrame(() => {
    syms.forEach(s => {
      const el = cards[s].el;
      if (!el.style.transform) return;
      el.style.transition = "transform 0.5s cubic-bezier(0.2, 0.8, 0.2, 1)";
      el.style.transform = "";
      el.addEventListener("transitionend", () => { el.style.transition = ""; el.style.zIndex = ""; }, { once: true });
    });
  });
}

function renderWatch() {
  const box = $("#watch-grid");
  if (!box) return;
  const m = state.signals && state.signals.market;
  const rows = (m && m.radar) || [];
  const modeled = new Set((state.config ? state.config.symbols : []));
  const hotFunding = r => Math.abs(r.funding || 0) >= 0.0005;
  const urg = r => Math.abs(r.chg || 0) + (r.social ? 2 : 0) + (hotFunding(r) ? 2 : 0) + (modeled.has(r.symbol) ? 6 : 0);
  const filter = ($("#watch-filter") && $("#watch-filter").value || "").trim().toUpperCase();
  let list = rows.slice().sort((a, b) => urg(b) - urg(a));
  if (filter) list = list.filter(r => (r.symbol || "").includes(filter) || (r.name || "").toUpperCase().includes(filter));
  const meta = $("#mw-meta");
  if (meta) meta.textContent = rows.length ? `${list.length} of ${rows.length} names, most urgent first` : "waiting for market scan…";
  box.innerHTML = list.map(r => {
    const mdl = modeled.has(r.symbol);
    const flags = [];
    if (mdl) flags.push(`<span class="mdl">modeled</span>`);
    if (r.social) flags.push(`<span class="soc">reddit ${r.social}</span>`);
    if (hotFunding(r)) flags.push(`<span class="fund">funding ${fmtRate(r.funding, 3)}</span>`);
    return `<div class="wtile ${mdl ? "modeled" : ""}" data-sym="${esc(r.symbol)}" title="${esc(r.name || "")} · details"><div class="wt"><span class="sym">${esc(r.base || base(r.symbol))}</span>` +
      `<span class="chg ${r.chg >= 0 ? "up" : "down"}">${r.chg >= 0 ? "+" : ""}${(r.chg || 0).toFixed(1)}%</span></div>` +
      `<div class="px">${r.price != null ? fmtPrice(r.price) : "·"}</div><div class="wf">${flags.join("")}</div></div>`;
  }).join("");
}

function renderRadar() {
  const box = $("#radar-table");
  if (!box) return;
  const m = state.signals && state.signals.market;
  const rows = (m && m.radar) || [];
  const meta = $("#radar-meta");
  if (meta) meta.textContent = rows.length ? `${rows.length} names by market cap, 24h change` : "waiting for scan…";
  const filter = ($("#radar-filter") && $("#radar-filter").value || "").trim().toUpperCase();
  const shown = filter ? rows.filter(r => (r.symbol || "").includes(filter) || (r.name || "").toUpperCase().includes(filter)) : rows;
  const fvol = v => v == null ? "·" : v >= 1e9 ? (v / 1e9).toFixed(1) + "B" : v >= 1e6 ? (v / 1e6).toFixed(1) + "M" : v >= 1e3 ? (v / 1e3).toFixed(0) + "K" : v;
  const head = `<div class="rrow head"><span>symbol</span><span>% chg</span><span>price</span><span>$ volume</span><span>category</span><span>signals</span></div>`;
  box.innerHTML = head + shown.map(r => {
    const flags = [];
    if (r.social) flags.push(`<span class="soc">reddit ${r.social}</span>`);
    if (Math.abs(r.funding || 0) >= 0.0005) flags.push(`<span class="fund">funding ${fmtRate(r.funding, 3)}</span>`);
    return `<div class="rrow"><span class="sym tk" data-sym="${esc(r.symbol)}">${esc(r.base || base(r.symbol))}</span>` +
      `<span class="chg ${r.chg >= 0 ? "up" : "down"}">${r.chg >= 0 ? "+" : ""}${(r.chg || 0).toFixed(1)}%</span>` +
      `<span>${r.price != null ? fmtPrice(r.price) : "·"}</span><span>${fvol(r.dollar_volume != null ? r.dollar_volume : (r.price || 0) * (r.vol || 0))}</span>` +
      `<span class="cat">${esc(r.sector || r.cat || "")}</span><span class="flags">${flags.join("") || `<span class="nm">${esc(r.name || "")}</span>`}</span></div>`;
  }).join("");
}

function clamp01(x) { return Math.max(0, Math.min(1, x)); }

function gaugeSVG(frac, big, label, color) {
  frac = clamp01(frac);
  const cx = 60, cy = 56, r = 46;
  const pol = d => [cx + r * Math.cos(d * Math.PI / 180), cy + r * Math.sin(d * Math.PI / 180)];
  const arc = (a, b) => { const [x0, y0] = pol(a), [x1, y1] = pol(b); const large = (b - a) > 180 ? 1 : 0;
    return `M${x0.toFixed(1)} ${y0.toFixed(1)} A${r} ${r} 0 ${large} 1 ${x1.toFixed(1)} ${y1.toFixed(1)}`; };
  return `<svg viewBox="0 0 120 74" class="gauge">` +
    `<path d="${arc(180, 360)}" fill="none" stroke="var(--grid)" stroke-width="9" stroke-linecap="round"/>` +
    `<path d="${arc(180, 180 + frac * 180)}" fill="none" stroke="${color}" stroke-width="9" stroke-linecap="round"/>` +
    `<text x="60" y="48" class="g-big">${big}</text><text x="60" y="68" class="g-lab">${esc(label)}</text></svg>`;
}

function rangeBar(q) {
  const scale = Math.max(12, Math.abs(q[0]), Math.abs(q[4])) * 1.15;
  const pos = v => (50 + (v / scale) * 50).toFixed(1);
  return `<div class="brange"><div class="zero"></div>` +
    `<div class="band" style="left:${pos(q[0])}%;right:${(100 - pos(q[4])).toFixed(1)}%"></div>` +
    `<div class="mid" style="left:${pos(q[2])}%"></div></div>`;
}

function briefText(sig, m) {
  const out = [];
  const fmtc = v => v == null ? "·" : (v >= 0 ? "+" : "") + v.toFixed(2) + "%";
  const breadth = m.breadth != null ? Math.round(m.breadth * 100) : null;
  let s1 = `Tone is <b>${esc(m.regime || "—")}</b>`;
  if (breadth != null) s1 += `, with <b>${breadth}%</b> of the top ${m.breadth_n || 100} names higher on the day`;
  if (m.sectors && m.sectors.length) { const a = m.sectors[0], z = m.sectors[m.sectors.length - 1];
    s1 += `; <b>${esc(a.name)}</b> leads (${fmtc(a.chg)}) and <b>${esc(z.name)}</b> lags (${fmtc(z.chg)})`; }
  const dv = sig.dvol || {};
  out.push(s1 + (dv.BTC ? `. BTC DVOL ${dv.BTC.toFixed(0)}.` : "."));
  if (m.total_mcap) out.push(`The market is <b>$${(m.total_mcap / 1e12).toFixed(2)}T</b> (${fmtc(m.mcap_chg24)} 24h)` +
    (m.btc_dom ? `, BTC dominance <b>${m.btc_dom.toFixed(1)}%</b>` : "") +
    (m.stable_flow_1d != null ? `; stablecoin supply ${fmtc(m.stable_flow_1d * 100)} on the day` : "") + ".");
  // retail + crowding extreme
  const cr = sig.crowding || {};
  const hot = Object.entries(cr).sort((a, b) => b[1] - a[1])[0];
  let topSoc = null, topN = 0;
  Object.entries(sig.per_asset || {}).forEach(([s, pa]) => { if ((pa.mentions || 0) > topN) { topN = pa.mentions; topSoc = s; } });
  if (topSoc && topN) out.push(`Retail attention is on <b>${esc(base(topSoc))}</b> (${topN} Reddit mentions)` +
    (hot && hot[1] > 0.2 ? `; most crowded name is <b>${esc(base(hot[0]))}</b> (${hot[1].toFixed(2)})` : "") + ".");
  // funding extreme
  const fr = Object.entries(sig.per_asset || {}).filter(([, pa]) => pa.funding_rate != null)
    .sort((a, b) => Math.abs(b[1].funding_rate) - Math.abs(a[1].funding_rate))[0];
  if (fr && Math.abs(fr[1].funding_rate) >= 0.0003) out.push(`Funding is most stretched on <b>${esc(base(fr[0]))}</b> at ${fmtRate(fr[1].funding_rate)} per 8h, ` +
    (fr[1].funding_rate > 0 ? "longs paying shorts" : "shorts paying longs") + ".");
  // model
  const m2 = state.metrics || {};
  const calls = state.config.symbols.map(s => state.latest[s]).filter(L => L && L.side && L.trusted);
  if (calls.length) out.push(`The model has <b>${calls.length}</b> live call${calls.length > 1 ? "s" : ""}: ` +
    calls.slice(0, 4).map(L => `${esc(base(L.symbol))} ${L.action}`).join(", ") + ".");
  else out.push(`The model is <b>holding</b> across the board` + (m2.trusted ? "" : ` while it warms up (${m2.live_labels || 0}/${state.config.min_labels} labels${m2.holdout_hit != null ? `, held-out ${Math.round(m2.holdout_hit * 100)}% on ${m2.holdout_calls} calls` : ""})`) + ".");
  return out;
}

function briefColumn() {
  const b = state.brief;
  const busy = b && b.generating;
  const regen = `<button id="brief-regen"${busy ? " disabled" : ""}>${busy ? "writing…" : "\u21bb regenerate"}</button>`;
  const masthead = meta => `<div class="col-masthead"><span class="col-name">Steel \u00b7 Crypto</span><span class="col-meta">${esc(meta)}</span>${regen}</div>`;
  if (busy && !(b && b.text)) {
    return `<div class="brief-column">${masthead("the local desk is writing\u2026")}<div class="col-loading"><div class="spark-mini"></div>` +
      `<p>Fast models are digesting the tape, macro and positioning; a bigger model writes the column. This runs entirely on your machine and can take a minute.</p></div></div>`;
  }
  if (!b || (!b.text && !b.error)) return null;                 // fall back to the templated summary
  if (b.error && !b.text) {
    return `<div class="brief-column">${masthead("local brief unavailable")}<div class="col-error">${esc(b.error)}</div>` +
      `<p class="col-hint">The written brief runs on a local model via <b>Ollama</b> \u2014 nothing leaves this machine.</p></div>`;
  }
  const parts = (b.text || "").trim().split(/\n{2,}/);
  const headline = parts.shift() || "Market Brief";
  const body = parts.map(x => `<p>${esc(x).replace(/\n/g, "<br>")}</p>`).join("");
  const models = b.models || {};
  const when = b.t ? new Date(b.t * 1000).toLocaleString() : "";
  const takesArr = Object.entries(b.takes || {}).filter(([, v]) => v);
  const takes = takesArr.length
    ? `<details class="desk-notes"><summary>desk notes${models.small ? " \u00b7 " + esc(models.small) : ""}</summary>` +
      takesArr.map(([k, v]) => `<div class="dnote"><span class="dk">${esc(k.replace("_", " "))}</span><span class="dv">${esc(v)}</span></div>`).join("") +
      `</details>` : "";
  return `<div class="brief-column">${masthead(when + (models.big ? " \u00b7 written locally by " + models.big : ""))}` +
    `<article class="col-body"><h2 class="col-head">${esc(headline)}</h2>${body}</article>${takes}` +
    `${busy ? '<div class="col-refreshing">refreshing\u2026</div>' : ""}</div>`;
}

function renderBrief() {
  const box = $("#brief-body");
  if (!box || !state.config) return;
  const sig = state.signals || {}, m = sig.market || {};
  const panel = $("#brief-panel"); if (panel) panel.hidden = !(state.brief && state.brief.text);
  if (!m.t && !Object.keys(sig).length) { return; }
  const rc = m.regime === "risk-on" ? "on" : m.regime === "risk-off" ? "off" : "mixed";
  const templated = `<div class="brief-hero"><div class="htop"><span class="regime ${rc}">${esc((m.regime || "—").toUpperCase())}</span>` +
    `<span class="hts">market brief · ${m.t ? fmtTime(m.t) : "scanning…"}</span></div>` +
    briefText(sig, m).map(p => `<p>${p}</p>`).join("") + `</div>`;
  const hero = briefColumn() || templated;

  // gauges
  const fg = sig.fear_greed || {}, dv = sig.dvol || {};
  const breadth = m.breadth;
  const gauges = [];
  if (breadth != null) gauges.push(gauge_card(gaugeSVG(breadth, Math.round(breadth * 100) + "%", "breadth", breadth > 0.55 ? C.good : breadth < 0.45 ? C.serious : C.warning), `${m.breadth_up || 0}/${m.breadth_n || 0} names up`));
  if (fg.value != null) gauges.push(gauge_card(gaugeSVG(fg.value / 100, fg.value, "fear/greed", fg.value > 60 ? C.serious : fg.value < 40 ? C.blue : C.warning), esc(fg.class || "")));
  if (dv.BTC) gauges.push(gauge_card(gaugeSVG(clamp01((dv.BTC - 30) / 90), dv.BTC.toFixed(0), "BTC DVOL", dv.BTC >= 70 ? C.serious : dv.BTC >= 45 ? C.warning : C.good), (dv.BTC >= 70 ? "elevated" : dv.BTC >= 45 ? "moderate" : "calm") + (dv.ETH ? ` · ETH ${dv.ETH.toFixed(0)}` : "")));
  if (m.btc_dom) gauges.push(gauge_card(gaugeSVG(clamp01(m.btc_dom / 100), m.btc_dom.toFixed(1) + "%", "BTC dominance", C.yellow), m.total_mcap ? `$${(m.total_mcap / 1e12).toFixed(2)}T total` : ""));
  if (m.stable_flow_1d != null) gauges.push(gauge_card(gaugeSVG(clamp01(0.5 + m.stable_flow_1d * 50), (m.stable_flow_1d >= 0 ? "+" : "") + (m.stable_flow_1d * 100).toFixed(2) + "%", "stablecoin flow", m.stable_flow_1d >= 0 ? C.good : C.serious), "supply change, 1 day"));
  const gaugeRow = gauges.length ? `<div class="gauges">${gauges.join("")}</div>` : "";

  // movers
  const mv = m.movers || {};
  const moverPanel = (title, rows, up) => `<div class="bpanel"><h3>${title}</h3>` +
    (rows || []).slice(0, 6).map(r => { const w = Math.min(100, Math.abs(r.chg) * 4);
      return `<div class="bmover"><span class="sym tk" data-sym="${esc(r.symbol)}">${esc(r.base || base(r.symbol))}</span>` +
      `<div class="bar" style="width:${w}%;background:${up ? C.good : C.serious}"></div>` +
      `<span class="v ${r.chg >= 0 ? "up" : "down"}">${r.chg >= 0 ? "+" : ""}${(r.chg || 0).toFixed(1)}%</span></div>`; }).join("") + `</div>`;

  // model calls (strongest by |score|)
  const calls = state.config.symbols.map(s => state.latest[s]).filter(L => L && L.q)
    .sort((a, b) => Math.abs(b.score) - Math.abs(a.score)).slice(0, 6);
  const callPanel = `<div class="bpanel"><h3>Model calls <small>${Math.round((state.config.horizon || 12) * (state.config.bar_seconds || 300) / 60)}-min forecast, 10–90 band</small></h3>` +
    (calls.length ? calls.map(L => `<div class="bcall"><span class="sym tk" data-sym="${esc(L.symbol)}">${esc(base(L.symbol))}</span>` +
      `<span class="badge ${L.action.toLowerCase()}">${L.action}</span>${rangeBar(L.q)}</div>`).join("")
      : `<div class="why">warming up…</div>`) + `</div>`;

  // positioning: fear & greed, the most stretched funding, the most crowded names
  const cr = sig.crowding || {};
  const crowdRows = Object.entries(cr).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1])).slice(0, 3)
    .map(([s, v]) => `<div class="bline"><span>${esc(base(s))} crowding</span><b class="${v >= 0 ? "down" : "up"}">${v >= 0 ? "+" : ""}${v.toFixed(2)}</b></div>`).join("");
  const fundRows = Object.entries(sig.per_asset || {}).filter(([, pa]) => pa.funding_rate != null)
    .sort((a, b) => Math.abs(b[1].funding_rate) - Math.abs(a[1].funding_rate)).slice(0, 3)
    .map(([s, pa]) => `<div class="bline"><span>${esc(base(s))} funding</span><b class="${pa.funding_rate >= 0 ? "down" : "up"}">${fmtRate(pa.funding_rate)}</b></div>`).join("");
  const posPanel = `<div class="bpanel"><h3>Positioning</h3>` +
    (fg.value != null ? `<div class="bline"><span>Fear &amp; Greed</span><b>${fg.value} ${esc(fg.class || "")}</b></div>` : "") +
    fundRows + crowdRows + `</div>`;

  // sectors
  const sectorPanel = m.sectors && m.sectors.length ? `<div class="bpanel"><h3>Category rotation</h3><div class="sector-heat">` +
    m.sectors.map(sc => `<div class="sc tk" data-sym="${esc((sc.lead || "") + "-USD")}" data-cat="${esc(sc.name)}" data-chg="${sc.chg}" style="background:${heatColor(sc.chg)}" title="${esc(sc.name)}"><span>${esc(sc.name)}</span><b>${sc.chg >= 0 ? "+" : ""}${sc.chg.toFixed(1)}</b></div>`).join("") + `</div></div>` : "";

  box.innerHTML = hero + gaugeRow + `<div class="brief-grid">` +
    callPanel + moverPanel("Top gainers", mv.gainers, true) + moverPanel("Top losers", mv.losers, false) +
    sectorPanel + posPanel + `</div>`;
}

function gauge_card(svg, sub) { return `<div class="gcard">${svg}<div class="gsub">${esc(sub)}</div></div>`; }

const heatColor = c => { const t = Math.max(-1, Math.min(1, (c || 0) / 2)); return t >= 0 ? `rgba(${C.heatUp},${0.25 + 0.55 * t})` : `rgba(${C.heatDown},${0.25 + 0.55 * -t})`; };
const CATEGORY_INFO = {
  "Store of value": "Bitcoin and the proof-of-work majors: the market's reserve asset and its beta.",
  "Smart contract L1": "Base-layer chains that run contracts: ETH, SOL, ADA, AVAX, SUI and the rest.",
  "Layer 2": "Rollups and scaling layers on Ethereum: ARB, OP, POL, STRK, ZK.",
  "DeFi": "Exchanges, lending, staking and yield protocols: UNI, AAVE, LDO, CRV, PENDLE.",
  "Perp DEX": "On-chain perpetual-futures venues: HYPE, GMX, dYdX.",
  "Oracle / infra": "Oracles and indexing that other protocols depend on: LINK, PYTH, GRT.",
  "Storage / infra": "Decentralized storage: FIL, AR.",
  "AI / compute": "Tokens for compute, agents and model networks: RENDER, FET, TAO, WLD.",
  "Meme": "Memecoins: pure attention and flow, no cash flows. DOGE, SHIB, PEPE, BONK, WIF.",
  "Exchange token": "Tokens issued by exchanges: BNB, CRO, OKB.",
  "Gaming / metaverse": "Game and virtual-world tokens: SAND, MANA, AXS, GALA.",
  "Payments": "Payment and settlement networks: XRP, XLM.",
  "Privacy": "Privacy coins: XMR, ZEC, DASH.",
  "RWA": "Tokenized real-world assets: ONDO.",
  "Modular / infra": "Data availability and interoperability layers: TIA.",
  "Identity / social": "Naming and social protocols: ENS.",
  "Enterprise": "Enterprise-oriented networks: QNT.",
  "Tokenized gold": "Gold-backed tokens: PAXG, XAUT.",
  "Stablecoin": "Dollar stablecoins: not traded, but their supply is a flow signal.",
  "Other": "Names the scan has not placed in a category yet.",
};
let _sectorTip = false;
document.addEventListener("mousemove", e => {
  const sc = e.target.closest && e.target.closest("[data-cat]");
  if (sc) {
    _sectorTip = true;
    const cat = sc.dataset.cat, chg = parseFloat(sc.dataset.chg);
    const secs = ((state.signals || {}).market || {}).sectors || [];
    const i = secs.findIndex(x => x.name === cat);
    const n = i >= 0 ? secs[i].n : null, lead = i >= 0 ? secs[i].lead : null;
    const rank = i === 0 ? "leading today" : (i === secs.length - 1 && secs.length > 1) ? "lagging today" : (i >= 0 ? `#${i + 1} of ${secs.length} today` : "");
    const pct = isNaN(chg) ? "" : `\n\n${chg >= 0 ? "+" : ""}${chg.toFixed(2)}% today (cap-weighted)${rank ? " \u00b7 " + rank : ""}${n ? ` \u00b7 ${n} names, led by ${lead}` : ""}\nclick for the leader's details`;
    showTooltip(e.clientX, e.clientY, (CATEGORY_INFO[cat] || cat) + pct);
  } else if (_sectorTip) {
    _sectorTip = false; hideTooltip();
  }
});

function fmtUSD(v) { const n = Math.abs(Math.round(v)); return (v < 0 ? "-$" : "$") + n.toLocaleString(); }

function paintPaperEquity() {
  const p = state.paper; if (!p) return;
  const eqEl = $("#p-equity"); if (eqEl) eqEl.textContent = fmtUSD(p.equity);
  const ret = (p.equity / p.start - 1) * 100, rEl = $("#p-return");
  if (rEl) { rEl.textContent = (ret >= 0 ? "+" : "") + ret.toFixed(2) + "%"; rEl.className = "peq-ret " + (ret >= 0 ? "up" : "down"); }
}

let btFetched = false;
async function fetchBacktest() {
  // a page opened mid-run has no report yet: ask for the live one instead of saying "no run yet"
  if (btFetched) return; btFetched = true;
  try {
    const r = await fetch("/api/backtest"); if (!r.ok) return;
    const j = await r.json(); const b = j.backtest || {};
    state.backtest = { ...b, progress: j.progress || {}, totals: (j.progress && j.progress.running) ? j.progress.totals : b.totals };
    renderBacktest();
  } catch (e) { /* offline: the next push fills it */ }
}

function fmtElapsed(s) { s = Math.max(0, Math.round(s)); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60); return h ? `${h} h ${m} min` : `${m} min`; }

let monLoaded = 0;
async function loadMonitor(force) {
  if (!vis("monitor") || (!force && Date.now() - monLoaded < 300000)) return;
  monLoaded = Date.now();
  const r = await fetch("/api/monitor"); if (!r.ok) return;
  state.monitor = await r.json(); renderMonitor();
}
function renderMonitor() {
  const M = state.monitor, cal = $("#mon-cal"), dr = $("#mon-drift"), card = $("#mon-card"), meta = $("#mon-meta"); if (!M || !cal) return;
  const flagged = (M.drift || []).filter(d => d.flag).length;
  if (meta) meta.textContent = `${(M.calibration || []).length} names with resolved calls · ${(M.drift || []).length} features over the last ${M.window_bars} bars${flagged ? ` · ${flagged} drifting` : " · no drift"} · ${(M.scorecard || []).length} days of scorecard`;
  cal.innerHTML = (M.calibration || []).length ? `<div class="rrow head mon"><span>name</span><span>calls</span><span>hit</span><span>in band</span><span>|err| bps</span><span>pnl bps</span></div>` +
    M.calibration.slice(0, 30).map(r => `<div class="rrow mon"><span>${esc(base(r.symbol))}</span><span>${r.n}</span><span class="${r.hit >= 0.5 ? "up" : "down"}">${Math.round(r.hit * 100)}%</span><span class="${r.inside >= 0.7 ? "" : "down"}">${Math.round(r.inside * 100)}%</span><span>${r.mae_bps}</span><span class="${r.pnl_bps >= 0 ? "up" : "down"}">${r.pnl_bps}</span></div>`).join("") : `<div class="why">no resolved calls yet</div>`;
  dr.innerHTML = (M.drift || []).length ? `<div class="rrow head mon4"><span>feature</span><span>mean</span><span>spread</span><span>flag</span></div>` +
    M.drift.map(d => `<div class="rrow mon4"><span>${esc(d.feature)}</span><span class="${Math.abs(d.mean) > 1 ? "down" : ""}">${d.mean > 0 ? "+" : ""}${d.mean.toFixed(2)}</span><span class="${d.sd > 2 || d.sd < 0.05 ? "down" : ""}">${d.sd.toFixed(2)}</span><span class="down">${esc(d.flag)}</span></div>`).join("") : `<div class="why">not enough replay rows yet</div>`;
  card.innerHTML = (M.scorecard || []).length ? `<div class="rrow head card"><span>day</span><span>held-out hit</span><span>calls</span><span>coverage</span><span>pinball</span><span>live hit</span><span>labels</span><span>trusted</span></div>` +
    M.scorecard.slice().reverse().map(r => `<div class="rrow card"><span>${esc(r.day)}</span><span class="${(r.holdout_hit || 0) >= 0.5 ? "up" : "down"}">${r.holdout_hit != null ? Math.round(r.holdout_hit * 100) + "%" : "·"}</span><span>${r.holdout_calls || 0}</span><span>${r.holdout_coverage != null ? Math.round(r.holdout_coverage * 100) + "%" : "·"}</span><span>${r.holdout_pinball != null ? r.holdout_pinball.toFixed(2) : "·"}</span><span>${r.hit_ema != null ? Math.round(r.hit_ema * 100) + "%" : "·"}</span><span>${r.labels}</span><span>${r.trusted ? "yes" : "no"}</span></div>`).join("") : `<div class="why">the scorecard is written once a day at the checkpoint</div>`;
}
document.addEventListener("click", e => { if (e.target.closest && e.target.closest("#mon-run")) loadMonitor(true); });
async function showTicket(sym) {
  const r = await fetch("/api/ticket/" + encodeURIComponent(sym)); const t = await r.json();
  let el = $("#ticket-modal");
  if (!el) { el = document.createElement("div"); el.id = "ticket-modal"; el.className = "ticket-modal"; document.body.appendChild(el); }
  el.innerHTML = `<div class="ticket-card"><h3>Order ticket <small>for a human to place by hand; Steel sends nothing</small></h3><pre id="ticket-text">${esc(t.error || t.text || "")}</pre>` +
    `<div class="row"><button type="button" class="mini" id="ticket-copy">Copy</button><button type="button" class="mini" id="ticket-json">Copy JSON</button><button type="button" class="mini" id="ticket-close">Close</button></div></div>`;
  el.hidden = false;
  $("#ticket-close").onclick = () => { el.hidden = true; };
  $("#ticket-copy").onclick = async () => { try { await navigator.clipboard.writeText(t.text || ""); $("#ticket-copy").textContent = "Copied"; } catch (e) { /* no clipboard */ } };
  $("#ticket-json").onclick = async () => { try { await navigator.clipboard.writeText(JSON.stringify(t, null, 1)); $("#ticket-json").textContent = "Copied"; } catch (e) { /* no clipboard */ } };
}
let arbrLoaded = 0;
async function loadArbReplay(force) {
  if (!vis("arbreplay") || (!force && Date.now() - arbrLoaded < 600000)) return;
  arbrLoaded = Date.now();
  const meta = $("#arbr-meta"); if (meta) meta.textContent = "computing…";
  const r = await fetch("/api/arb/replay" + (force ? "?force=1" : "")); if (!r.ok) return;
  const s = await r.json(); state.arbReplay = s; renderArbReplay();
}
function renderArbReplay() {
  const s = state.arbReplay, meta = $("#arbr-meta"), L = $("#arbr-ladder"), P = $("#arbr-pairs"); if (!s || !meta) return;
  if (s.error) { meta.textContent = s.error; return; }
  const w = s.window_ms || {};
  const sus = (s.ladder && s.ladder[0]) || {};
  meta.textContent = `${s.episodes} episodes over ${s.days} days${sus.suspect ? ` · ${sus.suspect} wider than ${sus.max_edge_bps} bps are data problems (${Object.entries(sus.suspect_venues || {}).map(([k, n]) => `${k} ${n}`).join(", ")})` : ""} · window median ${Math.round(w.median || 0)} ms, p90 ${Math.round(w.p90 || 0)} ms · ${w.over_300 || 0} lasted 300 ms+ · order cap ${fmtUSD(s.ladder && s.ladder[0] ? s.ladder[0].max_order_usd : 0)} · inventory rebalanced on-chain every ${fmtUSD(s.ladder && s.ladder[0] ? s.ladder[0].inventory_usd : 0)} of one-way flow`;
  L.innerHTML = `<div class="rrow head arbr"><span>latency</span><span>captured</span><span>too fast</span><span>too thin</span><span>cooling</span><span>mean bps</span><span>net USD</span><span>transfers</span></div>` +
    (s.ladder || []).map(r => `<div class="rrow arbr"><span>${r.latency_ms} ms</span><span>${r.captured}</span><span>${r.too_fast}</span><span>${r.too_thin}</span><span>${r.cooling}</span><span>${r.mean_bps.toFixed(2)}</span><span class="${r.usd >= 0 ? "up" : "down"}">${fmtUSD(r.usd)}</span><span>${fmtUSD(r.transfers_usd)}</span></div>`).join("");
  P.innerHTML = (s.pairs || []).length ? `<div class="rrow head arbr"><span>name</span><span>buy on</span><span>sell on</span><span>kind</span><span>captures</span><span>mean bps</span><span>net USD</span><span>transfers</span></div>` +
    s.pairs.slice(0, 10).map(p => `<div class="rrow arbr"><span>${esc(base(p.sym))}</span><span>${esc(p.buy)}</span><span>${esc(p.sell)}</span><span>${esc(p.kind)}</span><span>${p.n}</span><span>${p.mean_bps.toFixed(2)}</span><span class="${p.usd >= 0 ? "up" : "down"}">${fmtUSD(p.usd)}</span><span>${fmtUSD(p.transfers)}</span></div>`).join("") : `<div class="why">nothing capturable at 300 ms in this window</div>`;
}
document.addEventListener("click", e => { if (e.target.closest && e.target.closest("#arbr-run")) loadArbReplay(true); });
function renderBacktest() {
  const b = state.backtest, root = $("#bt-stats"); if (!root) return;
  const meta = $("#bt-meta"), pr = (b && b.progress) || {}, bar = $("#bt-progress");
  if (!b || !("progress" in b)) fetchBacktest();
  if (bar) {
    const warming = pr.running && !pr.day && !pr.days;
    bar.hidden = !pr.running;
    bar.classList.toggle("indeterminate", !!(pr.running && !pr.history_days));
    if (pr.running && pr.history_days) {
      const vi = Math.max(0, (pr.variants || []).indexOf(pr.variant)), nv = Math.max(1, (pr.variants || [1]).length);
      bar.firstElementChild.style.width = `${(100 * (vi + (pr.history_day || 0) / pr.history_days) / nv).toFixed(1)}%`;
    } else if (warming) bar.firstElementChild.style.width = "";
  }
  if (!b || (!b.totals && !pr.running)) {
    root.innerHTML = ""; $("#bt-days").innerHTML = ""; $("#bt-names").innerHTML = "";
    if (meta) meta.textContent = b && b.error ? b.error : "no run yet — runs once a week over the replay, or press Run now";
    paintBacktest([]); return;
  }
  const t = b.totals || {}, c = b.config || {};
  const f1 = v => v != null ? v.toFixed(1) : "·", f2 = v => v != null ? v.toFixed(2) : "·";
  root.innerHTML = [
    ["net", fmtBps(t.net_bps)], ["per day", fmtBps(t.net_bps_per_day)], ["Sharpe", f2(t.sharpe)], ["max drawdown", fmtBps(t.max_drawdown_bps)],
    ["hit rate", fmtPct(t.hit_rate)], ["10-90 coverage", fmtPct(t.coverage)], ["pinball", f1(t.pinball)],
    ["signals", t.signals ?? 0], ["days", `${t.days ?? 0}${pr.running ? " / " + (pr.days || "?") : ""}`], ["always long", fmtBps(t.long_bps)],
    ...(c.target === "relative" ? [["unhedged", fmtBps(t.unhedged_bps)]] : []),
    ...(b.baseline && b.baseline.totals ? [[`${b.baseline.target || "other"} target`, `${fmtBps(b.baseline.totals.net_bps)} · Sharpe ${f2(b.baseline.totals.sharpe)}`]] : []),
  ].map(([k, v]) => `<div class="pstat"><label>${esc(k)}</label><b>${esc(String(v))}</b></div>`).join("");
  const variant = pr.variant && pr.variants && pr.variants.length > 1 ? ` · ${pr.variant} target, ${pr.variants.indexOf(pr.variant) + 1} of ${pr.variants.length}` : (pr.variant ? ` · ${pr.variant} target` : "");
  const elapsed = pr.started ? ` · ${fmtElapsed(Date.now() / 1000 - pr.started)} elapsed` : "";
  const warmN = pr.warm_days || (state.controls && state.controls.backtest_warm_days) || 10;
  const phase = pr.phase === "warming" ? `warming up: training-only day ${pr.history_day || 1} of ${warmN}, scoring starts after`
    : pr.phase === "scoring" ? `scoring day ${pr.day} of ${pr.days || "?"}${pr.step_s ? ` · ${pr.step_s.toFixed(1)} s/step` : ""}`
    : (!pr.day && !pr.days) ? `warming up on the first ${warmN} training-only days, then scoring` : `day ${pr.day} of ${pr.days || "?"}`;
  if (meta) meta.textContent = pr.running ? `running: ${phase}${variant}${elapsed}`
    : (b.ran_at ? `ran ${fmtTime(b.ran_at)} · ${c.preset || "model"}${c.params ? " " + (c.params / 1e6).toFixed(1) + "M params" : ""} · ${c.target || "absolute"} target${c.index && c.index.length ? " vs " + c.index.join(", ") : ""} · cost ${c.cost_bps} bps · ${c.history_days} days of history, ${c.warm_days} warm · ${b.stopped ? "stopped early" : "complete"}` : "");
  const days = (b.days || []).slice(-40).reverse();
  const cell = (v, cls) => `<span class="num ${cls || ""}">${v}</span>`;
  // which experts the regime gate leaned on that day: the top three by mean weight, as E3 41 · E7 22 · E1 12
  const experts = d => (d.gate || []).map((v, i) => [v, i]).sort((a, b) => b[0] - a[0]).slice(0, 3).map(([v, i]) => `E${i + 1} ${(v * 100).toFixed(0)}`).join(" · ");
  const hasGate = days.some(d => d.gate && d.gate.length);
  $("#bt-days").innerHTML = days.length
    ? `<div class="prow pday phead"><span>day</span>${cell("net")}${cell("cum")}${cell("signals")}${cell("hit")}${cell("cover")}${cell("steps")}${hasGate ? cell("experts") : ""}</div>` +
      days.map(d => `<div class="prow pday"><span>${esc(d.date)}</span>${cell(fmtBps(d.net_bps), d.net_bps >= 0 ? "up" : "down")}${cell(fmtBps(d.cum_net_bps))}${cell(d.signals)}${cell(fmtPct(d.hit))}${cell(fmtPct(d.coverage))}${cell(d.steps)}${hasGate ? `<span class="num gate" title="mean gate weight per expert: ${(d.gate || []).map((v, i) => `E${i + 1} ${(v * 100).toFixed(0)}%`).join(", ")}">${experts(d)}</span>` : ""}</div>`).join("")
    : "";
  const dh = $("#bt-days-head"); if (dh) dh.textContent = pr.running ? `newest first, ${days.length} scored so far` : "newest first, last 40";
  const names = [...(b.best || []), ...(b.worst || [])];
  $("#bt-names").innerHTML = names.length
    ? `<div class="prow phead"><span>name</span>${cell("net bps")}${cell("signals")}${cell("hit")}</div>` +
      names.map(x => `<div class="prow"><span class="psym">${esc(x.sym)}</span>${cell(fmtBps(x.net_bps), x.net_bps >= 0 ? "up" : "down")}${cell(x.signals)}${cell(fmtPct(x.hit))}</div>`).join("")
    : "";
  paintBacktest(b.days || [], (b.baseline && b.baseline.days) || []);
  renderSweep(b.sweep);
}

function renderSweep(sw) {
  const root = $("#sw-table"), meta = $("#sw-meta"); if (!root) return;
  const cell = (v, cls) => `<span class="num ${cls || ""}">${v}</span>`;
  if (!sw || sw.error) { root.innerHTML = ""; if (meta && sw && sw.error) meta.textContent = sw.error; return; }
  const cur = sw.current || {}, prop = sw.proposal, pd = v => v != null ? fmtBps(v) + "/day" : "·";
  const half = r => r ? `${pd(r.net_bps_per_day)} · ${r.signals} signals · hit ${fmtPct(r.hit_rate)}` : "·";
  if (meta) meta.textContent = `${sw.days} days, split at day ${sw.split_day}; ${sw.evaluations} settings tried · ${sw.accepted ? "proposal confirmed on the second half" : (prop ? "proposal failed the second half: not recommended" : "no setting beat the current gates")} · ${esc(sw.target || "")} target, cost ${sw.cost_bps} bps`;
  const rows = [`<div class="prow phead"><span>gate</span>${cell("now")}${cell("proposed")}</div>`];
  Object.keys(cur.knobs || {}).forEach(k => {
    const v = cur.knobs[k], pv = prop && prop.knobs && k in prop.knobs ? prop.knobs[k] : null;
    rows.push(`<div class="prow"><span>${esc(k.replace(/_/g, " "))}</span>${cell(v)}${cell(pv != null ? pv : "—", pv != null ? (sw.accepted ? "up" : "") : "")}</div>`);
  });
  rows.push(`<div class="prow"><span>tuning half</span>${cell(half(cur.tune))}${cell(prop ? half(prop.tune) : "·")}</div>`);
  rows.push(`<div class="prow"><span>confirming half</span>${cell(half(cur.confirm))}${cell(prop ? half(prop.confirm) : "·", prop && sw.accepted ? "up" : prop ? "down" : "")}</div>`);
  root.innerHTML = rows.join("") + (sw.why ? `<div class="snote">${esc(sw.why)}</div>` : "");
}

function paintBacktest(days, baseline) {
  const canvas = $("#bt-curve"); if (!canvas) return;
  const { ctx: g, w: W, h: H } = ctx2d(canvas);
  if (days.length < 2) return;
  const cs = getComputedStyle(document.body), v = n => cs.getPropertyValue(n).trim();
  // grey: always long; green: the run; amber: the other target's run over the same days, when there was one
  const byDate = new Map((baseline || []).map(d => [d.date, d.cum_net_bps]));
  const series = [[days.map(d => d.cum_long_bps || 0), v("--muted") || "#888", 1], [days.map(d => d.cum_net_bps), v("--good") || "#3c6", 2]];
  if (byDate.size) series.push([days.map(d => byDate.has(d.date) ? byDate.get(d.date) : null), v("--warning") || "#da3", 1.5]);
  const all = series.flatMap(s => s[0]).filter(y => y != null), lo = Math.min(0, ...all), hi = Math.max(0, ...all), span = (hi - lo) || 1;
  const X = i => 8 + (W - 16) * i / (days.length - 1), Y = y => H - 8 - (H - 16) * (y - lo) / span;
  g.strokeStyle = v("--line") || "#444"; g.lineWidth = 1; g.beginPath(); g.moveTo(8, Y(0)); g.lineTo(W - 8, Y(0)); g.stroke();
  series.forEach(([vals, color, lw]) => { g.strokeStyle = color; g.lineWidth = lw; g.beginPath(); let pen = false; vals.forEach((y, i) => { if (y == null) { pen = false; return; } if (pen) g.lineTo(X(i), Y(y)); else g.moveTo(X(i), Y(y)); pen = true; }); g.stroke(); });
  g.fillStyle = v("--ink-2") || "#aaa"; g.font = "11px system-ui, sans-serif"; g.fillText(fmtBps(hi), 10, 14); g.fillText(fmtBps(lo), 10, H - 4);
}

let pfhLoaded = 0;
async function loadPortfolioHistory(force) {
  if (!vis("portfolio") || (!force && Date.now() - pfhLoaded < 300000)) return;
  pfhLoaded = Date.now();
  try { const r = await fetch("/api/portfolio/history?days=30"); if (!r.ok) return; state.pfHistory = await r.json(); drawPortfolioCurve(); } catch (e) { /* offline */ }
}
function drawPortfolioCurve() {
  const cv = $("#pf-curve"), meta = $("#pfh-meta"); if (!cv) return;
  const H = state.pfHistory || {}, rows = H.rows || [];
  const { ctx, w, h } = ctx2d(cv);
  ctx.font = "10px ui-monospace, monospace";
  if (rows.length < 2) { ctx.fillStyle = C.muted; ctx.font = "12px system-ui"; ctx.fillText("no balance history yet: it accrues one point per poll once a read-only key is set", 12, 24); return; }
  if (meta) meta.textContent = `${rows.length} points over ${H.days} days · ${(H.venues || []).join(", ")} · first ${fmtTime(H.first)}`;
  const padL = 58, padR = 12, padT = 12, padB = 22;
  const tot = rows.map(r => r.total);
  let lo = Math.min(...tot), hi = Math.max(...tot);
  const sp = (hi - lo) || Math.max(1, hi * 0.001); lo -= sp * 0.08; hi += sp * 0.08;
  const t0 = rows[0].t, t1 = rows[rows.length - 1].t || t0 + 1;
  const xs = t => padL + (t - t0) / Math.max(1, t1 - t0) * (w - padL - padR);
  const ys = val => padT + (hi - val) / (hi - lo) * (h - padT - padB);
  ctx.strokeStyle = C.grid; ctx.fillStyle = C.muted; ctx.lineWidth = 1; ctx.textBaseline = "middle";
  for (let k = 0; k < 3; k++) { const val = lo + (hi - lo) * (0.12 + 0.38 * k), y = Math.round(ys(val)) + 0.5;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke(); ctx.fillText(fmtUSD(val), 6, y); }
  const venues = H.venues || [], pal = [C.up, "#7aa2f7", "#e0af68", "#bb9af7", "#f7768e", "#2ac3de"];
  venues.forEach((vn, i) => {
    ctx.strokeStyle = pal[(i + 1) % pal.length]; ctx.lineWidth = 1; ctx.beginPath(); let started = false;
    rows.forEach(r => { const e = r.venues && r.venues[vn]; if (!e) return; const x = xs(r.t), y = ys(e.equity); started ? ctx.lineTo(x, y) : ctx.moveTo(x, y); started = true; });
    ctx.stroke(); ctx.fillStyle = pal[(i + 1) % pal.length]; ctx.fillText(vn, padL + 6 + i * 90, padT + 6);
  });
  const up = tot[tot.length - 1] >= tot[0];
  ctx.strokeStyle = up ? C.up : C.down; ctx.lineWidth = 2; ctx.lineJoin = "round";
  ctx.beginPath(); rows.forEach((r, i) => i ? ctx.lineTo(xs(r.t), ys(r.total)) : ctx.moveTo(xs(r.t), ys(r.total))); ctx.stroke();
  ctx.fillStyle = C.muted; ctx.fillText(fmtTime(t0), padL, h - 8); ctx.fillText(fmtTime(t1), w - padR - 60, h - 8);
}
function renderPortfolio() {
  const root = $("#pf-accounts"); if (!root) return;
  loadPortfolioHistory(false);
  const pf = state.portfolio, meta = $("#pf-meta");
  const pp = $("#pf-panel"); if (pp) pp.hidden = false;
  if (!pf || !pf.accounts || !pf.accounts.length) {
    root.innerHTML = "";
    $("#pf-total").textContent = "—"; $("#pf-pnl").textContent = "";
    if (meta) meta.textContent = pf && pf.error ? pf.error : `add a read-only exchange key on the Venues tab to see balances (${(state.keys || []).filter(k => k.venue !== "data").length} venues)`;
    return;
  }
  let total = 0, cash = 0;
  pf.accounts.forEach(a => { total += a.liquidation || 0; cash += a.cash || 0; });
  $("#pf-total").textContent = fmtUSD(total);
  const pe = $("#pf-pnl"); pe.textContent = cash ? `${fmtUSD(cash)} in dollars` : ""; pe.className = "peq-ret";
  if (meta) meta.textContent = (pf.t ? "updated " + fmtTime(pf.t) + " · read-only, no trading" : "") +
    (state.metrics && !state.metrics.trusted ? " · model warming up" : "");
  root.innerHTML = pf.accounts.map(a => {
    const perp = a.perp_notional ? ` <small>${a.perp_notional < 0 ? "short" : "long"} ${fmtUSD(Math.abs(a.perp_notional))} of perps</small>` : "";
    const head = `<div class="pf-acct"><span class="pf-name">${esc(a.name)}${a.venue ? ` <span class="tag">${esc(VENUE_WORD[a.venue] || a.venue)}</span>` : ""}</span><span class="pf-id">${a.cash != null ? fmtUSD(a.cash) + " cash" : esc(a.id || "")}${perp}</span><span class="pf-liq">${fmtUSD(a.liquidation || 0)}</span></div>`;
    if (a.error) return `<div class="block pf-block">${head}<div class="pf-rows"><div class="why">${esc(a.error)}</div></div></div>`;
    const rows = (a.positions || []).map(p => {
      const L = state.latest[p.symbol];
      let sig = `<span class="nm">not modeled</span>`;
      if (L && !L.trusted) {
        sig = `<span class="warm">warming up</span>`;
      } else if (L) {
        const act = L.action || "HOLD";
        const pu = L.p_up != null ? Math.round(L.p_up * 100) : "·";
        const pd = L.p_down != null ? Math.round(L.p_down * 100) : "·";
        const q50 = L.q ? fmtBps(L.q[2]) : "·";
        const bull = (L.p_up || 0) > (L.p_down || 0);
        const align = act === "HOLD" ? "" : (bull ? `<span class="al ok">aligned</span>` : `<span class="al warn">counter</span>`);
        sig = `<span class="badge ${act.toLowerCase()}">${act}</span> <span class="up">${pu}%↑</span> <span class="down">${pd}%↓</span> <span class="q">${q50}bps</span> ${align}`;
      }
      const isPerp = p.kind === "perp";
      const qty = isPerp ? `${p.qty < 0 ? "short " : "long "}${fmtQty(Math.abs(p.qty))}${p.leverage ? ` <small>${p.leverage}x</small>` : ""}` : `${fmtQty(p.qty)}${p.hold ? ` <small>${fmtQty(p.hold)} held</small>` : ""}`;
      const val = isPerp ? `${fmtUSD(Math.abs(p.value))}${p.upnl ? ` <small class="${p.upnl >= 0 ? "up" : "down"}">${p.upnl >= 0 ? "+" : ""}${fmtUSD(p.upnl)}</small>` : ""}` : (p.marked ? fmtUSD(p.value) : `<span class="nm">unpriced</span>`);
      const px = p.marked && p.price != null ? fmtPrice(p.price) + (isPerp && p.entry ? ` <small>in at ${fmtPrice(p.entry)}</small>` : "") : "·";
      return `<div class="pf-row${isPerp ? " perp" : ""}"><span class="psym tk" data-sym="${esc(p.symbol)}">${esc(p.asset || base(p.symbol))}${isPerp ? ` <span class="tag">perp</span>` : ""}</span>` +
        `<span>${qty}</span><span>${val}</span><span>${px}</span>` +
        `<span class="pf-sig">${sig}</span></div>`;
    }).join("");
    return `<div class="block pf-block">${head}<div class="pf-rows"><div class="pf-row phead"><span>asset</span><span>qty</span><span>value</span><span>price</span><span>Steel</span></div>${rows || `<div class="why">no balances</div>`}</div></div>`;
  }).join("");
}

let paperTradesAll = false;
const paperSort = {};   // table -> { col, dir }; a header click sorts, a second click flips
document.addEventListener("click", e => {
  const h = e.target.closest && e.target.closest(".hsort[data-table]"); if (!h) return;
  const t = h.dataset.table, c = h.dataset.col, st = paperSort[t];
  paperSort[t] = st && st.col === c ? { col: c, dir: -st.dir } : { col: c, dir: c === "sym" || c === "label" ? 1 : -1 };
  renderPaper();
});
document.addEventListener("DOMContentLoaded", () => { const b = $("#p-tr-more"); if (b) b.onclick = () => { paperTradesAll = !paperTradesAll; renderPaper(); }; });
// Fills from before this process started are an earlier session's: the book persists across
// restarts, and an old loss must not read as today's. Dim them, and mark the boundary once.
function tradeRows(rows) {
  const started = (state.status || {}).started || 0;
  let divided = false;
  return rows.map((t, i) => {
    const earlier = t.t < started;
    let divider = "";
    if (earlier && !divided) {
      divided = true;
      const prevEarlier = i > 0 && rows[i - 1].t < started;
      if (!prevEarlier) divider = `<div class="prow ptr pdivider"><span>earlier sessions</span></div>`;
    }
    return divider + `<div class="prow ptr${earlier ? " earlier" : ""}"><span>${fmtTime(t.t)}</span>` +
      `<span class="${tradeUp(t.side) ? "up" : "down"}"${t.note ? ` title="${esc(t.note)}"` : ""}>${esc(t.label)}</span>` +
      `<span class="psym tk" data-sym="${esc(t.sym)}">${esc(base(t.sym))}</span><span class="c-qty">${fmtQty(t.shares)}</span><span class="c-price">${fmtPrice(t.price)}</span><span>${t.notional != null ? fmtUSD(t.notional) : ""}</span></div>`;
  }).join("");
}

function renderPaper() {
  const p = state.paper; if (!p || !$("#p-stats")) return;
  paintPaperEquity();
  $("#p-stats").innerHTML = [
    ["cash", fmtUSD(p.cash)], ["invested", fmtUSD(p.gross)], ["net exposure", fmtUSD(p.net_exposure)],
    ["realized", fmtUSD(p.realized)], ["unrealized", fmtUSD(p.unrealized)], ["fees", fmtUSD(p.fees)], ["perp fees", fmtUSD(p.perp_fees || 0)],
    ["funding net", fmtUSD(p.funding_net || 0)], ["spread paid", fmtUSD(p.spread_cost || 0)], ["trades", p.n_trades], ["liquidations", p.n_liquidations || 0],
    ...((state.status && state.status.fees) ? [["fee tiers", { html: Object.entries(state.status.fees).map(([cls, f]) => `${esc(f.venue)} ${cls === "perp" ? f.perp_taker_bps : f.spot_taker_bps}/${cls === "perp" ? f.perp_maker_bps : f.spot_maker_bps} bps <small>${f.live ? "live tier" : "table"}</small>`).join("<br>") }]] : []),
    ...(p.venues && p.venues.split ? [["collateral", { html: `${fmtUSD(p.collateral || 0)} <small>${esc(p.venues.perp)}</small>` }], ["in flight", { html: (p.in_flight || []).length ? p.in_flight.map(f => `${fmtUSD(f.amount)} → ${esc(f.venue)} <small>${esc(f.rail)} · ${Math.ceil(f.lands_in_s / 60)} min</small>`).join("<br>") : "·" }], ["transfer fees", { html: `${fmtUSD(p.transfer_fees || 0)} <small>${p.n_transfers || 0} moves · ${p.transfer_wait_min || 0} min waited</small>` }]] : []),
    ["Sharpe", (p.sharpe || 0).toFixed(2)], ["max drawdown", (p.max_drawdown || 0).toFixed(1) + "%"],
  ].map(([k, v]) => `<div class="pstat"><label>${esc(k)}</label><b>${v && v.html != null ? v.html : esc(String(v))}</b></div>`).join("");
  const all = p.positions || [];
  // gain on cost: a spot position against what it cost, a short against the margin it posted
  const gain = x => { const cost = x.kind !== "spot" ? (x.risk || 0) : (x.value || 0) - (x.upnl || 0); return cost ? (x.upnl / Math.abs(cost)) * 100 : 0; };
  const pos = all.filter(x => x.kind === "spot").map(x => ({ ...x, gain: gain(x) }));
  const shorts = all.filter(x => x.kind === "short" || x.kind === "long").map(x => ({ ...x, gain: gain(x) }));
  const srt = (rows, key) => { const st = paperSort[key]; if (!st) return rows;
    return rows.slice().sort((a, b) => { const va = a[st.col], vb = b[st.col]; const r = typeof va === "number" || typeof vb === "number" ? (va || 0) - (vb || 0) : String(va || "").localeCompare(String(vb || "")); return st.dir * r; }); };
  const head = (table, cols) => `<div class="prow ${table === "trades" ? "ptr" : table === "shorts" ? "pshort" : ""} phead">` +
    cols.map(([col, label, cls]) => { const st = paperSort[table]; const on = st && st.col === col;
      return `<span class="hsort${on ? " on" : ""}${cls ? " " + cls : ""}" data-table="${table}" data-col="${col}">${label}${on ? (st.dir > 0 ? " ▲" : " ▼") : ""}</span>`; }).join("") + `</div>`;
  const pnlCell = x => `<span class="${x.upnl >= 0 ? "up" : "down"} c-unreal">${x.upnl >= 0 ? "+" : ""}${fmtUSD(x.upnl)}</span>`;
  const gainCell = x => `<span class="${x.gain >= 0 ? "up" : "down"} c-gain">${x.gain >= 0 ? "+" : ""}${x.gain.toFixed(1)}%</span>`;
  $("#p-pos-meta").textContent = pos.length ? `${pos.length} open` : "";
  $("#p-positions").innerHTML = pos.length
    ? head("positions", [["sym", "sym"], ["shares", "qty"], ["weight", "weight", "c-weight"], ["value", "value", "c-value"], ["upnl", "unrealized", "c-unreal"], ["gain", "gain", "c-gain"]]) +
      srt(pos, "positions").map(x => `<div class="prow"><span class="psym tk" data-sym="${esc(x.sym)}">${esc(base(x.sym))}</span>` +
        `<span class="up" title="spot, bought at ${fmtPrice(x.avg)}">${fmtQty(x.shares)}</span>` +
        `<span class="c-weight">${(x.weight * 100).toFixed(1)}%</span><span class="c-value">${fmtUSD(x.value)}</span>${pnlCell(x)}${gainCell(x)}</div>`).join("")
    : `<div class="why">nothing held — all cash${state.metrics && !state.metrics.trusted ? " (model still warming up)" : ""}</div>`;
  $("#p-opt-meta").textContent = shorts.length ? `${shorts.length} open, loss capped at the margin` : (p.long_mode === "perp" ? "longs and shorts, isolated margin" : "");
  $("#p-options").innerHTML = shorts.length
    ? head("shorts", [["sym", "sym"], ["avg", "entry → liq"], ["notional", "notional", "c-weight"], ["risk", "margin", "c-value"], ["funding", "funding", "c-fund"], ["upnl", "unrealized", "c-unreal"], ["gain", "gain", "c-gain"]]) +
      srt(shorts, "shorts").map(x => `<div class="prow pshort"><span class="psym tk" data-sym="${esc(x.sym)}">${esc(base(x.sym))} <small>${x.kind}</small></span>` +
        `<span class="${x.kind === "short" ? "down" : "up"}" title="isolated-margin perp ${x.kind} of ${fmtQty(x.shares)}: ${x.liq > 0 ? `liquidated at ${fmtPrice(x.liq)}, losing the margin and no more` : "no liquidation price at 1x; the margin is the worst case"}">${fmtPrice(x.avg)} → ${x.liq > 0 ? fmtPrice(x.liq) : "—"} <small>${x.leverage}x</small></span>` +
        `<span class="c-weight">${fmtUSD(x.notional)}</span><span class="c-value">${fmtUSD(x.risk)}</span>` +
        `<span class="c-fund ${(x.funding || 0) >= 0 ? "up" : "down"}">${(x.funding || 0) >= 0 ? "+" : ""}${fmtUSD(x.funding || 0)}</span>${pnlCell(x)}${gainCell(x)}</div>`).join("")
    : `<div class="why">no perps open</div>`;
  const tr = (p.trades || []).map(t => ({ ...t, label: tradeLabel(t) }));
  $("#p-tr-meta").textContent = p.n_trades ? `${p.n_trades} total, scroll for more` : "";
  const more = $("#p-tr-more"); if (more) more.hidden = true;
  $("#p-trades").innerHTML = tr.length
    ? head("trades", [["t", "time"], ["label", "action"], ["sym", "sym"], ["shares", "qty", "c-qty"], ["price", "price", "c-price"], ["notional", "value"]]) +
      tradeRows(srt(tr, "trades"))
    : `<div class="why">no trades yet${state.metrics && !state.metrics.trusted ? " — the model is still warming up" : ""}</div>`;
  drawEquityCurve(p);
}

function drawEquityCurve(p) {
  const cv = $("#p-curve"); if (!cv) return;
  const { ctx, w, h } = ctx2d(cv);
  const pts = p.curve || [];
  ctx.font = "10px ui-monospace, monospace";
  if (pts.length < 2) { ctx.fillStyle = C.muted; ctx.font = "12px system-ui"; ctx.fillText("building equity curve…", 12, 24); return; }
  const padL = 58, padR = 12, padT = 12, padB = 22;
  const eqs = pts.map(x => x.eq);
  let lo = Math.min(...eqs, p.start), hi = Math.max(...eqs, p.start);
  const sp = (hi - lo) || p.start * 0.001; lo -= sp * 0.08; hi += sp * 0.08;
  const xs = i => padL + i / (pts.length - 1) * (w - padL - padR);
  const ys = v => padT + (hi - v) / (hi - lo) * (h - padT - padB);
  ctx.strokeStyle = C.grid; ctx.fillStyle = C.muted; ctx.lineWidth = 1; ctx.textBaseline = "middle";
  for (let k = 0; k < 3; k++) { const v = lo + (hi - lo) * (0.12 + 0.38 * k), y = Math.round(ys(v)) + 0.5;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke(); ctx.fillText(fmtUSD(v), 6, y); }
  const y0 = ys(p.start); ctx.setLineDash([4, 3]); ctx.strokeStyle = C.muted;
  ctx.beginPath(); ctx.moveTo(padL, y0); ctx.lineTo(w - padR, y0); ctx.stroke(); ctx.setLineDash([]);
  const up = eqs[eqs.length - 1] >= p.start;
  ctx.strokeStyle = up ? C.up : C.down; ctx.lineWidth = 2; ctx.lineJoin = "round";
  ctx.beginPath(); pts.forEach((x, i) => i ? ctx.lineTo(xs(i), ys(x.eq)) : ctx.moveTo(xs(i), ys(x.eq))); ctx.stroke();
  ctx.lineTo(xs(pts.length - 1), ys(lo)); ctx.lineTo(xs(0), ys(lo)); ctx.closePath();
  ctx.fillStyle = up ? C.upFill : C.downFill; ctx.fill();
}

function renderDislocations() {
  const d = state.dislocations, box = $("#dis-table"), st = $("#dis-stats"), meta = $("#dis-meta");
  if (!box || !st) return;
  if (!d || !d.overall) { st.innerHTML = ""; box.innerHTML = `<div class="why">waiting for two venues to quote the same name…</div>`; return; }
  const o = d.overall || {}, ms = v => v == null ? "·" : v >= 1000 ? (v / 1000).toFixed(1) + " s" : Math.round(v) + " ms";
  const venues = (d.venues || []).length;
  if (meta) meta.textContent = `${venues} venues quoting · ${d.total || 0} episodes so far · same dollar and kind only (USD, USDT and USDC books sit a basis apart)`;
  st.innerHTML = [["episodes kept", o.n || 0], ["median window", ms(o.window_ms_median)], ["p90 window", ms(o.window_ms_p90)], ["longest", ms(o.window_ms_max)],
    ["over 1 s", o.over_1s || 0], ["over 5 s", o.over_5s || 0], ["median peak", o.peak_bps_median != null ? o.peak_bps_median.toFixed(1) + " bps" : "·"], ["best peak", o.peak_bps_max != null ? o.peak_bps_max.toFixed(1) + " bps" : "·"],
    ["open now", (d.open || []).length]].map(([k, val]) => `<div class="pstat"><label>${esc(k)}</label><b>${esc(String(val))}</b></div>`).join("");
  const rows = [...(d.open || []).map(e => ({ ...e, live: true })), ...(d.recent || [])].slice(0, 40);
  box.innerHTML = `<div class="rrow head dis"><span>name</span><span>buy on</span><span>sell on</span><span>net peak</span><span>window</span><span>when</span></div>` +
    (rows.length ? rows.map(e => `<div class="rrow dis${e.live ? " live" : ""}"><span class="sym tk" data-sym="${esc(e.sym)}">${esc(base(e.sym))} <small>${esc(e.kind)}/${esc(e.quote)}</small></span>` +
      `<span>${esc(e.buy)}</span><span>${esc(e.sell)}</span><span class="chg up">+${(e.peak_bps || 0).toFixed(1)} bps <small>gross ${(e.gross_peak_bps || 0).toFixed(1)}</small></span>` +
      `<span>${ms(e.window_ms)}${e.live ? " <small>open</small>" : ""}</span><span>${fmtTime(e.start)}</span></div>`).join("")
      : `<div class="why">no dislocation yet: every venue's bid sits below every other venue's ask after fees</div>`);
  const pairs = Object.entries(d.pairs || {}).slice(0, 6);
  if (pairs.length) box.innerHTML += `<div class="rrow head dis"><span>pair</span><span>episodes</span><span>median window</span><span>p90</span><span>median peak</span><span>names</span></div>` +
    pairs.map(([k, p]) => `<div class="rrow dis"><span>${esc(k.replace(">", " → "))}</span><span>${p.n}</span><span>${ms(p.window_ms_median)}</span><span>${ms(p.window_ms_p90)}</span><span>${p.peak_bps_median != null ? p.peak_bps_median.toFixed(1) + " bps" : "·"}</span><span>${esc((p.names || []).map(base).join(", "))}</span></div>`).join("");
}

function renderMarket() {
  const m = state.signals && state.signals.market;
  const meta = $("#market-meta");
  if (!meta) return;
  if (!m || !m.t) { meta.textContent = "scanning…"; return; }
  meta.textContent = "updated " + fmtTime(m.t);
  const pct = v => v == null ? "·" : (v >= 0 ? "+" : "") + v.toFixed(2) + "%";
  const sig = state.signals || {}, fg = sig.fear_greed || {}, dv = sig.dvol || {};
  const stats = [
    `<div class="mstat regime ${m.regime === "risk-on" ? "on" : m.regime === "risk-off" ? "off" : "mixed"}">regime <b>${esc(m.regime || "·")}</b></div>`,
    `<div class="mstat">breadth <b>${m.breadth != null ? Math.round(m.breadth * 100) + "%" : "·"}</b> ${m.breadth_up || 0}/${m.breadth_n || 0} up</div>`,
    m.btc_dom ? `<div class="mstat">BTC dom <b>${m.btc_dom.toFixed(1)}%</b></div>` : "",
    m.total_mcap ? `<div class="mstat">total cap <b>$${(m.total_mcap / 1e12).toFixed(2)}T</b> <span class="${(m.mcap_chg24 || 0) >= 0 ? "up" : "down"}">${pct(m.mcap_chg24)}</span></div>` : "",
    m.stable_flow_1d != null ? `<div class="mstat">stablecoins <b class="${m.stable_flow_1d >= 0 ? "up" : "down"}">${pct(m.stable_flow_1d * 100)}</b> 1d</div>` : "",
    fg.value != null ? `<div class="mstat">fear/greed <b>${fg.value}</b> ${esc(fg.class || "")}</div>` : "",
    dv.BTC ? `<div class="mstat">DVOL <b>${dv.BTC.toFixed(0)}</b>${dv.ETH ? ` <small>ETH ${dv.ETH.toFixed(0)}</small>` : ""}</div>` : "",
  ];
  $("#market-stats").innerHTML = stats.join("");
  $("#sector-heat").innerHTML = (m.sectors || []).map(sc =>
    `<div class="sc tk" data-sym="${esc((sc.lead || "") + "-USD")}" data-cat="${esc(sc.name)}" data-chg="${sc.chg}" style="background:${heatColor(sc.chg)}" title="${esc(sc.name)}"><span>${esc(sc.name)}</span><b>${sc.chg >= 0 ? "+" : ""}${sc.chg.toFixed(1)}</b></div>`).join("");
  drawHeatmap(m);
}

// ---- movers heatmap: one squarified treemap of the whole radar, block area ~ dollar volume, colour = % change ----
let heatBlocks = [], heatRO = null;
function squarify(items, x, y, w, h, out) {   // items: [{v,...}] sorted desc by v; fills rect with strips of near-square blocks
  let i = 0;
  while (i < items.length && w > 0 && h > 0) {
    const total = items.slice(i).reduce((a, b) => a + b.v, 0);   // area is shared by what is still unplaced
    if (total <= 0) break;
    const horiz = w >= h;                        // lay the next strip along the shorter side
    const side = horiz ? h : w, span = horiz ? w : h;
    let j = i, sum = 0, worst = Infinity;
    for (; j < items.length; j++) {              // grow the strip while the worst aspect ratio improves
      const sum2 = sum + items[j].v, len = (sum2 / total) * span;
      let wr = 0;
      for (let k = i; k <= j; k++) { const b = (items[k].v / sum2) * side; wr = Math.max(wr, Math.max(len / b, b / len)); }
      if (wr > worst && j > i) break;
      worst = wr; sum = sum2;
    }
    const len = (sum / total) * span;
    let off = 0;
    for (let k = i; k < j; k++) {
      const b = (items[k].v / sum) * side;
      out.push(horiz ? { ...items[k], x, y: y + off, w: len, h: b } : { ...items[k], x: x + off, y, w: b, h: len });
      off += b;
    }
    if (horiz) { x += len; w -= len; } else { y += len; h -= len; }
    i = j;
  }
}
function drawHeatmap(m) {
  const host = $("#market-movers"); if (!host) return;
  let cv = $("canvas", host);
  if (!cv) {
    host.innerHTML = ""; cv = document.createElement("canvas"); cv.className = "heatmap"; host.appendChild(cv);
    cv.addEventListener("mousemove", e => {
      const b = heatBlocks.find(k => e.offsetX >= k.x && e.offsetX < k.x + k.w && e.offsetY >= k.y && e.offsetY < k.y + k.h);
      if (!b) { hideTooltip(); cv.style.cursor = "default"; return; }
      cv.style.cursor = "pointer";
      showTooltip(e.clientX, e.clientY, `${b.base || base(b.symbol)}  ${b.name || ""}\n${b.chg >= 0 ? "+" : ""}${b.chg.toFixed(2)}% 24h${b.chg1h != null ? `  ·  ${b.chg1h >= 0 ? "+" : ""}${b.chg1h.toFixed(2)}% 1h` : ""}  ·  ${fmtPrice(b.price)}\n$${(b.dv / 1e6).toFixed(1)}M traded${b.mcap ? `  ·  cap $${(b.mcap / 1e9).toFixed(1)}B` : ""}  ·  ${b.sector || b.cat}${b.funding != null ? `  ·  funding ${fmtRate(b.funding, 3)}` : ""}` + "\nclick for details");
    });
    cv.addEventListener("mouseleave", hideTooltip);
    cv.addEventListener("click", e => {
      const b = heatBlocks.find(k => e.offsetX >= k.x && e.offsetX < k.x + k.w && e.offsetY >= k.y && e.offsetY < k.y + k.h);
      if (b) openDetail(b.symbol);
    });
    if (!heatRO) { heatRO = new ResizeObserver(() => { const mm = state.signals && state.signals.market; if (mm) drawHeatmap(mm); }); heatRO.observe(host); }
  }
  const rows = (m.radar || []).filter(r => r.chg != null && r.vol > 0 && r.price > 0);
  if (!cv.clientWidth || !rows.length) return;
  const { ctx, w, h } = ctx2d(cv);
  ctx.clearRect(0, 0, w, h);
  const cap = 400;                                                     // upper bound; the label test below decides how many actually show
  const seen = new Set();
  const area = r => Math.pow(r.mcap > 0 ? r.mcap : r.price * r.vol * 40, 0.6);   // area ~ market cap (fallback: dollar volume, scaled toward cap)
  const all = rows.filter(r => !seen.has(r.symbol) && seen.add(r.symbol))       // one block per name
    .map(r => ({ ...r, v: area(r), dv: r.dollar_volume != null ? r.dollar_volume : r.price * r.vol, group: r.sector || "Other" })).sort((a, b) => b.v - a.v).slice(0, cap);
  const grouped = all.some(r => r.sector);
  const minW = 34, minH = 22, lh = grouped ? 15 : 0;
  // sectors first (when the scan knows them), names inside each sector; then show only as many names as
  // can carry a label: binary-search the largest prefix (biggest first) whose every block fits its ticker
  const layout = n => {
    const items = all.slice(0, n), out = [];
    if (!grouped) { squarify(items, 0, 0, w, h, out); return out; }
    const by = {}; items.forEach(it => (by[it.group] = by[it.group] || []).push(it));
    const groups = Object.entries(by).map(([g, it]) => ({ group: g, items: it, v: it.reduce((a, b) => a + b.v, 0) })).sort((a, b) => b.v - a.v);
    const laid = []; squarify(groups, 0, 0, w, h, laid);
    for (const g of laid) { const inner = []; squarify(g.items, g.x + 1, g.y + lh, Math.max(0, g.w - 2), Math.max(0, g.h - lh - 1), inner); out.push(...inner); }
    out.frames = laid;
    return out;
  };
  const fits = n => { const out = layout(n); return out.every(b => b.w >= minW && b.h >= minH) ? out : null; };
  let lo = 1, hi = all.length, best = fits(1) || layout(1);
  while (lo < hi) { const mid = Math.ceil((lo + hi) / 2); const out = fits(mid); if (out) { best = out; lo = mid; } else hi = mid - 1; }
  heatBlocks = best;
  const mono = getComputedStyle(document.body).getPropertyValue("--mono") || "monospace";
  const color = c => { const t = Math.max(-1, Math.min(1, c / 5)); const a = 0.22 + 0.7 * Math.abs(t);
    return t >= 0 ? `rgba(${C.heatUp},${a})` : `rgba(${C.heatDown},${a})`; };
  for (const b of heatBlocks) {
    ctx.fillStyle = color(b.chg); ctx.fillRect(b.x, b.y, Math.max(0, b.w - 1), Math.max(0, b.h - 1));
    {
      const big = b.w > 62 && b.h > 34;
      ctx.fillStyle = C.onColor; ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.font = `${big ? 600 : 500} ${big ? 12 : 10}px ${mono}`;
      ctx.fillText(b.base || base(b.symbol), b.x + b.w / 2, b.y + b.h / 2 - (big ? 7 : 0), b.w - 6);
      if (big) { ctx.font = `500 10.5px ${mono}`; ctx.fillStyle = "rgba(255,255,255,0.8)"; ctx.fillText(`${b.chg >= 0 ? "+" : ""}${b.chg.toFixed(1)}%`, b.x + b.w / 2, b.y + b.h / 2 + 8, b.w - 6); }
    }
  }
  for (const g of (heatBlocks.frames || [])) {            // category frames and labels
    ctx.fillStyle = "rgba(0,0,0,0.42)"; ctx.fillRect(g.x, g.y, g.w, lh);
    ctx.fillStyle = "#d8d7cd"; ctx.textAlign = "left"; ctx.textBaseline = "middle"; ctx.font = "600 10px system-ui, sans-serif";
    ctx.fillText(g.group.toUpperCase(), g.x + 6, g.y + lh / 2, Math.max(0, g.w - 10));
    ctx.strokeStyle = "rgba(0,0,0,0.7)"; ctx.lineWidth = 1; ctx.strokeRect(g.x + 0.5, g.y + 0.5, g.w - 1, g.h - 1);
  }
}

function renderBriefCtl() {
  const wrap = $("#brief-toggle-wrap");
  if (wrap && !wrap.querySelector(".switch")) {
    const sw = document.createElement("label"); sw.className = "switch";
    sw.innerHTML = `<input type="checkbox"><span class="track"></span><span class="thumb"></span>`;
    sw.querySelector("input").onchange = e => control({ action: "toggle_brief", on: e.target.checked });
    wrap.appendChild(sw);
  }
  if (wrap) { const inp = wrap.querySelector("input"); if (inp) inp.checked = state.controls.brief_enabled !== false; }
  const el = $("#brief-status"); if (!el) return;
  const b = state.brief || {}, models = b.models || {};
  let status;
  if (b.generating) status = `<span class="bstat-run">writing\u2026</span> local analysts + writer running`;
  else if (b.error) status = `<span class="bstat-err">${esc(b.error)}</span>`;
  else if (b.text) status = `last written ${b.t ? fmtTime(b.t) : ""} \u00b7 <b>${esc(models.big || "?")}</b> from <b>${esc(models.small || "?")}</b> analysts`;
  else status = `no brief yet \u2014 click \u201cWrite now\u201d`;
  el.innerHTML = status;
}

function renderSignals() {
  const list = $("#signal-list");
  if (!list) return;
  list.innerHTML = "";
  state.signal_providers.forEach(src => {
    const el = document.createElement("div");
    el.className = "source" + (src.enabled ? "" : " off");
    el.innerHTML = `<div></div><div><div class="sname">${esc(src.name)}</div>` +
      (src.status ? `<div class="snote">${esc(src.status)}</div>` : "") + `</div>`;
    el.firstChild.replaceWith(sourceToggle("signal", src.id, src.enabled, false, ""));
    list.appendChild(el);
  });
  // crowd overlay master toggle
  const wrap = $("#overlay-toggle-wrap");
  if (wrap && !wrap.querySelector(".switch")) {
    const sw = document.createElement("label");
    sw.className = "switch";
    sw.innerHTML = `<input type="checkbox"><span class="track"></span><span class="thumb"></span>`;
    sw.querySelector("input").onchange = e => control({ action: "overlay", on: e.target.checked });
    wrap.appendChild(sw);
  }
  if (wrap) { const inp = wrap.querySelector("input"); if (inp) inp.checked = !!(state.overlay && state.overlay.enabled); }

  const sig = state.signals;
  const assets = $("#signal-assets");
  if (sig && sig.per_asset) {
    assets.innerHTML = state.config.symbols.map(sym => {
      const pa = sig.per_asset[sym] || {}; const f = pa.feat || {};
      const crowd = (sig.crowding || {})[sym] || 0;
      const sgn = v => (v == null ? "" : v >= 0 ? "pos" : "neg");
      const fmt = v => v == null ? "·" : (v >= 0 ? "+" : "") + Number(v).toFixed(2);
      return `<div class="sig-asset">
        <div class="sh"><b>${esc(base(sym))}</b><span class="crowd ${crowd > 0.15 ? "hot" : crowd < -0.15 ? "cold" : ""}">crowding ${fmt(crowd)}</span></div>
        <div class="rows">
          <span>reddit <b>${pa.mentions || 0}</b> mentions</span><span>reddit tone <b class="${sgn(f.social_sent)}">${fmt(f.social_sent)}</b></span>
          <span>funding <b class="${sgn(pa.funding_rate)}">${fmtRate(pa.funding_rate)}</b>/8h</span><span>OI chg <b class="${sgn(pa.oi_chg_raw)}">${fmtRate(pa.oi_chg_raw, 1)}</b></span>
          <span>long/short <b>${pa.longshort_raw != null ? Number(pa.longshort_raw).toFixed(2) : "·"}</b></span><span>basis <b class="${sgn(pa.basis_raw)}">${fmtRate(pa.basis_raw, 3)}</b></span>
          <span>DVOL <b>${pa.dvol_raw != null ? Number(pa.dvol_raw).toFixed(0) : "·"}</b></span>
        </div></div>`;
    }).join("");
  } else if (assets) assets.innerHTML = `<div class="why">signals not gathered yet</div>`;
}

function renderNews() {
  const n = state.news;
  if (!n || !n.t) { $("#news-meta").textContent = "not run yet"; return; }
  const pa = Object.entries(n.per_asset || {});
  const covered = pa.filter(([, a]) => a.mentions > 0)
    .sort((x, y) => y[1].mentions - x[1].mentions || Math.abs(y[1].sentiment) - Math.abs(x[1].sentiment));
  const quiet = pa.length - covered.length;
  const gen = (n.headlines || []).filter(h => !h.assets || !h.assets.length);
  const tone = gen.length ? gen.reduce((s, h) => s + h.sentiment, 0) / gen.length : 0;
  $("#news-meta").textContent = `${fmtTime(n.t)} via ${n.method}, ${n.headlines.length} headlines (${n.new} new), ${covered.length} of ${pa.length} names covered${n.scorer ? ` · tone by ${n.scorer}` : ""}`;
  // one row per covered name: tone bar and count, with its leading headline underneath (linked when it is in the list)
  const byTitle = new Map((n.headlines || []).map(h => [h.title, h.url]));
  $("#tone").innerHTML = covered.slice(0, 16).map(([s, a]) => {
    const v = a.sentiment, w = Math.abs(v) * 50, left = v >= 0 ? 50 : 50 - w;
    const top = (a.top || [])[0], url = top && byTitle.get(top);
    return `<div class="a" title="tone ${v.toFixed(2)} (${v > 0.15 ? "bullish" : v < -0.15 ? "bearish" : "mixed"}), attention ${a.attention.toFixed(2)}, ${a.mentions} headline${a.mentions === 1 ? "" : "s"}">` +
      `<span class="sym">${esc(base(s))}</span><div class="bar"><i style="left:${left}%;width:${w}%;background:${v >= 0 ? "var(--good)" : "var(--serious)"}"></i></div><small>${a.mentions}</small>` +
      (top ? `<div class="story">${url ? `<a href="${esc(safeUrl(url))}" target="_blank" rel="noopener noreferrer">${esc(top)}</a>` : esc(top)}</div>` : "") + `</div>`;
  }).join("") +
    `<div class="why tone-foot">${covered.length ? "tone −1 to +1 · count: headlines naming it" : "no name had direct coverage this pass"}` +
    `${quiet ? ` · ${quiet} names without coverage` : ""} · market tone ${tone >= 0 ? "+" : ""}${tone.toFixed(2)} over ${gen.length} general headlines</div>`;
  $("#headlines").innerHTML = n.headlines.slice(0, 60).map(h =>
    `<li class="${h.new ? "new" : ""}"><span class="s ${h.sentiment > 0 ? "pos" : h.sentiment < 0 ? "neg" : ""}">${h.sentiment >= 0 ? "+" : ""}${h.sentiment.toFixed(2)}</span>` +
    (h.assets.length ? h.assets : ["mkt"]).map(a => `<span class="tag">${esc(a)}</span>`).join("") +
    `<a href="${esc(safeUrl(h.url))}" target="_blank" rel="noopener noreferrer">${esc(h.title)}</a><span class="src">${esc(h.source)}</span></li>`).join("");
}

// Transport ------------------------------------------------------------------------

function handle(msg) {
  switch (msg.type) {
    case "snapshot":
      if (pendingDetail) setTimeout(() => { const p = pendingDetail; pendingDetail = null; openDetail(p, false); }, 0);
      Object.assign(state, { config: msg.config, status: msg.status, controls: msg.controls, prices: msg.prices, bars: msg.bars,
        latest: msg.latest, gate: msg.gate, outcomes: msg.outcomes, metrics: msg.metrics, history: msg.history, log: msg.log, news: msg.news,
        sources: msg.sources || [], news_sources: msg.news_sources || [], providers: (msg.status && msg.status.providers) || {}, classes: msg.classes || {},
        signals: msg.signals || null, signal_providers: msg.signal_providers || [], brief: msg.brief || null, paper: msg.paper || null, portfolio: msg.portfolio || null, dislocations: msg.dislocations || null, execution: msg.execution || null, live: msg.live || null, arb: msg.arb || null, vault: msg.vault || null, identity: msg.identity || null, backtest: msg.backtest || null, overlay: msg.overlay || { enabled: true }, keys: msg.keys || [], muted: msg.muted || [], universe: msg.universe || (msg.config ? msg.config.symbols : []), holds: msg.holds || [] });
      buildCards(); buildConsoles(); buildControls(); applyMuted();
      Object.values(msg.trace || {}).forEach(evs => evs.forEach(ev => appendTrace(ev, false)));
      Object.values(consoles).forEach(c => { c.body.scrollTop = c.body.scrollHeight; });
      { const t = $("#term"); if (t) t.innerHTML = ""; }
      Object.values(msg.trace || {}).flat().sort((a, b) => (a.seq || a.t) - (b.seq || b.t)).forEach(appendTerm);
      { const t = $("#term"); if (t) t.scrollTop = t.scrollHeight; }
      renderAll();
      requestAnimationFrame(() => requestAnimationFrame(redrawCharts));
      onboardStart();
      break;
    case "backtest":
      state.backtest = msg.backtest;
      if (vis("backtest")) renderBacktest();
      break;
    case "vault":
      state.vault = msg.vault; renderVault();
      break;
    case "identity":
      state.identity = msg.identity;
      break;
    case "live":
      state.live = msg.live; if (vis("exec")) renderLive();
      break;
    case "arb":
      state.arb = msg.arb; if (vis("exec")) renderArb();
      break;
    case "handoff":
      if (state.status) state.status.handoff = msg.handoff;
      if (vis("exec")) renderExec();
      break;
    case "execution":
      state.execution = msg.execution;
      if (vis("exec")) renderExec();
      break;
    case "dislocations":
      state.dislocations = msg.dislocations;
      if (vis("dislocations")) renderDislocations();
      break;
    case "portfolio":
      state.portfolio = msg.portfolio;
      if (vis("portfolio")) renderPortfolio();
      break;
    case "tick":
      state.prices = msg.prices; state.status = msg.status; state.providers = msg.status.providers || state.providers; renderStatus();
      if (msg.paper_eq != null && state.paper) { state.paper.equity = msg.paper_eq; if (vis("paper")) paintPaperEquity(); }
      if (vis("cards")) { state.config.symbols.forEach(updatePrice); scheduleDraw(); }
      break;
    case "bar":
      state.status = msg.status;
      Object.entries(msg.bars).forEach(([s, b]) => { (state.bars[s] ||= []).push(b); if (state.bars[s].length > 240) state.bars[s].shift(); });
      state.latest = msg.latest; state.gate = msg.gate; state.metrics = msg.metrics; state.outcomes = msg.outcomes; state.log = msg.log; if (msg.paper) state.paper = msg.paper;
      state.providers = (msg.status && msg.status.providers) || state.providers;
      absorbHistory(msg.history);
      renderAll();
      checkAlerts();
      break;
    case "metrics":
      state.metrics = msg.metrics; absorbHistory(msg.history);
      updateLoading();
      if (vis("tiles")) { renderTiles(); renderSparks(); }
      break;
    case "sources": state.sources = msg.sources; if (msg.status) { state.status = msg.status; state.providers = msg.status.providers || state.providers; } renderSources(); renderStatus(); if (vis("cards")) state.config.symbols.forEach(updateVia); break;
    case "news_sources": state.news_sources = msg.news_sources; renderSources(); break;
    case "signals": state.signals = msg.signals; if (msg.signal_providers) state.signal_providers = msg.signal_providers; renderSignals(); renderMarket(); renderRadar(); renderWatch(); renderBrief(); if (vis("cards")) { state.config.symbols.forEach(updateCard); reorderCards(true); } break;
    case "signal_providers": state.signal_providers = msg.signal_providers; renderSignals(); break;
    case "keys": state.keys = msg.keys; renderKeys(); break;
    case "muted": state.muted = msg.muted; applyMuted(); renderUniverse(); break;
    case "trace": appendTrace(msg.ev); appendTerm(msg.ev); break;
    case "brief": state.brief = msg.brief; if (vis("brief")) renderBrief(); if (vis("briefctl")) renderBriefCtl(); break;
    case "news": state.news = msg.news; renderNews(); break;
    case "status": state.status = msg.status; state.providers = msg.status.providers || state.providers; renderStatus(); if (vis("cards")) state.config.symbols.forEach(updateVia); break;
    case "controls": state.controls = msg.controls; state.metrics = msg.metrics; if (msg.sources) state.sources = msg.sources; if (msg.news_sources) state.news_sources = msg.news_sources; if (msg.signal_providers) state.signal_providers = msg.signal_providers; if (msg.controls && "overlay" in msg.controls) state.overlay.enabled = msg.controls.overlay; syncControls(); renderSources(); renderSignals(); if (vis("tiles")) renderTiles(); break;
  }
}

function absorbHistory(h) {
  Object.entries(h || {}).forEach(([k, pts]) => {
    const arr = state.history[k] ||= [];
    pts.forEach(p => { arr.push(p); if (arr.length > 240) arr.shift(); });
  });
}

// The sources list only arrives with the snapshot and on a toggle; the venues' tick counts and
// freshness move all the time, so poll it: the header pill and the Sources panel stay current.
async function refreshSources() {
  if (!wsUp) return;
  try {
    const r = await fetch("/api/sources"); if (!r.ok) return;
    const d = await r.json();
    if (d.sources) state.sources = d.sources;
    if (d.news_sources) state.news_sources = d.news_sources;
    if (d.providers) state.providers = d.providers;
    renderStatus();
    if (vis("sources")) renderSources();
  } catch (e) { /* the websocket carries the rest */ }
}
setInterval(refreshSources, 30000);

function connect() {
  ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws" + (opToken() ? "?token=" + encodeURIComponent(opToken()) : ""));
  ws.onopen = () => { retry = 1000; wsUp = true; renderStatus(); setTimeout(refreshSources, 3000); };
  // activity light on the market pill: its border flashes for 50 ms on every frame
  let blinkT = null;
  const blink = () => { const c = $("#market"); c.classList.add("busy"); clearTimeout(blinkT); blinkT = setTimeout(() => c.classList.remove("busy"), 140); };
  ws.onclose = () => { wsUp = false; renderStatus(); updateLoading(true); setTimeout(connect, retry); retry = Math.min(retry * 2, 15000); };
  ws.onerror = () => ws.close();
  ws.onmessage = e => { blink(); handle(JSON.parse(e.data)); };
  // bottom-left jump button: "past the cards" while the symbol grid fills the screen, "back to top"
  // once the reader is below it; hidden near the top of the page
  const skip = $("#skip-fab"), cards = $("#cards"), after = $("#market-watch");
  if (skip && cards && after) {
    let mode = "";
    const place = () => {
      const r = cards.getBoundingClientRect(), vh = window.innerHeight;
      const shown = cards.offsetParent !== null;
      let m = "";
      if (shown && r.top < vh && r.bottom > vh * 1.2) m = "skip";
      else if (window.scrollY > vh * 0.8) m = "top";
      if (m !== mode) { mode = m; skip.hidden = !m; skip.textContent = m === "top" ? "↑ Back to top" : "↓ Past the cards"; }
    };
    skip.onclick = () => {
      if (mode === "top") { window.scrollTo({ top: 0, behavior: "smooth" }); return; }
      const head = $(".top"); const pad = (head ? head.getBoundingClientRect().height : 0) + 14;   // land with the whole Market watch card visible
      window.scrollTo({ top: after.getBoundingClientRect().top + window.scrollY - pad, behavior: "smooth" });
    };
    addEventListener("scroll", place, { passive: true }); addEventListener("resize", place); place();
  }
}

function showTab(tab, updateHash) {
  tab = LEGACY_VIEW[tab] || tab;                       // a bookmark or a saved view from the old tab names
  const btn = $$(".tabs button").find(b => b.dataset.tab === tab && !b.hidden);
  if (!btn) return false;
  $$(".tabs button").forEach(x => x.classList.toggle("active", x === btn));
  document.body.dataset.view = tab;
  try { localStorage.setItem("steel.view", tab); } catch (e) { /* storage may be unavailable */ }
  if (updateHash && location.hash.split("/")[0].slice(1) !== tab) history.pushState(null, "", "#" + tab);   // a history entry per tab, no jump-to-anchor scroll
  if (updateHash) { if (detailSym) closeDetail(false); window.scrollTo(0, 0); }   // each tab starts at its top
  if (tab === "ops") { Object.values(consoles).forEach(c => { c.body.scrollTop = c.body.scrollHeight; }); setTermFollow(true); }
  renderAll();
  if (tab === "ops" || tab === "venues") setTimeout(() => { const a = document.activeElement; if (a && a.tagName === "INPUT") a.blur(); }, 0);
  return true;
}
$$(".tabs button").forEach(b => b.onclick = () => showTab(b.dataset.tab, true));
{ const b = $("#theme-toggle"); if (b) b.onclick = () => applyTheme(currentTheme() === "dark" ? "light" : "dark"); applyTheme(null);
  if (window.matchMedia) matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => applyTheme(null)); }
{ const b = $("#bt-run"); if (b) b.onclick = async () => { b.disabled = true; const r = await control({ action: "backtest" }); b.textContent = r && r.backtest ? r.backtest : "Run now"; setTimeout(() => { b.disabled = false; b.textContent = "Run now"; }, 4000); }; }
// routes: #<tab> or #<tab>/<SYMBOL> (symbol detail open). Back/forward and refresh replay them.
function route() {
  const [t, sym] = location.hash.slice(1).split("/");
  if (t) showTab(t, false);
  if (sym) openDetail(decodeURIComponent(sym), false); else if (detailSym) closeDetail(false);
}
window.addEventListener("hashchange", route);
window.addEventListener("popstate", route);
// on load the URL hash wins, then the last-used view; dashboard is already active in the markup
(function () {
  let t = location.hash.slice(1);
  if (!t) { try { t = localStorage.getItem("steel.view") || ""; } catch (e) { t = ""; } }
  if (t) { const [tab, sym] = t.split("/"); const tb = LEGACY_VIEW[tab] || tab; if (tb !== "market") showTab(tb, false); if (sym) pendingDetail = decodeURIComponent(sym); history.replaceState(null, "", "#" + tb + (sym ? "/" + sym : "")); }
})();

document.addEventListener("click", e => { if (e.target.closest && (e.target.closest("#brief-regen") || e.target.closest("#brief-now"))) control({ action: "brief" }); });
document.addEventListener("click", async e => {
  const b = e.target.closest && e.target.closest("#exec-kill"); if (!b) return;
  if (b.dataset.action === "kill" && !confirm("Kill switch: refuse every order until lifted by hand. Continue?")) return;
  const r = await api("/api/execution", { action: b.dataset.action });
  const res = await r.json(); if (res.execution) { state.execution = res.execution; renderExec(); }
});
{ const c = $("#onb-close"); if (c) c.onclick = onboardClose; }
{ const r = $("#run-setup"); if (r) r.onclick = () => onboardStart(true); }
window.addEventListener("resize", () => renderAll());
setInterval(() => {
  if (state.status && state.status.started) $("#uptime").textContent = fmtDur(Date.now() / 1000 - state.status.started);
  const now = Date.now() / 1000;
  Object.values(consoles).forEach(c => { while (c.times.length && c.times[0] < now - 60) c.times.shift(); c.rate.textContent = `${c.times.length}/min`; });
}, 1000);
document.addEventListener("input", e => { if (e.target && e.target.id === "radar-filter") renderRadar(); if (e.target && e.target.id === "watch-filter") renderWatch(); });
updateLoading();
const soundBtn = $("#sound-toggle");
if (soundBtn) {
  const paint = () => { soundBtn.checked = soundOn; };
  paint();
  soundBtn.onchange = () => {
    soundOn = soundBtn.checked;
    try { localStorage.setItem("steel.sound", soundOn ? "1" : "0"); } catch (e) { /* ignore */ }
    if (soundOn) { try { audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)(); if (audioCtx.state === "suspended") audioCtx.resume(); beep(880, 0.1); } catch (e) { /* ignore */ } }
    paint();
  };
}
if (document.fonts && document.fonts.ready) document.fonts.ready.then(redrawCharts).catch(() => {});
window.addEventListener("load", redrawCharts);
// ---- control panel: reorderable sections (drag the grip, or nudge with the arrows); saved per browser ----
(function () {
  const host = $("#consoles"); if (!host) return;
  const KEY = "steel.panel-order";
  const keyOf = el => el.id || [...el.classList].find(c => c !== "panel") || "section";
  const sections = () => [...host.children].filter(e => e.tagName === "SECTION");
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(sections().map(keyOf))); } catch (e) { /* storage may be unavailable */ } };
  const apply = () => {
    let order = null; try { order = JSON.parse(localStorage.getItem(KEY) || "null"); } catch (e) { order = null; }
    if (!Array.isArray(order)) return;
    const byKey = Object.fromEntries(sections().map(e => [keyOf(e), e]));
    order.forEach(k => { if (byKey[k]) host.appendChild(byKey[k]); });   // known keys in saved order; anything new stays at the end
  };
  const move = (el, dir) => {
    const sib = dir < 0 ? el.previousElementSibling : el.nextElementSibling;
    if (!sib || sib.tagName !== "SECTION") return;
    dir < 0 ? host.insertBefore(el, sib) : host.insertBefore(sib, el);
    save(); el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  };
  let dragging = null;
  sections().forEach(el => {
    const bar = document.createElement("div"); bar.className = "reorder";
    bar.innerHTML = `<button class="grip" title="drag to reorder" draggable="true">⋮⋮</button><button class="up" title="move up">▲</button><button class="down" title="move down">▼</button>`;
    el.appendChild(bar);
    $(".up", bar).onclick = () => move(el, -1);
    $(".down", bar).onclick = () => move(el, 1);
    const grip = $(".grip", bar);
    grip.addEventListener("dragstart", e => { dragging = el; el.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", keyOf(el)); });
    grip.addEventListener("dragend", () => { dragging = null; sections().forEach(s => s.classList.remove("dragging", "drop-before", "drop-after")); });
    el.addEventListener("dragover", e => {
      if (!dragging || dragging === el) return;
      e.preventDefault();
      const r = el.getBoundingClientRect(), before = e.clientY < r.top + r.height / 2;
      el.classList.toggle("drop-before", before); el.classList.toggle("drop-after", !before);
    });
    el.addEventListener("dragleave", () => el.classList.remove("drop-before", "drop-after"));
    el.addEventListener("drop", e => {
      if (!dragging || dragging === el) return;
      e.preventDefault();
      const r = el.getBoundingClientRect(), before = e.clientY < r.top + r.height / 2;
      host.insertBefore(dragging, before ? el : el.nextSibling);
      sections().forEach(s => s.classList.remove("drop-before", "drop-after"));
      save();
    });
  });
  apply();
})();

connect();
})();

