import React, { useState, useEffect, useRef } from "react";
import { createRoot } from "react-dom/client";
import { getCookie, InfoPopover, DL } from "./shared.jsx";
import {
  EMPTY_META,
  MLInteractionFilterPanel,
  MLProteinFilterPanel,
  allOptionIds,
  listNarrows,
} from "./filters.jsx";

const cfg = window.SPLITS_CONFIG;
const meta = cfg.meta || EMPTY_META;
const initial = cfg.initial || {};

const STEP_LABELS = {
  building_graph:     "Building interaction graph…",
  partitioning:       "Partitioning graph into splits…",
  sampling_negatives: "Sampling negative edges…",
  pruning:            "Pruning isolated nodes…",
  writing_files:      "Writing CSV files…",
  done:               "Done",
  starting:           "Starting…",
};

const TEAL  = "var(--hippie-teal)";
const RED   = "var(--hippie-accent, #e8590c)";
const GREY  = "var(--hippie-ink-muted)";

function toNum(v, fallback) {
  const n = parseFloat(v);
  return Number.isFinite(n) ? n : fallback;
}

// Seed editable filter state from the query-param hand-off (either Browse tab).
// An absent list means "no filter", which the page now shows as every box
// ticked — meta is injected by the template, so unlike the query pages we can
// seed synchronously instead of waiting for a fetch.
const seedList = (handoff, options) =>
  Array.isArray(handoff) && handoff.length ? handoff : allOptionIds(options);

const PROTEIN_INIT = {
  tissue:          seedList(initial.tissue_ids, meta.tissues),
  minRpkm:         toNum(initial.min_rpkm, 0),
  // Renamed from min_degree — it gates the global Protein.degree, not the degree
  // inside the filtered subgraph. The old key still arrives from older links.
  minDegree:       parseInt(initial.min_degree_global ?? initial.min_degree) || 0,
  minAvgScore:     toNum(initial.min_avg_score, 0),
  isoformMode:     initial.isoform_mode || "general",
};
const INTERACTION_INIT = {
  minScore:   toNum(initial.min_score, 0),
  maxScore:   initial.max_score === "" || initial.max_score == null ? 1 : toNum(initial.max_score, 1),
  source:     seedList(initial.source_ids, meta.sources),
  experiment: seedList(initial.experiment_ids, meta.experiments),
  type:       seedList(initial.type_ids, meta.interaction_types),
};

// ── Filter help text (definition lists shown in section-header pop-ups) ──────
// The two filter panels live in filters.jsx (shared with the query pages); only
// the sampling card is unique to this page.
const SAMPLING_HELP = DL([
  ["Negative ratio", "Number of sampled negative (non-interacting) edges generated per positive edge."],
  ["Random seed", "RNG seed for reproducible negative sampling and train/val/test partitioning."],
]);

// ── Statistics display primitives ───────────────────────────────────────────
// InfoPopover + DL are imported from shared.jsx (used across query pages).

const PROTEIN_STATS_HELP = DL([
  ["Proteins", "Filtered proteins that still have at least one surviving interaction under the current interaction filter."],
  ["Isoforms", "Surviving proteins that are UniProt isoform entries. Only counted when the isoform mode is “Isoforms” or “Both”."],
  ["Median degree (filtered set)", "Median number of surviving interactions per protein, counted only over edges that pass the current filter. This is a different quantity from the “Min. degree in all of HIPPIE” filter on the left, which is measured across the whole database — under a narrow source or score filter this number is usually far lower than that threshold."],
  ["Median avg score", "Median, across proteins, of each protein's own average interaction score over its surviving edges."],
  ["Proteins filtered out", "Proteins removed by the protein-level filter (tissue, RPKM, min-degree, min-avg-score, isoform exclusion) relative to the full protein table. Separate from “Orphaned by filter”, which counts proteins that pass the protein filter but lose all edges."],
  ["Orphaned by filter", "Proteins that pass the protein-level filter (tissue, RPKM, …) but lost every interaction to the score/type/source filter, leaving degree 0. Excluded from the medians above."],
  ["Node degree distribution", "Histogram of per-protein interaction counts (degree) under the current filter."],
]);

const INTERACTION_STATS_HELP = DL([
  ["Interactions", "Number of interactions passing the current filter."],
  ["Median score", "Median confidence score across the filtered interactions."],
  ["Experiment types", "Number of distinct experiment types among the filtered interactions."],
  ["Score distribution", "Histogram of interaction confidence scores under the current filter."],
]);

