import React, { useState, useEffect, useRef } from "react";
import { createRoot } from "react-dom/client";
import { Pagination } from "./shared.jsx";

const {
  proteinsApiUrl,
  interactionsApiUrl,
  exportApiUrl,
  filterMetaUrl,
  proteinDetailUrl,
  proteinQueryUrl,
  mlSplitsUrl,
} = window.HIPPIE_BROWSE_CONFIG;

const PAGE_SIZES = [10, 20, 50];
const DEFAULT_PAGE_SIZE = 50;

const PROTEIN_DEFAULTS = { tissue: [], source: [], minDegree: 0, minScore: 0, minRpkm: 0, includeIsoforms: false };
const INTERACTION_DEFAULTS = { minScore: 0, maxScore: 1, source: [], experiment: [], includeIsoforms: false };

function scoreClass(s) {
  return s >= 0.72 ? "score-high" : s >= 0.63 ? "score-med" : "score-low";
}

// ── Copy / TSV export of ALL matching rows (server-side, capped) ────────────
function ExportBar({ url, mode, disabled }) {
  const [busy, setBusy] = useState(false);
  const [msg,  setMsg]  = useState("");
  const flash = (m) => { setMsg(m); if (m) setTimeout(() => setMsg(""), 4000); };

  const run = async (action) => {
    setBusy(true); setMsg("");
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`Export failed (${res.status})`);
      const text = await res.text();
      const truncated = res.headers.get("X-Export-Truncated") === "1";
      if (action === "copy") {
        await navigator.clipboard.writeText(text);
        flash(truncated ? "Copied (first 50k)" : "Copied");
      } else {
        const a = Object.assign(document.createElement("a"), {
          href: URL.createObjectURL(new Blob([text], { type: "text/tab-separated-values" })),
          download: `hippie_browse_${mode}.tsv`,
        });
        a.click(); URL.revokeObjectURL(a.href);
        if (truncated) flash("Downloaded (first 50k)");
      }
    } catch (e) {
      flash(e.message || "Export error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="export-bar d-inline-flex align-items-center gap-2">
      <button disabled={disabled || busy} onClick={() => run("copy")}>
        <i className="bi bi-clipboard me-1"></i>Copy
      </button>
      <button disabled={disabled || busy} onClick={() => run("download")}>
        <i className="bi bi-download me-1"></i>TSV
      </button>
      {msg && <span className="text-muted-sm">{msg}</span>}
    </div>
  );
}

// ── Reusable multi-select checkbox list ──────────────────────────────────
function CheckboxList({ items, selected, onToggle }) {
  const selSet = new Set(selected.map(String));
  return (
    <div style={{
      maxHeight: "180px", overflowY: "auto", border: "1px solid var(--hippie-border)",
      borderRadius: "var(--radius-md)", padding: ".4rem .6rem",
    }}>
      {items.length === 0 && <span className="text-muted-sm">None available</span>}
      {items.map(it => (
        <label key={it.id} style={{display:"flex",alignItems:"center",gap:".4rem",cursor:"pointer",padding:".15rem 0"}}>
          <input type="checkbox" checked={selSet.has(String(it.id))}
                 onChange={() => onToggle(it.id)} style={{cursor:"pointer"}} />
          <span className="text-muted-sm" style={{color:"var(--hippie-ink)"}}>{it.name}</span>
        </label>
      ))}
    </div>
  );
}

function toggleIn(arr, id) {
  return arr.map(String).includes(String(id))
    ? arr.filter(x => String(x) !== String(id))
    : [...arr, id];
}

