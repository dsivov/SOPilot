#!/usr/bin/env python3
"""Render the constraint graph (GET /formflow/graph) as a house-style HTML page.

Prototype for the constraint-graph Studio: pull the real form's dependency graph from
the endpoint and render stats, the top "hub" controllers, an illustrative dependency
subgraph (mermaid), a per-stage breakdown, and the health block (contradiction seed).
"""
import json
import re
import sys
import urllib.request
from collections import Counter, defaultdict

API = "http://127.0.0.1:8100"
OUT = "docs/SMARTFORM_CONSTRAINT_GRAPH.html"


def get_graph():
    key = json.loads(urllib.request.urlopen(urllib.request.Request(
        f"{API}/admin/tenants/polartie/login-key", method="POST",
        headers={"X-Admin-Token": "dev-admin-token-p0"}, data=b"{}")).read())["api_key"]
    r = urllib.request.Request(f"{API}/formflow/graph",
                               headers={"Authorization": f"Bearer {key}", "X-Project": "smartform"})
    return json.loads(urllib.request.urlopen(r).read())


def sid(name):                       # safe mermaid node id
    return "n_" + re.sub(r"[^A-Za-z0-9]", "_", str(name))


def esc(s):                          # mermaid label text
    return re.sub(r'["\[\]{}|<>]', "", str(s))[:26]