function Metric({ label, value }) {
  return (
    <div style={{
      background:"var(--hippie-bg)", border:"1px solid var(--hippie-border)",
      borderLeft:`3px solid ${TEAL}`, borderRadius:"var(--radius-md)", padding:".6rem .8rem",
    }}>
      <div style={{
        fontSize:".58rem", fontFamily:"var(--font-mono)", textTransform:"uppercase",
        letterSpacing:".08em", color:GREY, marginBottom:".25rem",
      }}>{label}</div>
      <div style={{fontFamily:"var(--font-display)", fontSize:"1.35rem", lineHeight:1.1}}>
        {value}
      </div>
    </div>
  );
}

function Histogram({ title, bars }) {
  const max = Math.max(1, ...bars.map(b => b.count));
  const PLOT_H = 96;  // px — drawing area height for the tallest bar
  return (
    <div className="mt-3">
      <div style={{fontSize:".62rem", fontFamily:"var(--font-mono)", textTransform:"uppercase",
                   letterSpacing:".1em", color:GREY, marginBottom:".5rem"}}>{title}</div>
      {/* Bars: bins along the X axis, height ∝ count. */}
      <div style={{display:"flex", alignItems:"flex-end", gap:"3px", height:`${PLOT_H}px`}}>
        {bars.map(b => (
          <div key={b.label} title={`${b.label}: ${b.count.toLocaleString()}`}
               style={{flex:"1 1 0", display:"flex", flexDirection:"column",
                       alignItems:"center", justifyContent:"flex-end", height:"100%", minWidth:0}}>
            <span className="mono" style={{fontSize:".58rem", color:GREY, lineHeight:1,
                                           marginBottom:"2px", whiteSpace:"nowrap"}}>
              {b.count > 0 ? b.count.toLocaleString() : ""}
            </span>
            <div style={{
              width:"100%",
              height:`${Math.max(b.count > 0 ? 2 : 0, Math.round((b.count / max) * (PLOT_H - 14)))}px`,
              background:TEAL, borderRadius:"3px 3px 0 0",
            }} />
          </div>
        ))}
      </div>
      {/* X axis line + bin labels */}
      <div style={{height:"1px", background:"var(--hippie-border)", margin:"0 0 3px"}} />
      <div style={{display:"flex", gap:"3px"}}>
        {bars.map(b => (
          <span key={b.label} className="mono"
                style={{flex:"1 1 0", textAlign:"center", fontSize:".55rem", color:GREY,
                        lineHeight:1.1, overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap"}}>
            {b.label}
          </span>
        ))}
      </div>
    </div>
  );
}

function StatsPlaceholder({ loading, error }) {
  return (
    <div className="text-muted-sm d-flex align-items-center justify-content-center text-center"
         style={{height:"100%", minHeight:"160px", color: error ? RED : GREY}}>
      {error
        ? <span><i className="bi bi-exclamation-circle me-1"></i>{error}</span>
        : loading
          ? <span style={{whiteSpace:"pre-line"}}>
              <span className="spinner-sm me-1"></span>{"Calculating…\nthis can take up to 30 seconds"}
            </span>
          : <span><i className="bi bi-bar-chart me-1"></i>Press “Calculate Statistics” to preview this filter set.</span>}
    </div>
  );
}

function ProteinStatsBox({ stats, loading, error }) {
  return (
    <div className="hippie-card mb-0" style={{height:"100%", borderTop:`3px solid ${TEAL}`}}>
      <div className="filter-section-label">
        Protein Statistics
        <InfoPopover title="Protein Statistics" html={PROTEIN_STATS_HELP} />
      </div>
      {!stats
        ? <StatsPlaceholder loading={loading} error={error} />
        : (
          <>
            <div style={{display:"grid", gridTemplateColumns:"repeat(2,1fr)", gap:".6rem"}}>
              <Metric label="Proteins" value={stats.n_proteins.toLocaleString()} />
              <Metric label="Isoforms" value={stats.n_isoforms.toLocaleString()} />
              <Metric label="Median degree (filtered set)" value={stats.median_degree} />
              <Metric label="Median avg score"
                      value={stats.median_avg_score == null ? "—" : stats.median_avg_score} />
              <Metric label="Proteins filtered out" value={stats.n_filtered_out.toLocaleString()} />
              <Metric label="Orphaned by filter" value={stats.n_orphaned_by_filter.toLocaleString()} />
            </div>
            {stats.degree_histogram && (
              <Histogram title="Node degree distribution" bars={stats.degree_histogram} />
            )}
          </>
        )}
    </div>
  );
}

function InteractionStatsBox({ stats, loading, error }) {
  return (
    <div className="hippie-card mb-0" style={{height:"100%", borderTop:`3px solid ${TEAL}`}}>
      <div className="filter-section-label">
        Interaction Statistics
        <InfoPopover title="Interaction Statistics" html={INTERACTION_STATS_HELP} />
      </div>
      {!stats
        ? <StatsPlaceholder loading={loading} error={error} />
        : (
          <>
            <div style={{display:"grid", gridTemplateColumns:"repeat(3,1fr)", gap:".6rem"}}>
              <Metric label="Interactions" value={stats.n_interactions.toLocaleString()} />
              <Metric label="Median score"
                      value={stats.median_score == null ? "—" : stats.median_score} />
              <Metric label="Experiment types"
                      value={`${stats.n_experiments}`} />
            </div>
            <Histogram title="Score distribution" bars={stats.score_histogram} />
          </>
        )}
    </div>
  );
}

// ── Negative-sampling config ────────────────────────────────────────────────
function SamplingCard({ negRatio, setNegRatio, seed, setSeed }) {
  return (
    <div className="hippie-card mb-3">
      <div className="filter-section-label">
        Negative Sampling
        <InfoPopover title="Negative Sampling" html={SAMPLING_HELP} />
      </div>
      <div className="row g-3">
        <div className="col-md-6">
          <label className="form-label" htmlFor="neg-ratio">
            Negative ratio{" "}
            <span className="text-muted-sm" style={{fontSize:".75rem"}}>(neg edges per positive edge)</span>
          </label>
          <input id="neg-ratio" type="number" className="form-control"
                 min="0.1" max="10" step="0.1" value={negRatio}
                 onChange={e => setNegRatio(e.target.value)} />
        </div>
        <div className="col-md-6">
          <label className="form-label" htmlFor="seed">Random seed</label>
          <input id="seed" type="number" className="form-control"
                 value={seed} onChange={e => setSeed(e.target.value)} />
        </div>
      </div>
    </div>
  );
}

// ── Run-history cards ─────────────────────────────────────────────────────
// Each generated run is its own self-polling card, newest on top. A card
// transitions running → done/failed in place; ids persist in localStorage
// so the history survives reloads, and ?jobs=id1,id2 deep-links finished runs.
const STORAGE_KEY = "hippie.ml_splits.runs";
const MAX_RUNS = 20;

function loadStoredRuns() {
  try {
    const arr = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    return Array.isArray(arr) ? arr.filter(x => typeof x === "string") : [];
  } catch { return []; }
}
function saveStoredRuns(ids) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(ids)); } catch { /* quota / disabled */ }
}

