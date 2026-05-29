import React, { useState, useCallback, useEffect, useRef } from "react";
import { createRoot } from "react-dom/client";
import { scoreClass, uniprotUrl, entrezUrl, ExtLink, PaginationRow } from "./shared.jsx";

const PAGE_SIZE = 10;
const EXAMPLES  = ["HTT", "P42858", "3064", "BRCA1_HUMAN"];
const INITIAL_Q = new URLSearchParams(window.location.search).get("q") || "";
const DEFAULT_FILTERS = { includeIsoforms: false, showMode: "interactions" };

function ExportBar({ data, symbol }) {
  const tsv = () => {
    const h = "Gene\tUniProt\tEntrez\tScore\tSources\tExperiments";
    const rows = data.map(r =>
      [r.partner.symbol, r.partner.uniprot_id, r.partner.gene_id ?? "",
       r.score, r.source_count ?? "—", r.experiment_count ?? "—"].join("\t"));
    return [h, ...rows].join("\n");
  };
  const copy     = () => navigator.clipboard.writeText(tsv());
  const download = () => {
    const a = Object.assign(document.createElement("a"), {
      href: URL.createObjectURL(new Blob([tsv()], {type:"text/tab-separated-values"})),
      download: `hippie_${symbol}.tsv`,
    });
    a.click(); URL.revokeObjectURL(a.href);
  };
  return (
    <div className="export-bar">
      <button onClick={copy}><i className="bi bi-clipboard me-1"></i>Copy</button>
      <button onClick={download}><i className="bi bi-download me-1"></i>TSV</button>
    </div>
  );
}

