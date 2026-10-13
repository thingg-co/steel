// Classic indicators and structure layers for Steel's chart, computed client-side over the loaded candles.
// Pure functions from candles to series; chart.js draws them. No build step: a plain object on window.
(function () {
  const ema = (vals, n) => { const k = 2 / (n + 1); let e = null; return vals.map(v => (e = e == null ? v : v * k + e * (1 - k))); };
  const sma = (vals, n) => vals.map((_, i) => i + 1 < n ? null : vals.slice(i + 1 - n, i + 1).reduce((a, b) => a + b, 0) / n);
  const std = (vals, n) => vals.map((_, i) => { if (i + 1 < n) return null; const w = vals.slice(i + 1 - n, i + 1), m = w.reduce((a, b) => a + b, 0) / n; return Math.sqrt(w.reduce((a, b) => a + (b - m) ** 2, 0) / n); });
  function rsi(closes, n = 14) { const out = [], g = [], l = []; let ag = 0, al = 0; for (let i = 0; i < closes.length; i++) { const d = i ? closes[i] - closes[i - 1] : 0; const up = Math.max(d, 0), dn = Math.max(-d, 0);
      if (i <= n) { g.push(up); l.push(dn); ag = g.reduce((a, b) => a + b, 0) / Math.max(1, g.length); al = l.reduce((a, b) => a + b, 0) / Math.max(1, l.length); } else { ag = (ag * (n - 1) + up) / n; al = (al * (n - 1) + dn) / n; }
      out.push(i < n ? null : al === 0 ? 100 : 100 - 100 / (1 + ag / al)); } return out; }
  function macd(closes, f = 12, s = 26, sig = 9) { const a = ema(closes, f), b = ema(closes, s), m = a.map((v, i) => v - b[i]), sg = ema(m, sig); return { macd: m, signal: sg, hist: m.map((v, i) => v - sg[i]) }; }
  function atr(c, n = 14) { const tr = c.map((b, i) => i ? Math.max(b.h - b.l, Math.abs(b.h - c[i - 1].c), Math.abs(b.l - c[i - 1].c)) : b.h - b.l); return ema(tr, n); }
  function supertrend(c, n = 10, mult = 3) { const a = atr(c, n), out = []; let up = null, dn = null, dir = 1;
    c.forEach((b, i) => { const hl2 = (b.h + b.l) / 2, bu = hl2 + mult * a[i], bl = hl2 - mult * a[i]; const pu = up == null ? bu : (bu < up || c[i - 1].c > up ? bu : up), pl = dn == null ? bl : (bl > dn || c[i - 1].c < dn ? bl : dn);
      if (i) { if (dir > 0 && b.c < pl) dir = -1; else if (dir < 0 && b.c > pu) dir = 1; } up = pu; dn = pl; out.push({ line: dir > 0 ? pl : pu, dir }); }); return out; }
  function ichimoku(c, t = 9, k = 26, s = 52) { const mid = (i, n) => { if (i + 1 < n) return null; const w = c.slice(i + 1 - n, i + 1); return (Math.max(...w.map(x => x.h)) + Math.min(...w.map(x => x.l))) / 2; };
    const tenkan = c.map((_, i) => mid(i, t)), kijun = c.map((_, i) => mid(i, k)), spanA = tenkan.map((v, i) => v != null && kijun[i] != null ? (v + kijun[i]) / 2 : null), spanB = c.map((_, i) => mid(i, s)); return { tenkan, kijun, spanA, spanB, shift: k }; }
  // swings, structure breaks, order blocks and fair value gaps (the "smart money" vocabulary, kept to its checkable definitions)
  function structure(c, len = 5) { const sh = [], sl = []; for (let i = len; i < c.length - len; i++) { const w = c.slice(i - len, i + len + 1); if (c[i].h === Math.max(...w.map(x => x.h))) sh.push(i); if (c[i].l === Math.min(...w.map(x => x.l))) sl.push(i); }
    const breaks = []; let lastH = null, lastL = null, trend = 0; const hs = new Set(sh), ls = new Set(sl);
    for (let i = 0; i < c.length; i++) { if (lastH != null && c[i].c > c[lastH].h) { breaks.push({ i, px: c[lastH].h, kind: trend >= 0 ? "BOS" : "CHoCH", dir: 1 }); trend = 1; lastH = null; } if (lastL != null && c[i].c < c[lastL].l) { breaks.push({ i, px: c[lastL].l, kind: trend <= 0 ? "BOS" : "CHoCH", dir: -1 }); trend = -1; lastL = null; } if (hs.has(i)) lastH = i; if (ls.has(i)) lastL = i; }
    const fvg = []; for (let i = 2; i < c.length; i++) { if (c[i].l > c[i - 2].h) fvg.push({ i: i - 1, lo: c[i - 2].h, hi: c[i].l, dir: 1 }); if (c[i].h < c[i - 2].l) fvg.push({ i: i - 1, lo: c[i].h, hi: c[i - 2].l, dir: -1 }); }
    const ob = []; breaks.forEach(b => { for (let j = b.i - 1; j >= Math.max(0, b.i - 20); j--) { const k = c[j]; if ((b.dir > 0 && k.c < k.o) || (b.dir < 0 && k.c > k.o)) { ob.push({ i: j, lo: k.l, hi: k.h, dir: b.dir }); break; } } });
    return { swingHighs: sh, swingLows: sl, breaks, fvg: fvg.slice(-30), ob: ob.slice(-12) }; }
  const SESSIONS = [["Asia", 0, 8, "rgba(187,154,247,.08)"], ["London", 7, 16, "rgba(122,162,247,.08)"], ["New York", 12.5, 21, "rgba(224,175,104,.08)"]];
  function sessions(c) { const out = []; if (!c.length) return out; const days = new Set(c.map(x => Math.floor(x.t / 86400))); days.forEach(d => SESSIONS.forEach(([name, a, b, color]) => { const t0 = d * 86400 + a * 3600, t1 = d * 86400 + b * 3600; const w = c.filter(x => x.t >= t0 && x.t < t1); if (!w.length) return; out.push({ name, t0, t1, color, hi: Math.max(...w.map(x => x.h)), lo: Math.min(...w.map(x => x.l)) }); })); return out; }
  function pvsra(c) { const vols = c.map(x => x.v || 0), avg = sma(vols, 10); return c.map((x, i) => { const a = avg[i] || vols[i] || 1, r = vols[i] / a, spread = Math.abs(x.c - x.o) * vols[i]; const big = r >= 2 || spread >= 2 * (avg[i] || 1) * Math.abs(x.c - x.o); return r >= 2 ? (x.c >= x.o ? "lime" : "red") : r >= 1.5 ? (x.c >= x.o ? "blue" : "violet") : null; }); }
  function zscore(vals, n = 50) { return vals.map((v, i) => { if (i + 1 < n || v == null) return null; const w = vals.slice(i + 1 - n, i + 1).filter(x => x != null); const m = w.reduce((a, b) => a + b, 0) / w.length, sd = Math.sqrt(w.reduce((a, b) => a + (b - m) ** 2, 0) / w.length) || 1; return (v - m) / sd; }); }
  window.SteelInd = { ema, sma, std, rsi, macd, atr, supertrend, ichimoku, structure, sessions, pvsra, zscore };
})();
