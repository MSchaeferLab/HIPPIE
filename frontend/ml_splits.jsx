import React, { useState, useEffect, useRef } from "react";
import { createRoot } from "react-dom/client";

const cfg = window.SPLITS_CONFIG;

const STEP_LABELS = {
  building_graph:     "Building interaction graph…",
  partitioning:       "Partitioning graph into splits…",
  sampling_negatives: "Sampling negative edges…",
  pruning:            "Pruning isolated nodes…",
  writing_files:      "Writing CSV files…",
  done:               "Done",
  starting:           "Starting…",
};

function getCookie(name) {
  const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
  return m ? decodeURIComponent(m[1]) : "";
}

const FILTER_DEFS = [
  { key:"tissue",   label:"Tissue",    icon:"bi-heart-pulse",    format: v => `ID ${v}`,      empty:"Any tissue" },
  { key:"source",   label:"Source",    icon:"bi-database",       format: v => `ID ${v}`,      empty:"Any source" },
  { key:"minScore", label:"Min Score", icon:"bi-bar-chart-line", format: v => `≥ ${v}`, empty:"None",
    active: () => !!(cfg.minScore && cfg.minScore !== "0") },
];

function FilterSubCard({ label, icon, value, active }) {
  return (
    <div style={{
      background:"var(--hippie-bg)",
      border:`1px solid ${active ? "var(--hippie-teal)" : "var(--hippie-border)"}`,
      borderLeft:`3px solid ${active ? "var(--hippie-teal)" : "var(--hippie-border)"}`,
      borderRadius:"var(--radius-md)",
      padding:".75rem 1rem",
    }}>
      <div style={{
        fontSize:".62rem", fontFamily:"var(--font-mono)", textTransform:"uppercase",
        letterSpacing:".1em", color:"var(--hippie-ink-muted)", marginBottom:".3rem",
        display:"flex", alignItems:"center", gap:".3rem",
      }}>
        <i className={`bi ${icon}`}></i>{label}
      </div>
      <div style={{
        fontFamily:"var(--font-mono)", fontSize:".9rem", fontWeight:600,
        color: active ? "var(--hippie-teal)" : "var(--hippie-ink-muted)",
      }}>
        {value}
      </div>
    </div>
  );
}

function FilterSummary() {
  return (
    <div className="hippie-card mb-3">
      <div className="filter-section-label">Active Browse Filters</div>
      <div style={{display:"grid", gridTemplateColumns:"repeat(3,1fr)", gap:".75rem", marginBottom:".75rem"}}>
        {FILTER_DEFS.map(f => {
          const rawVal = cfg[f.key];
          const isActive = f.active ? f.active() : !!rawVal;
          return (
            <FilterSubCard key={f.key} label={f.label} icon={f.icon}
              value={isActive ? f.format(rawVal) : f.empty}
              active={isActive} />
          );
        })}
      </div>
      <p className="mb-0 text-muted-sm">
        <a href={cfg.browseUrl}>← Back to Browse</a>
        {" "}to change these filters.
      </p>
    </div>
  );
}