// One-line filter recap from the job's stored params (counts, not names — the
// status API returns only ids). Mirrors the "score 0.63–1.0 · 2 src …" recap.
function paramSummary(p) {
  if (!p) return null;
  const parts = [];
  if (p.min_score !== 0 && p.max_score !== 1){
    parts.push(`score min/max ${toNum(p.min_score, 0).toFixed(2)}–${toNum(p.max_score, 1).toFixed(2)}`)
  } else if (p.min_score !== 0) {
    parts.push(`score min ${toNum(p.min_score, 0).toFixed(2)}`)
  } else if (p.max_score !== 1) {
    parts.push(`score max ${toNum(p.max_score, 1).toFixed(2)}`)
  }
  if (p.source_ids?.length)     parts.push(`${p.source_ids.length} src`);
  if (p.experiment_ids?.length) parts.push(`${p.experiment_ids.length} exp`);
  if (p.type_ids?.length)       parts.push(`${p.type_ids.length} type`);
  if (p.tissue_ids?.length)     parts.push(`${p.tissue_ids?.length} tissue`);
  if (p.min_rpkm > 0)           parts.push(`min rpkm≥${p.min_rpkm}`);
  const minDeg = p.min_degree_global ?? p.min_degree;   // pre-rename jobs
  if (minDeg > 0)               parts.push(`min global deg≥${minDeg}`);
  if (p.min_avg_score > 0)      parts.push(`min avg≥${toNum(p.min_avg_score, 0).toFixed(2)}`);
  if (p.isoform_mode === "isoforms") parts.push("isoforms only");
  else if (p.isoform_mode === "both") parts.push("including isoforms");
  parts.push(`neg ratio ${p.neg_ratio}`);
  parts.push(`seed ${p.seed}`);
  return parts.join(" · ");
}

