// Network Query — React (Batch 5). Converts the last server-rendered query
// page to React so it shares the exact FilterBox + InteractionTable used by the
// other pages. Seed proteins → first-shell sub-network ("set vs. HIPPIE"),
// rendered as a Cytoscape graph + the shared interaction table.

import React, { useState, useRef, useCallback, useEffect } from "react";
import { createRoot } from "react-dom/client";
import { getCookie } from "./shared.jsx";
import { InteractionTable } from "./tables.jsx";
import {
  FilterBox,
  FILTER_DEFAULTS,
  filtersToBody,
  filtersEqual,
  confThresholds,
  useFilterMeta,
} from "./filters.jsx";

const { apiUrl, filterMetaUrl, maxProteins } = window.HIPPIE_NQ_CONFIG;

const EXAMPLES = [
  { label: "Example — Huntington", text: "HTT, HAP40, HAP1, HIP1, TCERG1, MED15, SYT1, YKT6, SNAP47" },
  { label: "Example — tumor suppressors", text: "TP53\nBRCA1\nATM" },
  { label: "Example — EGFR signalling", text: "EGFR\nERBB2\nGRB2\nSOS1" },
];

// Split free-text into a de-duplicated list of identifiers (whitespace, commas,
// semicolons or tabs — one seed can appear on its own line or inline).
function parseSeeds(text) {
  const tokens = text
    .split(/[\s,;]+/)
    .map((t) => t.trim())
    .filter(Boolean);
  return [...new Set(tokens)];
}

function parseFile(file) {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = (e) => resolve(parseSeeds(e.target.result));
    reader.onerror = () => resolve([]);
    reader.readAsText(file);
  });
}

// Map one network API row into the shared InteractionTable shape.
function mapRow(row, i) {
  const side = (p) => ({
    id: p.id,
    symbol: p.symbol,
    uniprot: p.uniprot_id || null,
    entrez: p.gene_id ?? null,
    isoform: p.isoform_uniprot_id || null,
    is_reviewed: p.is_reviewed,
  });
  return {
    key: `${row.is_noninteraction ? "n" : "i"}-${row.id}-${i}`,
    a: side(row.a),
    b: side(row.b),
    score: row.score,
    sourceCount: row.source_count,
    experimentCount: row.experiment_count,
    isNoninteraction: !!row.is_noninteraction,
    detailUrl: row.detail_url || "",
    seedInteraction: !!row.seed_interaction,
  };
}

