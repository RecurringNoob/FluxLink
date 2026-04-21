import { useState, useEffect, useRef, useCallback } from "react";
import { TransformWrapper, TransformComponent, useTransformContext } from "react-zoom-pan-pinch";
import type { ReactZoomPanPinchRef } from "react-zoom-pan-pinch";
import { fetchDiff, BOARD_URLS } from "./api";
import type { Finding, ViewMode, DiffResponse, BoardBounds, Severity } from "./types/finding";

// ─── Constants ──────────────────────────────────────────────────────────────

const SEVERITY_COLOR: Record<Severity, string> = {
  CRITICAL: "#ef4444",
  WARNING: "#f59e0b",
  INFO: "#22c55e",
};

const SEVERITY_BG: Record<Severity, string> = {
  CRITICAL: "rgba(239,68,68,0.12)",
  WARNING: "rgba(245,158,11,0.10)",
  INFO: "rgba(34,197,94,0.08)",
};

const CATEGORY_LABEL: Record<string, string> = {
  ERC: "ERC",
  POWER: "Power",
  DIFF_PAIR: "Diff Pairs",
  GROUND: "Ground",
  IMPEDANCE: "Impedance",
  BOM: "BOM",
  COMPONENT: "Components",
};

// ─── Coordinate mapping ──────────────────────────────────────────────────────

function mmToPercent(
  mmX: number,
  mmY: number,
  bounds: BoardBounds
): { left: string; top: string } {
  const boardW = bounds.max_x - bounds.min_x;
  const boardH = bounds.max_y - bounds.min_y;
  const left = ((mmX - bounds.min_x) / boardW) * 100;
  const top  = ((mmY - bounds.min_y) / boardH) * 100;
  return { left: `${left}%`, top: `${top}%` };
}

// ─── Marker component ────────────────────────────────────────────────────────

function Marker({
  finding,
  bounds,
  active,
  onClick,
  scale,
}: {
  finding: Finding;
  bounds: BoardBounds;
  active: boolean;
  onClick: () => void;
  scale: number;
}) {
  if (!finding.coordinates) return null;
  const { left, top } = mmToPercent(
    finding.coordinates.x,
    finding.coordinates.y,
    bounds
  );
  const color = SEVERITY_COLOR[finding.severity];
  const markerScale = 1 / scale;

  return (
    <div
      onClick={(e) => { e.stopPropagation(); onClick(); }}
      style={{
        position: "absolute",
        left,
        top,
        transform: `translate(-50%, -50%) scale(${markerScale})`,
        transformOrigin: "center center",
        cursor: "pointer",
        zIndex: 10,
      }}
    >
      {active && (
        <div style={{
          position: "absolute",
          inset: "-8px",
          borderRadius: "50%",
          border: `2px solid ${color}`,
          animation: "pulse-ring 1.2s ease-out infinite",
          pointerEvents: "none",
        }} />
      )}
      <div style={{
        width: 18,
        height: 18,
        borderRadius: "50%",
        background: color,
        border: `2px solid ${active ? "#fff" : "rgba(255,255,255,0.6)"}`,
        boxShadow: active
          ? `0 0 0 3px ${color}55, 0 2px 8px rgba(0,0,0,0.4)`
          : "0 1px 4px rgba(0,0,0,0.4)",
        transition: "box-shadow 0.15s, border-color 0.15s",
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
        }}>
          {finding.severity === "CRITICAL" ? "!" : finding.severity === "WARNING" ? "▲" : "i"}
        </span>
      </div>
    </div>
  );
}

// ─── Scale-aware marker layer ────────────────────────────────────────────────

function MarkerLayerInner({
  findings,
  activeFinding,
  bounds,
  onMarkerClick,
}: {
  findings: Finding[];
  activeFinding: Finding | null;
  bounds: BoardBounds;
  onMarkerClick: (f: Finding) => void;
}) {
  const { state } = useTransformContext();
  const scale = state.scale;

  return (
    <div style={{ position: "absolute", inset: 0, pointerEvents: "none" }}>
      <div style={{ position: "relative", width: "100%", height: "100%", pointerEvents: "auto" }}>
        {findings
          .filter((f) => f.coordinates && f.severity !== "INFO")
          .map((f, i) => (
            <Marker
              key={i}
              finding={f}
              bounds={bounds}
              active={activeFinding === f}
              onClick={() => onMarkerClick(f)}
              scale={scale}
            />
          ))}
      </div>
    </div>
  );
}