const PILL = {
  PENDING:   { bg:"var(--hippie-border)", fg:GREY,   label:"Queued"    },
  RUNNING:   { bg:TEAL,                   fg:"#fff",  label:"Running"   },
  DONE:      { bg:TEAL,                   fg:"#fff",  label:"Done"      },
  FAILED:    { bg:RED,                    fg:"#fff",  label:"Failed"    },
  NOT_FOUND: { bg:RED,                    fg:"#fff",  label:"Not Found" },
};

function StatusPill({ status }) {
  const s = PILL[status] || PILL.PENDING;
  return (
    <span style={{
      background:s.bg, color:s.fg, borderRadius:"100px", padding:".12rem .6rem",
      fontSize:".62rem", fontFamily:"var(--font-mono)", fontWeight:700,
      textTransform:"uppercase", letterSpacing:".08em", whiteSpace:"nowrap",
    }}>{s.label}</span>
  );
}

function SplitCards({ summary }) {
  if (!summary || !summary.splits) return null;
  return (
    <div style={{display:"grid", gridTemplateColumns:"repeat(3,1fr)", gap:".75rem", marginTop:"1rem"}}>
      {summary.splits.map(s => {
        const total = s.n_pos + s.n_neg;
        const posFrac = total > 0 ? s.n_pos / total : 0;
        return (
          <div key={s.name} style={{
            background:"var(--hippie-bg)", border:`1px solid ${TEAL}`,
            borderTop:`3px solid ${TEAL}`, borderRadius:"var(--radius-md)", padding:"1rem",
          }}>
            <div style={{fontFamily:"var(--font-mono)", fontSize:".7rem", fontWeight:700,
                         textTransform:"uppercase", letterSpacing:".1em", color:TEAL, marginBottom:".35rem"}}>
              {s.name}
            </div>
            <div style={{fontFamily:"var(--font-display)", fontSize:"1.6rem", lineHeight:1.1, marginBottom:".5rem"}}>
              {total.toLocaleString()}
            </div>
            <div style={{height:"4px", background:"var(--hippie-border)", borderRadius:"100px", overflow:"hidden", marginBottom:".4rem"}}>
              <div style={{height:"100%", width:`${Math.round(posFrac*100)}%`, background:TEAL, borderRadius:"100px"}} />
            </div>
            <div style={{display:"flex", justifyContent:"space-between",
                         fontSize:".72rem", fontFamily:"var(--font-mono)", color:GREY}}>
              <span><i className="bi bi-plus-circle me-1"></i>{s.n_pos.toLocaleString()} pos</span>
              <span><i className="bi bi-dash-circle me-1"></i>{s.n_neg.toLocaleString()} neg</span>
            </div>
            <div style={{fontSize:".72rem", fontFamily:"var(--font-mono)", color:GREY, marginTop:".3rem"}}>
              <i className="bi bi-diagram-3 me-1"></i>{(s.n_proteins ?? 0).toLocaleString()} proteins
            </div>
          </div>
        );
      })}
    </div>
  );
}

