"use client";

// The interactive canvas. A live d3-force simulation the founder can grab:
// drag a node and it PINS where dropped (fx/fy held, amber pin dot shown),
// double-click releases it, wheel/background-drag zoom+pan, sliders spread the
// layout, search focuses a node. Nothing here fetches — the parent owns data;
// this file owns motion, hit-testing and the render.
//
// Zoom transform lives in React state and is applied to one <g>; node positions
// come from the simulation objects held in a ref and are flushed to React at
// most once per animation frame (rAF-throttled), which keeps 50-200 nodes at
// frame rate without a second rendering system fighting React for the DOM.

import { drag as d3drag, type D3DragEvent } from "d3-drag";
import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
  type ForceLink,
  type ForceManyBody,
  type Simulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from "d3-force";
import { select } from "d3-selection";
import { zoom as d3zoom, zoomIdentity, type D3ZoomEvent, type ZoomBehavior, type ZoomTransform } from "d3-zoom";
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";

import { colorFor, docLabel, truncate, type GraphEdge, type GraphNode, type Origin } from "./model";

// Fallback box used before the container has been measured. The live viewBox
// tracks the container (see `size`) so the canvas never letterboxes — with a
// fixed box, opening the profile column left ~40% of the panel empty.
const FALLBACK_W = 900;
const FALLBACK_H = 560;

const SCALE_EXTENT: [number, number] = [0.2, 8];

type SimNode = SimulationNodeDatum & {
  id: number;
  label: string;
  type: string | null;
  score: number;
  confidence?: number | null;
  mentions?: number | null;
  origin: Origin;
  r: number;
  pinned: boolean;
};

type SimLink = SimulationLinkDatum<SimNode> & {
  predicate: string | null;
  weight: number | null;
  confidence?: number | null;
  doc_id?: string | null;
  origin: Origin;
  key: string;
};

type Props = {
  nodes: GraphNode[];
  edges: GraphEdge[];
  /** entity types the user has toggled off — hidden, never deleted */
  hiddenTypes: string[];
  search: string;
  selectedId: number | null;
  expandedIds: number[];
  busyId: number | null;
  linkDistance: number;
  charge: number;
  onSelect: (node: GraphNode) => void;
};

function endpoint(value: SimNode | string | number | undefined): SimNode | null {
  return value && typeof value === "object" ? (value as SimNode) : null;
}

function radiusFor(node: GraphNode, maxScore: number): number {
  if (node.origin === "expand" || !node.score) return 5.5;
  return 5 + 13 * Math.sqrt(node.score / maxScore);
}

