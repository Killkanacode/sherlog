import {
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  useNodesState,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useEffect, useMemo } from "react";
import type { Topology } from "../lib/types";
import { cx } from "./ui";

// The shop's dependency graph, learned by Sherlog from traces. Known nodes get
// a fixed, readable layout; anything new is placed by depth.
const LAYOUT: Record<string, [number, number]> = {
  gateway: [0, 150],
  orders: [240, 150],
  payments: [480, 60],
  inventory: [480, 240],
  "card-provider": [720, 0],
  redis: [720, 150],
  postgresql: [720, 300],
};

type NodeData = {
  label: string;
  kind: "service" | "dependency";
  status: "ok" | "anomalous";
  version?: string;
  rps?: number;
  role?: "root" | "suspect";
};

function ServiceNode({ data }: NodeProps<Node<NodeData>>) {
  const bad = data.status === "anomalous";
  return (
    <div className="relative">
      {data.role && (
        <div
          className={cx(
            "absolute -top-5 left-0 font-mono text-[10px] font-medium tracking-wider uppercase",
            data.role === "root" ? "text-brass" : "text-muted",
          )}
        >
          {data.role === "root" ? "Root cause" : "Suspect"}
        </div>
      )}
      <div
        className={cx(
          "w-[156px] border px-3 py-2",
          bad ? "border-bad bg-[#2a1614]" : "border-line-strong bg-raised",
          data.role === "root" && "outline-2 outline-offset-2 outline-brass",
        )}
      >
        <Handle type="target" position={Position.Left} />
        <div className="flex items-center gap-2">
          <span className={cx("size-2 shrink-0", bad ? "bg-bad blink" : "bg-ok")} />
          <span className="truncate text-[13px] font-semibold text-ink">{data.label}</span>
          <span className="ml-auto font-mono text-[10px] text-faint uppercase">
            {data.kind === "service" ? "svc" : data.label === "card-provider" ? "ext" : "db"}
          </span>
        </div>
        <div className="mt-1 flex items-center justify-between font-mono text-[11px] text-muted">
          <span className="truncate">{data.version ?? (data.kind === "service" ? "" : "dependency")}</span>
          {data.rps != null && <span className="num">{data.rps.toFixed(1)}/s</span>}
        </div>
        <Handle type="source" position={Position.Right} />
      </div>
    </div>
  );
}

const nodeTypes = { service: ServiceNode };
const NONE: string[] = [];

export function ServiceMap({
  topology,
  versions,
  rootCause,
  suspects = NONE,
  height = 360,
  anomalous,
}: {
  topology: Topology | null;
  versions?: Record<string, string>;
  rootCause?: string | null;
  suspects?: string[];
  height?: number;
  anomalous?: Set<string>; // overrides the live status (e.g. an incident's services)
}) {
  const { nodes, edges } = useMemo(() => {
    if (!topology) return { nodes: [], edges: [] };
    const inbound: Record<string, number> = {};
    for (const e of topology.edges) inbound[e.target] = (inbound[e.target] ?? 0) + e.rps;

    const depth: Record<string, number> = {};
    const visit = (id: string, d: number) => {
      if ((depth[id] ?? -1) >= d || d > 10) return;
      depth[id] = d;
      topology.edges.filter((e) => e.source === id).forEach((e) => visit(e.target, d + 1));
    };
    topology.nodes.filter((n) => !topology.edges.some((e) => e.target === n.id)).forEach((n) => visit(n.id, 0));
    const perLayer: Record<number, number> = {};

    const nodes: Node<NodeData>[] = topology.nodes.map((n) => {
      let position = LAYOUT[n.id];
      if (!position) {
        const d = depth[n.id] ?? 0;
        perLayer[d] = (perLayer[d] ?? 0) + 1;
        position = [d * 240, 380 + perLayer[d] * 80];
      }
      const bad = anomalous ? anomalous.has(n.id) : n.status === "anomalous";
      return {
        id: n.id,
        type: "service",
        position: { x: position[0], y: position[1] },
        data: {
          label: n.id,
          kind: n.kind,
          status: bad ? "anomalous" : "ok",
          version: versions?.[n.id],
          rps: inbound[n.id] ?? (n.id === "gateway" ? undefined : 0),
          role: n.id === rootCause ? "root" : suspects.includes(n.id) ? "suspect" : undefined,
        },
        draggable: false,
        selectable: false,
      };
    });

    const edges: Edge[] = topology.edges.map((e) => {
      const color = e.error_rate > 0.05 ? "#ee5b4d" : e.error_rate > 0.01 ? "#e0a43a" : "#4a4d55";
      const err = e.error_rate > 0.01 ? ` · ${(e.error_rate * 100).toFixed(0)}% err` : "";
      return {
        id: `${e.source}->${e.target}`,
        source: e.source,
        target: e.target,
        type: "smoothstep",
        label: `${e.rps.toFixed(1)}/s${err}`,
        style: { stroke: color, strokeWidth: 1.25 + Math.min(2.5, e.rps / 12) },
        markerEnd: { type: MarkerType.ArrowClosed, color, width: 14, height: 14 },
        labelBgPadding: [4, 2] as [number, number],
      };
    });
    return { nodes, edges };
  }, [topology, versions, rootCause, suspects, anomalous]);

  // React Flow 12 records each node's measured size through onNodesChange;
  // keep it across data refreshes so nodes don't flicker hidden.
  const [shown, setShown, onNodesChange] = useNodesState<Node<NodeData>>([]);
  useEffect(() => {
    setShown((prev) => nodes.map((n) => ({ ...n, measured: prev.find((p) => p.id === n.id)?.measured })));
  }, [nodes, setShown]);

  return (
    <div style={{ height }} className="w-full">
      {shown.length > 0 && (
        <ReactFlow
          nodes={shown}
          edges={edges}
          onNodesChange={onNodesChange}
          nodeTypes={nodeTypes}
          colorMode="dark"
          fitView
          fitViewOptions={{ padding: 0.12 }}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable={false}
          panOnDrag={false}
          zoomOnScroll={false}
          zoomOnPinch={false}
          zoomOnDoubleClick={false}
          preventScrolling={false}
          proOptions={{ hideAttribution: true }}
        />
      )}
    </div>
  );
}