// Self-contained, self-polling run card. Fetches its own status until a
// terminal state, so several concurrent runs each track independently.
function RunCard({ jobId }) {
  const [data, setData] = useState(null);
  const [notFound, setNotFound] = useState(false);
  const [copied, setCopied] = useState(false);
  const timer = useRef(null);
  useEffect(() => {
    let cancelled = false;
    function tick() {
      fetch(cfg.statusBase + jobId + "/")
        .then(r => {
          if (r.status === 404) { if (!cancelled) setNotFound(true); return null; }
          if (!r.ok) throw new Error(String(r.status));
          return r.json();
        })
        .then(d => {
          if (cancelled || !d) return;
          setData(d);
          if (d.status !== "DONE" && d.status !== "FAILED")
            timer.current = setTimeout(tick, 1500);
        })
        .catch(() => { if (!cancelled) timer.current = setTimeout(tick, 3000); });
    }
    tick();
    return () => { cancelled = true; clearTimeout(timer.current); };
  }, [jobId]);

  const status = notFound ? "NOT_FOUND" : (data?.status || "PENDING");
  const running = status !== "DONE" && status !== "FAILED" && status !== "NOT_FOUND";
  const border  = status === "FAILED" || status === "NOT_FOUND" ? RED
                : status === "DONE" ? TEAL : "var(--hippie-border)";
  const pct     = Math.round((data?.progress || 0) * 100);
  const queued  = status === "PENDING" && (data?.queue_position || 0) > 0;
  const recap   = data ? paramSummary(data.params) : null;

  // Deep-link back to this page with the job id so a colleague can load + download it.
  const shareLink = () => {
    const url = `${window.location.origin}${window.location.pathname}?jobs=${jobId}`;
    const done = () => { setCopied(true); setTimeout(() => setCopied(false), 1500); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(done).catch(() => {});
    }
  };

  return (
    <div className="hippie-card mb-3" style={{borderColor:border}}>
      <div className="filter-section-label">Run</div>
      <div className="d-flex justify-content-between align-items-center mb-1">
        <span className="mono" style={{fontSize:".72rem", color:GREY, wordBreak:"break-all"}}>
          Job ID: {jobId}
        </span>
        <StatusPill status={status} />
      </div>
      {recap && (
        <p className="mb-2 mono" style={{fontSize:".72rem", color:GREY}}>{recap}</p>
      )}

      {notFound && (
        <p className="mb-0 text-muted-sm">Run not found — it may have been removed.</p>
      )}

      {!data && !notFound && (
        <p className="mb-0 text-muted-sm"><span className="spinner-sm me-1"></span>Loading…</p>
      )}

      {data && running && (
        <>
          <div className="d-flex justify-content-between align-items-center mb-1">
            <span className="text-muted-sm">
              <span className="spinner-sm me-1"></span>
              {queued
                ? `Queued — position ${data.queue_position}`
                : (STEP_LABELS[data.step] || data.step || "Starting…")}
            </span>
            <span className="text-muted-sm">{pct}%</span>
          </div>
          <div className="batch-progress">
            <div className="batch-progress-fill" style={{width:`${pct}%`}} />
          </div>
        </>
      )}

      {data && status === "DONE" && (
        <>
          <div className="d-flex gap-2 flex-wrap align-items-center">
            <a href={data.download_url} style={{
              display:"inline-block", background:TEAL, color:"#fff", border:"none",
              borderRadius:"var(--radius-md)", padding:".6rem 1.5rem",
              fontWeight:600, fontFamily:"var(--font-body)", fontSize:".95rem", textDecoration:"none",
            }}>
              <i className="bi bi-download me-1"></i> Download ZIP
            </a>
            <button type="button" onClick={shareLink} title="Copy a shareable link to this run" style={{
              display:"inline-block", background:"transparent", color:TEAL,
              border:`1px solid ${TEAL}`, borderRadius:"var(--radius-md)", padding:".6rem 1.5rem",
              fontWeight:600, fontFamily:"var(--font-body)", fontSize:".95rem", cursor:"pointer",
            }}>
              <i className={`bi ${copied ? "bi-check2" : "bi-share"} me-1`}></i>
              {copied ? "Copied!" : "Share Link"}
            </button>
          </div>
          <SplitCards summary={data.summary} />
        </>
      )}

      {data && status === "FAILED" && (
        <pre style={{fontSize:".8rem", whiteSpace:"pre-wrap", margin:0, color:RED}}>
          {data.error || "Unknown error"}
        </pre>
      )}
    </div>
  );
}