export function GraphCanvas({
  nodes,
  edges,
  hiddenTypes,
  search,
  selectedId,
  expandedIds,
  busyId,
  linkDistance,
  charge,
  onSelect,
}: Props) {
  const svgRef = useRef<SVGSVGElement | null>(null);
  const nodeLayerRef = useRef<SVGGElement | null>(null);
  const simRef = useRef<Simulation<SimNode, SimLink> | null>(null);
  const nodesRef = useRef<SimNode[]>([]);
  const linksRef = useRef<SimLink[]>([]);
  const byIdRef = useRef<Map<number, SimNode>>(new Map());
  const zoomRef = useRef<ZoomBehavior<SVGSVGElement, unknown> | null>(null);
  const frameRef = useRef(0);
  const paramsRef = useRef({ linkDistance, charge });
  const maxWeightRef = useRef(1);
  const transformRef = useRef<ZoomTransform>(zoomIdentity);
  const fitRef = useRef<() => void>(() => {});
  const gestureRef = useRef({ moved: false, wasPinned: false });

  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState({ w: FALLBACK_W, h: FALLBACK_H });
  const [transform, setTransform] = useState<ZoomTransform>(zoomIdentity);
  const [hoveredEdge, setHoveredEdge] = useState<string | null>(null);
  const [, flush] = useReducer((n: number) => n + 1, 0);

  // 1 viewBox unit = 1 CSS pixel. The profile column opening no longer shrinks
  // the drawing — it re-centres the forces into the space that is actually left.
  useEffect(() => {
    const element = wrapRef.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => {
      const box = entries[0]?.contentRect;
      if (!box) return;
      setSize((current) => {
        const w = Math.max(320, Math.round(box.width));
        const h = Math.max(320, Math.round(box.height));
        return Math.abs(w - current.w) < 2 && Math.abs(h - current.h) < 2 ? current : { w, h };
      });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  paramsRef.current = { linkDistance, charge };

  const scheduleFlush = useCallback(() => {
    if (frameRef.current) return;
    frameRef.current = window.requestAnimationFrame(() => {
      frameRef.current = 0;
      flush();
    });
  }, []);

  // Force accessors read the sliders through a ref, so changing a slider
  // re-initialises the force in place instead of rebuilding the graph.
  const linkDistanceOf = useCallback((link: SimLink) => {
    const base = paramsRef.current.linkDistance;
    const weight = link.weight ?? 0;
    // heavier co-retrieval pulls tighter; unknown-weight (2-hop) links sit long
    return weight ? base * (1 - 0.55 * (weight / maxWeightRef.current)) : base * 1.15;
  }, []);
  const chargeOf = useCallback(() => paramsRef.current.charge, []);

  // ── the simulation: reconciled in place so an expansion never resets layout ──
  useEffect(() => {
    const previous = byIdRef.current;
    const maxScore = Math.max(1, ...nodes.map((n) => n.score || 0));
    const next: SimNode[] = [];
    const nextById = new Map<number, SimNode>();

    // seed a brand-new node next to a neighbour that already has a position,
    // so expansions grow out of the clicked node instead of flying in from 0,0
    const anchorOf = new Map<number, SimNode>();
    for (const edge of edges) {
      const a = previous.get(edge.source);
      const b = previous.get(edge.target);
      if (a && !previous.has(edge.target)) anchorOf.set(edge.target, a);
      if (b && !previous.has(edge.source)) anchorOf.set(edge.source, b);
    }

    for (const node of nodes) {
      const existing = previous.get(node.id);
      if (existing) {
        existing.label = node.label;
        existing.type = node.type;
        existing.score = node.score;
        existing.confidence = node.confidence;
        existing.mentions = node.mentions;
        existing.r = radiusFor(node, maxScore);
        next.push(existing);
        nextById.set(node.id, existing);
        continue;
      }
      const anchor = anchorOf.get(node.id);
      const angle = (node.id % 360) * (Math.PI / 180);
      const fresh: SimNode = {
        id: node.id,
        label: node.label,
        type: node.type,
        score: node.score,
        confidence: node.confidence,
        mentions: node.mentions,
        origin: node.origin ?? "panel",
        r: radiusFor(node, maxScore),
        pinned: false,
        x: (anchor?.x ?? size.w / 2) + Math.cos(angle) * (anchor ? 46 : 120),
        y: (anchor?.y ?? size.h / 2) + Math.sin(angle) * (anchor ? 46 : 120),
      };
      next.push(fresh);
      nextById.set(node.id, fresh);
    }

    const links: SimLink[] = [];
    const seen = new Set<string>();
    for (const edge of edges) {
      if (!nextById.has(edge.source) || !nextById.has(edge.target)) continue;
      const key = `${Math.min(edge.source, edge.target)}-${Math.max(edge.source, edge.target)}-${edge.predicate ?? ""}`;
      if (seen.has(key)) continue;
      seen.add(key);
      links.push({
        source: edge.source,
        target: edge.target,
        predicate: edge.predicate,
        weight: edge.weight,
        confidence: edge.confidence ?? null,
        doc_id: edge.doc_id ?? null,
        origin: edge.origin ?? "panel",
        key,
      });
    }

    nodesRef.current = next;
    linksRef.current = links;
    byIdRef.current = nextById;

    maxWeightRef.current = Math.max(1, ...links.map((l) => l.weight ?? 0));
    const grew = next.length > previous.size;
    const simulation =
      simRef.current ?? forceSimulation<SimNode, SimLink>().velocityDecay(0.32).on("tick", scheduleFlush);
    simRef.current = simulation;
    simulation.nodes(next);
    simulation
      .force("link", forceLink<SimNode, SimLink>(links).id((d) => d.id).distance(linkDistanceOf).strength(0.45))
      .force("charge", forceManyBody<SimNode>().strength(chargeOf))
      .force("center", forceCenter(size.w / 2, size.h / 2))
      .force("gx", forceX<SimNode>(size.w / 2).strength(0.045))
      .force("gy", forceY<SimNode>(size.h / 2).strength(0.06))
      .force("collide", forceCollide<SimNode>((d) => d.r + 6));

    if (grew) {
      // Burn the coarse part of the layout synchronously — simulation.tick(n)
      // advances without dispatching, so nothing paints — then frame it. Waiting
      // for the live settle instead left the founder staring at a knot in the
      // middle of an empty canvas for seconds (measured in headless Chrome).
      simulation.alpha(0.9);
      simulation.tick(previous.size === 0 ? 170 : 60);
      simulation.alpha(0.14).restart();
      fitRef.current();
    } else {
      simulation.alpha(0.4).restart();
    }
    scheduleFlush();
  }, [nodes, edges, size.w, size.h, scheduleFlush, linkDistanceOf, chargeOf]);

  // spread controls — re-set the same accessors so each force re-initialises
  // against the new slider values; the node objects (and the pins) survive
  useEffect(() => {
    const simulation = simRef.current;
    if (!simulation) return;
    (simulation.force("link") as ForceLink<SimNode, SimLink> | undefined)?.distance(linkDistanceOf);
    (simulation.force("charge") as ForceManyBody<SimNode> | undefined)?.strength(chargeOf);
    simulation.alpha(0.6).restart();
  }, [linkDistance, charge, linkDistanceOf, chargeOf]);

  useEffect(
    () => () => {
      if (frameRef.current) window.cancelAnimationFrame(frameRef.current);
      if (glideRef.current) window.cancelAnimationFrame(glideRef.current);
      simRef.current?.stop();
    },
    [],
  );

  // ── zoom + pan ─────────────────────────────────────────────────────────────
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const behaviour = d3zoom<SVGSVGElement, unknown>()
      .scaleExtent(SCALE_EXTENT)
      .on("zoom", (event: D3ZoomEvent<SVGSVGElement, unknown>) => {
        transformRef.current = event.transform;
        setTransform(event.transform);
      });
    zoomRef.current = behaviour;
    const selection = select(svg);
    selection.call(behaviour);
    // double-click is ours (release a pin), so it must not also zoom
    selection.on("dblclick.zoom", null);
    return () => {
      selection.on(".zoom", null);
    };
  }, []);

  // Own the tween instead of d3-transition. Two bundler facts made this the
  // simple path, both found by running the built page, not by reading docs:
  //   1. `import "d3-transition"` is a bare side-effect import and gets dropped,
  //      so selection.transition() is not a function at runtime;
  //   2. d3-zoom's own zoom.transform() calls selection.interrupt() — also a
  //      d3-transition prototype method — so it throws for the same reason.
  // d3-zoom keeps its state in the element's `__zoom` property; writing that
  // (plus our React state) is the whole of what a programmatic zoom needs, and
  // the next wheel/pan gesture picks up exactly where we left it.
  const glideRef = useRef(0);
  const applyTransform = useCallback((svg: SVGSVGElement, next: ZoomTransform) => {
    (svg as unknown as { __zoom: ZoomTransform }).__zoom = next;
    transformRef.current = next;
    setTransform(next);
  }, []);

  const glideTo = useCallback(
    (target: ZoomTransform, ms = 300) => {
      const svg = svgRef.current;
      if (!svg) return;
      if (glideRef.current) window.cancelAnimationFrame(glideRef.current);
      const start = transformRef.current;
      const started = performance.now();
      const step = () => {
        const progress = Math.min(1, (performance.now() - started) / ms);
        const eased = progress * (2 - progress);
        applyTransform(
          svg,
          zoomIdentity
            .translate(start.x + (target.x - start.x) * eased, start.y + (target.y - start.y) * eased)
            .scale(start.k + (target.k - start.k) * eased),
        );
        glideRef.current = progress < 1 ? window.requestAnimationFrame(step) : 0;
      };
      glideRef.current = window.requestAnimationFrame(step);
    },
    [applyTransform],
  );

  const zoomBy = useCallback(
    (factor: number) => {
      const current = transformRef.current;
      const k = Math.max(SCALE_EXTENT[0], Math.min(SCALE_EXTENT[1], current.k * factor));
      const ratio = k / current.k;
      // keep the canvas centre fixed while the scale changes
      glideTo(
        zoomIdentity
          .translate(size.w / 2 - (size.w / 2 - current.x) * ratio, size.h / 2 - (size.h / 2 - current.y) * ratio)
          .scale(k),
        200,
      );
    },
    [glideTo, size.w, size.h],
  );

  const resetView = useCallback(() => glideTo(zoomIdentity), [glideTo]);

  const fitView = useCallback(() => {
    const placed = nodesRef.current.filter((n) => n.x != null && n.y != null);
    if (!placed.length) return;
    const xs = placed.map((n) => n.x as number);
    const ys = placed.map((n) => n.y as number);
    const minX = Math.min(...xs) - 40;
    const maxX = Math.max(...xs) + 40;
    const minY = Math.min(...ys) - 40;
    const maxY = Math.max(...ys) + 40;
    const k = Math.max(
      SCALE_EXTENT[0],
      Math.min(SCALE_EXTENT[1], 0.92 * Math.min(size.w / (maxX - minX), size.h / (maxY - minY))),
    );
    glideTo(
      zoomIdentity
        .translate(size.w / 2, size.h / 2)
        .scale(k)
        .translate(-(minX + maxX) / 2, -(minY + maxY) / 2),
      340,
    );
  }, [glideTo, size.w, size.h]);

  fitRef.current = fitView;

  // ── search focus: centre the first match, parent dims the rest ─────────────
  useEffect(() => {
    const query = search.trim().toLowerCase();
    if (query.length < 2) return;
    const hit = nodesRef.current.find((n) => n.label.toLowerCase().includes(query));
    if (!hit || hit.x == null || hit.y == null) return;
    // read the live zoom through a ref: re-centring on every pan would fight
    // the user's own drag
    const k = Math.max(1.1, transformRef.current.k);
    glideTo(zoomIdentity.translate(size.w / 2, size.h / 2).scale(k).translate(-hit.x, -hit.y), 380);
  }, [search, glideTo, size.w, size.h]);

  // ── drag to pin ────────────────────────────────────────────────────────────
  const dragBehaviour = useMemo(
    () =>
      d3drag<SVGGElement, SimNode>()
        .on("start", (event: D3DragEvent<SVGGElement, SimNode, SimNode>, d) => {
          if (!event.active) simRef.current?.alphaTarget(0.28).restart();
          gestureRef.current = { moved: false, wasPinned: d.pinned };
          d.fx = d.x;
          d.fy = d.y;
        })
        .on("drag", (event: D3DragEvent<SVGGElement, SimNode, SimNode>, d) => {
          gestureRef.current.moved = true;
          d.fx = event.x;
          d.fy = event.y;
          scheduleFlush();
        })
        .on("end", (event: D3DragEvent<SVGGElement, SimNode, SimNode>, d) => {
          if (!event.active) simRef.current?.alphaTarget(0);
          if (gestureRef.current.moved) {
            // dropped = pinned. The pin is the point: the founder's arrangement
            // must survive the next tick, and the amber dot says it will.
            d.pinned = true;
          } else if (!gestureRef.current.wasPinned) {
            // a plain click (mousedown+mouseup, no movement) is a selection,
            // not a pin — release the hold d3 took at gesture start
            d.fx = null;
            d.fy = null;
          }
          scheduleFlush();
        }),
    [scheduleFlush],
  );

  // Not memoised on purpose: nodesRef is refilled by the reconcile effect AFTER
  // the render that changed `nodes`, so a memo would serve a stale node list and
  // an expansion would never appear.
  const hiddenSet = new Set(hiddenTypes);
  const visible = nodesRef.current.filter((n) => !hiddenSet.has(n.type ?? "untyped"));
  const visibleKey = visible.map((n) => n.id).join(",");

  useEffect(() => {
    const layer = nodeLayerRef.current;
    if (!layer) return;
    layer.querySelectorAll<SVGGElement>("g[data-node-id]").forEach((element) => {
      const node = byIdRef.current.get(Number(element.dataset.nodeId));
      if (!node) return;
      select(element).datum(node).call(dragBehaviour);
    });
  }, [visibleKey, dragBehaviour]);

  const unpin = useCallback(
    (node: SimNode) => {
      node.fx = null;
      node.fy = null;
      node.pinned = false;
      simRef.current?.alpha(0.5).restart();
      scheduleFlush();
    },
    [scheduleFlush],
  );

  const releaseAll = useCallback(() => {
    for (const node of nodesRef.current) {
      node.fx = null;
      node.fy = null;
      node.pinned = false;
    }
    simRef.current?.alpha(0.7).restart();
    scheduleFlush();
  }, [scheduleFlush]);

  const query = search.trim().toLowerCase();
  const matches = (node: SimNode) => query.length < 2 || node.label.toLowerCase().includes(query);
  const expanded = new Set(expandedIds);
  const pinnedCount = nodesRef.current.filter((n) => n.pinned).length;
  const labelScale = Math.min(2.4, Math.max(0.55, 1 / transform.k));

  const drawnLinks = linksRef.current.filter((link) => {
    const a = endpoint(link.source);
    const b = endpoint(link.target);
    if (!a || !b) return false;
    return !hiddenSet.has(a.type ?? "untyped") && !hiddenSet.has(b.type ?? "untyped");
  });

  // Label the selected node's edges with their predicate — but a hub whose 14
  // edges all read CO_ACTIVITY is noise, not semantics: at most 2 labels per
  // distinct predicate, on the longest (most legible) edges.
  const incidentLabels: SimLink[] = [];
  if (selectedId != null) {
    const perPredicate = new Map<string, number>();
    const incident = drawnLinks
      .filter((l) => endpoint(l.source)?.id === selectedId || endpoint(l.target)?.id === selectedId)
      .sort((a, b) => {
        const span = (l: SimLink) => {
          const s = endpoint(l.source);
          const t = endpoint(l.target);
          return s && t ? Math.hypot((s.x ?? 0) - (t.x ?? 0), (s.y ?? 0) - (t.y ?? 0)) : 0;
        };
        return span(b) - span(a);
      });
    for (const link of incident) {
      const key = link.predicate ?? "RELATED";
      const used = perPredicate.get(key) ?? 0;
      if (used >= 2 || incidentLabels.length >= 8) continue;
      perPredicate.set(key, used + 1);
      incidentLabels.push(link);
    }
  }

  const hovered = hoveredEdge ? drawnLinks.find((l) => l.key === hoveredEdge) : undefined;
  const maxWeight = Math.max(1, ...drawnLinks.map((l) => l.weight ?? 0));

  const buttonStyle: React.CSSProperties = {
    background: "rgba(13, 20, 38, 0.86)",
    border: "1px solid var(--plate-border)",
    borderRadius: 9,
    color: "var(--text-muted)",
    cursor: "pointer",
    fontSize: 12,
    lineHeight: 1,
    padding: "7px 9px",
  };

  return (
    <div ref={wrapRef} style={{ position: "relative", width: "100%", height: "min(66vh, 620px)" }}>
      <svg
        ref={svgRef}
        viewBox={`0 0 ${size.w} ${size.h}`}
        preserveAspectRatio="xMidYMid meet"
        style={{
          width: "100%",
          height: "100%",
          display: "block",
          cursor: "grab",
          touchAction: "none",
        }}
      >
        <g transform={transform.toString()}>
          <g>
            {drawnLinks.map((link) => {
              const a = endpoint(link.source);
              const b = endpoint(link.target);
              if (!a || !b) return null;
              const touchesSelected = selectedId != null && (a.id === selectedId || b.id === selectedId);
              const lit = query.length >= 2 ? matches(a) || matches(b) : true;
              const width = link.weight
                ? Math.min(3.4, 0.6 + 2.8 * Math.sqrt(link.weight / maxWeight))
                : 1;
              return (
                <line
                  key={link.key}
                  x1={a.x ?? 0}
                  y1={a.y ?? 0}
                  x2={b.x ?? 0}
                  y2={b.y ?? 0}
                  stroke={touchesSelected || hoveredEdge === link.key ? "rgba(79,216,196,0.62)" : "rgba(79,216,196,0.16)"}
                  strokeWidth={width}
                  strokeDasharray={link.origin === "expand" ? "3 4" : undefined}
                  opacity={lit ? 1 : 0.08}
                  style={{ pointerEvents: "stroke" }}
                  onMouseEnter={() => setHoveredEdge(link.key)}
                  onMouseLeave={() => setHoveredEdge((current) => (current === link.key ? null : current))}
                >
                  <title>
                    {`${a.label} —${link.predicate ?? "RELATED"}→ ${b.label}\n`}
                    {link.weight != null ? `weight ${link.weight}` : "weight unknown (2-hop traversal)"}
                    {link.confidence != null ? ` · confidence ${link.confidence}` : ""}
                    {`\nfrom ${docLabel(link.doc_id)}`}
                  </title>
                </line>
              );
            })}
          </g>

          {/* predicates of the selected node's edges, drawn not hidden in a tooltip */}
          <g style={{ pointerEvents: "none" }}>
            {incidentLabels.map((link) => {
              const a = endpoint(link.source);
              const b = endpoint(link.target);
              if (!a || !b) return null;
              const mx = ((a.x ?? 0) + (b.x ?? 0)) / 2;
              const my = ((a.y ?? 0) + (b.y ?? 0)) / 2;
              return (
                <text
                  key={`lbl-${link.key}`}
                  x={mx}
                  y={my}
                  textAnchor="middle"
                  fill="var(--text-muted)"
                  transform={`translate(${mx} ${my}) scale(${labelScale}) translate(${-mx} ${-my})`}
                  style={{ fontSize: 8.5, fontFamily: "var(--mono)", opacity: 0.85 }}
                >
                  {link.predicate ?? "RELATED"}
                </text>
              );
            })}
          </g>

          <g ref={nodeLayerRef}>
            {visible.map((node) => {
              const color = colorFor(node.type);
              const active = node.id === selectedId;
              const lit = matches(node);
              const showLabel = active || node.r >= 8 || (query.length >= 2 && lit);
              return (
                <g
                  key={node.id}
                  data-node-id={node.id}
                  transform={`translate(${node.x ?? 0} ${node.y ?? 0})`}
                  opacity={lit ? 1 : 0.1}
                  style={{ cursor: "pointer" }}
                  onClick={() => onSelect({ id: node.id, label: node.label, type: node.type, score: node.score })}
                  onDoubleClick={(event) => {
                    event.stopPropagation();
                    unpin(node);
                  }}
                >
                  <title>
                    {`${node.label} · ${node.type ?? "untyped"}${node.pinned ? " · pinned (double-click to release)" : ""}`}
                  </title>
                  {active && (
                    <circle r={node.r + 7} fill="none" stroke={color} strokeWidth={1} opacity={0.42} />
                  )}
                  {expanded.has(node.id) && (
                    <circle r={node.r + 3.4} fill="none" stroke={color} strokeWidth={0.8} strokeDasharray="2 3" opacity={0.5} />
                  )}
                  {busyId === node.id && (
                    <circle className="pulse" r={node.r + 11} fill="none" stroke="var(--conf-validated)" strokeWidth={1.4} opacity={0.55} />
                  )}
                  <circle
                    r={node.r}
                    fill="rgba(13, 20, 38, 0.92)"
                    stroke={color}
                    strokeWidth={active ? 2.2 : 1.2}
                    strokeDasharray={node.origin === "expand" ? "2.5 2.5" : undefined}
                  />
                  {node.pinned && (
                    <circle cx={node.r * 0.72} cy={-node.r * 0.72} r={2.8} fill="var(--conf-warn)" stroke="var(--field)" strokeWidth={0.8} />
                  )}
                  {showLabel && (
                    <text
                      y={node.r + 11}
                      textAnchor="middle"
                      transform={`scale(${labelScale})`}
                      fill={active ? "var(--text)" : "var(--text-muted)"}
                      style={{ fontSize: 9.5, fontFamily: "var(--mono)", pointerEvents: "none" }}
                    >
                      {truncate(node.label, 22)}
                    </text>
                  )}
                </g>
              );
            })}
          </g>

          {hovered && (() => {
            const a = endpoint(hovered.source);
            const b = endpoint(hovered.target);
            if (!a || !b) return null;
            const mx = ((a.x ?? 0) + (b.x ?? 0)) / 2;
            const my = ((a.y ?? 0) + (b.y ?? 0)) / 2;
            const text = `${hovered.predicate ?? "RELATED"} · ${
              hovered.weight != null ? `w ${hovered.weight}` : "weight unknown"
            } · ${docLabel(hovered.doc_id)}`;
            return (
              <g
                transform={`translate(${mx} ${my}) scale(${labelScale})`}
                style={{ pointerEvents: "none" }}
              >
                <rect
                  x={-Math.min(230, text.length * 3.2)}
                  y={-24}
                  width={Math.min(460, text.length * 6.4)}
                  height={17}
                  rx={5}
                  fill="rgba(10, 15, 30, 0.94)"
                  stroke="var(--plate-border)"
                />
                <text x={0} y={-12} textAnchor="middle" fill="var(--text)" style={{ fontSize: 9.5, fontFamily: "var(--mono)" }}>
                  {truncate(text, 72)}
                </text>
              </g>
            );
          })()}
        </g>
      </svg>

      <div style={{ position: "absolute", top: 10, right: 10, display: "flex", flexDirection: "column", gap: 6 }}>
        <button type="button" style={buttonStyle} onClick={() => zoomBy(1.45)} aria-label="zoom in" title="zoom in">
          +
        </button>
        <button type="button" style={buttonStyle} onClick={() => zoomBy(1 / 1.45)} aria-label="zoom out" title="zoom out">
          −
        </button>
        <button type="button" style={buttonStyle} onClick={fitView} title="fit every node in view">
          fit
        </button>
        <button type="button" style={buttonStyle} onClick={resetView} title="reset zoom and pan">
          reset
        </button>
      </div>

      <div
        style={{
          alignItems: "center",
          bottom: 8,
          display: "flex",
          flexWrap: "wrap",
          gap: 10,
          justifyContent: "space-between",
          left: 12,
          position: "absolute",
          right: 12,
        }}
      >
        <span className="receipt" style={{ color: "var(--text-faint)", pointerEvents: "none" }}>
          {size.w >= 640
            ? "drag a node to pin it · double-click to release · scroll to zoom · drag the field to pan"
            : "drag to pin · double-click to release · scroll to zoom"}
        </span>
        <span style={{ alignItems: "center", display: "flex", gap: 10 }}>
          {pinnedCount > 0 && (
            <button type="button" style={{ ...buttonStyle, color: "var(--conf-warn)" }} onClick={releaseAll}>
              {pinnedCount} pinned — release all
            </button>
          )}
          <span className="receipt" style={{ color: "var(--text-faint)", pointerEvents: "none" }}>
            {visible.length} shown · {drawnLinks.length} links · zoom {transform.k.toFixed(2)}×
          </span>
        </span>
      </div>
    </div>
  );
}