// ── Proteins filter panel ─────────────────────────────────────────────────
function ProteinFilterPanel({ filters, onChange, meta }) {
  return (
    <div className="filter-panel mb-3">
      <div className="row g-3">
        <div className="col-md-4">
          <div className="filter-section-label">Tissue Expression</div>
          <label className="form-label">Expressed in any selected tissue</label>
          <CheckboxList items={meta.tissues} selected={filters.tissue}
            onToggle={id => onChange({ ...filters, tissue: toggleIn(filters.tissue, id) })} />
          {filters.tissue.length > 0 && (
            <>
              <label className="form-label mt-2">Min. median RPKM ≥</label>
              <input type="number" className="form-control" min="0" step="1" placeholder="0"
                     value={filters.minRpkm || ""}
                     onChange={e => onChange({ ...filters, minRpkm: parseFloat(e.target.value) || 0 })} />
            </>
          )}
        </div>
        <div className="col-md-4">
          <div className="filter-section-label">Source Database</div>
          <label className="form-label">Has interaction in any selected source</label>
          <CheckboxList items={meta.sources} selected={filters.source}
            onToggle={id => onChange({ ...filters, source: toggleIn(filters.source, id) })} />
        </div>
        <div className="col-md-4">
          <div className="filter-section-label">Quantitative Filters</div>
          <label className="form-label">
            Min. degree ≥ <span className="mono">{filters.minDegree || 0}</span>
          </label>
          <input type="range" className="form-range mb-2" min="0" max="500" step="5"
                 value={filters.minDegree || 0}
                 onChange={e => onChange({ ...filters, minDegree: parseInt(e.target.value) })} />
          <label className="form-label">
            Min. avg score ≥ <span className="mono">{(filters.minScore || 0).toFixed(2)}</span>
          </label>
          <input type="range" className="form-range mb-3" min="0" max="1" step="0.01"
                 value={filters.minScore || 0}
                 onChange={e => onChange({ ...filters, minScore: parseFloat(e.target.value) })} />
          <div className="filter-section-label">Isoforms</div>
          <label style={{display:"inline-flex",alignItems:"center",gap:".5rem",cursor:"pointer",userSelect:"none"}}>
            <input type="checkbox" checked={filters.includeIsoforms}
                   onChange={e => onChange({ ...filters, includeIsoforms: e.target.checked })}
                   style={{cursor:"pointer"}} />
            <span className="text-muted-sm">Include isoforms</span>
          </label>
        </div>
      </div>
    </div>
  );
}

// ── Interactions filter panel ──────────────────────────────────────────────
function InteractionFilterPanel({ filters, onChange, meta }) {
  return (
    <div className="filter-panel mb-3">
      <div className="row g-3">
        <div className="col-md-4">
          <div className="filter-section-label">Confidence Score</div>
          <label className="form-label">
            Min. score ≥ <span className="mono">{(filters.minScore || 0).toFixed(2)}</span>
          </label>
          <input type="range" className="form-range mb-2" min="0" max="1" step="0.01"
                 value={filters.minScore || 0}
                 onChange={e => onChange({ ...filters, minScore: parseFloat(e.target.value) })} />
          <label className="form-label">
            Max. score ≤ <span className="mono">{(filters.maxScore ?? 1).toFixed(2)}</span>
          </label>
          <input type="range" className="form-range" min="0" max="1" step="0.01"
                 value={filters.maxScore ?? 1}
                 onChange={e => onChange({ ...filters, maxScore: parseFloat(e.target.value) })} />
        </div>
        <div className="col-md-4">
          <div className="filter-section-label">Source Database</div>
          <label className="form-label">In any selected source</label>
          <CheckboxList items={meta.sources} selected={filters.source}
            onToggle={id => onChange({ ...filters, source: toggleIn(filters.source, id) })} />
        </div>
        <div className="col-md-4">
          <div className="filter-section-label">Experimental System</div>
          <label className="form-label">Detected by any selected method</label>
          <CheckboxList items={meta.experiments} selected={filters.experiment}
            onToggle={id => onChange({ ...filters, experiment: toggleIn(filters.experiment, id) })} />
          <div className="filter-section-label mt-3">Isoforms</div>
          <label style={{display:"inline-flex",alignItems:"center",gap:".5rem",cursor:"pointer",userSelect:"none"}}>
            <input type="checkbox" checked={filters.includeIsoforms}
                   onChange={e => onChange({ ...filters, includeIsoforms: e.target.checked })}
                   style={{cursor:"pointer"}} />
            <span className="text-muted-sm">Include isoforms</span>
          </label>
        </div>
      </div>
    </div>
  );
}