// ── Cytoscape graph ───────────────────────────────────────────────────────
function NetworkGraph({ rows, seedIds }) {
  const ref = useRef(null);
  const [note, setNote] = useState("");
  const VIS_LIMIT = 300;

  useEffect(() => {
    const cyLib = window.cytoscape;
    if (!cyLib || !ref.current || rows.length === 0) return;

    const seedSet = new Set(seedIds.map(String));
    let edges = rows;
    if (rows.length > VIS_LIMIT) {
      // Keep every seed–seed edge, then fill with the highest-scoring rest.
      const seedEdges = rows.filter((r) => r.seedInteraction);
      const others = rows.filter((r) => !r.seedInteraction);
      edges = [...seedEdges, ...others].slice(0, VIS_LIMIT);
      setNote(
        `Showing top ${VIS_LIMIT} of ${rows.length} edges by score. The full set is in the table below.`,
      );
    } else {
      setNote("");
    }

    const nodeId = (p) => p.symbol || p.uniprot || `#${p.id}`;
    const seen = new Set();
    const elements = [];
    edges.forEach((r) => {
      [r.a, r.b].forEach((p) => {
        const id = nodeId(p);
        if (!seen.has(id)) {
          seen.add(id);
          elements.push({ data: { id, label: id, isSeed: seedSet.has(String(p.id)) } });
        }
      });
      elements.push({
        data: {
          id: `edge-${r.key}`,
          source: nodeId(r.a),
          target: nodeId(r.b),
          score: r.score,
          isSeed: r.seedInteraction,
          isNonint: r.isNoninteraction,
          url: r.detailUrl,
        },
      });
    });

    const cy = cyLib({
      container: ref.current,
      elements,
      layout: {
        name: "cose",
        animate: true,
        animationDuration: 800,
        animationEasing: "ease-out",
        padding: 28,
        nodeRepulsion: 6000,
        idealEdgeLength: 80,
        randomize: true,
      },
      style: [
        {
          selector: "node",
          style: {
            label: "data(label)",
            "font-size": 10,
            "font-family": "DM Mono, monospace",
            "text-valign": "center",
            "text-halign": "center",
            "background-color": "#e3f0f0",
            "border-color": "#1a6b6b",
            "border-width": 1,
            color: "#1a1612",
            width: 60,
            height: 22,
            padding: "6px",
            shape: "round-rectangle",
          },
        },
        {
          selector: "node[?isSeed]",
          style: {
            "background-color": "#f9e8e6",
            "border-color": "#c0392b",
            "border-width": 2,
            "font-weight": 700,
          },
        },
        {
          selector: "edge",
          style: {
            width: "mapData(score, 0, 1, 0.8, 4)",
            "line-color": "mapData(score, 0, 1, #d4cfc9, #1a6b6b)",
            "curve-style": "bezier",
            "target-arrow-shape": "none",
            opacity: 0.75,
          },
        },
        {
          selector: "edge[?isSeed]",
          style: { "line-color": "#c0392b", opacity: 1 },
        },
        {
          selector: "edge[?isNonint]",
          style: {
            "line-color": "#b9b3ac",
            "line-style": "dashed",
            width: 1.2,
            opacity: 0.6,
          },
        },
        { selector: "node:active, node.hover", style: { "overlay-opacity": 0.08 } },
      ],
    });

    cy.on("tap", "edge", (evt) => {
      const url = evt.target.data("url");
      if (url) window.open(url, "_blank");
    });
    cy.on("mouseover", "node", (evt) => evt.target.addClass("hover"));
    cy.on("mouseout", "node", (evt) => evt.target.removeClass("hover"));

    return () => cy.destroy();
  }, [rows, seedIds]);

  const { med, high } = confThresholds("interactions");
  return (
    <>
      {note && (
        <div className="alert alert-info py-1 px-2 mb-2" style={{ fontSize: ".78rem" }}>
          {note}
        </div>
      )}
      <div
        ref={ref}
        style={{
          width: "100%",
          height: "520px",
          background: "var(--hippie-bg)",
          border: "1px solid var(--hippie-border)",
          borderRadius: "var(--radius-lg)",
        }}
      />
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: ".75rem 1.5rem",
          alignItems: "center",
          padding: ".5rem .75rem",
          fontSize: ".75rem",
          color: "var(--hippie-ink-muted)",
          border: "1px solid var(--hippie-border)",
          borderRadius: "var(--radius-md)",
          background: "var(--hippie-surface)",
          margin: ".75rem 0 1rem",
        }}
      >
        <strong style={{ fontSize: ".7rem", textTransform: "uppercase", letterSpacing: ".05em", color: "var(--hippie-ink)" }}>
          Edge legend
        </strong>
        <LegendSwatch color="#c0392b" h={3} label="Seed interaction (both in query set)" />
        <LegendSwatch color="#1a6b6b" h={3} label={`High conf. ≥ ${high.toFixed(2)}`} />
        <LegendSwatch color="#6b9e9e" h={2} label={`Medium conf. ≥ ${med.toFixed(2)}`} />
        <LegendSwatch color="#d4cfc9" h={1.5} label={`Low conf. < ${med.toFixed(2)}`} />
        <LegendSwatch color="#b9b3ac" h={1.5} dashed label="Non-interaction" />
        <span style={{ display: "flex", alignItems: "center", gap: ".4rem", marginLeft: "auto" }}>
          <em>Width ∝ score</em>
        </span>
      </div>
    </>
  );
}

