import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { TransformWrapper, TransformComponent, useTransformContext } from "react-zoom-pan-pinch";
import type { ReactZoomPanPinchRef } from "react-zoom-pan-pinch";
import { fetchDiff, BOARD_URLS } from "./api";
import type { Finding, ViewMode, DiffResponse, BoardBounds, Severity } from "./types/finding";

// ─── Constants ───────────────────────────────────────────────────────────────

const SEVERITY_COLOR: Record<Severity, string> = {
  CRITICAL: "#ef4444",
  WARNING:  "#f59e0b",
  INFO:     "#22c55e",
};

const SEVERITY_BG: Record<Severity, string> = {
  CRITICAL: "rgba(239,68,68,0.12)",
  WARNING:  "rgba(245,158,11,0.10)",
  INFO:     "rgba(34,197,94,0.08)",
};

const CATEGORY_LABEL: Record<string, string> = {
  ERC:       "ERC",
  POWER:     "Power",
  DIFF_PAIR: "Diff Pairs",
  GROUND:    "Ground",
  IMPEDANCE: "Impedance",
  BOM:       "BOM",
  COMPONENT: "Components",
};

// ─── Types ───────────────────────────────────────────────────────────────────

type SidebarTab = "findings" | "components";

interface ComponentChange {
  type: "added" | "removed" | "moved" | "value" | "footprint" | "layer" | "rotation" | "swapped" | "reconnected" | "disconnected" | "annotated";
  ref: string;
  message: string;
  // For moved/swapped we try to extract coordinates from the message
  fromPos?: { x: number; y: number };
  toPos?: { x: number; y: number };
}

// ─── Coordinate mapping ──────────────────────────────────────────────────────
//
// KiCad SVG Y increases downward — same as screen Y — so no axis flip needed.
//
// ROOT CAUSE of off-board markers: `objectFit: contain` letterboxes the image
// inside its container. Markers are placed as % of the *container* div but
// the board only occupies a sub-rectangle of it (with black bars top/bottom
// or left/right). We must make the marker layer co-extensive with the *rendered
// image pixels*, not the full container.
//
// Solution: the marker layer is sized/positioned to match the rendered image
// exactly. We compute the letterbox offset + rendered size from the img element
// using a ResizeObserver (see BoardViewer / useImageRect hook below).

// Raw 0-1 fraction of a coordinate within the board bounding box
function mmToFrac(
  mmX: number,
  mmY: number,
  bounds: BoardBounds
): { fx: number; fy: number } {
  const boardW = bounds.max_x - bounds.min_x;
  const boardH = bounds.max_y - bounds.min_y;
  if (boardW === 0 || boardH === 0) return { fx: 0.5, fy: 0.5 };
  return {
    fx: (mmX - bounds.min_x) / boardW,
    fy: (mmY - bounds.min_y) / boardH,
  };
}

// Rendered image rect inside a contain-fitted container.
// offsetX/Y = left/top black-bar size in px; w/h = rendered image size in px.
interface ImageRect { offsetX: number; offsetY: number; w: number; h: number; }

function getContainRect(
  containerW: number,
  containerH: number,
  naturalW: number,
  naturalH: number
): ImageRect {
  if (naturalW === 0 || naturalH === 0) {
    return { offsetX: 0, offsetY: 0, w: containerW, h: containerH };
  }
  const containerAR = containerW / containerH;
  const imageAR     = naturalW / naturalH;
  let w: number, h: number;
  if (imageAR > containerAR) {
    // pillarbox — image fills width, bars on top/bottom
    w = containerW;
    h = containerW / imageAR;
  } else {
    // letterbox — image fills height, bars on left/right
    h = containerH;
    w = containerH * imageAR;
  }
  return {
    offsetX: (containerW - w) / 2,
    offsetY: (containerH - h) / 2,
    w,
    h,
  };
}

// Navigate a transform ref to center on (mmX, mmY).
// We must account for the letterbox offset when computing the pan target.
function panToMm(
  ref: React.MutableRefObject<ReactZoomPanPinchRef | null>,
  mmX: number,
  mmY: number,
  bounds: BoardBounds,
  zoom = 5,
  imgRect?: ImageRect
) {
  const transformRef = ref.current;
  if (!transformRef) return;
  const wrapper = transformRef.instance.wrapperComponent;
  if (!wrapper) return;
  const { clientWidth: cw, clientHeight: ch } = wrapper;

  const { fx, fy } = mmToFrac(mmX, mmY, bounds);

  // If we have the real image rect, map through it; else fall back to container
  const boardX = imgRect ? imgRect.offsetX + fx * imgRect.w : fx * cw;
  const boardY = imgRect ? imgRect.offsetY + fy * imgRect.h : fy * ch;

  // Pan so that boardX/boardY lands at the center of the wrapper
  const x = cw / 2 - boardX * zoom;
  const y = ch / 2 - boardY * zoom;
  transformRef.setTransform(x, y, zoom, 350, "easeOut");
}

// ─── Parse component changes from diff strings ────────────────────────────────

function parseComponentChanges(raw: string[]): ComponentChange[] {
  return raw
    .map((msg): ComponentChange | null => {
      // "Component added: U3"
      const added = msg.match(/Component added:\s*(\S+)/);
      if (added) return { type: "added", ref: added[1], message: msg };

      // "Component removed: R2"
      const removed = msg.match(/Component removed:\s*(\S+)/);
      if (removed) return { type: "removed", ref: removed[1], message: msg };

      // "Component moved: U1 (12.300,45.600) -> (15.100,48.200)"
      const moved = msg.match(/Component moved:\s*(\S+)\s+\(([^)]+)\)\s*->\s*\(([^)]+)\)/);
      if (moved) {
        const fromParts = moved[2].split(",").map(Number);
        const toParts   = moved[3].split(",").map(Number);
        return {
          type: "moved",
          ref: moved[1],
          message: msg,
          fromPos: { x: fromParts[0], y: fromParts[1] },
          toPos:   { x: toParts[0],  y: toParts[1]  },
        };
      }

      // "Components swapped positions: R1 <-> C3"
      const swapped = msg.match(/Components swapped positions:\s*(\S+)\s*<->\s*(\S+)/);
      if (swapped) return { type: "swapped", ref: `${swapped[1]}, ${swapped[2]}`, message: msg };

      // "Component value changed: R1 [10kohm] -> [2kohm]"
      const value = msg.match(/Component value changed:\s*(\S+)/);
      if (value) return { type: "value", ref: value[1], message: msg };

      // "Component footprint changed: U2 ..."
      const footprint = msg.match(/Component footprint changed:\s*(\S+)/);
      if (footprint) return { type: "footprint", ref: footprint[1], message: msg };

      // "Component layer changed: D1 ..."
      const layer = msg.match(/Component layer changed:\s*(\S+)/);
      if (layer) return { type: "layer", ref: layer[1], message: msg };

      // "Component rotation changed: J2 ..."
      const rotation = msg.match(/Component rotation changed:\s*(\S+)/);
      if (rotation) return { type: "rotation", ref: rotation[1], message: msg };

      // "Component re-annotated: R3 -> R5 ..."
      const annotated = msg.match(/Component re-annotated:\s*(\S+)/);
      if (annotated) return { type: "annotated", ref: annotated[1], message: msg };

      // "CRITICAL: REF pad N changed from X -> Y"
      const padNet = msg.match(/(?:CRITICAL|WARNING|INFO):\s*(\S+)\s+pad/);
      if (padNet) return { type: "reconnected", ref: padNet[1], message: msg };

      return null;
    })
    .filter(Boolean) as ComponentChange[];
}