// ─── Board viewer ─────────────────────────────────────────────────────────────

function BoardViewer({
  src,
  label,
  findings,
  activeFinding,
  bounds,
  onMarkerClick,
  showMarkers,
  transformRef,
}: {
  src: string;
  label: string;
  findings: Finding[];
  activeFinding: Finding | null;
  bounds: BoardBounds;
  onMarkerClick: (f: Finding) => void;
  showMarkers: boolean;
  transformRef?: React.MutableRefObject<ReactZoomPanPinchRef | null>;
}) {
  return (
    <div style={{ position: "relative", width: "100%", height: "100%", background: "#0f1117" }}>
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
        background: "rgba(15,17,23,0.8)",
        padding: "3px 8px",
        borderRadius: 4,
        border: "1px solid #1e2d3d",
      }}>
        {label}
      </div>

      <TransformWrapper
        ref={transformRef}
        limitToBounds={false}
        minScale={0.3}
        maxScale={20}
        smooth
      >
        {() => (
          <>
            <TransformComponent
              wrapperStyle={{ width: "100%", height: "100%" }}
              contentStyle={{ width: "100%", height: "100%" }}
            >
              <div style={{ position: "relative", width: "100%", height: "100%" }}>
                <img
                  src={src}
                  alt={label}
                  style={{ width: "100%", height: "100%", objectFit: "contain", display: "block" }}
                  draggable={false}
                />
                {showMarkers && (
                  <MarkerLayerInner
                    findings={findings}
                    activeFinding={activeFinding}
                    bounds={bounds}
                    onMarkerClick={onMarkerClick}
                  />
                )}
              </div>
            </TransformComponent>
          </>
        )}
      </TransformWrapper>
    </div>
  );
}

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
        padding: "10px 12px",
        borderRadius: 6,
        marginBottom: 4,
        cursor: "pointer",
        background: active ? `${color}20` : bg,
        border: `1px solid ${active ? color : "transparent"}`,
        transition: "border-color 0.15s, background 0.15s",
      }}
    >
      <div style={{ display: "flex", alignItems: "flex-start", gap: 8 }}>
        <span style={{
          fontSize: 10,
          fontWeight: 800,
          color,
          fontFamily: "'IBM Plex Mono', monospace",
          letterSpacing: "0.06em",
          marginTop: 1,
          flexShrink: 0,
        }}>
          {finding.severity}
        </span>
        <span style={{
          fontSize: 12,
          color: "#cbd5e1",
          lineHeight: 1.5,
          fontFamily: "'IBM Plex Mono', monospace",
        }}>
          {finding.message}
        </span>
      </div>

      {finding.coordinates && (
        <div style={{
          marginTop: 4,
          fontSize: 10,
          color: "#475569",
          fontFamily: "'IBM Plex Mono', monospace",
        }}>
          📍 {finding.coordinates.x.toFixed(2)}, {finding.coordinates.y.toFixed(2)} mm
          {" · "}
          <span style={{ color: "#64748b" }}>click to navigate</span>
        </div>
      )}

      {finding.related_refs.length > 0 && (
        <div style={{ marginTop: 4, display: "flex", flexWrap: "wrap", gap: 3 }}>
          {finding.related_refs.slice(0, 6).map((r) => (
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
          {finding.related_refs.length > 6 && (
            <span style={{ fontSize: 9, color: "#475569", alignSelf: "center" }}>
              +{finding.related_refs.length - 6} more
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
  activeFinding,
  onSelect,
}: {
  findings: Finding[];
  activeFinding: Finding | null;
  onSelect: (f: Finding) => void;
}) {
  const [openCategories, setOpenCategories] = useState<Set<string>>(
    () => new Set(["ERC", "POWER", "DIFF_PAIR", "GROUND", "IMPEDANCE", "BOM"])
  );

  const toggleCategory = (cat: string) => {
    setOpenCategories((prev) => {
      const next = new Set(prev);
      next.has(cat) ? next.delete(cat) : next.add(cat);
      return next;
    });
  };

  const byCategory = findings.reduce<Record<string, Finding[]>>((acc, f) => {
    if (!acc[f.category]) acc[f.category] = [];
    acc[f.category].push(f);
    return acc;
  }, {});

  const criticalCount = findings.filter((f) => f.severity === "CRITICAL").length;
  const warningCount  = findings.filter((f) => f.severity === "WARNING").length;

  return (
    <div style={{
      width: 340,
      flexShrink: 0,
      background: "#0a0e17",
      borderLeft: "1px solid #1e2d3d",
      display: "flex",
      flexDirection: "column",
      fontFamily: "'IBM Plex Mono', monospace",
      overflow: "hidden",
    }}>
      {/* Header */}
      <div style={{
        padding: "16px 16px 12px",
        borderBottom: "1px solid #1e2d3d",
        flexShrink: 0,
      }}>
        <div style={{
          fontSize: 13,
          fontWeight: 700,
          color: "#e2e8f0",
          letterSpacing: "0.05em",
          marginBottom: 8,
        }}>
          FINDINGS
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          {criticalCount > 0 && (
            <span style={{
              fontSize: 11,
              padding: "2px 8px",
              borderRadius: 4,
              background: "rgba(239,68,68,0.15)",
              color: "#ef4444",
              fontWeight: 700,
            }}>
              {criticalCount} CRITICAL
            </span>
          )}
          {warningCount > 0 && (
            <span style={{
              fontSize: 11,
              padding: "2px 8px",
              borderRadius: 4,
              background: "rgba(245,158,11,0.12)",
              color: "#f59e0b",
              fontWeight: 700,
            }}>
              {warningCount} WARNING
            </span>
          )}
          {criticalCount === 0 && warningCount === 0 && (
            <span style={{
              fontSize: 11,
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
      </div>

      {/* Scrollable list */}
      <div style={{ flex: 1, overflowY: "auto", padding: "8px 10px" }}>
        {Object.entries(byCategory).map(([cat, catFindings]) => {
          const isOpen = openCategories.has(cat);
          const hasCrit = catFindings.some((f) => f.severity === "CRITICAL");
          const hasWarn = catFindings.some((f) => f.severity === "WARNING");
          const dotColor = hasCrit
            ? SEVERITY_COLOR.CRITICAL
            : hasWarn
            ? SEVERITY_COLOR.WARNING
            : SEVERITY_COLOR.INFO;

          return (
            <div key={cat} style={{ marginBottom: 4 }}>
              <button
                onClick={() => toggleCategory(cat)}
                style={{
                  width: "100%",
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  padding: "6px 8px",
                  background: "transparent",
                  border: "none",
                  cursor: "pointer",
                  borderRadius: 5,
                  marginBottom: isOpen ? 4 : 0,
                }}
              >
                <span style={{
                  width: 7,
                  height: 7,
                  borderRadius: "50%",
                  background: dotColor,
                  flexShrink: 0,
                }} />
                <span style={{
                  flex: 1,
                  textAlign: "left",
                  fontSize: 11,
                  fontWeight: 700,
                  color: "#94a3b8",
                  letterSpacing: "0.1em",
                  textTransform: "uppercase",
                }}>
                  {CATEGORY_LABEL[cat] ?? cat}
                </span>
                <span style={{ fontSize: 10, color: "#475569", marginRight: 4 }}>
                  {catFindings.length}
                </span>
                <span style={{
                  fontSize: 10,
                  color: "#475569",
                  transform: isOpen ? "rotate(0deg)" : "rotate(-90deg)",
                  transition: "transform 0.15s",
                  display: "inline-block",
                }}>
                  ▾
                </span>
              </button>

              {isOpen && catFindings.map((f, i) => (
                <FindingCard
                  key={i}
                  finding={f}
                  active={activeFinding === f}
                  onClick={() => onSelect(f)}
                />
              ))}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ─── View mode bar ────────────────────────────────────────────────────────────

function ViewModeBar({
  mode,
  onChange,
}: {
  mode: ViewMode;
  onChange: (m: ViewMode) => void;
}) {
  const modes: { id: ViewMode; label: string }[] = [
    { id: "sidebyside", label: "Side by Side" },
    { id: "toggle",     label: "Toggle" },
    { id: "overlay",    label: "Overlay" },
  ];
  return (
    <div style={{ display: "flex", gap: 2, padding: "0 16px", alignItems: "center" }}>
      {modes.map(({ id, label }) => (
        <button
          key={id}
          onClick={() => onChange(id)}
          style={{
            padding: "4px 14px",
            borderRadius: 5,
            border: "1px solid",
            borderColor: mode === id ? "#6366f1" : "#1e2d3d",
            background: mode === id ? "rgba(99,102,241,0.15)" : "transparent",
            color: mode === id ? "#818cf8" : "#475569",
            fontSize: 12,
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

// ─── Main App ─────────────────────────────────────────────────────────────────

export default function App() {
  const [data, setData]               = useState<DiffResponse | null>(null);
  const [error, setError]             = useState<string | null>(null);
  const [viewMode, setViewMode]       = useState<ViewMode>("sidebyside");
  const [activeFinding, setActiveFinding] = useState<Finding | null>(null);
  const [toggleBoard, setToggleBoard] = useState<"after" | "before">("after");

  const afterTransformRef  = useRef<ReactZoomPanPinchRef | null>(null);
  const beforeTransformRef = useRef<ReactZoomPanPinchRef | null>(null);

  useEffect(() => {
    fetchDiff()
      .then(setData)
      .catch((e: Error) => setError(e.message));
  }, []);

  const allFindings: Finding[] = data
    ? Object.values(data.findings).flat()
    : [];

  const handleFindingSelect = useCallback(
    (finding: Finding) => {
      setActiveFinding(finding);
      if (!finding.coordinates || !data?.board_bounds) return;

      const bounds = data.board_bounds;
      const boardW = bounds.max_x - bounds.min_x;
      const boardH = bounds.max_y - bounds.min_y;
      const pctX = (finding.coordinates.x - bounds.min_x) / boardW;
      const pctY = (finding.coordinates.y - bounds.min_y) / boardH;

      const ref = afterTransformRef.current;
      if (ref) {
        const wrapper = ref.instance.wrapperComponent;
        if (wrapper) {
          const { clientWidth: w, clientHeight: h } = wrapper;
          const scale = 4;
          const x = w / 2 - pctX * w * scale;
          const y = h / 2 - pctY * h * scale;
          ref.setTransform(x, y, scale, 400, "easeOut");
        }
      }
    },
    [data]
  );

  if (error) {
    return (
      <div style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        height: "100vh",
        background: "#0a0e17",
        color: "#ef4444",
        fontFamily: "'IBM Plex Mono', monospace",
        fontSize: 14,
      }}>
        Error: {error}
      </div>
    );
  }

  if (!data) {
    return (
      <div style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        height: "100vh",
        background: "#0a0e17",
        color: "#475569",
        fontFamily: "'IBM Plex Mono', monospace",
        fontSize: 13,
        gap: 12,
      }}>
        <span style={{ animation: "spin 1s linear infinite", display: "inline-block" }}>◌</span>
        Loading diff…
      </div>
    );
  }

  const bounds = data.board_bounds;

  return (
    <div style={{
      display: "flex",
      flexDirection: "column",
      height: "100vh",
      background: "#0a0e17",
      overflow: "hidden",
    }}>
      {/* Top bar */}
      <div style={{
        height: 48,
        flexShrink: 0,
        borderBottom: "1px solid #1e2d3d",
        display: "flex",
        alignItems: "center",
        gap: 16,
        padding: "0 0 0 20px",
        background: "#080c14",
      }}>
        <span style={{
          fontSize: 14,
          fontWeight: 800,
          color: "#e2e8f0",
          fontFamily: "'IBM Plex Mono', monospace",
          letterSpacing: "0.1em",
        }}>
          FLUX<span style={{ color: "#6366f1" }}>DIFF</span>
        </span>
        <div style={{ width: 1, height: 20, background: "#1e2d3d" }} />
        <ViewModeBar mode={viewMode} onChange={setViewMode} />

        {viewMode === "toggle" && (
          <>
            <div style={{ width: 1, height: 20, background: "#1e2d3d" }} />
            <div style={{ display: "flex", gap: 2 }}>
              {(["after", "before"] as const).map((b) => (
                <button
                  key={b}
                  onClick={() => setToggleBoard(b)}
                  style={{
                    padding: "3px 10px",
                    borderRadius: 4,
                    border: "1px solid",
                    borderColor: toggleBoard === b ? "#6366f1" : "#1e2d3d",
                    background: toggleBoard === b ? "rgba(99,102,241,0.15)" : "transparent",
                    color: toggleBoard === b ? "#818cf8" : "#475569",
                    fontSize: 11,
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
        <span style={{
          fontSize: 10,
          color: "#334155",
          fontFamily: "'IBM Plex Mono', monospace",
          paddingRight: 16,
        }}>
          {allFindings.filter((f) => f.severity === "CRITICAL").length} critical ·{" "}
          {allFindings.filter((f) => f.severity === "WARNING").length} warnings
        </span>
      </div>

      {/* Main content */}
      <div style={{ flex: 1, display: "flex", overflow: "hidden" }}>
        <div style={{ flex: 1, display: "flex", overflow: "hidden" }}>

          {viewMode === "sidebyside" && (
            <>
              <div style={{ flex: 1, borderRight: "1px solid #1e2d3d" }}>
                <BoardViewer
                  src={BOARD_URLS.before}
                  label="BEFORE"
                  findings={[]}
                  activeFinding={null}
                  bounds={bounds}
                  onMarkerClick={() => {}}
                  showMarkers={false}
                  transformRef={beforeTransformRef}
                />
              </div>
              <div style={{ flex: 1 }}>
                <BoardViewer
                  src={BOARD_URLS.after}
                  label="AFTER"
                  findings={allFindings}
                  activeFinding={activeFinding}
                  bounds={bounds}
                  onMarkerClick={handleFindingSelect}
                  showMarkers
                  transformRef={afterTransformRef}
                />
              </div>
            </>
          )}

          {viewMode === "toggle" && (
            <BoardViewer
              src={toggleBoard === "after" ? BOARD_URLS.after : BOARD_URLS.before}
              label={toggleBoard === "after" ? "AFTER" : "BEFORE"}
              findings={toggleBoard === "after" ? allFindings : []}
              activeFinding={toggleBoard === "after" ? activeFinding : null}
              bounds={bounds}
              onMarkerClick={handleFindingSelect}
              showMarkers={toggleBoard === "after"}
              transformRef={afterTransformRef}
            />
          )}

          {viewMode === "overlay" && (
            <BoardViewer
              src={BOARD_URLS.overlay}
              label="DIFF OVERLAY"
              findings={allFindings}
              activeFinding={activeFinding}
              bounds={bounds}
              onMarkerClick={handleFindingSelect}
              showMarkers
              transformRef={afterTransformRef}
            />
          )}
        </div>

        <FindingsPanel
          findings={allFindings}
          activeFinding={activeFinding}
          onSelect={handleFindingSelect}
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

        ::-webkit-scrollbar { width: 5px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: #1e2d3d; border-radius: 3px; }
        ::-webkit-scrollbar-thumb:hover { background: #2d3f53; }

        @keyframes pulse-ring {
          0%   { transform: scale(0.8); opacity: 1; }
          100% { transform: scale(2.4); opacity: 0; }
        }

        @keyframes spin {
          to { transform: rotate(360deg); }
        }
      `}</style>
    </div>
  );
}