function LegendSwatch({ color, h, label, dashed }) {
  return (
    <span style={{ display: "flex", alignItems: "center", gap: ".4rem" }}>
      <span
        style={{
          display: "inline-block",
          width: "32px",
          height: `${h}px`,
          borderRadius: "2px",
          background: dashed
            ? `repeating-linear-gradient(90deg, ${color} 0 5px, transparent 5px 9px)`
            : color,
        }}
      />
      {label}
    </span>
  );
}

// ── Page ────────────────────────────────────────────────────────────────
function App() {
  // Seed input (draft) -------------------------------------------------------
  const [mode, setMode] = useState("text"); // text | file
  const [text, setText] = useState("");
  const [file, setFile] = useState(null);
  const [dragOver, setDragOver] = useState(false);
  const [inputError, setInputError] = useState(null);
  const fileRef = useRef(null);

  // Filters (always visible; applied only on Search) -------------------------
  const [filters, setFilters] = useState(FILTER_DEFAULTS);
  const [appliedFilters, setAppliedFilters] = useState(FILTER_DEFAULTS);
  const [appliedSeedKey, setAppliedSeedKey] = useState("");
  const meta = useFilterMeta(filterMetaUrl);

  // Results ------------------------------------------------------------------
  const [rows, setRows] = useState([]);
  const [seedIds, setSeedIds] = useState([]);
  const [summary, setSummary] = useState(null); // { nodes, edges, unresolved, truncated, total }
  const [loading, setLoading] = useState(false);
  const [globalErr, setGlobalErr] = useState(null);
  const [showGraph, setShowGraph] = useState(true);
  const [searched, setSearched] = useState(false);
  const abortRef = useRef(null);

  const runQuery = useCallback(async (seeds, f) => {
    if (abortRef.current) abortRef.current.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true);
    setGlobalErr(null);
    setSearched(true);

    try {
      const res = await fetch(apiUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCookie("csrftoken") },
        body: JSON.stringify({ proteins: seeds.join("\n"), ...filtersToBody(f) }),
        signal: controller.signal,
      });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || `Server error ${res.status}`);
      setRows(data.interactions.map(mapRow));
      setSeedIds(data.seed_ids || []);
      setSummary({
        nodes: data.node_count,
        edges: data.edge_count,
        unresolved: data.unresolved || [],
        truncated: !!data.truncated,
        total: data.total_edges ?? data.edge_count,
      });
    } catch (err) {
      if (err.name !== "AbortError") {
        setGlobalErr(err.message);
        setRows([]);
        setSeedIds([]);
        setSummary(null);
      }
    } finally {
      setLoading(false);
    }
  }, []);

  const switchMode = (m) => {
    setMode(m);
    setInputError(null);
    if (m === "text") setFile(null);
    else setText("");
  };

  const handleDrop = (e) => {
    e.preventDefault();
    setDragOver(false);
    const f = e.dataTransfer.files[0];
    if (f) setFile(f);
  };

  const submit = async () => {
    setInputError(null);
    let seeds;
    if (mode === "text") {
      seeds = parseSeeds(text);
    } else {
      if (!file) return setInputError("Please select or drop a file.");
      seeds = await parseFile(file);
    }
    if (!seeds || seeds.length === 0) return setInputError("Please enter at least one seed protein.");
    if (seeds.length > maxProteins)
      return setInputError(`Too many seeds. Maximum is ${maxProteins.toLocaleString()}.`);
    setAppliedFilters(filters);
    setAppliedSeedKey(seedKey);
    runQuery(seeds, filters);
  };

  const onExampleClick = (exText) => {
    setMode("text");
    setFile(null);
    setText(exText);
    setInputError(null);
    const seeds = parseSeeds(exText);
    if (seeds.length > 0) {
      setAppliedFilters(filters);
      setAppliedSeedKey(exText.trim());
      runQuery(seeds, filters);
    }
  };

  const canSubmit = !loading && (mode === "text" ? text.trim().length > 0 : file !== null);
  // Unapplied-changes indicator: seed input or filters changed since last Search.
  const seedKey = mode === "text" ? text.trim() : file ? `${file.name}:${file.size}` : "";
  const dirty = !filtersEqual(filters, appliedFilters) || seedKey !== appliedSeedKey;
  const cyReady = typeof window !== "undefined" && !!window.cytoscape;

  return (
    <div>
      <div className="hippie-hero">
        <h1>
          Network
          <br />
          <em style={{ color: "var(--hippie-teal)" }}>Query</em>
        </h1>
        <p>
          Define a set of seed proteins to extract a confidence-scored interaction sub-network from HIPPIE
          (every edge touching a seed). Filter the sub-network with the controls on the left; switch the
          result type to include non-interactions.
        </p>
      </div>

      <div className="d-flex gap-4 align-items-start flex-wrap flex-md-nowrap">
        {/* LEFT: seed input + always-visible vertical filters */}
        <aside className="nq-sidebar">
          <div className="hippie-card p-3 mb-3">
            <div className="form-section-label">Seed Proteins</div>

            <div className="mode-toggle mb-3">
              <button className={mode === "text" ? "active" : ""} onClick={() => switchMode("text")}>
                <i className="bi bi-input-cursor-text me-1"></i>Text
              </button>
              <button className={mode === "file" ? "active" : ""} onClick={() => switchMode("file")}>
                <i className="bi bi-file-earmark-arrow-up me-1"></i>File
              </button>
            </div>

            {mode === "text" && (
              <textarea
                className="form-control mb-2"
                rows={5}
                placeholder={"Separate the identifiers with new lines or commas, e.g.\nHTT\nBRCA1\nTP53"}
                value={text}
                onChange={(e) => setText(e.target.value)}
              />
            )}

            {mode === "file" && (
              <>
                <div
                  className={`file-dropzone mb-2${dragOver ? " drag-over" : ""}${file ? " has-file" : ""}`}
                  onClick={() => fileRef.current.click()}
                  onDragOver={(e) => {
                    e.preventDefault();
                    setDragOver(true);
                  }}
                  onDragLeave={() => setDragOver(false)}
                  onDrop={handleDrop}
                >
                  <i className={file ? "bi bi-file-earmark-check" : "bi bi-file-earmark-arrow-up"}></i>
                  {file ? (
                    <>
                      <strong style={{ fontSize: ".875rem" }}>{file.name}</strong>
                      <span className="text-muted-sm">{(file.size / 1024).toFixed(1)} KB — click to change</span>
                    </>
                  ) : (
                    <>
                      <span style={{ fontSize: ".875rem", color: "var(--hippie-ink-muted)" }}>
                        Drop a file here or click to browse
                      </span>
                      <span className="text-muted-sm">One identifier per line — TSV, CSV, TXT</span>
                    </>
                  )}
                </div>
                <input
                  ref={fileRef}
                  type="file"
                  accept=".tsv,.csv,.txt,.tab"
                  style={{ display: "none" }}
                  onChange={(e) => {
                    if (e.target.files[0]) setFile(e.target.files[0]);
                  }}
                />
              </>
            )}

            {inputError && (
              <div
                className="d-flex align-items-start gap-2 mb-2 p-2 rounded"
                style={{ background: "var(--hippie-accent-soft)", color: "var(--hippie-accent)", fontSize: ".85rem" }}
              >
                <i className="bi bi-exclamation-circle mt-1" style={{ flexShrink: 0 }}></i>
                <span>{inputError}</span>
              </div>
            )}

            <div className="d-flex flex-wrap gap-1 mb-3">
              <span className="text-muted-sm w-100" style={{ fontFamily: "var(--font-mono)" }}>
                Try:
              </span>
              {EXAMPLES.map((ex, i) => (
                <span key={i} className="tag-chip example-chip" onClick={() => !loading && onExampleClick(ex.text)}>
                  {ex.label}
                </span>
              ))}
            </div>

            <button
              className="btn-nq-submit"
              onClick={submit}
              disabled={!canSubmit}
              style={dirty ? { background: "var(--hippie-accent)" } : undefined}
            >
              {loading ? (
                <>
                  <span className="spinner me-2"></span>Building…
                </>
              ) : (
                <>
                  <i className="bi bi-search me-1"></i>Search
                </>
              )}
              {dirty && !loading && <span className="search-dirty-dot" title="Unapplied changes — click Search"></span>}
            </button>
          </div>

          <div className="hippie-card p-3">
            <div className="form-section-label">Filters</div>
            <FilterBox value={filters} onChange={setFilters} meta={meta} layout="vertical" />
          </div>
        </aside>

        {/* RIGHT: results */}
        <div className="flex-grow-1 min-width-0">
          {globalErr && (
            <div
              className="hippie-card mb-3 text-center"
              style={{ borderColor: "var(--hippie-accent)", color: "var(--hippie-accent)" }}
            >
              <i className="bi bi-exclamation-circle fs-3 d-block mb-2"></i>
              <strong>{globalErr}</strong>
            </div>
          )}

          {summary && summary.unresolved.length > 0 && (
            <div className="alert alert-warning rounded-3 mb-3 py-2" style={{ fontSize: ".82rem" }}>
              <i className="bi bi-exclamation-triangle me-2"></i>
              <strong>
                {summary.unresolved.length} identifier{summary.unresolved.length !== 1 ? "s" : ""} not found:
              </strong>
              {summary.unresolved.map((u, i) => (
                <span key={i} className="tag-chip ms-1">
                  {u}
                </span>
              ))}
            </div>
          )}

          {summary && summary.truncated && (
            <div className="alert alert-info rounded-3 mb-3 py-2" style={{ fontSize: ".82rem" }}>
              <i className="bi bi-info-circle me-2"></i>
              Network capped at {summary.edges.toLocaleString()} of {summary.total.toLocaleString()} edges. Tighten
              the filters to narrow the result.
            </div>
          )}

          {rows.length > 0 && (
            <>
              <div className="d-flex justify-content-between align-items-baseline flex-wrap gap-2 mb-2">
                <span style={{ fontFamily: "var(--font-display)", fontSize: "1.2rem" }}>
                  Network results
                  <span className="text-muted-sm ms-2" style={{ fontFamily: "var(--font-body)", fontSize: ".875rem" }}>
                    — {summary.nodes.toLocaleString()} node{summary.nodes !== 1 ? "s" : ""},{" "}
                    {summary.edges.toLocaleString()} edge{summary.edges !== 1 ? "s" : ""}
                  </span>
                </span>
                {cyReady && (
                  <button className="btn-filter-toggle" onClick={() => setShowGraph((g) => !g)}>
                    <i className={`bi bi-diagram-3${showGraph ? "-fill" : ""}`}></i>
                    {showGraph ? "Hide graph" : "Show graph"}
                  </button>
                )}
              </div>

              {cyReady && showGraph && <NetworkGraph rows={rows} seedIds={seedIds} />}

              <InteractionTable
                rows={rows}
                title="Interactions"
                countLabel={`${rows.length.toLocaleString()} edge${rows.length !== 1 ? "s" : ""}`}
                exportFilename="hippie_network.tsv"
                showSeed
              />
            </>
          )}

          {!loading && searched && rows.length === 0 && !globalErr && (
            <div className="nq-results-placeholder">
              <i className="bi bi-search"></i>
              <p className="mb-1 fw-semibold">No edges matched</p>
              <p className="text-muted-sm mb-0">Try relaxing the score threshold or removing filters.</p>
            </div>
          )}

          {!searched && !loading && (
            <div className="nq-results-placeholder">
              <i className="bi bi-diagram-3"></i>
              <p className="mb-1 fw-semibold">No network built yet</p>
              <p className="text-muted-sm mb-0">
                Enter seed proteins on the left and click <em>Search</em>.
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

createRoot(document.getElementById("hippie-nq-app")).render(<App />);