def esch(s):                         # html
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def main():
    g = get_graph()
    if "error" in g:
        print("error:", g["error"]); return 1
    nodes = {n["name"]: n for n in g["nodes"]}
    edges = g["edges"]
    stats = g["stats"]

    # per-stage counts
    by_stage_fields = Counter(n["stage_title"] for n in g["nodes"])
    by_stage_cond = Counter(n["stage_title"] for n in g["nodes"] if n["conditional"])
    by_stage_edges = Counter(nodes[e["dst"]]["stage_title"] for e in edges)
    stage_order = [s["title"] for s in g["stages"]]

    # hub mermaid: the top controller and up to 12 of the fields it gates
    top = stats["top_controllers"][0]["name"] if stats["top_controllers"] else None
    hub_targets = [e["dst"] for e in edges if e["src"] == top][:12]
    total_targets = sum(1 for e in edges if e["src"] == top)
    hub_lines = ["flowchart LR"]
    if top:
        hub_lines.append(f'  {sid(top)}["🎛 {esc(top)} · {esc(nodes[top]["label"])}"]:::hub')
        for t in hub_targets:
            hub_lines.append(f'  {sid(top)} --> {sid(t)}["{esc(t)} · {esc(nodes[t]["label"])}"]')
        if total_targets > len(hub_targets):
            hub_lines.append(f'  {sid(top)} --> more["…+{total_targets - len(hub_targets)} more fields"]:::more')
    hub_lines.append("  classDef hub fill:#2a2114,stroke:#f0a73c,color:#fff;")
    hub_lines.append("  classDef more fill:#1b1f2a,stroke:#39425a,color:#8a93a8;")
    hub_mermaid = "\n".join(hub_lines)

    # top controllers bar chart (SVG)
    tc = stats["top_controllers"]
    mx = max((c["controls"] for c in tc), default=1)
    bars = []
    for i, c in enumerate(tc):
        y = 20 + i * 26
        w = int(560 * c["controls"] / mx)
        bars.append(f'<text x="8" y="{y+13}" fill="#c9d3e6" font="700 11px sans-serif" '
                    f'style="font:700 11px sans-serif">{esch(c["name"])}</text>'
                    f'<rect x="120" y="{y}" width="{w}" height="18" rx="3" fill="#5b8def"/>'
                    f'<text x="{125+w}" y="{y+13}" fill="#9cc0ff" style="font:700 11px sans-serif">{c["controls"]}</text>')
    bar_svg = (f'<svg viewBox="0 0 720 {30 + len(tc)*26}" role="img" aria-label="top controllers">'
               + "".join(bars) + "</svg>")

    stage_rows = "".join(
        f"<tr><td>{esch(t)}</td><td>{by_stage_fields[t]}</td><td>{by_stage_cond[t]}</td><td>{by_stage_edges[t]}</td></tr>"
        for t in stage_order)

    dangling = g["health"]["dangling"]
    health_ok = (stats["dangling_refs"] == 0)

    html = f"""<style>
  :root{{--bg:#0f1420;--panel:#161b28;--ink:#c9d3e6;--mut:#8a93a8;--line:#2b3140;--blue:#5b8def;--grn:#19b89a;--amb:#f0a73c;--red:#e0574f;}}
  @media (prefers-color-scheme:light){{:root{{--bg:#f7f8fb;--panel:#fff;--ink:#1c2330;--mut:#5a6473;--line:#e3e7ee;}}}}
  :root[data-theme=dark]{{--bg:#0f1420;--panel:#161b28;--ink:#c9d3e6;--mut:#8a93a8;--line:#2b3140;}}
  :root[data-theme=light]{{--bg:#f7f8fb;--panel:#fff;--ink:#1c2330;--mut:#5a6473;--line:#e3e7ee;}}
  body{{background:var(--bg);color:var(--ink);font:15px/1.55 "Segoe UI",system-ui,sans-serif;}}
  .wrap{{max-width:960px;margin:0 auto;padding:32px 22px 64px;}}
  h1{{font-size:26px;font-weight:800;margin:0 0 8px;text-wrap:balance;}}
  h2{{font-size:18px;font-weight:800;margin:34px 0 6px;padding-top:14px;border-top:1px solid var(--line);}}
  .lead{{color:var(--ink);font-size:15px;}} .small{{color:var(--mut);font-size:12.5px;}}
  .mono{{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.92em;color:var(--amb);}}
  .cards{{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0;}}
  .card{{flex:1 1 120px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;}}
  .card .big{{font:800 30px "Segoe UI",sans-serif;color:var(--blue);}}
  .card.ok .big{{color:var(--grn);}} .card.bad .big{{color:var(--red);}}
  .card .cap{{color:var(--mut);font-size:11.5px;margin-top:2px;}}
  .chart{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px;overflow-x:auto;}}
  .chart svg{{max-width:100%;height:auto;}}
  .tw{{overflow-x:auto;}} table{{border-collapse:collapse;width:100%;font-size:13px;margin:10px 0;}}
  th,td{{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);}} th{{color:var(--mut);font-weight:700;}}
  td{{font-variant-numeric:tabular-nums;}}
  .callout{{border:1px solid var(--line);border-left:4px solid var(--grn);border-radius:8px;background:var(--panel);padding:12px 14px;margin:12px 0;font-size:13.5px;}}
  .callout.bad{{border-left-color:var(--red);}}
  pre.mermaid{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;overflow-x:auto;}}
</style>
<div class="wrap">
<h1>Constraint graph — {esch(g.get('form'))}</h1>
<p class="lead">A prototype of the <b>constraint-graph Studio</b>: the form's show-if rules as a live
<b>dependency graph</b>, read straight from <span class="mono">GET /formflow/graph</span> (backed by
<span class="mono">constraints.py</span>). This is step 1 — visualize; next come AI-assisted editing with
contradiction-checking and a live execution trace.</p>

<div class="cards">
  <div class="card"><div class="big">{stats['fields']}</div><div class="cap">fields</div></div>
  <div class="card"><div class="big">{stats['conditional_fields']}</div><div class="cap">have a show-if rule</div></div>
  <div class="card"><div class="big">{stats['edges']}</div><div class="cap">dependency edges</div></div>
  <div class="card"><div class="big">{stats['stages']}</div><div class="cap">stages (groups)</div></div>
  <div class="card ok"><div class="big">{stats['cross_stage_edges']}</div><div class="cap">cross-stage edges</div></div>
  <div class="card {'ok' if health_ok else 'bad'}"><div class="big">{stats['dangling_refs']}</div><div class="cap">dangling refs</div></div>
</div>

<h2>Hub controllers — a few questions gate most of the form</h2>
<p class="small">Fan-out = how many fields a single answer shows/hides. These hubs are where an edit ripples
furthest — exactly what reconcile and the live trace watch.</p>
<div class="chart">{bar_svg}</div>

<h2>Dependency subgraph — the top hub, <span class="mono">{esch(top)}</span></h2>
<p class="small">One answer (<b>{esch(top)}</b>) controls the visibility of {total_targets} fields. Rendered live from the graph edges.</p>
<pre class="mermaid">{hub_mermaid}</pre>

<h2>Per-stage breakdown</h2>
<div class="tw"><table>
  <thead><tr><th>Stage</th><th>Fields</th><th>Conditional</th><th>Edges</th></tr></thead>
  <tbody>{stage_rows}</tbody>
</table></div>

<h2>Health — the contradiction-check foundation</h2>
<div class="callout {'ok' if health_ok else 'bad'}">
  <b>{'✓ Clean.' if health_ok else '⚠ Issues found.'}</b>
  <b>{stats['dangling_refs']}</b> dangling references (a condition pointing at a field that doesn't exist) and
  <b>{stats['cross_stage_edges']}</b> cross-stage dependencies — i.e. every stage is self-contained, which is why the
  grouping is safe. Dangling-ref detection is the first of the contradiction checks the Studio will run before an edit is
  saved (next: unreachable fields, cyclic conditions, always-false rules).
  {"" if health_ok else "<br>" + "<br>".join(f"{esch(d['field'])} → missing {esch(d['missing_ref'])}" for d in dangling[:10])}
</div>
</div>"""
    with open(OUT, "w") as fh:
        fh.write(html)
    print(f"wrote {OUT}: {stats['fields']} fields, {stats['edges']} edges, hub {top} → {total_targets}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
