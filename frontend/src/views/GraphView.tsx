// SOP conversation-graph view — pure SVG, design-system tokens only.
// Auto-layout: actions banded left→right by ordering layer, states beneath.
// Nodes are draggable (pointer events); manual positions persist per-SOP in
// localStorage and "Reset layout" returns to auto.
import { useEffect, useMemo, useRef, useState } from "react";

type Def = {
  agent_actions?: Array<{ name: string; data_dependencies?: string[]; must_say?: string[] }>;
  user_states?: Array<{ name: string }>;
  conversation_profile?: { success_markers?: string[]; failure_markers?: string[] };
  sop?: { edges?: Array<{ src: string; dst: string; direction?: string }> };
};

type XY = { x: number; y: number };

const NODE_H = 34;
const GAP_Y = 16;
const GAP_X = 56;
const BAND_GAP = 64;

function nodeW(name: string): number {
  return Math.max(96, name.length * 7.4 + 26);
}

function storageKey(sopId: string): string {
  return `sopilot-graph-layout-${sopId}`;
}

export default function GraphView({ def, sopId }: { def: Def; sopId: string }) {
  const [overrides, setOverrides] = useState<Record<string, XY>>({});
  const drag = useRef<{ name: string; dx: number; dy: number } | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  useEffect(() => {
    try {
      setOverrides(JSON.parse(localStorage.getItem(storageKey(sopId)) || "{}"));
    } catch {
      setOverrides({});
    }
  }, [sopId]);

  const layout = useMemo(() => {
    const actions = (def.agent_actions ?? []).map((a) => a.name);
    const states = (def.user_states ?? []).map((s) => s.name);
    const actionSet = new Set(actions);
    const stateSet = new Set(states);
    const edges = (def.sop?.edges ?? []).filter((e) => e.src && e.dst);
    const success = new Set(def.conversation_profile?.success_markers ?? []);
    const failure = new Set(def.conversation_profile?.failure_markers ?? []);
    const hasData = new Map((def.agent_actions ?? []).map((a) => [a.name, (a.data_dependencies ?? []).length > 0]));

    const prereqs = new Map<string, string[]>(actions.map((a) => [a, []]));
    const triggers: Array<{ state: string; action: string }> = [];
    for (const e of edges) {
      const dir = e.direction ?? "forward";
      if (dir === "both") continue;
      const [src, dst] = dir === "backward" ? [e.dst, e.src] : [e.src, e.dst];
      if (actionSet.has(src) && actionSet.has(dst)) prereqs.get(dst)?.push(src);
      else if (stateSet.has(src) && actionSet.has(dst)) triggers.push({ state: src, action: dst });
    }

    const layer = new Map<string, number>();
    const visiting = new Set<string>();
    const layerOf = (a: string): number => {
      if (layer.has(a)) return layer.get(a)!;
      if (visiting.has(a)) return 0;
      visiting.add(a);
      const ps = prereqs.get(a) ?? [];
      const l = ps.length ? 1 + Math.max(...ps.map(layerOf)) : 0;
      visiting.delete(a);
      layer.set(a, l);
      return l;
    };
    actions.forEach(layerOf);
    const maxLayer = actions.length ? Math.max(...[...layer.values()], 0) : 0;

    const cols = new Map<number, string[]>();
    for (const a of actions) {
      const l = layer.get(a) ?? 0;
      cols.set(l, [...(cols.get(l) ?? []), a]);
    }
    const colX: number[] = [];
    let x = 16;
    for (let l = 0; l <= maxLayer; l++) {
      colX[l] = x;
      const widest = Math.max(...(cols.get(l) ?? [""]).map(nodeW), 96);
      x += widest + GAP_X;
    }
    const pos = new Map<string, { x: number; y: number; w: number; kind: string }>();
    let bandBottom = 0;
    for (let l = 0; l <= maxLayer; l++) {
      (cols.get(l) ?? []).forEach((a, i) => {
        const y = 28 + i * (NODE_H + GAP_Y);
        pos.set(a, { x: colX[l], y, w: nodeW(a), kind: "action" });
        bandBottom = Math.max(bandBottom, y + NODE_H);
      });
    }

    const stateY = bandBottom + BAND_GAP;
    const triggerByState = new Map<string, string[]>();
    triggers.forEach((t) => triggerByState.set(t.state, [...(triggerByState.get(t.state) ?? []), t.action]));
    const orderedStates = [...states].sort((a, b) => {
      const rank = (s: string) => (success.has(s) || failure.has(s) ? 2 : triggerByState.has(s) ? 0 : 1);
      return rank(a) - rank(b);
    });
    let sx = 16;
    let row2 = false;
    for (const s of orderedStates) {
      const targets = triggerByState.get(s) ?? [];
      let wantX = sx;
      if (targets.length) {
        const t = pos.get(targets[0]);
        if (t) wantX = Math.max(sx, t.x - 10);
      }
      const w = nodeW(s);
      pos.set(s, { x: wantX, y: stateY + (row2 ? NODE_H + GAP_Y : 0), w, kind: "state" });
      sx = wantX + w + 24;
      if (sx > x + 200) {
        sx = 16;
        row2 = true;
      }
    }
    const orderingEdges: Array<{ from: string; to: string }> = [];
    for (const [dst, ps] of prereqs) for (const src of ps) orderingEdges.push({ from: src, to: dst });
    return { actions, orderedStates, pos, orderingEdges, triggers, success, failure, hasData, stateY };
  }, [def]);

  if (layout.actions.length === 0)
    return <div className="empty">No agent actions yet — the graph appears as you add them.</div>;

  const posOf = (name: string) => {
    const base = layout.pos.get(name);
    if (!base) return undefined;
    const o = overrides[name];
    return o ? { ...base, x: o.x, y: o.y } : base;
  };

  const all = [...layout.actions, ...layout.orderedStates];
  const width = Math.max(...all.map((n) => (posOf(n)?.x ?? 0) + (posOf(n)?.w ?? 0)), 640) + 40;
  const height = Math.max(...all.map((n) => posOf(n)?.y ?? 0), layout.stateY) + NODE_H + 46;

  const svgPoint = (e: React.PointerEvent): XY => {
    const rect = svgRef.current!.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  };

  const onPointerDown = (name: string) => (e: React.PointerEvent) => {
    const p = svgPoint(e);
    const node = posOf(name)!;
    drag.current = { name, dx: p.x - node.x, dy: p.y - node.y };
    (e.target as Element).setPointerCapture(e.pointerId);
  };
  const onPointerMove = (e: React.PointerEvent) => {
    if (!drag.current) return;
    const p = svgPoint(e);
    const { name, dx, dy } = drag.current;
    setOverrides((o) => ({ ...o, [name]: { x: Math.max(0, p.x - dx), y: Math.max(0, p.y - dy) } }));
  };
  const onPointerUp = () => {
    if (!drag.current) return;
    drag.current = null;
    setOverrides((o) => {
      localStorage.setItem(storageKey(sopId), JSON.stringify(o));
      return o;
    });
  };

  const resetLayout = () => {
    localStorage.removeItem(storageKey(sopId));
    setOverrides({});
  };

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
        <span style={{ color: "var(--muted)", fontSize: 11.5 }}>drag nodes to arrange — layout is saved in this browser</span>
        {Object.keys(overrides).length > 0 && (
          <button className="btn ghost sm" onClick={resetLayout}>Reset layout</button>
        )}
      </div>
      <div style={{ overflow: "auto" }}>
        <svg
          ref={svgRef}
          width={width}
          height={height}
          style={{ minWidth: "100%", display: "block" }}
          role="img"
          aria-label="SOP conversation graph"
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
        >
          <defs>
            <marker id="sopArr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6.5" markerHeight="6.5" orient="auto-start-reverse">
              <path d="M0 0L10 5L0 10z" fill="var(--muted)" />
            </marker>
          </defs>
          {layout.orderingEdges.map((e, i) => {
            const a = posOf(e.from);
            const b = posOf(e.to);
            if (!a || !b) return null;
            const x1 = a.x + a.w, y1 = a.y + NODE_H / 2, x2 = b.x, y2 = b.y + NODE_H / 2;
            const mx = (x1 + x2) / 2;
            return <path key={`o${i}`} d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2 - 3},${y2}`} fill="none" stroke="var(--muted)" strokeWidth={1.5} markerEnd="url(#sopArr)" />;
          })}
          {layout.triggers.map((t, i) => {
            const s = posOf(t.state);
            const a = posOf(t.action);
            if (!s || !a) return null;
            const x1 = s.x + s.w / 2, y1 = s.y, x2 = a.x + a.w / 2, y2 = a.y + NODE_H;
            return (
              <path key={`t${i}`} d={`M${x1},${y1} C${x1},${y1 - 30} ${x2},${y2 + 30} ${x2},${y2 + 3}`} fill="none" stroke="var(--muted)" strokeWidth={1.4} strokeDasharray="5 4" markerEnd="url(#sopArr)" />
            );
          })}
          {all.map((name) => {
            const p = posOf(name)!;
            const isAction = p.kind === "action";
            const isGood = layout.success.has(name);
            const isBad = layout.failure.has(name);
            const stroke = isAction ? "var(--comm)" : isGood ? "var(--good)" : isBad ? "var(--crit)" : "var(--accent)";
            const fill = isAction ? "var(--comm-dim)" : isGood ? "var(--good-dim)" : isBad ? "var(--crit-dim)" : "var(--accent-dim)";
            return (
              <g key={name} onPointerDown={onPointerDown(name)} style={{ cursor: "grab" }}>
                <title>{name}{isAction && layout.hasData.get(name) ? " — needs external data" : isGood ? " — ends: success" : isBad ? " — ends: failure" : ""}</title>
                <rect x={p.x} y={p.y} width={p.w} height={NODE_H} rx={9} fill={fill} stroke={stroke} strokeWidth={1.2} />
                <text x={p.x + p.w / 2} y={p.y + 21} textAnchor="middle" style={{ fill: "var(--text)", fontSize: 12, fontWeight: 600, pointerEvents: "none", userSelect: "none" }}>
                  {name}{isGood ? " ✓" : isBad ? " ✕" : ""}
                </text>
                {isAction && layout.hasData.get(name) && <circle cx={p.x + p.w - 8} cy={p.y + 8} r={3.5} fill="var(--warn)" />}
              </g>
            );
          })}
          <text x={16} y={height - 10} style={{ fill: "var(--muted)", fontSize: 10.5 }}>
            solid = ordering · dashed = state trigger · amber dot = needs external data · ✓/✕ = conversation ends
          </text>
        </svg>
      </div>
    </div>
  );
}