function ResultsTable({ interactions, queryProtein, isoformsIncluded }) {
  const [sortKey, setSortKey] = useState("score");
  const [sortDir, setSortDir] = useState("desc");
  const [page,    setPage]    = useState(1);

  const handleSort = (key) => {
    if (sortKey === key) setSortDir(d => d === "asc" ? "desc" : "asc");
    else { setSortKey(key); setSortDir(key === "score" ? "desc" : "asc"); }
    setPage(1);
  };

  const sorted = [...interactions].sort((a, b) => {
    const vals = {
      score:       [a.score,              b.score],
      symbol:      [a.partner.symbol,     b.partner.symbol],
      uniprot_id:  [a.partner.uniprot_id, b.partner.uniprot_id],
      gene_id:     [a.partner.gene_id??0, b.partner.gene_id??0],
      sources:     [a.source_count??-1,   b.source_count??-1],
      experiments: [a.experiment_count??-1, b.experiment_count??-1],
    };
    const [va, vb] = vals[sortKey] ?? [0, 0];
    if (typeof va === "string") return sortDir === "asc" ? va.localeCompare(vb) : vb.localeCompare(va);
    return sortDir === "asc" ? va - vb : vb - va;
  });

  const totalPages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
  const rows       = sorted.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
  const thCls      = (k) => sortKey === k ? `sorted-${sortDir}` : "";

  return (
    <div>
      <div className="d-flex justify-content-between align-items-baseline flex-wrap gap-2 mb-3">
        <div>
          <h2 className="results-title">Results for <em>{queryProtein.symbol}</em></h2>
          <span className="text-muted-sm">{interactions.length.toLocaleString()} result{interactions.length !== 1 ? "s" : ""}</span>
        </div>
        <ExportBar data={sorted} symbol={queryProtein.symbol} />
      </div>

      <div className="hippie-card p-0 overflow-hidden">
        <div style={{overflowX:"auto"}}>
          <table className="hippie-table">
            <thead>
              <tr>
                {isoformsIncluded && <th style={{cursor:"default"}}>Via</th>}
                <th onClick={() => handleSort("symbol")}      className={thCls("symbol")}>Gene Symbol</th>
                <th onClick={() => handleSort("uniprot_id")}  className={thCls("uniprot_id")}>UniProt ID</th>
                <th onClick={() => handleSort("gene_id")}     className={thCls("gene_id")}>Entrez ID</th>
                <th onClick={() => handleSort("score")}       className={thCls("score")}>Score</th>
                <th onClick={() => handleSort("sources")}     className={thCls("sources")}>Sources</th>
                <th onClick={() => handleSort("experiments")} className={thCls("experiments")}>Experiments</th>
                <th style={{cursor:"default"}}>Evidence</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(row => (
                <tr key={`${row.is_noninteraction ? "ni" : "i"}-${row.id}`}
                    className={row.is_noninteraction ? "row-noninteraction" : ""}>
                  {isoformsIncluded && (
                    <td>
                      <span className="mono" style={{fontSize:".8rem",color:"var(--hippie-ink-muted)"}}>
                        {row.query_side?.isoform_uniprot_id || row.query_side?.uniprot_id || "—"}
                      </span>
                    </td>
                  )}
                  <td><ExtLink href={entrezUrl(row.partner.gene_id)}><strong>{row.partner.symbol}</strong></ExtLink></td>
                  <td><ExtLink href={uniprotUrl(row.partner.uniprot_id)}><span className="mono">{row.partner.uniprot_id || "—"}</span></ExtLink></td>
                  <td><ExtLink href={entrezUrl(row.partner.gene_id)}><span className="mono">{row.partner.gene_id ?? "—"}</span></ExtLink></td>
                  <td><span className={scoreClass(row.score)}>{row.score.toFixed(4)}</span></td>
                  <td>
                    {row.is_noninteraction
                      ? <span className="text-muted-sm">—</span>
                      : <span className="tag-chip">{row.source_count}</span>}
                  </td>
                  <td>
                    {row.is_noninteraction
                      ? <span className="text-muted-sm">—</span>
                      : <span className="tag-chip">{row.experiment_count}</span>}
                  </td>
                  <td>
                    {row.detail_url
                      ? <a href={row.detail_url}><i className="bi bi-journal-text me-1"></i>View</a>
                      : <span className="text-muted-sm">—</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="mt-3">
        <PaginationRow page={page} totalPages={totalPages} totalItems={interactions.length}
          pageSize={PAGE_SIZE} onChange={p => { setPage(p); window.scrollTo({top:0,behavior:"smooth"}); }} />
      </div>
    </div>
  );
}

function FilterPanel({ filters, onChange }) {
  return (
    <div className="filter-panel mb-3">
      <div className="row g-4">
        <div className="col-md-7">
          <div className="filter-section-label">Show Results</div>
          <div className="mode-toggle">
            <button className={filters.showMode === "interactions" ? "active" : ""}
                    onClick={() => onChange({ ...filters, showMode: "interactions" })}>Interactions</button>
            <button className={filters.showMode === "noninteractions" ? "active" : ""}
                    onClick={() => onChange({ ...filters, showMode: "noninteractions" })}>Non-interactions</button>
            <button className={filters.showMode === "both" ? "active" : ""}
                    onClick={() => onChange({ ...filters, showMode: "both" })}>Both</button>
          </div>
        </div>
        <div className="col-md-5">
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

function ActiveFilterBadges({ filters, onChange }) {
  const badges = [];
  if (filters.showMode !== "interactions") {
    badges.push({
      key: "showMode",
      label: filters.showMode === "noninteractions" ? "Non-interactions only" : "Interactions + Non-interactions",
    });
  }
  if (filters.includeIsoforms) badges.push({ key: "includeIsoforms", label: "Isoforms included" });
  if (badges.length === 0) return null;
  return (
    <div className="d-flex gap-2 flex-wrap align-items-center mt-2">
      <span className="text-muted-sm">Active filters:</span>
      {badges.map(b => (
        <span key={b.key} className="active-filter-badge">
          {b.label}
          <button onClick={() => {
            const defaults = { showMode: "interactions", includeIsoforms: false };
            onChange({ ...filters, [b.key]: defaults[b.key] });
          }}>×</button>
        </span>
      ))}
    </div>
  );
}

function App() {
  const [query,       setQuery]       = useState(INITIAL_Q);
  const [filters,     setFilters]     = useState(DEFAULT_FILTERS);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [loading,     setLoading]     = useState(false);
  const [error,       setError]       = useState(null);
  const [result,      setResult]      = useState(null);

  const activeFilterCount = (
    (filters.showMode !== "interactions" ? 1 : 0) +
    (filters.includeIsoforms ? 1 : 0)
  );

  useEffect(() => { if (INITIAL_Q) handleSearch(INITIAL_Q); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const handleSearch = useCallback(async (overrideQ) => {
    const q = (overrideQ !== undefined ? overrideQ : query).trim();
    if (!q) return;
    setLoading(true); setError(null); setResult(null);
    try {
      const isoParam  = filters.includeIsoforms ? "&include_isoforms=1" : "";
      const showParam = filters.showMode !== "interactions" ? `&show=${filters.showMode}` : "";
      const data = await fetch(
        `${window.HIPPIE_CONFIG.apiUrl}?q=${encodeURIComponent(q)}${isoParam}${showParam}`
      ).then(r => r.json());
      data.error ? setError(data.error) : setResult(data);
    } catch { setError("Network error — could not reach the server."); }
    finally   { setLoading(false); }
  }, [query, filters]);

  // Toggling a filter (isoforms / show-mode) re-runs the search automatically,
  // as long as there is a query to run — no need to press Search again.
  const filtersInited = useRef(false);
  useEffect(() => {
    if (!filtersInited.current) { filtersInited.current = true; return; }
    if (query.trim()) handleSearch();
  }, [filters.includeIsoforms, filters.showMode]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div>
      <div className="hippie-hero">
        <h1>Query Protein<br /><em style={{color:"var(--hippie-teal)"}}>Interactions</em></h1>
        <p>Enter a UniProt ID, UniProt accession, Entrez gene ID, or gene symbol to retrieve
           all known human protein–protein interactions and their confidence scores.</p>
      </div>

      <div className="hippie-card mb-3">
        <div className="hippie-search-form d-flex mb-3">
          <input type="text" className="form-control"
            placeholder="e.g. HTT, P42858, 3064, BRCA1_HUMAN …"
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={e => e.key === "Enter" && handleSearch()}
            autoFocus />
          <button className="btn-hippie btn-hippie--attached" onClick={() => handleSearch()} disabled={loading || !query.trim()}>
            {loading
              ? <span className="spinner" style={{width:16,height:16,borderWidth:2,verticalAlign:"middle"}}></span>
              : <><i className="bi bi-search me-1"></i>Search</>}
          </button>
        </div>
        <div className="d-flex align-items-center justify-content-between flex-wrap gap-2">
          <div className="d-flex align-items-center gap-2 flex-wrap">
            <span className="text-muted-sm" style={{fontFamily:"var(--font-mono)"}}>Try:</span>
            {EXAMPLES.map(ex => (
              <span key={ex} className="tag-chip example-chip"
                    onClick={() => { setQuery(ex); handleSearch(ex); }}>{ex}</span>
            ))}
          </div>
          <button className={`btn-filter-toggle${filtersOpen ? " active" : ""}`}
                  onClick={() => setFiltersOpen(o => !o)}>
            <i className={`bi bi-funnel${activeFilterCount > 0 ? "-fill" : ""}`}></i>
            Filters
            {activeFilterCount > 0 && (
              <span style={{background:"var(--hippie-teal)",color:"#fff",borderRadius:"100px",
                            fontSize:".65rem",padding:".05rem .4rem",marginLeft:".1rem"}}>
                {activeFilterCount}
              </span>
            )}
          </button>
        </div>
        {activeFilterCount > 0 && !filtersOpen && (
          <ActiveFilterBadges filters={filters} onChange={setFilters} />
        )}
      </div>

      {filtersOpen && <FilterPanel filters={filters} onChange={setFilters} />}

      {loading && (
        <div className="state-box">
          <span className="spinner d-block mx-auto mb-3"></span>
          Resolving identifier and loading interactions…
        </div>
      )}
      {!loading && error && (
        <div className="hippie-card text-center" style={{borderColor:"var(--hippie-accent)",color:"var(--hippie-accent)"}}>
          <i className="bi bi-exclamation-circle fs-3 d-block mb-2"></i>
          <strong>{error}</strong>
          <p className="text-muted-sm mt-2 mb-0">
            Supported formats: gene symbol, UniProt ID, UniProt accession, Entrez gene ID.
          </p>
        </div>
      )}
      {!loading && result && result.interactions.length === 0 && (
        <div className="state-box">
          <i className="bi bi-inbox state-icon"></i>
          No results found for <strong>{result.query_protein?.symbol}</strong>.
        </div>
      )}
      {!loading && result && result.interactions.length > 0 && (
        <ResultsTable interactions={result.interactions} queryProtein={result.query_protein}
                      isoformsIncluded={result.isoforms_included || false} />
      )}

      {!result && !loading && (
        <div id="about" className="mt-5">
          <div className="hippie-card">
            <h3 className="mb-3">About HIPPIE</h3>
            <p className="mb-2">
              HIPPIE (<strong>H</strong>uman <strong>I</strong>ntegrated <strong>P</strong>rotein–<strong>P</strong>rotein
              <strong> I</strong>nteraction r<strong>E</strong>ference) is a resource providing confidence-scored,
              functionally annotated human protein–protein interactions aggregated from multiple experimental databases.
            </p>
            <p className="mb-0 text-muted-sm">
              Confidence scores range from 0 to 1 and are computed as a weighted sum of experimental technique quality,
              number of supporting studies, and cross-species conservation.
              Scores ≥ 0.72 are considered <em>high confidence</em>; ≥ 0.63 <em>medium confidence</em>.
            </p>
          </div>
        </div>
      )}
    </div>
  );
}

createRoot(document.getElementById("hippie-app")).render(<App />);
