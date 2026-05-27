import React, { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { createRoot } from "react-dom/client";
import { Pagination } from "./shared.jsx";

const {
  proteinsApiUrl,
  filterMetaUrl,
  proteinDetailUrl,
  proteinQueryUrl,
  mlSplitsUrl,
} = window.HIPPIE_BROWSE_CONFIG;

const PAGE_SIZE  = 50;
const CHUNK_SIZE = 500;

function FilterPanel({ filters, onChange, meta }) {
  const { tissues, sources } = meta;
  return (
    <div className="filter-panel mb-3">
      <div className="row g-3">
        <div className="col-md-4">
          <div className="filter-section-label">Tissue Expression</div>
          <label className="form-label">Expressed in tissue</label>
          <select className="form-select" value={filters.tissue}
                  onChange={e => onChange({
                    ...filters,
                    tissue: e.target.value,
                    minRpkm: e.target.value ? filters.minRpkm : 0,
                  })}>
            <option value="">Any tissue</option>
            {tissues.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select>
          {filters.tissue && (
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
          <label className="form-label">Has interaction in source</label>
          <select className="form-select" value={filters.source}
                  onChange={e => onChange({ ...filters, source: e.target.value })}>
            <option value="">Any source</option>
            {sources.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
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
          <input type="range" className="form-range" min="0" max="1" step="0.01"
                 value={filters.minScore || 0}
                 onChange={e => onChange({ ...filters, minScore: parseFloat(e.target.value) })} />
        </div>
      </div>
    </div>
  );
}

function ActiveFilterBadges({ filters, meta, onRemove }) {
  const badges = [];
  if (filters.tissue) {
    const t = meta.tissues.find(x => String(x.id) === String(filters.tissue));
    if (t) badges.push({ key: "tissue", label: `Tissue: ${t.name}` });
  }
  if (filters.source) {
    const s = meta.sources.find(x => String(x.id) === String(filters.source));
    if (s) badges.push({ key: "source", label: `Source: ${s.name}` });
  }
  if (filters.minDegree > 0) badges.push({ key: "minDegree", label: `Degree ≥ ${filters.minDegree}` });
  if (filters.minScore  > 0) badges.push({ key: "minScore",  label: `Avg score ≥ ${filters.minScore.toFixed(2)}` });
  if (filters.tissue && filters.minRpkm > 0) badges.push({ key: "minRpkm", label: `Min RPKM ≥ ${filters.minRpkm}` });
  if (badges.length === 0) return null;
  return (
    <div className="d-flex gap-2 flex-wrap align-items-center mb-2">
      <span className="text-muted-sm">Active filters:</span>
      {badges.map(b => (
        <span key={b.key} className="active-filter-badge">
          {b.label}
          <button onClick={() => onRemove(b.key)} title="Remove filter">×</button>
        </span>
      ))}
    </div>
  );
}

function App() {
  const [allRows,    setAllRows]    = useState([]);
  const [streaming,  setStreaming]  = useState(false);
  const [progress,   setProgress]  = useState(0);
  const [totalCount, setTotalCount] = useState(null);
  const [loadError,  setLoadError]  = useState(null);
  const [meta,       setMeta]       = useState({ tissues: [], sources: [] });
  const [search,       setSearch]       = useState("");
  const [filtersOpen,  setFiltersOpen]  = useState(false);
  const [filters,      setFilters]      = useState({ tissue: "", source: "", minDegree: 0, minScore: 0, minRpkm: 0 });
  const [sortKey,      setSortKey]      = useState("symbol");
  const [sortDir,      setSortDir]      = useState("asc");
  const [page,         setPage]         = useState(1);
  const abortRef = useRef(null);

  useEffect(() => {
    fetch(filterMetaUrl).then(r => r.json()).then(setMeta).catch(() => {});
  }, []);

  useEffect(() => {
    if (abortRef.current) abortRef.current.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setAllRows([]); setTotalCount(null); setLoadError(null);
    setStreaming(true); setProgress(0); setPage(1);

    const params = new URLSearchParams();
    if (filters.tissue)        params.set("tissue",     filters.tissue);
    if (filters.source)        params.set("source",     filters.source);
    if (filters.minDegree > 0) params.set("min_degree", filters.minDegree);
    if (filters.minScore  > 0) params.set("min_score",  filters.minScore);
    if (filters.tissue && filters.minRpkm > 0) params.set("min_rpkm", filters.minRpkm);

    let offset = 0, total = null;
    const accumulated = [];

    async function fetchNextChunk() {
      if (ctrl.signal.aborted) return;
      const url = `${proteinsApiUrl}?${params}&offset=${offset}&limit=${CHUNK_SIZE}`;
      try {
        const res = await fetch(url, { signal: ctrl.signal });
        if (!res.ok) throw new Error(`Server error ${res.status}`);
        const data = await res.json();
        if (total === null) { total = data.total; setTotalCount(total); }
        accumulated.push(...data.proteins);
        setAllRows([...accumulated]);
        setProgress(accumulated.length / total);
        if (accumulated.length < total && data.proteins.length > 0) {
          offset += CHUNK_SIZE; fetchNextChunk();
        } else { setStreaming(false); setProgress(1); }
      } catch (err) {
        if (err.name !== "AbortError") { setLoadError(err.message); setStreaming(false); }
      }
    }
    fetchNextChunk();
    return () => ctrl.abort();
  }, [filters.tissue, filters.source, filters.minDegree, filters.minScore, filters.minRpkm]);

  const filtered = useMemo(() => {
    let rows = allRows;
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      rows = rows.filter(r =>
        r.symbol.toLowerCase().includes(q) ||
        r.uniprot_id.toLowerCase().includes(q) ||
        String(r.entrez_id).includes(q)
      );
    }
    return [...rows].sort((a, b) => {
      const vals = {
        symbol:    [a.symbol,    b.symbol],
        uniprot_id:[a.uniprot_id,b.uniprot_id],
        entrez_id: [a.entrez_id ?? 0, b.entrez_id ?? 0],
        degree:    [a.degree,    b.degree],
        avg_score: [a.avg_score ?? 0, b.avg_score ?? 0],
      };
      const [va, vb] = vals[sortKey] ?? [0, 0];
      if (typeof va === "string") return sortDir === "asc" ? va.localeCompare(vb) : vb.localeCompare(va);
      return sortDir === "asc" ? va - vb : vb - va;
    });
  }, [allRows, search, sortKey, sortDir]);

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const pageRows   = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  const handleSort = (key) => {
    if (sortKey === key) setSortDir(d => d === "asc" ? "desc" : "asc");
    else { setSortKey(key); setSortDir(key === "degree" || key === "avg_score" ? "desc" : "asc"); }
    setPage(1);
  };
  const thCls = (k) => sortKey === k ? `sorted-${sortDir}` : "";

  const removeFilter = (key) => {
    const defaults = { tissue: "", source: "", minDegree: 0, minScore: 0, minRpkm: 0 };
    setFilters(f => ({
      ...f,
      [key]: defaults[key],
      ...(key === "tissue" ? { minRpkm: 0 } : {}),
    }));
  };
  const hasActiveFilters = filters.tissue || filters.source || filters.minDegree > 0 || filters.minScore > 0 || (filters.tissue && filters.minRpkm > 0);

  function handleGenerateSplits() {
    const params = new URLSearchParams();
    if (filters.tissue)       params.set("tissue",    filters.tissue);
    if (filters.source)       params.set("source",    filters.source);
    if (filters.minScore > 0) params.set("min_score", filters.minScore);
    if (filters.tissue && filters.minRpkm > 0) params.set("min_rpkm", filters.minRpkm);
    const qs = params.toString();
    window.location.href = mlSplitsUrl + (qs ? "?" + qs : "");
  }

  return (
    <div>
      <div className="hippie-hero">
        <h1>Browse<br /><em style={{color:"var(--hippie-teal)"}}>Proteins</em></h1>
        <p>All human proteins in HIPPIE with their interaction degree, confidence scores,
           and supporting databases. Click any row to view the protein detail page.</p>
      </div>

      <div className="hippie-card mb-3">
        <div className="browse-search d-flex gap-2 align-items-center flex-wrap">
          <input type="text" className="form-control flex-grow-1"
            placeholder="Search by gene symbol, UniProt ID, or Entrez ID…"
            value={search}
            onChange={e => { setSearch(e.target.value); setPage(1); }} />
          <button className={`btn-filter-toggle${filtersOpen ? " active" : ""}`}
                  onClick={() => setFiltersOpen(o => !o)}>
            <i className={`bi bi-funnel${hasActiveFilters ? "-fill" : ""}`}></i>
            Filters
            {hasActiveFilters && (
              <span style={{background:"var(--hippie-teal)",color:"#fff",borderRadius:"100px",
                            fontSize:".65rem",padding:".05rem .4rem",marginLeft:".2rem"}}>
                {[filters.tissue, filters.source, filters.minDegree > 0, filters.minScore > 0, filters.tissue && filters.minRpkm > 0].filter(Boolean).length}
              </span>
            )}
          </button>
        </div>
        {hasActiveFilters && !filtersOpen && (
          <div className="mt-2">
            <ActiveFilterBadges filters={filters} meta={meta} onRemove={removeFilter} />
          </div>
        )}
      </div>

      {filtersOpen && <FilterPanel filters={filters} onChange={setFilters} meta={meta} />}

      {streaming && (
        <div className="mb-2">
          <div className="d-flex justify-content-between align-items-center mb-1">
            <span className="text-muted-sm">
              Loading proteins… {allRows.length.toLocaleString()}
              {totalCount !== null ? ` / ${totalCount.toLocaleString()}` : ""}
            </span>
            <span className="text-muted-sm">{Math.round(progress * 100)}%</span>
          </div>
          <div className="batch-progress">
            <div className="batch-progress-fill" style={{width:`${Math.round(progress*100)}%`}} />
          </div>
        </div>
      )}

      {loadError && (
        <div className="hippie-card mb-3 text-center"
             style={{borderColor:"var(--hippie-accent)",color:"var(--hippie-accent)"}}>
          <i className="bi bi-exclamation-circle fs-3 d-block mb-2"></i>
          <strong>{loadError}</strong>
        </div>
      )}

      {!loadError && (allRows.length > 0 || !streaming) && (
        <div className="d-flex justify-content-between align-items-baseline flex-wrap gap-2 mb-2">
          <div>
            <span style={{fontFamily:"var(--font-display)",fontSize:"1.1rem"}}>Proteins in HIPPIE</span>
            <span className="text-muted-sm ms-2">
              {filtered.length.toLocaleString()} shown
              {totalCount !== null && filtered.length < totalCount
                ? ` (of ${totalCount.toLocaleString()} loaded)` : ""}
              {streaming && " — still loading…"}
            </span>
          </div>
          <button
            onClick={handleGenerateSplits}
            style={{
              background:"var(--hippie-teal)",color:"#fff",border:"none",
              borderRadius:"var(--radius-md)",padding:".45rem 1.1rem",
              fontWeight:600,fontFamily:"var(--font-body)",fontSize:".88rem",cursor:"pointer",
            }}
          >
            <i className="bi bi-scissors me-1"></i> Generate ML Splits
          </button>
        </div>
      )}

      {allRows.length > 0 && (
        <>
          <div className="hippie-card p-0 overflow-hidden mb-3">
            <div style={{overflowX:"auto"}}>
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
                  {pageRows.map(row => (
                    <tr key={row.id}
                        onClick={() => { window.location.href = proteinDetailUrl.replace("{id}", row.id); }}
                        title={`View protein detail for ${row.symbol}`}>
                      <td>
                        {row.uniprot_id
                          ? <a href={`https://www.uniprot.org/uniprot/${row.uniprot_id}`}
                               target="_blank" rel="noopener noreferrer"
                               onClick={e => e.stopPropagation()}>
                              <span className="mono">{row.uniprot_id}</span>
                            </a>
                          : <span className="text-muted-sm">—</span>}
                      </td>
                      <td>
                        {row.entrez_id
                          ? <a href={`https://www.ncbi.nlm.nih.gov/gene/${row.entrez_id}`}
                               target="_blank" rel="noopener noreferrer"
                               onClick={e => e.stopPropagation()}>
                              <span className="mono">{row.entrez_id}</span>
                            </a>
                          : <span className="text-muted-sm">—</span>}
                      </td>
                      <td><strong>{row.symbol}</strong></td>
                      <td><span className="tag-chip">{row.degree}</span></td>
                      <td>
                        {row.avg_score != null
                          ? <span className={`score-badge ${row.avg_score >= 0.72 ? "score-high" : row.avg_score >= 0.63 ? "score-med" : "score-low"}`}>
                              {row.avg_score.toFixed(4)}
                            </span>
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
            </div>
          </div>
          {totalPages > 1 && (
            <div className="d-flex justify-content-between align-items-center flex-wrap gap-2">
              <span className="text-muted-sm">
                Page {page} of {totalPages} — {(page-1)*PAGE_SIZE+1}–{Math.min(page*PAGE_SIZE, filtered.length)} of {filtered.length}
              </span>
              <Pagination page={page} totalPages={totalPages}
                onChange={p => { setPage(p); window.scrollTo({top:0,behavior:"smooth"}); }} />
            </div>
          )}
        </>
      )}

      {!streaming && !loadError && allRows.length === 0 && (
        <div className="state-box">
          <i className="bi bi-inbox state-icon"></i>
          <p className="mb-0">No proteins match the current filters.</p>
        </div>
      )}
    </div>
  );
}

createRoot(document.getElementById("hippie-browse-app")).render(<App />);
