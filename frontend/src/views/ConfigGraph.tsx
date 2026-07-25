// Dependency-graph view of a PolarTie config — pure SVG, design-system tokens.
// Three columns: agent → configured surface (tools/MCP/KB) → dependencies
// (transfer topics, backends). Colour = status; red edge = broken dependency.
import type { Graph, GNode, Status } from "../config/configModel";

const NODE_H = 44;
const GAP_Y = 14;
const COL_X = [16, 300, 606];
const COL_W = [250, 262, 210];
const TOP = 28;

function colorOf(kind: GNode["kind"], status: Status): { fill: string; stroke: string } {
  if (status === "error") return { fill: "var(--crit-dim)", stroke: "var(--crit)" };
  if (status === "off") return { fill: "var(--surface)", stroke: "var(--line)" };
  switch (kind) {
    case "agent": return { fill: "var(--accent-dim)", stroke: "var(--accent)" };
    case "mcp": return { fill: "var(--accent-dim)", stroke: "var(--accent)" };
    case "kb": return { fill: "var(--good-dim)", stroke: "var(--good)" };
    case "backend": return { fill: "var(--good-dim)", stroke: "var(--good)" };
    case "transfer": return { fill: "var(--surface)", stroke: "var(--line)" };
    default: return { fill: "var(--comm-dim)", stroke: "var(--comm)" };
  }
}

export default function ConfigGraph({ graph }: { graph: Graph }) {
  // stack nodes within each column
  const pos = new Map<string, { x: number; y: number; w: number }>();
  for (let c = 0; c < 3; c++) {
    const inCol = graph.nodes.filter((n) => n.col === c);
    inCol.forEach((n, i) => {
      const y = c === 0 ? 200 : TOP + i * (NODE_H + GAP_Y); // agent centred-ish
      pos.set(n.id, { x: COL_X[c], y, w: COL_W[c] });
    });
  }
  const width = COL_X[2] + COL_W[2] + 24;
  const height = Math.max(...[...pos.values()].map((p) => p.y + NODE_H), 300) + 40;

  const edgeStroke = (s: string) => (s === "error" ? "var(--crit)" : s === "info" ? "var(--muted)" : "var(--comm)");

  return (
    <div style={{ overflow: "auto" }}>
      <svg width={width} height={height} style={{ minWidth: "100%", display: "block" }} role="img" aria-label="Config dependency graph">
        <defs>
          <marker id="cgArr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6.5" markerHeight="6.5" orient="auto-start-reverse">
            <path d="M0 0L10 5L0 10z" fill="var(--muted)" />
          </marker>
          <marker id="cgArrR" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6.5" markerHeight="6.5" orient="auto-start-reverse">
            <path d="M0 0L10 5L0 10z" fill="var(--crit)" />
          </marker>
        </defs>
        <text x={COL_X[0]} y={16} style={{ fill: "var(--muted)", fontSize: 10.5, fontWeight: 700, letterSpacing: ".1em" }}>AGENT</text>
        <text x={COL_X[1]} y={16} style={{ fill: "var(--muted)", fontSize: 10.5, fontWeight: 700, letterSpacing: ".1em" }}>CONFIGURED SURFACE</text>
        <text x={COL_X[2]} y={16} style={{ fill: "var(--muted)", fontSize: 10.5, fontWeight: 700, letterSpacing: ".1em" }}>DEPENDENCIES</text>

        {graph.edges.map((e, i) => {
          const a = pos.get(e.from), b = pos.get(e.to);
          if (!a || !b) return null;
          const x1 = a.x + a.w, y1 = a.y + NODE_H / 2, x2 = b.x, y2 = b.y + NODE_H / 2;
          const mx = (x1 + x2) / 2;
          return (
            <path key={i} d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2 - 3},${y2}`} fill="none"
              stroke={edgeStroke(e.status)} strokeWidth={e.status === "error" ? 1.8 : 1.4}
              strokeDasharray={e.status === "error" ? "5 4" : undefined}
              markerEnd={e.status === "error" ? "url(#cgArrR)" : "url(#cgArr)"} />
          );
        })}

        {graph.nodes.map((n) => {
          const p = pos.get(n.id)!;
          const c = colorOf(n.kind, n.status);
          return (
            <g key={n.id}>
              <title>{n.label}{n.sub ? ` — ${n.sub}` : ""}{n.status === "error" ? " (broken dependency)" : ""}</title>
              <rect x={p.x} y={p.y} width={p.w} height={NODE_H} rx={9} fill={c.fill} stroke={c.stroke} strokeWidth={n.status === "error" ? 2 : 1.2} />
              <text x={p.x + 12} y={p.y + (n.sub ? 19 : 27)} style={{ fill: "var(--text)", fontSize: 12.5, fontWeight: 600 }}>{n.label}</text>
              {n.sub && <text x={p.x + 12} y={p.y + 33} style={{ fill: n.status === "error" ? "var(--crit)" : "var(--muted)", fontSize: 9.5, fontFamily: "var(--mono)" }}>{n.sub}</text>}
            </g>
          );
        })}
        <text x={COL_X[0]} y={height - 8} style={{ fill: "var(--muted)", fontSize: 10.5 }}>
          purple = agent/MCP · teal = tools · green = knowledge/backends · red (dashed) = broken dependency
        </text>
      </svg>
    </div>
  );
}