const CHANGE_TYPE_COLOR: Record<ComponentChange["type"], string> = {
  added:       "#22c55e",
  removed:     "#ef4444",
  moved:       "#6366f1",
  swapped:     "#a855f7",
  value:       "#f59e0b",
  footprint:   "#f59e0b",
  layer:       "#f59e0b",
  rotation:    "#64748b",
  reconnected: "#ef4444",
  disconnected:"#ef4444",
  annotated:   "#64748b",
};

const CHANGE_TYPE_LABEL: Record<ComponentChange["type"], string> = {
  added:       "ADDED",
  removed:     "REMOVED",
  moved:       "MOVED",
  swapped:     "SWAPPED",
  value:       "VALUE",
  footprint:   "FOOTPRINT",
  layer:       "LAYER",
  rotation:    "ROTATION",
  reconnected: "NET",
  disconnected:"NET",
  annotated:   "REF",
};

// ─── Marker ──────────────────────────────────────────────────────────────────

function Marker({
  finding,
  pxPos,
  active,
  onClick,
  scale,
}: {
  finding: Finding;
  pxPos: { left: string; top: string };
  active: boolean;
  onClick: () => void;
  scale: number;
}) {
  const color = SEVERITY_COLOR[finding.severity];
  const markerScale = 1 / Math.max(scale, 0.5);

  return (
    <div
      onClick={(e) => { e.stopPropagation(); onClick(); }}
      title={finding.message}
      style={{
        position: "absolute",
        left: pxPos.left,
        top: pxPos.top,
        transform: `translate(-50%, -50%) scale(${markerScale})`,
        transformOrigin: "center center",
        cursor: "pointer",
        zIndex: 10,
      }}
    >
      {active && (
        <div style={{
          position: "absolute",
          inset: "-10px",
          borderRadius: "50%",
          border: `2px solid ${color}`,
          animation: "pulse-ring 1.2s ease-out infinite",
          pointerEvents: "none",
        }} />
      )}
      <div style={{
        width: 20,
        height: 20,
        borderRadius: "50%",
        background: active ? color : `${color}cc`,
        border: `2px solid ${active ? "#fff" : "rgba(255,255,255,0.5)"}`,
        boxShadow: active
          ? `0 0 0 3px ${color}44, 0 2px 10px rgba(0,0,0,0.5)`
          : "0 1px 5px rgba(0,0,0,0.4)",
        transition: "all 0.15s",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}>
        <span style={{
          fontSize: 9,
          fontWeight: 800,
          color: "#fff",
          fontFamily: "monospace",
          lineHeight: 1,
          userSelect: "none",
        }}>
          {finding.severity === "CRITICAL" ? "!" : finding.severity === "WARNING" ? "▲" : "i"}
        </span>
      </div>

      {/* Tooltip on active */}
      {active && (
        <div style={{
          position: "absolute",
          bottom: "calc(100% + 8px)",
          left: "50%",
          transform: "translateX(-50%)",
          background: "#0f1b29",
          border: `1px solid ${color}44`,
          borderRadius: 6,
          padding: "6px 10px",
          minWidth: 220,
          maxWidth: 320,
          zIndex: 50,
          pointerEvents: "none",
        }}>
          <div style={{
            fontSize: 10,
            fontWeight: 700,
            color,
            fontFamily: "'IBM Plex Mono', monospace",
            marginBottom: 3,
            letterSpacing: "0.06em",
          }}>
            {finding.severity} · {finding.category}
          </div>
          <div style={{
            fontSize: 11,
            color: "#cbd5e1",
            fontFamily: "'IBM Plex Mono', monospace",
            lineHeight: 1.4,
          }}>
            {finding.message}
          </div>
          {finding.coordinates && (
            <div style={{
              fontSize: 10,
              color: "#475569",
              fontFamily: "'IBM Plex Mono', monospace",
              marginTop: 4,
            }}>
              📍 {finding.coordinates.x.toFixed(2)}, {finding.coordinates.y.toFixed(2)} mm
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ─── Component position marker ────────────────────────────────────────────────

function CompMarker({
  change,
  pxPos,
  active,
  onClick,
  scale,
}: {
  change: ComponentChange;
  pxPos: { left: string; top: string };
  active: boolean;
  onClick: () => void;
  scale: number;
}) {
  const pos = change.toPos ?? change.fromPos;
  const color = CHANGE_TYPE_COLOR[change.type];
  const markerScale = 1 / Math.max(scale, 0.5);

  return (
    <div
      onClick={(e) => { e.stopPropagation(); onClick(); }}
      style={{
        position: "absolute",
        left: pxPos.left,
        top: pxPos.top,
        transform: `translate(-50%, -50%) scale(${markerScale})`,
        transformOrigin: "center center",
        cursor: "pointer",
        zIndex: 11,
      }}
    >
      {active && (
        <div style={{
          position: "absolute",
          inset: "-10px",
          borderRadius: 4,
          border: `2px solid ${color}`,
          animation: "pulse-ring 1.2s ease-out infinite",
          pointerEvents: "none",
        }} />
      )}
      <div style={{
        width: 22,
        height: 22,
        borderRadius: 4,
        background: active ? color : `${color}cc`,
        border: `2px solid ${active ? "#fff" : "rgba(255,255,255,0.4)"}`,
        boxShadow: active
          ? `0 0 0 3px ${color}44, 0 2px 10px rgba(0,0,0,0.5)`
          : "0 1px 5px rgba(0,0,0,0.4)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        fontSize: 8,
        fontWeight: 800,
        color: "#fff",
        fontFamily: "monospace",
        userSelect: "none",
      }}>
        {change.type === "added" ? "+" : change.type === "removed" ? "−" : "~"}
      </div>

      {active && (
        <div style={{
          position: "absolute",
          bottom: "calc(100% + 8px)",
          left: "50%",
          transform: "translateX(-50%)",
          background: "#0f1b29",
          border: `1px solid ${color}44`,
          borderRadius: 6,
          padding: "6px 10px",
          minWidth: 180,
          maxWidth: 280,
          zIndex: 50,
          pointerEvents: "none",
        }}>
          <div style={{
            fontSize: 10,
            fontWeight: 700,
            color,
            fontFamily: "'IBM Plex Mono', monospace",
            marginBottom: 3,
          }}>
            {CHANGE_TYPE_LABEL[change.type]} · {change.ref}
          </div>
          <div style={{
            fontSize: 11,
            color: "#cbd5e1",
            fontFamily: "'IBM Plex Mono', monospace",
            lineHeight: 1.4,
          }}>
            {change.message.replace(/^(?:CRITICAL|WARNING|INFO):\s*/, "")}
          </div>
          {pos && (
            <div style={{
              fontSize: 10,
              color: "#475569",
              fontFamily: "'IBM Plex Mono', monospace",
              marginTop: 4,
            }}>
              📍 {pos.x.toFixed(2)}, {pos.y.toFixed(2)} mm
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ─── Scale-aware marker layer ─────────────────────────────────────────────────
// The layer is absolutely positioned to match the *rendered* image rect inside
// the contain-fitted container, eliminating black-bar offsets.

function MarkerLayerInner({
  findings,
  compChanges,
  activeFinding,
  activeComp,
  bounds,
  onMarkerClick,
  onCompClick,
  showCompMarkers,
  imgRect,
}: {
  findings: Finding[];
  compChanges: ComponentChange[];
  activeFinding: Finding | null;
  activeComp: ComponentChange | null;
  bounds: BoardBounds;
  onMarkerClick: (f: Finding) => void;
  onCompClick: (c: ComponentChange) => void;
  showCompMarkers: boolean;
  imgRect: ImageRect;
}) {
  const { state } = useTransformContext();
  const scale = state.scale;

  // Position a marker at (mmX, mmY) in pixel space within the image rect
  function pxPos(mmX: number, mmY: number) {
    const { fx, fy } = mmToFrac(mmX, mmY, bounds);
    return {
      left: `${(imgRect.offsetX + fx * imgRect.w).toFixed(2)}px`,
      top:  `${(imgRect.offsetY + fy * imgRect.h).toFixed(2)}px`,
    };
  }

  return (
    <div style={{ position: "absolute", inset: 0, pointerEvents: "none" }}>
      <div style={{ position: "relative", width: "100%", height: "100%", pointerEvents: "auto" }}>
        {findings
          .filter((f) => f.coordinates && f.severity !== "INFO")
          .map((f, i) => (
            <Marker
              key={`f-${i}`}
              finding={f}
              pxPos={pxPos(f.coordinates!.x, f.coordinates!.y)}
              active={activeFinding === f}
              onClick={() => onMarkerClick(f)}
              scale={scale}
            />
          ))}
        {showCompMarkers &&
          compChanges
            .filter((c) => c.toPos || c.fromPos)
            .map((c, i) => {
              const pos = c.toPos ?? c.fromPos!;
              return (
                <CompMarker
                  key={`c-${i}`}
                  change={c}
                  pxPos={pxPos(pos.x, pos.y)}
                  active={activeComp === c}
                  onClick={() => onCompClick(c)}
                  scale={scale}
                />
              );
            })}
      </div>
    </div>
  );
}

// ─── Board Viewer ─────────────────────────────────────────────────────────────

function BoardViewer({
  src,
  label,
  findings,
  compChanges,
  activeFinding,
  activeComp,
  bounds,
  onMarkerClick,
  onCompClick,
  showMarkers,
  showCompMarkers,
  transformRef,
  onImgRectChange,
}: {
  src: string;
  label: string;
  findings: Finding[];
  compChanges: ComponentChange[];
  activeFinding: Finding | null;
  activeComp: ComponentChange | null;
  bounds: BoardBounds;
  onMarkerClick: (f: Finding) => void;
  onCompClick: (c: ComponentChange) => void;
  showMarkers: boolean;
  showCompMarkers: boolean;
  transformRef?: React.MutableRefObject<ReactZoomPanPinchRef | null>;
  onImgRectChange?: (r: ImageRect) => void;
}) {
  // Track the rendered image rectangle inside the contain-fitted container.
  // Re-computed whenever the container or image natural size changes.
  const containerRef = useRef<HTMLDivElement>(null);
  const imgRef       = useRef<HTMLImageElement>(null);
  const [imgRect, setImgRect] = useState<ImageRect>({ offsetX: 0, offsetY: 0, w: 1, h: 1 });

  const updateRect = useCallback(() => {
    const img = imgRef.current;
    const con = containerRef.current;
    if (!img || !con) return;
    const { naturalWidth: nw, naturalHeight: nh } = img;
    const { clientWidth: cw, clientHeight: ch } = con;
    if (!nw || !nh || !cw || !ch) return;
    const r = getContainRect(cw, ch, nw, nh);
    setImgRect(r);
    onImgRectChange?.(r);
  }, [onImgRectChange]);

  // Re-compute when the container resizes (zoom, window resize)
  useEffect(() => {
    const con = containerRef.current;
    if (!con) return;
    const ro = new ResizeObserver(updateRect);
    ro.observe(con);
    return () => ro.disconnect();
  }, [updateRect]);

  return (
    <div ref={containerRef} style={{ position: "relative", width: "100%", height: "100%", background: "#0f1117" }}>
      <div style={{
        position: "absolute",
        top: 8,
        left: 12,
        zIndex: 20,
        fontSize: 11,
        fontWeight: 700,
        letterSpacing: "0.12em",
        color: "#94a3b8",
        textTransform: "uppercase",
        fontFamily: "'IBM Plex Mono', monospace",
        background: "rgba(15,17,23,0.85)",
        padding: "3px 8px",
        borderRadius: 4,
        border: "1px solid #1e2d3d",
      }}>
        {label}
      </div>

      {/* Zoom controls */}
      <div style={{
        position: "absolute",
        bottom: 12,
        right: 12,
        zIndex: 20,
        display: "flex",
        flexDirection: "column",
        gap: 4,
      }}>
        {transformRef && (
          <>
            <button
              onClick={() => transformRef.current?.zoomIn(0.5)}
              style={zoomBtnStyle}
              title="Zoom in"
            >+</button>
            <button
              onClick={() => transformRef.current?.zoomOut(0.5)}
              style={zoomBtnStyle}
              title="Zoom out"
            >−</button>
            <button
              onClick={() => transformRef.current?.resetTransform()}
              style={{ ...zoomBtnStyle, fontSize: 9, letterSpacing: "0.04em" }}
              title="Reset view"
            >FIT</button>
          </>
        )}
      </div>

      <TransformWrapper
        ref={transformRef}
        limitToBounds={false}
        minScale={0.2}
        maxScale={30}
        smooth
        wheel={{ step: 0.08 }}
      >
        {() => (
          <TransformComponent
            wrapperStyle={{ width: "100%", height: "100%" }}
            contentStyle={{ width: "100%", height: "100%" }}
          >
            <div style={{ position: "relative", width: "100%", height: "100%" }}>
              <img
                ref={imgRef}
                src={src}
                alt={label}
                onLoad={updateRect}
                style={{ width: "100%", height: "100%", objectFit: "contain", display: "block" }}
                draggable={false}
              />
              {showMarkers && (
                <MarkerLayerInner
                  findings={findings}
                  compChanges={compChanges}
                  activeFinding={activeFinding}
                  activeComp={activeComp}
                  bounds={bounds}
                  onMarkerClick={onMarkerClick}
                  onCompClick={onCompClick}
                  showCompMarkers={showCompMarkers}
                  imgRect={imgRect}
                />
              )}
            </div>
          </TransformComponent>
        )}
      </TransformWrapper>
    </div>
  );
}

const zoomBtnStyle: React.CSSProperties = {
  width: 28,
  height: 28,
  background: "rgba(15,23,42,0.85)",
  border: "1px solid #1e2d3d",
  borderRadius: 5,
  color: "#94a3b8",
  fontSize: 14,
  fontWeight: 700,
  cursor: "pointer",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  fontFamily: "monospace",
  transition: "background 0.12s, color 0.12s",
};

// ─── Finding card ─────────────────────────────────────────────────────────────

function FindingCard({
  finding,
  active,
  onClick,
}: {
  finding: Finding;
  active: boolean;
  onClick: () => void;
}) {
  const color = SEVERITY_COLOR[finding.severity];
  const bg    = SEVERITY_BG[finding.severity];
  const ref   = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (active && ref.current) {
      ref.current.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }, [active]);

  return (
    <div
      ref={ref}
      onClick={onClick}
      style={{
        padding: "9px 11px",
        borderRadius: 6,
        marginBottom: 3,
        cursor: "pointer",
        background: active ? `${color}20` : bg,
        border: `1px solid ${active ? color : "transparent"}`,
        transition: "border-color 0.15s, background 0.15s",
      }}
    >
      <div style={{ display: "flex", alignItems: "flex-start", gap: 7 }}>
        <span style={{
          fontSize: 9,
          fontWeight: 800,
          color,
          fontFamily: "'IBM Plex Mono', monospace",
          letterSpacing: "0.06em",
          marginTop: 2,
          flexShrink: 0,
          minWidth: 54,
        }}>
          {finding.severity}
        </span>
        <span style={{
          fontSize: 11,
          color: "#cbd5e1",
          lineHeight: 1.5,
          fontFamily: "'IBM Plex Mono', monospace",
        }}>
          {finding.message}
        </span>
      </div>

      <div style={{ display: "flex", gap: 8, marginTop: 5, flexWrap: "wrap", alignItems: "center" }}>
        {finding.coordinates && (
          <span style={{
            fontSize: 9,
            color: "#475569",
            fontFamily: "'IBM Plex Mono', monospace",
          }}>
            📍 {finding.coordinates.x.toFixed(1)}, {finding.coordinates.y.toFixed(1)} mm
          </span>
        )}
        {finding.related_refs.slice(0, 5).map((r) => (
          <span key={r} style={{
            fontSize: 9,
            padding: "1px 5px",
            borderRadius: 3,
            background: "rgba(99,102,241,0.15)",
            color: "#818cf8",
            fontFamily: "'IBM Plex Mono', monospace",
            fontWeight: 600,
          }}>{r}</span>
        ))}
        {finding.related_refs.length > 5 && (
          <span style={{ fontSize: 9, color: "#475569" }}>
            +{finding.related_refs.length - 5} more
          </span>
        )}
        {!finding.coordinates && (
          <span style={{ fontSize: 9, color: "#334155", fontFamily: "'IBM Plex Mono', monospace" }}>
            no board location
          </span>
        )}
      </div>
    </div>
  );
}

// ─── Component change card ────────────────────────────────────────────────────

function CompChangeCard({
  change,
  active,
  hasPosition,
  onClick,
}: {
  change: ComponentChange;
  active: boolean;
  hasPosition: boolean;
  onClick: () => void;
}) {
  const color = CHANGE_TYPE_COLOR[change.type];
  const ref   = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (active && ref.current) {
      ref.current.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }, [active]);

  const pos = change.toPos ?? change.fromPos;

  return (
    <div
      ref={ref}
      onClick={onClick}
      style={{
        padding: "9px 11px",
        borderRadius: 6,
        marginBottom: 3,
        cursor: "pointer",
        background: active ? `${color}18` : "rgba(255,255,255,0.02)",
        border: `1px solid ${active ? color : "rgba(255,255,255,0.05)"}`,
        transition: "border-color 0.15s, background 0.15s",
      }}
    >
      <div style={{ display: "flex", alignItems: "flex-start", gap: 8 }}>
        <span style={{
          fontSize: 9,
          fontWeight: 800,
          color,
          fontFamily: "'IBM Plex Mono', monospace",
          letterSpacing: "0.06em",
          marginTop: 2,
          flexShrink: 0,
          minWidth: 60,
        }}>
          {CHANGE_TYPE_LABEL[change.type]}
        </span>
        <div style={{ flex: 1 }}>
          <div style={{
            fontSize: 11,
            fontWeight: 700,
            color: "#e2e8f0",
            fontFamily: "'IBM Plex Mono', monospace",
            marginBottom: 2,
          }}>
            {change.ref}
          </div>
          <div style={{
            fontSize: 10,
            color: "#64748b",
            fontFamily: "'IBM Plex Mono', monospace",
            lineHeight: 1.4,
            wordBreak: "break-word",
          }}>
            {change.message.replace(/^Component (?:added|removed|moved|value changed|footprint changed|layer changed|rotation changed|re-annotated|swapped positions):\s*/i, "").replace(/^(?:CRITICAL|WARNING|INFO):\s*/i, "")}
          </div>
        </div>
      </div>

      {(pos || hasPosition) && (
        <div style={{
          marginTop: 5,
          display: "flex",
          alignItems: "center",
          gap: 6,
        }}>
          {pos ? (
            <span style={{
              fontSize: 9,
              color: "#475569",
              fontFamily: "'IBM Plex Mono', monospace",
            }}>
              📍 {pos.x.toFixed(1)}, {pos.y.toFixed(1)} mm · <span style={{ color: "#6366f1" }}>click to navigate</span>
            </span>
          ) : (
            <span style={{ fontSize: 9, color: "#334155", fontFamily: "'IBM Plex Mono', monospace" }}>
              no board position
            </span>
          )}
        </div>
      )}
    </div>
  );
}

// ─── Findings panel ───────────────────────────────────────────────────────────

function FindingsPanel({
  findings,
  compChanges,
  activeFinding,
  activeComp,
  onSelectFinding,
  onSelectComp,
  tab,
  onTabChange,
  showCompMarkers,
  onToggleCompMarkers,
}: {
  findings: Finding[];
  compChanges: ComponentChange[];
  activeFinding: Finding | null;
  activeComp: ComponentChange | null;
  onSelectFinding: (f: Finding) => void;
  onSelectComp: (c: ComponentChange) => void;
  tab: SidebarTab;
  onTabChange: (t: SidebarTab) => void;
  showCompMarkers: boolean;
  onToggleCompMarkers: () => void;
}) {
  const [search, setSearch]             = useState("");
  const [severityFilter, setSeverity]   = useState<Set<Severity>>(new Set(["CRITICAL","WARNING","INFO"]));
  const [openCategories, setOpenCats]   = useState<Set<string>>(
    () => new Set(["ERC","POWER","DIFF_PAIR","GROUND","IMPEDANCE","BOM","COMPONENT"])
  );
  const compTypeFilter = new Set(["added","removed","moved","swapped","value","footprint","layer","rotation","reconnected","annotated"]);

  const toggleCat = (cat: string) =>
    setOpenCats((p) => {
      const n = new Set(p);
      if (n.has(cat)) { n.delete(cat); } else { n.add(cat); }
      return n;
    });

  const toggleSev = (s: Severity) =>
    setSeverity((p) => {
      const n = new Set(p);
      if (n.has(s)) { n.delete(s); } else { n.add(s); }
      return n;
    });

  // Filter findings
  const filteredFindings = useMemo(() => {
    const q = search.toLowerCase();
    return findings.filter((f) =>
      severityFilter.has(f.severity) &&
      (f.message.toLowerCase().includes(q) ||
       f.related_refs.some((r) => r.toLowerCase().includes(q)) ||
       f.category.toLowerCase().includes(q))
    );
  }, [findings, search, severityFilter]);

  // Filter comp changes
  const filteredComps = useMemo(() => {
    const q = search.toLowerCase();
    return compChanges.filter((c) =>
      compTypeFilter.has(c.type) &&
      (c.ref.toLowerCase().includes(q) || c.message.toLowerCase().includes(q))
    );
  }, [compChanges, search, compTypeFilter]);

  const byCategory = useMemo(() =>
    filteredFindings.reduce<Record<string, Finding[]>>((acc, f) => {
      (acc[f.category] = acc[f.category] || []).push(f);
      return acc;
    }, {}),
    [filteredFindings]
  );

  const critCount = findings.filter((f) => f.severity === "CRITICAL").length;
  const warnCount = findings.filter((f) => f.severity === "WARNING").length;

  return (
    <div style={{
      width: 360,
      flexShrink: 0,
      background: "#080c14",
      borderLeft: "1px solid #1e2d3d",
      display: "flex",
      flexDirection: "column",
      fontFamily: "'IBM Plex Mono', monospace",
      overflow: "hidden",
    }}>

      {/* Header */}
      <div style={{
        padding: "14px 14px 0",
        borderBottom: "1px solid #1e2d3d",
        flexShrink: 0,
      }}>
        {/* Tab row */}
        <div style={{ display: "flex", gap: 0, marginBottom: 12 }}>
          {([
            { id: "findings" as SidebarTab, label: "Findings", count: findings.length },
            { id: "components" as SidebarTab, label: "Components", count: compChanges.length },
          ] as const).map(({ id, label, count }) => (
            <button
              key={id}
              onClick={() => onTabChange(id)}
              style={{
                flex: 1,
                padding: "6px 0",
                background: "transparent",
                border: "none",
                borderBottom: `2px solid ${tab === id ? "#6366f1" : "transparent"}`,
                color: tab === id ? "#818cf8" : "#475569",
                fontSize: 11,
                fontWeight: 700,
                fontFamily: "'IBM Plex Mono', monospace",
                letterSpacing: "0.08em",
                cursor: "pointer",
                textTransform: "uppercase",
                transition: "color 0.15s, border-color 0.15s",
              }}
            >
              {label}
              <span style={{
                marginLeft: 6,
                fontSize: 10,
                padding: "1px 5px",
                borderRadius: 3,
                background: tab === id ? "rgba(99,102,241,0.2)" : "rgba(255,255,255,0.06)",
                color: tab === id ? "#818cf8" : "#475569",
              }}>
                {count}
              </span>
            </button>
          ))}
        </div>

        {/* Summary badges (findings tab) */}
        {tab === "findings" && (
          <div style={{ display: "flex", gap: 6, marginBottom: 10, flexWrap: "wrap" }}>
            {critCount > 0 && (
              <button
                onClick={() => toggleSev("CRITICAL")}
                style={{
                  fontSize: 10,
                  padding: "2px 8px",
                  borderRadius: 4,
                  background: severityFilter.has("CRITICAL") ? "rgba(239,68,68,0.18)" : "rgba(239,68,68,0.05)",
                  color: severityFilter.has("CRITICAL") ? "#ef4444" : "#7f1d1d",
                  fontWeight: 700,
                  border: `1px solid ${severityFilter.has("CRITICAL") ? "#ef444444" : "transparent"}`,
                  cursor: "pointer",
                  fontFamily: "'IBM Plex Mono', monospace",
                  transition: "all 0.15s",
                }}
              >
                {critCount} CRITICAL
              </button>
            )}
            {warnCount > 0 && (
              <button
                onClick={() => toggleSev("WARNING")}
                style={{
                  fontSize: 10,
                  padding: "2px 8px",
                  borderRadius: 4,
                  background: severityFilter.has("WARNING") ? "rgba(245,158,11,0.15)" : "rgba(245,158,11,0.04)",
                  color: severityFilter.has("WARNING") ? "#f59e0b" : "#78350f",
                  fontWeight: 700,
                  border: `1px solid ${severityFilter.has("WARNING") ? "#f59e0b44" : "transparent"}`,
                  cursor: "pointer",
                  fontFamily: "'IBM Plex Mono', monospace",
                  transition: "all 0.15s",
                }}
              >
                {warnCount} WARNING
              </button>
            )}
            {critCount === 0 && warnCount === 0 && (
              <span style={{
                fontSize: 10,
                padding: "2px 8px",
                borderRadius: 4,
                background: "rgba(34,197,94,0.1)",
                color: "#22c55e",
                fontWeight: 700,
              }}>
                ✓ All clear
              </span>
            )}
          </div>
        )}

        {/* Component tab controls */}
        {tab === "components" && (
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
            <button
              onClick={onToggleCompMarkers}
              style={{
                fontSize: 10,
                padding: "2px 8px",
                borderRadius: 4,
                background: showCompMarkers ? "rgba(99,102,241,0.18)" : "rgba(255,255,255,0.05)",
                color: showCompMarkers ? "#818cf8" : "#475569",
                border: `1px solid ${showCompMarkers ? "#6366f144" : "transparent"}`,
                cursor: "pointer",
                fontFamily: "'IBM Plex Mono', monospace",
                fontWeight: 700,
                transition: "all 0.15s",
              }}
            >
              {showCompMarkers ? "◉ markers on" : "○ markers off"}
            </button>
            <span style={{ fontSize: 10, color: "#334155" }}>
              {filteredComps.length} change{filteredComps.length !== 1 ? "s" : ""}
            </span>
          </div>
        )}

        {/* Search */}
        <div style={{ position: "relative", marginBottom: 10 }}>
          <span style={{
            position: "absolute",
            left: 9,
            top: "50%",
            transform: "translateY(-50%)",
            fontSize: 12,
            color: "#334155",
            pointerEvents: "none",
          }}>⌕</span>
          <input
            type="text"
            placeholder={tab === "findings" ? "Search findings…" : "Search components…"}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            style={{
              width: "100%",
              background: "rgba(255,255,255,0.04)",
              border: "1px solid #1e2d3d",
              borderRadius: 5,
              padding: "5px 8px 5px 26px",
              fontSize: 11,
              color: "#e2e8f0",
              fontFamily: "'IBM Plex Mono', monospace",
              outline: "none",
              boxSizing: "border-box",
            }}
          />
          {search && (
            <button
              onClick={() => setSearch("")}
              style={{
                position: "absolute",
                right: 7,
                top: "50%",
                transform: "translateY(-50%)",
                background: "none",
                border: "none",
                color: "#475569",
                cursor: "pointer",
                fontSize: 13,
                lineHeight: 1,
                padding: 0,
              }}
            >×</button>
          )}
        </div>
      </div>

      {/* Scrollable content */}
      <div style={{ flex: 1, overflowY: "auto", padding: "8px 10px" }}>

        {/* ── FINDINGS TAB ── */}
        {tab === "findings" && (
          Object.entries(byCategory).length === 0
            ? (
              <div style={{
                padding: "40px 0",
                textAlign: "center",
                color: "#334155",
                fontSize: 12,
                fontFamily: "'IBM Plex Mono', monospace",
              }}>
                {search ? "No findings match your search" : "No findings"}
              </div>
            )
            : Object.entries(byCategory).map(([cat, catFindings]) => {
              const isOpen = openCategories.has(cat);
              const hasCrit = catFindings.some((f) => f.severity === "CRITICAL");
              const hasWarn = catFindings.some((f) => f.severity === "WARNING");
              const dotColor = hasCrit ? SEVERITY_COLOR.CRITICAL : hasWarn ? SEVERITY_COLOR.WARNING : SEVERITY_COLOR.INFO;

              return (
                <div key={cat} style={{ marginBottom: 4 }}>
                  <button
                    onClick={() => toggleCat(cat)}
                    style={{
                      width: "100%",
                      display: "flex",
                      alignItems: "center",
                      gap: 7,
                      padding: "5px 7px",
                      background: "transparent",
                      border: "none",
                      cursor: "pointer",
                      borderRadius: 5,
                      marginBottom: isOpen ? 4 : 0,
                    }}
                  >
                    <span style={{ width: 6, height: 6, borderRadius: "50%", background: dotColor, flexShrink: 0 }} />
                    <span style={{
                      flex: 1,
                      textAlign: "left",
                      fontSize: 10,
                      fontWeight: 700,
                      color: "#94a3b8",
                      letterSpacing: "0.1em",
                      textTransform: "uppercase",
                    }}>
                      {CATEGORY_LABEL[cat] ?? cat}
                    </span>
                    <span style={{ fontSize: 10, color: "#475569", marginRight: 3 }}>{catFindings.length}</span>
                    <span style={{
                      fontSize: 9,
                      color: "#334155",
                      transform: isOpen ? "rotate(0deg)" : "rotate(-90deg)",
                      transition: "transform 0.15s",
                      display: "inline-block",
                    }}>▾</span>
                  </button>

                  {isOpen && catFindings.map((f, i) => (
                    <FindingCard
                      key={i}
                      finding={f}
                      active={activeFinding === f}
                      onClick={() => onSelectFinding(f)}
                    />
                  ))}
                </div>
              );
            })
        )}

        {/* ── COMPONENTS TAB ── */}
        {tab === "components" && (
          filteredComps.length === 0
            ? (
              <div style={{
                padding: "40px 0",
                textAlign: "center",
                color: "#334155",
                fontSize: 12,
                fontFamily: "'IBM Plex Mono', monospace",
              }}>
                {search ? "No components match your search" : "No component changes"}
              </div>
            )
            : filteredComps.map((c, i) => (
              <CompChangeCard
                key={i}
                change={c}
                active={activeComp === c}
                hasPosition={!!(c.toPos ?? c.fromPos)}
                onClick={() => onSelectComp(c)}
              />
            ))
        )}
      </div>
    </div>
  );
}

// ─── View mode bar ─────────────────────────────────────────────────────────────

function ViewModeBar({ mode, onChange }: { mode: ViewMode; onChange: (m: ViewMode) => void }) {
  const modes: { id: ViewMode; label: string }[] = [
    { id: "sidebyside", label: "Side by Side" },
    { id: "toggle",     label: "Toggle" },
    { id: "overlay",    label: "Overlay" },
  ];
  return (
    <div style={{ display: "flex", gap: 2, padding: "0 12px", alignItems: "center" }}>
      {modes.map(({ id, label }) => (
        <button
          key={id}
          onClick={() => onChange(id)}
          style={{
            padding: "4px 12px",
            borderRadius: 5,
            border: "1px solid",
            borderColor: mode === id ? "#6366f1" : "#1e2d3d",
            background: mode === id ? "rgba(99,102,241,0.15)" : "transparent",
            color: mode === id ? "#818cf8" : "#475569",
            fontSize: 11,
            fontWeight: 600,
            fontFamily: "'IBM Plex Mono', monospace",
            cursor: "pointer",
            letterSpacing: "0.04em",
            transition: "all 0.12s",
          }}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

// ─── Keyboard hint bar ────────────────────────────────────────────────────────

function KeyHint({ label, key: k }: { label: string; key: string }) {
  return (
    <span style={{
      display: "inline-flex",
      alignItems: "center",
      gap: 4,
      fontSize: 10,
      color: "#334155",
      fontFamily: "'IBM Plex Mono', monospace",
    }}>
      <kbd style={{
        padding: "1px 4px",
        background: "#0f1b29",
        border: "1px solid #1e2d3d",
        borderRadius: 3,
        fontSize: 9,
        color: "#475569",
      }}>{k}</kbd>
      {label}
    </span>
  );
}

// ─── Main App ──────────────────────────────────────────────────────────────────

export default function App() {
  const [data, setData]               = useState<DiffResponse | null>(null);
  const [error, setError]             = useState<string | null>(null);
  const [viewMode, setViewMode]       = useState<ViewMode>("sidebyside");
  const [activeFinding, setActiveFinding] = useState<Finding | null>(null);
  const [activeComp, setActiveComp]   = useState<ComponentChange | null>(null);
  const [toggleBoard, setToggleBoard] = useState<"after" | "before">("after");
  const [tab, setTab]                 = useState<SidebarTab>("findings");
  const [showCompMarkers, setShowCompMarkers] = useState(true);

  const afterTransformRef  = useRef<ReactZoomPanPinchRef | null>(null);
  const beforeTransformRef = useRef<ReactZoomPanPinchRef | null>(null);
  // Tracks the rendered image rect in the after-board viewer (for accurate pan)
  const afterImgRectRef = useRef<ImageRect>({ offsetX: 0, offsetY: 0, w: 1, h: 1 });

  useEffect(() => {
    fetchDiff().then(setData).catch((e: Error) => setError(e.message));
  }, []);

  const allFindings: Finding[] = useMemo(() =>
    data ? Object.values(data.findings).flat() : [],
    [data]
  );

  const compChanges: ComponentChange[] = useMemo(() => {
    if (!data) return [];
    // Combine component_changes + net_changes that reference component pads
    const raw = [...(data.components ?? []), ...(data.nets ?? [])];
    return parseComponentChanges(raw);
  }, [data]);

  const bounds: BoardBounds = useMemo(() =>
    data?.board_bounds ?? { min_x: 0, min_y: 0, max_x: 100, max_y: 100 },
    [data]
  );

  // Keyboard navigation through findings
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (!allFindings.length) return;
      if (e.key === "ArrowDown" || e.key === "j") {
        setActiveFinding((prev) => {
          const idx = prev ? allFindings.indexOf(prev) : -1;
          return allFindings[Math.min(idx + 1, allFindings.length - 1)];
        });
      } else if (e.key === "ArrowUp" || e.key === "k") {
        setActiveFinding((prev) => {
          const idx = prev ? allFindings.indexOf(prev) : allFindings.length;
          return allFindings[Math.max(idx - 1, 0)];
        });
      } else if (e.key === "Escape") {
        setActiveFinding(null);
        setActiveComp(null);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [allFindings]);

  const handleFindingSelect = useCallback((finding: Finding) => {
    setActiveFinding(finding);
    setActiveComp(null);
    setTab("findings");
    if (!finding.coordinates || !data?.board_bounds) return;
    panToMm(afterTransformRef, finding.coordinates.x, finding.coordinates.y, data.board_bounds, 5, afterImgRectRef.current);
  }, [data]);

  const handleCompSelect = useCallback((change: ComponentChange) => {
    setActiveComp(change);
    setActiveFinding(null);
    const pos = change.toPos ?? change.fromPos;
    if (!pos || !data?.board_bounds) return;
    panToMm(afterTransformRef, pos.x, pos.y, data.board_bounds, 6, afterImgRectRef.current);
  }, [data]);

  // Also navigate when active finding changes via keyboard
  useEffect(() => {
    if (!activeFinding?.coordinates || !data?.board_bounds) return;
    panToMm(afterTransformRef, activeFinding.coordinates.x, activeFinding.coordinates.y, data.board_bounds, 5, afterImgRectRef.current);
  }, [activeFinding, data]);

  if (error) return (
    <div style={{
      display: "flex", alignItems: "center", justifyContent: "center",
      height: "100vh", background: "#0a0e17", color: "#ef4444",
      fontFamily: "'IBM Plex Mono', monospace", fontSize: 14,
      flexDirection: "column", gap: 12,
    }}>
      <div>⚠ Error loading diff</div>
      <div style={{ fontSize: 11, color: "#7f1d1d" }}>{error}</div>
    </div>
  );

  if (!data) return (
    <div style={{
      display: "flex", alignItems: "center", justifyContent: "center",
      height: "100vh", background: "#0a0e17", color: "#475569",
      fontFamily: "'IBM Plex Mono', monospace", fontSize: 13, gap: 12,
    }}>
      <span style={{ animation: "spin 1s linear infinite", display: "inline-block" }}>◌</span>
      Loading diff…
    </div>
  );

  const critCount = allFindings.filter((f) => f.severity === "CRITICAL").length;
  const warnCount = allFindings.filter((f) => f.severity === "WARNING").length;

  // Decide what markers/components to pass to "after" board
  const afterFindings = allFindings;
  const afterComps    = compChanges;

  return (
    <div style={{
      display: "flex",
      flexDirection: "column",
      height: "100vh",
      background: "#0a0e17",
      overflow: "hidden",
    }}>

      {/* ── Top bar ── */}
      <div style={{
        height: 46,
        flexShrink: 0,
        borderBottom: "1px solid #1e2d3d",
        display: "flex",
        alignItems: "center",
        gap: 12,
        padding: "0 0 0 18px",
        background: "#060a10",
      }}>
        <span style={{
          fontSize: 13,
          fontWeight: 800,
          color: "#e2e8f0",
          fontFamily: "'IBM Plex Mono', monospace",
          letterSpacing: "0.12em",
          flexShrink: 0,
        }}>
          FLUX<span style={{ color: "#6366f1" }}>DIFF</span>
        </span>

        <div style={{ width: 1, height: 18, background: "#1e2d3d", flexShrink: 0 }} />

        <ViewModeBar mode={viewMode} onChange={setViewMode} />

        {viewMode === "toggle" && (
          <>
            <div style={{ width: 1, height: 18, background: "#1e2d3d" }} />
            <div style={{ display: "flex", gap: 2 }}>
              {(["after", "before"] as const).map((b) => (
                <button
                  key={b}
                  onClick={() => setToggleBoard(b)}
                  style={{
                    padding: "3px 9px",
                    borderRadius: 4,
                    border: "1px solid",
                    borderColor: toggleBoard === b ? "#6366f1" : "#1e2d3d",
                    background: toggleBoard === b ? "rgba(99,102,241,0.15)" : "transparent",
                    color: toggleBoard === b ? "#818cf8" : "#475569",
                    fontSize: 10,
                    fontWeight: 600,
                    fontFamily: "'IBM Plex Mono', monospace",
                    cursor: "pointer",
                    textTransform: "uppercase",
                  }}
                >
                  {b}
                </button>
              ))}
            </div>
          </>
        )}

        <div style={{ flex: 1 }} />

        {/* Keyboard hints */}
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <KeyHint label="prev" key="k" />
          <KeyHint label="next" key="j" />
          <KeyHint label="dismiss" key="Esc" />
        </div>

        <div style={{ width: 1, height: 18, background: "#1e2d3d" }} />

        {/* Status pills */}
        <div style={{ display: "flex", gap: 6, paddingRight: 16, alignItems: "center" }}>
          {critCount > 0 && (
            <span style={{
              fontSize: 10, padding: "2px 7px", borderRadius: 4,
              background: "rgba(239,68,68,0.14)", color: "#ef4444", fontWeight: 700,
              fontFamily: "'IBM Plex Mono', monospace",
            }}>
              {critCount} critical
            </span>
          )}
          {warnCount > 0 && (
            <span style={{
              fontSize: 10, padding: "2px 7px", borderRadius: 4,
              background: "rgba(245,158,11,0.12)", color: "#f59e0b", fontWeight: 700,
              fontFamily: "'IBM Plex Mono', monospace",
            }}>
              {warnCount} warnings
            </span>
          )}
          {critCount === 0 && warnCount === 0 && (
            <span style={{
              fontSize: 10, padding: "2px 7px", borderRadius: 4,
              background: "rgba(34,197,94,0.1)", color: "#22c55e", fontWeight: 700,
              fontFamily: "'IBM Plex Mono', monospace",
            }}>
              ✓ clean
            </span>
          )}
        </div>
      </div>

      {/* ── Main content ── */}
      <div style={{ flex: 1, display: "flex", overflow: "hidden" }}>

        {/* Board area */}
        <div style={{ flex: 1, display: "flex", overflow: "hidden" }}>

          {viewMode === "sidebyside" && (
            <>
              <div style={{ flex: 1, borderRight: "1px solid #1e2d3d" }}>
                <BoardViewer
                  src={BOARD_URLS.before}
                  label="BEFORE"
                  findings={[]}
                  compChanges={[]}
                  activeFinding={null}
                  activeComp={null}
                  bounds={bounds}
                  onMarkerClick={() => {}}
                  onCompClick={() => {}}
                  showMarkers={false}
                  showCompMarkers={false}
                  transformRef={beforeTransformRef}
                />
              </div>
              <div style={{ flex: 1 }}>
                <BoardViewer
                  src={BOARD_URLS.after}
                  label="AFTER"
                  findings={afterFindings}
                  compChanges={afterComps}
                  activeFinding={activeFinding}
                  activeComp={activeComp}
                  bounds={bounds}
                  onMarkerClick={handleFindingSelect}
                  onCompClick={handleCompSelect}
                  showMarkers
                  showCompMarkers={showCompMarkers && tab === "components"}
                  transformRef={afterTransformRef}
                  onImgRectChange={(r) => { afterImgRectRef.current = r; }}
                />
              </div>
            </>
          )}

          {viewMode === "toggle" && (
            <BoardViewer
              src={toggleBoard === "after" ? BOARD_URLS.after : BOARD_URLS.before}
              label={toggleBoard === "after" ? "AFTER" : "BEFORE"}
              findings={toggleBoard === "after" ? afterFindings : []}
              compChanges={toggleBoard === "after" ? afterComps : []}
              activeFinding={toggleBoard === "after" ? activeFinding : null}
              activeComp={toggleBoard === "after" ? activeComp : null}
              bounds={bounds}
              onMarkerClick={handleFindingSelect}
              onCompClick={handleCompSelect}
              showMarkers={toggleBoard === "after"}
              showCompMarkers={showCompMarkers && tab === "components" && toggleBoard === "after"}
              transformRef={afterTransformRef}
              onImgRectChange={(r) => { afterImgRectRef.current = r; }}
            />
          )}

          {viewMode === "overlay" && (
            <BoardViewer
              src={BOARD_URLS.overlay}
              label="DIFF OVERLAY"
              findings={afterFindings}
              compChanges={afterComps}
              activeFinding={activeFinding}
              activeComp={activeComp}
              bounds={bounds}
              onMarkerClick={handleFindingSelect}
              onCompClick={handleCompSelect}
              showMarkers
              showCompMarkers={showCompMarkers && tab === "components"}
              transformRef={afterTransformRef}
              onImgRectChange={(r) => { afterImgRectRef.current = r; }}
            />
          )}
        </div>

        {/* Sidebar */}
        <FindingsPanel
          findings={allFindings}
          compChanges={compChanges}
          activeFinding={activeFinding}
          activeComp={activeComp}
          onSelectFinding={handleFindingSelect}
          onSelectComp={handleCompSelect}
          tab={tab}
          onTabChange={setTab}
          showCompMarkers={showCompMarkers}
          onToggleCompMarkers={() => setShowCompMarkers((v) => !v)}
        />
      </div>

      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600;700;800&display=swap');

        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
          background: #0a0e17;
          color: #e2e8f0;
          overflow: hidden;
        }

        ::-webkit-scrollbar { width: 4px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: #1e2d3d; border-radius: 3px; }
        ::-webkit-scrollbar-thumb:hover { background: #2d3f53; }

        input::placeholder { color: #334155; }
        input:focus { border-color: #2d3f53 !important; }

        @keyframes pulse-ring {
          0%   { transform: scale(0.8); opacity: 1; }
          100% { transform: scale(2.6); opacity: 0; }
        }

        @keyframes spin {
          to { transform: rotate(360deg); }
        }
      `}</style>
    </div>
  );
}