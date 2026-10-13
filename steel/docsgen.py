"""`steel appendix`: the whitepaper's appendix generated from the code, so it cannot drift.

Three tables are rendered between `<!-- generated:appendix -->` and `<!-- /generated:appendix -->`
in docs/whitepaper.html: the settings (every `Config` field with its default and its comment,
grouped by the comment headings in config.py; the ones the dashboard changes live marked),
the feature columns (every group in features.py with the comment on its line), and the data
API's routes (steel/apiv1.py's ROUTES). `--check` exits 1 when the file is stale, for CI.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
START, END = "<!-- generated:appendix", "<!-- /generated:appendix -->"
FIELD = re.compile(r'^    (\w+): [^=]+ = _(env|flag)\("(\w+)", (.+?)\)\s*(?:#\s*(.*))?$')
PLAIN = re.compile(r'^    (\w+): [^=]+ = ((?!_env|_flag|field\()[^#]+?)\s*(?:#\s*(.*))?$')
SECTION = re.compile(r"^    # ([A-Z][^\n]*)$")
LIST = re.compile(r'^(MICRO|EXOGENOUS|BOOK|FLOW|OPTIONS|EVENTS) = \[(.+?)\]\s*(?:#\s*(.*))?$')


def settings(src: str | None = None) -> list[dict]:
    """[{section, name, env, default, note, live}] in file order."""
    src = src if src is not None else (ROOT / "steel" / "config.py").read_text()
    from .engine import TUNABLE
    out, section, in_class = [], "Settings", False
    for line in src.splitlines():
        if line.startswith("class Config"):
            in_class = True
            continue
        if not in_class:
            continue
        m = SECTION.match(line)
        if m:
            section = m.group(1).strip().rstrip(":")
            continue
        m = FIELD.match(line)
        if m:
            name, kind, env, default, note = m.groups()
            default = default.strip()
            if kind == "flag":
                default = "on" if default.strip("'\"") in ("1", "true", "True") else "off"
            out.append({"section": section, "name": name, "env": "STEEL_" + env, "default": default, "note": (note or "").strip(), "live": name in TUNABLE})
            continue
        m = PLAIN.match(line)
        if m and not line.lstrip().startswith("#"):
            name, default, note = m.groups()
            out.append({"section": section, "name": name, "env": "STEEL_" + name.upper(), "default": default.strip(), "note": (note or "").strip(), "live": name in TUNABLE})
    return out


def features(src: str | None = None) -> list[dict]:
    src = src if src is not None else (ROOT / "steel" / "features.py").read_text()
    out = []
    for line in src.splitlines():
        m = LIST.match(line)
        if m:
            names = [x.strip().strip("'\"") for x in m.group(2).split(",") if x.strip()]
            out.append({"group": m.group(1), "names": names, "note": (m.group(3) or "").strip()})
    from .clock import CALENDAR
    out.append({"group": "CALENDAR", "names": list(CALENDAR), "note": "functions of the bar's timestamp: the hour and the day of week as angles, time to the next funding settlement, whether the CME session is open, the Friday options expiry"})
    order = re.search(r"^FEATURES = (.+)$", src, flags=re.M)          # the order the net reads them, not the order the lists are declared
    if order:
        rank = {g: i for i, g in enumerate(re.findall(r"[A-Z]+", order.group(1)))}
        out.sort(key=lambda f: rank.get(f["group"], 99))
    return out


def routes() -> list[dict]:
    from .apiv1 import ROUTES
    return [{"route": k, "weight": w, "returns": d} for k, (w, d) in ROUTES.items()]


def render() -> str:
    e = html.escape
    rows = settings()
    n_live = sum(1 for r in rows if r["live"])
    parts = ['    <div class="table-wrap">\n      <table class="t-settings">\n',
             f'        <caption>Table 3. Every setting, generated from <code>steel/config.py</code>: {len(rows)} knobs, each <code>STEEL_&lt;NAME&gt;</code> in the environment; the {n_live} marked live change from the dashboard without a restart.</caption>\n',
             '        <thead><tr><th>Section</th><th>Setting</th><th>Default</th><th>Role</th></tr></thead>\n        <tbody>\n']
    last = None
    for r in rows:
        sec = "" if r["section"] == last else e(r["section"])
        last = r["section"]
        parts.append(f'          <tr><td>{sec}</td><td><code>{e(r["name"])}</code>{" (live)" if r["live"] else ""}</td><td><code>{e(r["default"])}</code></td><td>{e(r["note"])}</td></tr>\n')
    parts.append("        </tbody>\n      </table>\n    </div>\n")
    feats = features()
    total = sum(len(f["names"]) for f in feats)
    parts += ['    <div class="table-wrap">\n      <table>\n',
              f'        <caption>Table 4. The feature columns, generated from <code>steel/features.py</code> and <code>steel/clock.py</code>: {total} per pair per bar, in the order the net reads them.</caption>\n',
              '        <thead><tr><th>Group</th><th class="num">Columns</th><th>Features</th><th>What they read</th></tr></thead>\n        <tbody>\n']
    for f in feats:
        parts.append(f'          <tr><td><b>{e(f["group"].title())}</b></td><td class="num">{len(f["names"])}</td><td>{", ".join("<code>" + e(n) + "</code>" for n in f["names"])}</td><td>{e(f["note"])}</td></tr>\n')
    parts.append("        </tbody>\n      </table>\n    </div>\n")
    rts = routes()
    parts += ['    <div class="table-wrap">\n      <table>\n',
              f'        <caption>Table 5. The data API, version 1, generated from <code>steel/apiv1.py</code>: {len(rts)} routes under <code>/api/v1</code>, each costing its weight against a sliding-minute budget; the operator token on every request when auth is on. Shapes in <a href="https://github.com/thingg-co/steel/blob/main/docs/api.md">docs/api.md</a>. The same stores answer <code>steel mcp</code>, whose tools read and never order.</caption>\n',
              '        <thead><tr><th>Route</th><th class="num">Weight</th><th>Returns</th></tr></thead>\n        <tbody>\n']
    for r in rts:
        parts.append(f'          <tr><td><code>{e(r["route"])}</code></td><td class="num">{r["weight"]}</td><td>{e(r["returns"])}</td></tr>\n')
    parts.append("        </tbody>\n      </table>\n    </div>\n")
    return "".join(parts)


def splice(doc: str, body: str) -> str:
    i = doc.find(START)
    j = doc.find(END)
    if i < 0 or j < 0 or j < i:
        raise ValueError("the appendix markers are missing from the document")
    head_end = doc.find("\n", i) + 1          # keep the opening marker's line
    line_start = doc.rfind("\n", 0, j) + 1     # and the closing marker's whole line, indentation included
    return doc[:head_end] + body + doc[line_start:]


def main(argv: list[str]) -> int:
    path = ROOT / "docs" / "whitepaper.html"
    for i, a in enumerate(argv):
        if a == "--file" and i + 1 < len(argv):
            path = Path(argv[i + 1])
    doc = path.read_text()
    new = splice(doc, render())
    if "--check" in argv:
        if new != doc:
            print(f"{path} is stale: run `steel appendix`")
            return 1
        print(f"{path} is current")
        return 0
    path.write_text(new)
    print(f"wrote the appendix into {path}: {len(settings())} settings, {sum(len(f['names']) for f in features())} features, {len(routes())} routes")
    return 0
