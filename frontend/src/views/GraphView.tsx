// SOP conversation-graph view — pure SVG, design-system tokens only.
// Layout: agent actions banded left→right by ordering layer (longest path over
// action→action edges); user states in a band below, aligned to the actions
// they trigger; terminal states pushed right and marked ✓/✕.

type Def = {
  agent_actions?: Array<{ name: string; data_dependencies?: string[]; must_say?: string[] }>;
  user_states?: Array<{ name: string }>;
  conversation_profile?: { success_markers?: string[]; failure_markers?: string[] };
  sop?: { edges?: Array<{ src: string; dst: string; direction?: string }> };
};

const NODE_H = 34;
const GAP_Y = 16;
const GAP_X = 56;
const BAND_GAP = 64;

function nodeW(name: string): number {
  return Math.max(96, name.length * 7.4 + 26);
}

export default function GraphView({ def }: { def: Def }) {
  const actions = (def.agent_actions ?? []).map((a) => a.name);
  const states = (def.user_states ?? []).map((s) => s.name);
  const actionSet = new Set(actions);
  const stateSet = new Set(states);
  const edges = (def.sop?.edges ?? []).filter((e) => e.src && e.dst);
  const success = new Set(def.conversation_profile?.success_markers ?? []);
  const failure = new Set(def.conversation_profile?.failure_markers ?? []);
  const hasData = new Map((def.agent_actions ?? []).map((a) => [a.name, (a.data_dependencies ?? []).length > 0]));

  if (actions.length === 0) return <div className="empty">No agent actions yet — the graph appears as you add them.</div>;

  // ordering prereqs (action→action forward; backward flips)
  const prereqs = new Map<string, string[]>(actions.map((a) => [a, []]));
  const triggers: Array<{ state: string; action: string }> = [];
  for (const e of edges) {
    const dir = e.direction ?? "forward";
    if (dir === "both") continue;
    const [src, dst] = dir === "backward" ? [e.dst, e.src] : [e.src, e.dst];
    if (actionSet.has(src) && actionSet.has(dst)) prereqs.get(dst)?.push(src);
    else if (stateSet.has(src) && actionSet.has(dst)) triggers.push({ state: src, action: dst });
  }

  // layer = longest prereq chain (cycle-safe: visiting set breaks loops; lint flags them)
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
  const maxLayer = Math.max(...[...layer.values()], 0);

  // action positions: one column per layer, stacked vertically
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
  const pos = new Map<string, { x: number; y: number; w: number }>();
  let bandBottom = 0;
  for (let l = 0; l <= maxLayer; l++) {
    const col = cols.get(l) ?? [];
    col.forEach((a, i) => {
      const y = 28 + i * (NODE_H + GAP_Y);
      pos.set(a, { x: colX[l], y, w: nodeW(a) });
      bandBottom = Math.max(bandBottom, y + NODE_H);
    });
  }

  // state positions: triggers under their (leftmost) target; terminals rightmost; rest trailing
  const stateY = bandBottom + BAND_GAP;
  const placedStates = new Map<string, { x: number; y: number; w: number }>();
  const triggerByState = new Map<string, string[]>();
  triggers.forEach((t) => triggerByState.set(t.state, [...(triggerByState.get(t.state) ?? []), t.action]));
  const orderedStates = [...states].sort((a, b) => {
    const rank = (s: string) => (success.has(s) || failure.has(s) ? 2 : triggerByState.has(s) ? 0 : 1);
    return rank(a) - rank(b);
  });
  let sx = 16;
  let stateRow2 = false;
  for (const s of orderedStates) {
    const targets = triggerByState.get(s) ?? [];
    let wantX = sx;
    if (targets.length) {
      const t = pos.get(targets[0]);
      if (t) wantX = Math.max(sx, t.x - 10);
    }
    const w = nodeW(s);
    const y = stateY + (stateRow2 ? NODE_H + GAP_Y : 0);
    placedStates.set(s, { x: wantX, y, w });
    sx = wantX + w + 24;
    if (sx > x + 200) {
      sx = 16;
      stateRow2 = true;
    }
  }
  const width = Math.max(x, sx, 640);
  const height = stateY + NODE_H * (stateRow2 ? 2 : 1) + GAP_Y * (stateRow2 ? 1 : 0) + 40;

  const orderingEdges: Array<{ from: string; to: string }> = [];
  for (const [dst, ps] of prereqs) for (const src of ps) orderingEdges.push({ from: src, to: dst });

  return (
    <div style={{ overflow: "auto" }}>
      <svg width={width} height={height} style={{ minWidth: "100%", display: "block" }} role="img" aria-label="SOP conversation graph">
        <defs>
          <marker id="sopArr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6.5" markerHeight="6.5" orient="auto-start-reverse">
            <path d="M0 0L10 5L0 10z" fill="var(--muted)" />
          </marker>
        </defs>
        <text x={16} y={14} style={{ fill: "var(--muted)", fontSize: 10.5, fontWeight: 600, letterSpacing: ".6px", textTransform: "uppercase" }}>
          Agent actions — ordering enforced
        </text>
        {orderingEdges.map((e, i) => {
          const a = pos.get(e.from);
          const b = pos.get(e.to);
          if (!a || !b) return null;
          const x1 = a.x + a.w, y1 = a.y + NODE_H / 2, x2 = b.x, y2 = b.y + NODE_H / 2;
          const mx = (x1 + x2) / 2;
          return <path key={`o${i}`} d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2 - 3},${y2}`} fill="none" stroke="var(--muted)" strokeWidth={1.5} markerEnd="url(#sopArr)" />;
        })}
        {triggers.map((t, i) => {
          const s = placedStates.get(t.state);
          const a = pos.get(t.action);
          if (!s || !a) return null;
          const x1 = s.x + s.w / 2, y1 = s.y, x2 = a.x + a.w / 2, y2 = a.y + NODE_H;
          return (
            <path key={`t${i}`} d={`M${x1},${y1} C${x1},${y1 - 30} ${x2},${y2 + 30} ${x2},${y2 + 3}`} fill="none" stroke="var(--muted)" strokeWidth={1.4} strokeDasharray="5 4" markerEnd="url(#sopArr)" />
          );
        })}
        {actions.map((a) => {
          const p = pos.get(a)!;
          return (
            <g key={a}>
              <title>{a}{hasData.get(a) ? " — needs external data" : ""}</title>
              <rect x={p.x} y={p.y} width={p.w} height={NODE_H} rx={9} fill="var(--comm-dim)" stroke="var(--comm)" strokeWidth={1.2} />
              <text x={p.x + p.w / 2} y={p.y + 21} textAnchor="middle" style={{ fill: "var(--text)", fontSize: 12, fontWeight: 600 }}>
                {a}
              </text>
              {hasData.get(a) && <circle cx={p.x + p.w - 8} cy={p.y + 8} r={3.5} fill="var(--warn)" />}
            </g>
          );
        })}
        <text x={16} y={stateY - 12} style={{ fill: "var(--muted)", fontSize: 10.5, fontWeight: 600, letterSpacing: ".6px", textTransform: "uppercase" }}>
          User states — triggers &amp; terminals
        </text>
        {orderedStates.map((s) => {
          const p = placedStates.get(s)!;
          const isGood = success.has(s), isBad = failure.has(s);
          const stroke = isGood ? "var(--good)" : isBad ? "var(--crit)" : "var(--accent)";
          const fill = isGood ? "var(--good-dim)" : isBad ? "var(--crit-dim)" : "var(--accent-dim)";
          return (
            <g key={s}>
              <title>{s}{isGood ? " — conversation ends: success" : isBad ? " — conversation ends: failure" : ""}</title>
              <rect x={p.x} y={p.y} width={p.w} height={NODE_H} rx={9} fill={fill} stroke={stroke} strokeWidth={1.2} />
              <text x={p.x + p.w / 2} y={p.y + 21} textAnchor="middle" style={{ fill: "var(--text)", fontSize: 12, fontWeight: 600 }}>
                {s}{isGood ? " ✓" : isBad ? " ✕" : ""}
              </text>
            </g>
          );
        })}
        <text x={16} y={height - 10} style={{ fill: "var(--muted)", fontSize: 10.5 }}>
          solid = ordering · dashed = state trigger · amber dot = needs external data · ✓/✕ = conversation ends
        </text>
      </svg>
    </div>
  );
}
