// Workspaces (M18): every panel is a widget. Drag its grip to reorder it among its neighbours,
// collapse it to its title, and the layout (order and collapsed state per view, plus the chart's
// layers and the pair link) is saved in this browser and can be exported or imported as JSON.
// The fixed views stay as the presets; "reset" returns a view to the markup's order.
(() => {
  const KEY = "steel.layout.v1";
  const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const load = () => { try { return JSON.parse(localStorage.getItem(KEY) || "{}") || {}; } catch (e) { return {}; } };
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(layout)); } catch (e) { /* storage may be unavailable */ } };
  let layout = load();
  layout.containers = layout.containers || {};
  layout.link = layout.link !== false;

  const containers = () => $$("main.view, main.view .row").filter(c => widgetsOf(c).length);
  const widgetsOf = c => [...c.children].filter(el => el.matches("section.panel, section.console-grid"));
  const cidOf = c => c.id || `${c.closest("main").id}:${[...c.parentElement.children].indexOf(c)}`;
  function widOf(el, c, i) {
    if (el.id) return el.id;
    const h = el.querySelector("h3");
    const txt = h ? (h.childNodes[0] && h.childNodes[0].textContent || h.textContent).trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") : "panel";
    el.id = `wg-${c.closest("main").id}-${txt || "panel"}-${i}`;
    return el.id;
  }
  function decorate(el) {
    if (el.querySelector(":scope > .wg-bar")) return;
    el.classList.add("wg");
    const bar = document.createElement("div"); bar.className = "wg-bar";
    bar.innerHTML = `<span class="wg-grip" draggable="true" title="drag to reorder">⋮⋮</span><button type="button" class="wg-move" data-dir="-1" title="move up">▲</button><button type="button" class="wg-move" data-dir="1" title="move down">▼</button><button type="button" class="wg-collapse" title="collapse or expand">–</button>`;
    el.prepend(bar);
    const grip = bar.querySelector(".wg-grip");
    grip.addEventListener("dragstart", e => { dragging = el; el.classList.add("wg-dragging"); e.dataTransfer.effectAllowed = "move"; try { e.dataTransfer.setData("text/plain", el.id); } catch (x) { /* ie */ } });
    grip.addEventListener("dragend", () => { if (dragging) dragging.classList.remove("wg-dragging"); dragging = null; $$(".wg-over").forEach(x => x.classList.remove("wg-over")); });
    el.addEventListener("dragover", e => { if (!dragging || dragging === el || dragging.parentElement !== el.parentElement) return; e.preventDefault(); e.dataTransfer.dropEffect = "move"; el.classList.add("wg-over"); });
    el.addEventListener("dragleave", () => el.classList.remove("wg-over"));
    el.addEventListener("drop", e => {
      if (!dragging || dragging === el || dragging.parentElement !== el.parentElement) return; e.preventDefault(); el.classList.remove("wg-over");
      const r = el.getBoundingClientRect(), after = (e.clientY - r.top) > r.height / 2;
      el.parentElement.insertBefore(dragging, after ? el.nextSibling : el);
      remember(el.parentElement);
    });
    bar.querySelectorAll(".wg-move").forEach(b => { b.onclick = () => { const sib = widgetsOf(el.parentElement), i = sib.indexOf(el), j = i + Number(b.dataset.dir); if (j < 0 || j >= sib.length) return; el.parentElement.insertBefore(el, Number(b.dataset.dir) < 0 ? sib[j] : sib[j].nextSibling); remember(el.parentElement); }; });
    bar.querySelector(".wg-collapse").onclick = () => { el.classList.toggle("wg-collapsed"); bar.querySelector(".wg-collapse").textContent = el.classList.contains("wg-collapsed") ? "+" : "–"; remember(el.parentElement); };
  }
  let dragging = null;
  function remember(c) {
    const ws = widgetsOf(c);
    layout.containers[cidOf(c)] = { order: ws.map(w => w.id), collapsed: ws.filter(w => w.classList.contains("wg-collapsed")).map(w => w.id) };
    save();
  }
  function apply(c) {
    const ws = widgetsOf(c); ws.forEach((w, i) => { widOf(w, c, i); decorate(w); });
    const L = layout.containers[cidOf(c)]; if (!L) return;
    const by = Object.fromEntries(ws.map(w => [w.id, w]));
    (L.order || []).forEach(id => { if (by[id]) c.appendChild(by[id]); });                 // saved order first, unknown panels keep their place after
    ws.forEach(w => { const on = (L.collapsed || []).includes(w.id); w.classList.toggle("wg-collapsed", on); const b = w.querySelector(":scope > .wg-bar .wg-collapse"); if (b) b.textContent = on ? "+" : "–"; });
  }
  function applyAll() { containers().forEach(apply); }
  function resetView(view) {
    containers().filter(c => c.closest("main").id === view).forEach(c => { delete layout.containers[cidOf(c)]; });
    save(); location.reload();
  }
  function exportJson() {
    const ws = { version: 1, layout, chartLayers: (window.steelChart && window.steelChart.layers) || null, exported: new Date().toISOString() };
    const blob = new Blob([JSON.stringify(ws, null, 1)], { type: "application/json" }), a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = "steel-workspace.json"; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }
  function importJson(text) {
    try {
      const ws = JSON.parse(text); if (!ws || !ws.layout) throw new Error("not a Steel workspace");
      layout = ws.layout; layout.containers = layout.containers || {}; save();
      if (ws.chartLayers) { try { localStorage.setItem("steel.chart.layers", JSON.stringify(ws.chartLayers)); } catch (e) { /* ignore */ } }
      location.reload();
    } catch (e) { alert("Could not import: " + e.message); }
  }
  // the pair link: the chart follows the pair you open anywhere else
  window.addEventListener("hashchange", () => {
    if (!layout.link) return;
    const m = location.hash.match(/^#([a-z]+)\/([^/?]+)/); if (!m || m[1] === "chart") return;
    const sym = decodeURIComponent(m[2]), S = window.steel, cs = S && S.chart && S.chart();
    if (!cs || cs.pair === sym) return;
    cs.pair = sym; cs.venues.clear(); cs.railClosed = false; if (cs.chart && cs.chart.data) cs.chart.data = null;
  });
  function menu() {
    const host = $("#theme-toggle"); if (!host || $("#wg-menu")) return;
    const d = document.createElement("details"); d.className = "ch-menu wg-menu"; d.id = "wg-menu";
    d.innerHTML = `<summary title="arrange the panels: drag a grip to reorder, – to collapse; saved in this browser">Layout</summary><div class="ch-menu-body wg-menu-body">
      <label><input type="checkbox" id="wg-link"${layout.link ? " checked" : ""}> the chart follows the pair I open</label>
      <button type="button" class="mini" id="wg-reset-view">Reset this view</button>
      <button type="button" class="mini" id="wg-reset-all">Reset every view</button>
      <button type="button" class="mini" id="wg-export">Export workspace</button>
      <label class="mini wg-import">Import workspace <input type="file" id="wg-import" accept="application/json" hidden></label>
      <span class="muted">Panels keep their order and collapsed state per view in this browser. The views are the presets.</span></div>`;
    host.before(d);
    $("#wg-link").onchange = e => { layout.link = e.target.checked; save(); };
    $("#wg-reset-view").onclick = () => resetView(document.body.dataset.view || "market");
    $("#wg-reset-all").onclick = () => { layout.containers = {}; save(); location.reload(); };
    $("#wg-export").onclick = exportJson;
    $("#wg-import").onchange = e => { const f = e.target.files && e.target.files[0]; if (!f) return; f.text().then(importJson); };
    document.addEventListener("click", e => { if (d.open && !d.contains(e.target)) d.open = false; });
  }
  const boot = () => { applyAll(); menu(); };
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", boot) : boot();
  window.steelWidgets = { apply: applyAll, layout: () => layout, reset: resetView, export: exportJson, import: importJson, containers: () => containers().map(cidOf) };
})();