function App() {
  const [proteinFilters,     setProteinFiltersRaw]     = useState(PROTEIN_INIT);
  const [interactionFilters, setInteractionFiltersRaw] = useState(INTERACTION_INIT);
  const [negRatio, setNegRatio] = useState("1.0");
  const [seed,     setSeed]     = useState("78539105873");

  // Statistics
  const [statsFresh,   setStatsFresh]   = useState(false);
  const [statsLoading, setStatsLoading] = useState(false);
  const [statsError,   setStatsError]   = useState(null);
  const [proteinStats, setProteinStats] = useState(null);
  const [interStats,   setInterStats]   = useState(null);

  // Run history — ids of generated jobs, newest first. Each renders its own
  // self-polling RunCard. Persisted in localStorage so history survives a
  // reload; ?jobs=id1,id2 deep-links finished runs.
  const [runIds,      setRunIds]      = useState([]);
  const [submitting,  setSubmitting]  = useState(false);
  const [submitError, setSubmitError] = useState(null);
  // Mirrors localStorage exactly — deep-linked (?jobs=) ids are shown via
  // runIds but never enter storedIds, so visiting a shared link never
  // persists someone else's run ids into this browser's history.
  const storedIdsRef = useRef([]);

  useEffect(() => {
    const jobsParam = new URLSearchParams(window.location.search).get("jobs");
    const stored = loadStoredRuns();
    storedIdsRef.current = stored;
    if (jobsParam) {
      // Deep-linked ids first (de-duplicated), then any locally-saved runs
      // not already listed. Display-only — not written back to storage.
      const linked = [...new Set(jobsParam.split(",").map(s => s.trim()).filter(Boolean))];
      setRunIds([...linked, ...stored.filter(x => !linked.includes(x))].slice(0, MAX_RUNS));
    } else {
      setRunIds(stored);
    }
  }, []);

  function addRun(id) {
    const next = [id, ...storedIdsRef.current.filter(x => x !== id)].slice(0, MAX_RUNS);
    storedIdsRef.current = next;
    saveStoredRuns(next);
    setRunIds(prev => [id, ...prev.filter(x => x !== id)].slice(0, MAX_RUNS));
  }

  // Any filter change invalidates the computed statistics → re-grey and disable
  // the Generate button (re-enables only after a fresh Calculate) and stale the
  // stats boxes.
  function staleStats() {
    setStatsFresh(false);
    setProteinStats(null);
    setInterStats(null);
    setStatsError(null);
  }
  const setProteinFilters     = (f) => { setProteinFiltersRaw(f);     staleStats(); };
  const setInteractionFilters = (f) => { setInteractionFiltersRaw(f); staleStats(); };

  function buildPayload() {
    return {
      // interaction-level
      min_score:  interactionFilters.minScore,
      max_score:  interactionFilters.maxScore,
      // A full selection means the same thing as an empty one (the backend skips
      // the predicate), so send [] rather than 190 ids on every Calculate.
      source_ids: listNarrows(interactionFilters.source, meta.sources) ? interactionFilters.source : [],
      experiment_ids: listNarrows(interactionFilters.experiment, meta.experiments) ? interactionFilters.experiment : [],
      type_ids:   listNarrows(interactionFilters.type, meta.interaction_types) ? interactionFilters.type : [],
      // protein-level
      tissue_ids: listNarrows(proteinFilters.tissue, meta.tissues) ? proteinFilters.tissue : [],
      min_rpkm:   proteinFilters.minRpkm,
      min_degree_global: proteinFilters.minDegree,
      min_avg_score: proteinFilters.minAvgScore,
      isoform_mode: proteinFilters.isoformMode,
      // sampling
      neg_ratio:  toNum(negRatio, 1.0),
      seed:       parseInt(seed) || 0,
    };
  }

  async function handleCalculateStats() {
    setStatsLoading(true);
    setStatsError(null);
    try {
      const r = await fetch(cfg.statsUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCookie("csrftoken") },
        body: JSON.stringify(buildPayload()),
      });
      if (!r.ok) throw await r.json().catch(() => ({ detail: `Server error ${r.status}` }));
      const data = await r.json();
      setProteinStats(data.protein);
      setInterStats(data.interaction);
      setStatsFresh(true);
    } catch (err) {
      setStatsError(err.detail || "Failed to compute statistics");
    } finally {
      setStatsLoading(false);
    }
  }

  async function handleGenerate() {
    setSubmitting(true);
    setSubmitError(null);
    try {
      const r = await fetch(cfg.createUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCookie("csrftoken") },
        body: JSON.stringify(buildPayload()),
      });
      if (!r.ok) throw await r.json().catch(() => ({ detail: `Server error ${r.status}` }));
      const data = await r.json();
      addRun(data.job_id);  // prepends a self-polling RunCard
    } catch (err) {
      setSubmitError(err.detail || JSON.stringify(err));
    } finally {
      setSubmitting(false);
    }
  }

  // A run can only be generated once statistics are fresh (Generate stays grey +
  // disabled until then), and never while a create request is in flight.
  const canGenerate = statsFresh && !submitting;

  // Calculate is teal until stats are fresh, then greys out. Generate is greyed
  // out + disabled until stats are fresh, then turns teal (enabled).
  const calcStyle = {
    background: statsFresh ? "var(--hippie-border)" : TEAL,
    color: statsFresh ? GREY : "#fff",
    border:"none", borderRadius:"var(--radius-md)", padding:".6rem 1.5rem",
    fontWeight:600, fontFamily:"var(--font-body)", fontSize:".95rem",
    cursor: statsLoading ? "wait" : "pointer", opacity: statsLoading ? .7 : 1,
  };
  const genStyle = {
    background: statsFresh ? TEAL : "var(--hippie-border)",
    color: statsFresh ? "#fff" : GREY,
    border:"none", borderRadius:"var(--radius-md)", padding:".6rem 1.5rem",
    fontWeight:600, fontFamily:"var(--font-body)", fontSize:".95rem",
    cursor: canGenerate ? "pointer" : "not-allowed", opacity: submitting ? .7 : 1,
  };

  return (
    <div>
      <div className="hippie-hero">
        <h1>Generate<br /><em style={{color:TEAL}}>ML Splits</em></h1>
        <p>Configure the protein- and interaction-level filters, preview how restrictive they are
           with “Calculate Statistics”, then partition the resulting HIPPIE interaction graph into
           train / validation / test splits (Kernighan–Lin bisection with balanced negative sampling).</p>
      </div>

      {/* Nothing else on this page links to the ML guide, and a split built
          without reading it is easy to get wrong (leakage, degree bias). */}
      {cfg.mlGuideUrl && (
        <div className="hippie-note hippie-note--info mb-3">
          <strong>New to ML on PPI data?</strong> The{" "}
          <a href={cfg.mlGuideUrl}>Machine Learning guide</a> explains how to choose
          positives and negatives, where data leakage creeps into a PPI split, and how
          to train and evaluate a baseline on what you generate here. It also links the
          pre-computed splits on the{" "}
          {cfg.downloadUrl ? <a href={cfg.downloadUrl}>Download page</a> : "Download page"}{" "}
          if you would rather not configure your own.
        </div>
      )}

      {/* Protein filters + stats */}
      <div className="row g-3 mb-3">
        <div className="col-lg-6"><MLProteinFilterPanel meta={meta} filters={proteinFilters} onChange={setProteinFilters} /></div>
        <div className="col-lg-6"><ProteinStatsBox stats={proteinStats} loading={statsLoading} error={statsError} /></div>
      </div>

      {/* Interaction filters + stats */}
      <div className="row g-3 mb-3">
        <div className="col-lg-6"><MLInteractionFilterPanel meta={meta} filters={interactionFilters} onChange={setInteractionFilters} /></div>
        <div className="col-lg-6"><InteractionStatsBox stats={interStats} loading={statsLoading} error={statsError} /></div>
      </div>

      <SamplingCard negRatio={negRatio} setNegRatio={v => setNegRatio(v)}
                    seed={seed} setSeed={v => setSeed(v)} />

      <div className="d-flex gap-2 align-items-center">
        <button type="button" onClick={handleCalculateStats} disabled={statsLoading} style={calcStyle}>
          <i className="bi bi-calculator me-1"></i> Calculate Statistics
        </button>
        <button type="button" onClick={handleGenerate} disabled={!canGenerate} style={genStyle}>
          <i className="bi bi-play-fill me-1"></i> Generate Splits
        </button>
        {!statsFresh && !statsLoading && (
          <span className="text-muted-sm" style={{color:GREY}}>
            Tip: Calculate statistics before generating your split!
          </span>
        )}
      </div>

      {submitError && (
        <div className="hippie-card mb-3 mt-3" style={{borderColor:RED}}>
          <p className="mb-0" style={{color:RED, fontSize:".85rem"}}>
            <i className="bi bi-exclamation-circle me-1"></i>{submitError}
          </p>
        </div>
      )}

      {runIds.length > 0 && (
        <div className="mt-4">
          {runIds.map(id => <RunCard key={id} jobId={id} />)}
        </div>
      )}
    </div>
  );
}

createRoot(document.getElementById("hippie-ml-splits-app")).render(<App />);