function SplitsForm({ onSubmit, disabled }) {
  const [negRatio, setNegRatio] = useState("1.0");
  const [seed,     setSeed]     = useState("78539105873");
  const [typeIds,  setTypeIds]  = useState([]);

  const toggleType = (id) =>
    setTypeIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);

  const handleSubmit = (e) => {
    e.preventDefault();
    onSubmit({
      neg_ratio:  parseFloat(negRatio),
      seed:       parseInt(seed),
      type_ids:   typeIds,
      min_score:  cfg.minScore ? parseFloat(cfg.minScore) : 0.0,
      tissue_ids: cfg.tissue   ? [parseInt(cfg.tissue)]   : [],
      source_ids: cfg.source   ? [parseInt(cfg.source)]   : [],
    });
  };

  return (
    <form onSubmit={handleSubmit}>
      <div className="hippie-card mb-3">
        <div className="filter-section-label">Negative Sampling</div>
        <div className="row g-3">
          <div className="col-md-6">
            <label className="form-label" htmlFor="neg-ratio">
              Negative ratio{" "}
              <span className="text-muted-sm" style={{fontSize:".75rem"}}>(neg edges per positive edge)</span>
            </label>
            <input id="neg-ratio" type="number" className="form-control"
                   min="0.1" max="10" step="0.1" value={negRatio} required
                   onChange={e => setNegRatio(e.target.value)} />
          </div>
          <div className="col-md-6">
            <label className="form-label" htmlFor="seed">Random seed</label>
            <input id="seed" type="number" className="form-control"
                   value={seed} required
                   onChange={e => setSeed(e.target.value)} />
          </div>
        </div>
      </div>

      {cfg.interactionTypes && cfg.interactionTypes.length > 0 && (
        <div className="hippie-card mb-3">
          <div className="filter-section-label">
            Interaction Type Filter{" "}
            <span style={{fontWeight:400, textTransform:"none", letterSpacing:0, fontSize:".78rem"}}>
              (optional — leave blank for all types)
            </span>
          </div>
          <div className="row g-2">
            {cfg.interactionTypes.map(t => (
              <div key={t.id} className="col-md-4 col-6">
                <div className="form-check">
                  <input className="form-check-input" type="checkbox"
                         id={`type-${t.id}`}
                         checked={typeIds.includes(t.id)}
                         onChange={() => toggleType(t.id)} />
                  <label className="form-check-label" htmlFor={`type-${t.id}`}
                         style={{fontSize:".85rem"}}>{t.name}</label>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      <button type="submit" disabled={disabled} style={{
        background:"var(--hippie-teal)", color:"#fff", border:"none",
        borderRadius:"var(--radius-md)", padding:".6rem 1.5rem",
        fontWeight:600, fontFamily:"var(--font-body)", fontSize:".95rem",
        cursor: disabled ? "not-allowed" : "pointer",
        opacity: disabled ? .6 : 1, transition:"opacity .15s",
      }}>
        <i className="bi bi-play-fill me-1"></i> Generate Splits
      </button>
    </form>
  );
}

function ProgressSection({ step, progress, jobId }) {
  const pct = Math.round(progress * 100);
  const isDone = step === "done";
  return (
    <div className="hippie-card mb-3 mt-4">
      <div className="filter-section-label">Job Progress</div>
      <div className="d-flex justify-content-between align-items-center mb-1">
        <span className="text-muted-sm">
          {!isDone && <span className="spinner-sm me-1"></span>}
          {STEP_LABELS[step] || step || "Starting…"}
        </span>
        <span className="text-muted-sm">{pct}%</span>
      </div>
      <div className="batch-progress">
        <div className="batch-progress-fill" style={{width:`${pct}%`}} />
      </div>
      {jobId && (
        <p className="mb-0 mt-1 mono"
           style={{fontSize:".75rem", color:"var(--hippie-ink-muted)"}}>
          Job ID: {jobId}
        </p>
      )}
    </div>
  );
}

function DownloadSection({ downloadUrl, summary }) {
  return (
    <div className="hippie-card mb-3 mt-4" style={{borderColor:"var(--hippie-teal)"}}>
      <div className="filter-section-label">Done — Download Your Splits</div>
      <a href={downloadUrl} style={{
        display:"inline-block",
        background:"var(--hippie-teal)", color:"#fff", border:"none",
        borderRadius:"var(--radius-md)", padding:".6rem 1.5rem",
        fontWeight:600, fontFamily:"var(--font-body)", fontSize:".95rem",
        textDecoration:"none",
      }}>
        <i className="bi bi-download me-1"></i> Download ZIP
      </a>
      {summary && summary.splits && (
        <div style={{display:"grid", gridTemplateColumns:"repeat(3,1fr)", gap:".75rem", marginTop:"1rem"}}>
          {summary.splits.map(s => {
            const total   = s.n_pos + s.n_neg;
            const posFrac = total > 0 ? s.n_pos / total : 0;
            return (
              <div key={s.name} style={{
                background:"var(--hippie-bg)",
                border:"1px solid var(--hippie-teal)",
                borderTop:"3px solid var(--hippie-teal)",
                borderRadius:"var(--radius-md)",
                padding:"1rem",
              }}>
                <div style={{
                  fontFamily:"var(--font-mono)", fontSize:".7rem", fontWeight:700,
                  textTransform:"uppercase", letterSpacing:".1em",
                  color:"var(--hippie-teal)", marginBottom:".35rem",
                }}>{s.name}</div>
                <div style={{
                  fontFamily:"var(--font-display)", fontSize:"1.6rem", lineHeight:1.1, marginBottom:".5rem",
                }}>{total.toLocaleString()}</div>
                <div style={{height:"4px", background:"var(--hippie-border)", borderRadius:"100px", overflow:"hidden", marginBottom:".4rem"}}>
                  <div style={{height:"100%", width:`${Math.round(posFrac*100)}%`, background:"var(--hippie-teal)", borderRadius:"100px"}} />
                </div>
                <div style={{
                  display:"flex", justifyContent:"space-between",
                  fontSize:".72rem", fontFamily:"var(--font-mono)", color:"var(--hippie-ink-muted)",
                }}>
                  <span><i className="bi bi-plus-circle me-1"></i>{s.n_pos.toLocaleString()} pos</span>
                  <span><i className="bi bi-dash-circle me-1"></i>{s.n_neg.toLocaleString()} neg</span>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ErrorSection({ message }) {
  return (
    <div className="hippie-card mb-3 mt-4" style={{borderColor:"var(--hippie-accent)"}}>
      <div className="filter-section-label" style={{color:"var(--hippie-accent)", borderColor:"var(--hippie-accent)"}}>Job Failed</div>
      <pre style={{fontSize:".8rem", whiteSpace:"pre-wrap", margin:0}}>{message}</pre>
    </div>
  );
}

function App() {
  const [phase,       setPhase]       = useState("idle");
  const [step,        setStep]        = useState("starting");
  const [progress,    setProgress]    = useState(0);
  const [jobId,       setJobId]       = useState(null);
  const [downloadUrl, setDownloadUrl] = useState(null);
  const [summary,     setSummary]     = useState(null);
  const [errorMsg,    setErrorMsg]    = useState(null);
  const pollRef = useRef(null);

  useEffect(() => () => clearTimeout(pollRef.current), []);

  function poll(id) {
    fetch(cfg.statusBase + id + "/")
      .then(r => r.json())
      .then(data => {
        setStep(data.step || "starting");
        setProgress(data.progress || 0);

        if (data.status === "DONE") {
          setProgress(1);
          setStep("done");
          setDownloadUrl(data.download_url);
          setSummary(data.summary);
          setPhase("done");
        } else if (data.status === "FAILED") {
          setErrorMsg(data.error || "Unknown error");
          setPhase("failed");
        } else {
          pollRef.current = setTimeout(() => poll(id), 1500);
        }
      })
      .catch(() => {
        pollRef.current = setTimeout(() => poll(id), 3000);
      });
  }

  async function handleSubmit(payload) {
    clearTimeout(pollRef.current);
    setPhase("running");
    setStep("starting");
    setProgress(0);
    setJobId(null);
    setDownloadUrl(null);
    setSummary(null);
    setErrorMsg(null);

    try {
      const r = await fetch(cfg.createUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCookie("csrftoken") },
        body: JSON.stringify(payload),
      });
      if (!r.ok) {
        const e = await r.json();
        throw e;
      }
      const data = await r.json();
      setJobId(data.job_id);
      poll(data.job_id);
    } catch (err) {
      setErrorMsg(err.detail || JSON.stringify(err));
      setPhase("failed");
    }
  }

  return (
    <div>
      <div className="hippie-hero">
        <h1>Generate<br /><em style={{color:"var(--hippie-teal)"}}>ML Splits</em></h1>
        <p>Configure the train / val / test partitioning of the HIPPIE interaction graph.
           The graph will be filtered by the parameters you selected on the Browse page,
           then split using Kernighan–Lin bisection with balanced negative sampling.</p>
      </div>

      <FilterSummary />

      <SplitsForm onSubmit={handleSubmit} disabled={phase === "running"} />

      {(phase === "running" || phase === "done") && (
        <ProgressSection
          step={phase === "done" ? "done" : step}
          progress={phase === "done" ? 1 : progress}
          jobId={jobId}
        />
      )}

      {phase === "done" && downloadUrl && (
        <DownloadSection downloadUrl={downloadUrl} summary={summary} />
      )}

      {phase === "failed" && errorMsg && (
        <ErrorSection message={errorMsg} />
      )}
    </div>
  );
}

createRoot(document.getElementById("hippie-ml-splits-app")).render(<App />);