function PageSizeSelect({ pageSize, onChange }) {
  return (
    <label className="text-muted-sm d-inline-flex align-items-center gap-1">
      Per page
      <select className="form-select form-select-sm" style={{width:"auto",display:"inline-block"}}
              value={pageSize} onChange={e => onChange(parseInt(e.target.value))}>
        {PAGE_SIZES.map(s => <option key={s} value={s}>{s}</option>)}
      </select>
    </label>
  );
}

function App() {
  const [mode,       setMode]       = useState("proteins");
  const [meta,       setMeta]       = useState({ tissues: [], sources: [], experiments: [] });
  const [rows,       setRows]       = useState([]);
  const [total,      setTotal]      = useState(0);
  const [loading,    setLoading]    = useState(false);
  const [loadError,  setLoadError]  = useState(null);

  const [page,       setPage]       = useState(1);
  const [pageSize,   setPageSize]   = useState(DEFAULT_PAGE_SIZE);
  const [filtersOpen, setFiltersOpen] = useState(false);

  // Proteins-mode state
  const [search,         setSearch]         = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [proteinFilters, setProteinFilters] = useState(PROTEIN_DEFAULTS);
  const [sortKey, setSortKey] = useState("symbol");
  const [sortDir, setSortDir] = useState("asc");

  // Interactions-mode state
  const [interactionFilters, setInteractionFilters] = useState(INTERACTION_DEFAULTS);
  const [intSortDir, setIntSortDir] = useState("desc");

  const abortRef = useRef(null);

  useEffect(() => {
    fetch(filterMetaUrl).then(r => r.json()).then(setMeta).catch(() => {});
  }, []);

  // Debounce the free-text search (proteins mode).
  useEffect(() => {
    const id = setTimeout(() => { setDebouncedSearch(search); setPage(1); }, 300);
    return () => clearTimeout(id);
  }, [search]);

  // Build query params for the current mode + filters. ``forList`` adds
  // pagination (offset/limit); the export variant adds ``mode`` instead so the
  // server returns every matching row. Single source of truth shared by the
  // list fetch and the export buttons.
  const buildParams = (forList) => {
    const params = new URLSearchParams();
    if (forList) {
      params.set("offset", (page - 1) * pageSize);
      params.set("limit", pageSize);
    } else {
      params.set("mode", mode);
    }
    if (debouncedSearch.trim()) params.set("q", debouncedSearch.trim());
    if (mode === "proteins") {
      params.set("sort", sortKey);
      params.set("dir", sortDir);
      proteinFilters.tissue.forEach(t => params.append("tissue", t));
      proteinFilters.source.forEach(s => params.append("source", s));
      if (proteinFilters.minDegree > 0) params.set("min_degree", proteinFilters.minDegree);
      if (proteinFilters.minScore  > 0) params.set("min_score",  proteinFilters.minScore);
      if (proteinFilters.tissue.length > 0 && proteinFilters.minRpkm > 0)
        params.set("min_rpkm", proteinFilters.minRpkm);
      if (proteinFilters.includeIsoforms) params.set("include_isoforms", "1");
    } else {
      params.set("dir", intSortDir);
      if (interactionFilters.minScore > 0) params.set("min_score", interactionFilters.minScore);
      if (interactionFilters.maxScore < 1) params.set("max_score", interactionFilters.maxScore);
      interactionFilters.source.forEach(s => params.append("source", s));
      interactionFilters.experiment.forEach(e => params.append("experiment", e));
      if (interactionFilters.includeIsoforms) params.set("include_isoforms", "1");
    }
    return params;
  };

  // Fetch the current page whenever any query input changes.
  useEffect(() => {
    if (abortRef.current) abortRef.current.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setLoading(true); setLoadError(null);

    const apiUrl = mode === "proteins" ? proteinsApiUrl : interactionsApiUrl;
    const url = `${apiUrl}?${buildParams(true)}`;

    fetch(url, { signal: ctrl.signal })
      .then(r => { if (!r.ok) throw new Error(`Server error ${r.status}`); return r.json(); })
      .then(data => {
        setTotal(data.total ?? 0);
        setRows(mode === "proteins" ? data.proteins : data.interactions);
        setLoading(false);
      })
      .catch(err => {
        if (err.name !== "AbortError") { setLoadError(err.message); setLoading(false); }
      });

    return () => ctrl.abort();
  }, [mode, page, pageSize, debouncedSearch, sortKey, sortDir, intSortDir, proteinFilters, interactionFilters]);

  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  const switchMode = (m) => {
    if (m === mode) return;
    setMode(m); setRows([]); setTotal(0); setPage(1); setFiltersOpen(false);
  };

  const handleSort = (key) => {
    if (sortKey === key) setSortDir(d => d === "asc" ? "desc" : "asc");
    else { setSortKey(key); setSortDir(key === "degree" || key === "avg_score" ? "desc" : "asc"); }
    setPage(1);
  };
  const thCls = (k) => sortKey === k ? `sorted-${sortDir}` : "";

  const updateProteinFilters = (f) => { setProteinFilters(f); setPage(1); };
  const updateInteractionFilters = (f) => { setInteractionFilters(f); setPage(1); };

  const proteinFilterCount =
    proteinFilters.tissue.length + proteinFilters.source.length +
    (proteinFilters.minDegree > 0 ? 1 : 0) + (proteinFilters.minScore > 0 ? 1 : 0) +
    (proteinFilters.includeIsoforms ? 1 : 0);
  const interactionFilterCount =
    interactionFilters.source.length + interactionFilters.experiment.length +
    (interactionFilters.minScore > 0 ? 1 : 0) + (interactionFilters.maxScore < 1 ? 1 : 0) +
    (interactionFilters.includeIsoforms ? 1 : 0);
  const activeFilterCount = mode === "proteins" ? proteinFilterCount : interactionFilterCount;

  function handleGenerateSplits() {
    const params = new URLSearchParams();
    proteinFilters.tissue.forEach(t => params.append("tissue", t));
    proteinFilters.source.forEach(s => params.append("source", s));
    if (proteinFilters.minScore > 0) params.set("min_score", proteinFilters.minScore);
    const qs = params.toString();
    window.location.href = mlSplitsUrl + (qs ? "?" + qs : "");
  }

  return (
    <div>
      <div className="hippie-hero">
        <h1>Browse<br /><em style={{color:"var(--hippie-teal)"}}>HIPPIE</em></h1>
        <p>Browse all human proteins or the full interaction table. Switch modes below;
           click any protein row to open its detail page.</p>
      </div>

      {/* Mode toggle */}
      <div className="hippie-card mb-3">
        <div className="mode-toggle">
          <button className={mode === "proteins" ? "active" : ""} onClick={() => switchMode("proteins")}>
            <i className="bi bi-diagram-3 me-1"></i>Proteins
          </button>
          <button className={mode === "interactions" ? "active" : ""} onClick={() => switchMode("interactions")}>
            <i className="bi bi-share me-1"></i>Interactions
          </button>
        </div>
      </div>

      {/* Search + filter toggle */}
      <div className="hippie-card mb-3">
        <div className="browse-search d-flex gap-2 align-items-center flex-wrap">
          <input type="text" className="form-control flex-grow-1"
            placeholder={mode === "proteins"
              ? "Search by gene symbol, UniProt ID, or Entrez ID…"
              : "Search interactions by a partner's gene symbol, UniProt ID, or Entrez ID…"}
            value={search}
            onChange={e => setSearch(e.target.value)} />
          <button className={`btn-filter-toggle${filtersOpen ? " active" : ""}`}
                  onClick={() => setFiltersOpen(o => !o)}>
            <i className={`bi bi-funnel${activeFilterCount > 0 ? "-fill" : ""}`}></i>
            Filters
            {activeFilterCount > 0 && (
              <span style={{background:"var(--hippie-teal)",color:"#fff",borderRadius:"100px",
                            fontSize:".65rem",padding:".05rem .4rem",marginLeft:".2rem"}}>
                {activeFilterCount}
              </span>
            )}
          </button>
        </div>
      </div>

      {filtersOpen && mode === "proteins" && (
        <ProteinFilterPanel filters={proteinFilters} onChange={updateProteinFilters} meta={meta} />
      )}
      {filtersOpen && mode === "interactions" && (
        <InteractionFilterPanel filters={interactionFilters} onChange={updateInteractionFilters} meta={meta} />
      )}

      {loadError && (
        <div className="hippie-card mb-3 text-center"
             style={{borderColor:"var(--hippie-accent)",color:"var(--hippie-accent)"}}>
          <i className="bi bi-exclamation-circle fs-3 d-block mb-2"></i>
          <strong>{loadError}</strong>
        </div>
      )}

      {/* Header row: count + (proteins) splits button */}
      <div className="d-flex justify-content-between align-items-baseline flex-wrap gap-2 mb-2">
        <div>
          <span style={{fontFamily:"var(--font-display)",fontSize:"1.1rem"}}>
            {mode === "proteins" ? "Proteins in HIPPIE" : "Interactions in HIPPIE"}
          </span>
          <span className="text-muted-sm ms-2">
            {total.toLocaleString()} {mode === "proteins" ? "matching" : "matching"}
            {loading && " — loading…"}
          </span>
        </div>
        <div className="d-flex align-items-center gap-3">
          <ExportBar url={`${exportApiUrl}?${buildParams(false)}`} mode={mode}
                     disabled={loading || total === 0} />
          <PageSizeSelect pageSize={pageSize} onChange={(s) => { setPageSize(s); setPage(1); }} />
          {mode === "proteins" && (
            <button onClick={handleGenerateSplits} style={{
              background:"var(--hippie-teal)",color:"#fff",border:"none",
              borderRadius:"var(--radius-md)",padding:".45rem 1.1rem",
              fontWeight:600,fontFamily:"var(--font-body)",fontSize:".88rem",cursor:"pointer",
            }}>
              <i className="bi bi-scissors me-1"></i> Generate ML Splits
            </button>
          )}
        </div>
      </div>

      {/* Results table */}
      {rows.length > 0 ? (
        <>
          <div className="hippie-card p-0 overflow-hidden mb-3">
            <div style={{overflowX:"auto"}}>
              {mode === "proteins" ? (
                <table className="hippie-table">
                  <thead>
                    <tr>
                      <th onClick={() => handleSort("uniprot_id")} className={thCls("uniprot_id")}>UniProt ID</th>
                      <th onClick={() => handleSort("entrez_id")}  className={thCls("entrez_id")}>Entrez Gene ID</th>
                      <th onClick={() => handleSort("symbol")}     className={thCls("symbol")}>Gene Symbol</th>
                      <th onClick={() => handleSort("degree")}     className={thCls("degree")}>Degree</th>
                      <th onClick={() => handleSort("avg_score")}  className={thCls("avg_score")}>Avg. Score</th>
                      <th style={{cursor:"default"}}>Interactions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map(row => (
                      <tr key={row.id}
                          onClick={() => { window.location.href = proteinDetailUrl.replace("{id}", row.id); }}
                          title={`View protein detail for ${row.symbol}`}>
                        <td>
                          {row.uniprot_id
                            ? <a href={`https://www.uniprot.org/uniprot/${row.uniprot_id}`}
                                 target="_blank" rel="noopener noreferrer"
                                 onClick={e => e.stopPropagation()}>
                                <span className="mono">{row.uniprot_id}</span></a>
                            : <span className="text-muted-sm">—</span>}
                        </td>
                        <td>
                          {row.entrez_id
                            ? <a href={`https://www.ncbi.nlm.nih.gov/gene/${row.entrez_id}`}
                                 target="_blank" rel="noopener noreferrer"
                                 onClick={e => e.stopPropagation()}>
                                <span className="mono">{row.entrez_id}</span></a>
                            : <span className="text-muted-sm">—</span>}
                        </td>
                        <td><strong>{row.symbol}</strong></td>
                        <td><span className="tag-chip">{row.degree}</span></td>
                        <td>
                          {row.avg_score != null
                            ? <span className={`score-badge ${scoreClass(row.avg_score)}`}>{row.avg_score.toFixed(4)}</span>
                            : <span className="text-muted-sm">—</span>}
                        </td>
                        <td>
                          <a href={`${proteinQueryUrl}?q=${encodeURIComponent(row.symbol)}`}
                             onClick={e => e.stopPropagation()}>show</a>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <table className="hippie-table">
                  <thead>
                    <tr>
                      <th style={{cursor:"default"}}>Protein A</th>
                      <th style={{cursor:"default"}}>Protein B</th>
                      <th onClick={() => { setIntSortDir(d => d === "asc" ? "desc" : "asc"); setPage(1); }}
                          className={`sorted-${intSortDir}`}>Score</th>
                      <th style={{cursor:"default"}}>Sources</th>
                      <th style={{cursor:"default"}}>Experiments</th>
                      <th style={{cursor:"default"}}>Evidence</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map(row => (
                      <tr key={row.id}>
                        <td>
                          {row.protein_a.uniprot_id
                            ? <a href={`https://www.uniprot.org/uniprot/${row.protein_a.uniprot_id}`}
                                 target="_blank" rel="noopener noreferrer"><strong>{row.protein_a.symbol}</strong></a>
                            : <strong>{row.protein_a.symbol}</strong>}
                        </td>
                        <td>
                          {row.protein_b.uniprot_id
                            ? <a href={`https://www.uniprot.org/uniprot/${row.protein_b.uniprot_id}`}
                                 target="_blank" rel="noopener noreferrer"><strong>{row.protein_b.symbol}</strong></a>
                            : <strong>{row.protein_b.symbol}</strong>}
                        </td>
                        <td><span className={`score-badge ${scoreClass(row.score)}`}>{row.score.toFixed(4)}</span></td>
                        <td><span className="tag-chip">{row.source_count}</span></td>
                        <td><span className="tag-chip">{row.experiment_count}</span></td>
                        <td>
                          <a href={row.detail_url}><i className="bi bi-journal-text me-1"></i>View</a>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>
          {totalPages > 1 && (
            <div className="d-flex justify-content-between align-items-center flex-wrap gap-2">
              <span className="text-muted-sm">
                Page {page} of {totalPages} — {(page-1)*pageSize+1}–{Math.min(page*pageSize, total)} of {total.toLocaleString()}
              </span>
              <Pagination page={page} totalPages={totalPages}
                onChange={p => { setPage(p); window.scrollTo({top:0,behavior:"smooth"}); }} />
            </div>
          )}
        </>
      ) : (
        !loading && !loadError && (
          <div className="state-box">
            <i className="bi bi-inbox state-icon"></i>
            <p className="mb-0">No {mode === "proteins" ? "proteins" : "interactions"} match the current filters.</p>
          </div>
        )
      )}
    </div>
  );
}

createRoot(document.getElementById("hippie-browse-app")).render(<App />);
