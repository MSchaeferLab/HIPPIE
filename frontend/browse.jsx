import React, { useState, useEffect, useRef } from "react";
import { createRoot } from "react-dom/client";
import { InteractionTable, ProteinTable } from "./tables.jsx";
import {
  FilterBox,
  FILTER_DEFAULTS,
  filtersToQuery,
  countActiveFilters,
  filtersEqual,
} from "./filters.jsx";

const {
  proteinsApiUrl,
  interactionsApiUrl,
  exportApiUrl,
  filterMetaUrl,
  proteinQueryUrl,
  mlSplitsUrl,
} = window.HIPPIE_BROWSE_CONFIG;

// Filter controls shown per tab. Proteins are protein-level (no result-type,
// score-range, experiment or interaction-type controls); the Interactions tab
// gets the full interaction-oriented set incl. the interactions/non-interactions
// /both toggle.
const PROTEIN_CONTROLS = ["source", "tissue", "protein", "reviewed", "isoforms"];
const INTERACTION_CONTROLS = ["showMode", "score", "source", "experiment", "interactionType", "isoforms"];

const EXAMPLES = ["BRCA1", "TP53", "EGFR"];
const DEFAULT_PAGE_SIZE = 25;

// Default sort per tab (proteins: gene symbol asc; interactions: score desc).
const DEFAULT_SORT = {
  proteins: { key: "symbol", dir: "asc" },
  interactions: { key: "score", dir: "desc" },
};

function App() {
  const [mode, setMode] = useState("proteins");
  const [meta, setMeta] = useState({ tissues: [], sources: [], experiments: [], interaction_types: [] });

  // ── Draft vs applied ────────────────────────────────────────────────────
  // The search box + FilterBox edit draft state; nothing is fetched until the
  // draft is committed via Search (or Enter / an example). The data fetch below
  // depends only on the applied copies, so toggling filters issues zero
  // requests until one explicit submit.
  const [search, setSearch] = useState("");
  const [filters, setFilters] = useState(FILTER_DEFAULTS);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [appliedSearch, setAppliedSearch] = useState("");
  const [appliedFilters, setAppliedFilters] = useState(FILTER_DEFAULTS);

  // ── Server pagination + sort (owned here, driven into the shared tables) ──
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [sort, setSort] = useState(DEFAULT_SORT.proteins);

  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState(null);
  const abortRef = useRef(null);

  useEffect(() => {
    fetch(filterMetaUrl).then((r) => r.json()).then(setMeta).catch(() => {});
  }, []);

  const controls = mode === "proteins" ? PROTEIN_CONTROLS : INTERACTION_CONTROLS;
  const activeCount = countActiveFilters(filters, controls);
  const dirty = search !== appliedSearch || !filtersEqual(filters, appliedFilters);

  // Commit the draft search + filters → applied, back to page 1.
  const applySearch = () => {
    setAppliedSearch(search);
    setAppliedFilters(filters);
    setPage(1);
    setFiltersOpen(false);
  };

  const runExample = (ex) => {
    setSearch(ex);
    setAppliedSearch(ex);
    setAppliedFilters(filters);
    setPage(1);
  };

  const switchMode = (m) => {
    if (m === mode) return;
    setMode(m);
    setRows([]);
    setTotal(0);
    setPage(1);
    setFiltersOpen(false);
    setSearch("");
    setAppliedSearch("");
    setFilters(FILTER_DEFAULTS);
    setAppliedFilters(FILTER_DEFAULTS);
    setSort(DEFAULT_SORT[m]);
  };

  // Server-side sort toggle handed to the shared tables.
  const onSort = (key) => {
    setSort((s) =>
      s.key === key
        ? { key, dir: s.dir === "asc" ? "desc" : "asc" }
        : { key, dir: key === "score" || key === "degree" || key === "avg_score" ? "desc" : "asc" },
    );
    setPage(1);
  };

  // Single source of truth for query params (list + export share the filters).
  const paramsFor = (list) => {
    const p = filtersToQuery(appliedFilters);
    if (appliedSearch.trim()) p.set("q", appliedSearch.trim());
    p.set("sort", sort.key);
    p.set("dir", sort.dir);
    if (list) {
      p.set("offset", (page - 1) * pageSize);
      p.set("limit", pageSize);
    } else {
      p.set("mode", mode);
    }
    return p;
  };

  // Fetch the current page whenever any applied query input changes.
  useEffect(() => {
    if (abortRef.current) abortRef.current.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setLoading(true);
    setLoadError(null);

    const apiUrl = mode === "proteins" ? proteinsApiUrl : interactionsApiUrl;
    fetch(`${apiUrl}?${paramsFor(true)}`, { signal: ctrl.signal })
      .then((r) => {
        if (!r.ok) throw new Error(`Server error ${r.status}`);
        return r.json();
      })
      .then((data) => {
        setTotal(data.total ?? 0);
        setRows(mode === "proteins" ? data.proteins : data.interactions);
        setLoading(false);
      })
      .catch((err) => {
        if (err.name !== "AbortError") {
          setLoadError(err.message);
          setLoading(false);
        }
      });

    return () => ctrl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, page, pageSize, sort, appliedSearch, appliedFilters]);

  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  const server = {
    sort,
    onSort,
    page,
    pageSize,
    total,
    totalPages,
    onPageChange: (p) => setPage(p),
    onPageSizeChange: (s) => {
      setPageSize(s);
      setPage(1);
    },
  };

  // Hand the current mode's applied filters to the ML Splits page via query
  // params. Protein-browse "min avg score" maps to the split min_avg_score;
  // interaction-browse min/max score map to the interaction-level split filters.
  const handleGenerateSplits = () => {
    const p = new URLSearchParams();
    const f = appliedFilters;
    if (mode === "proteins") {
      f.tissue.forEach((t) => p.append("tissue", t));
      f.source.forEach((s) => p.append("source", s));
      if (f.minDegree > 0) p.set("min_degree", f.minDegree);
      if (f.minAvgScore > 0) p.set("min_avg_score", f.minAvgScore);
      if (f.tissue.length > 0 && f.minRpkm > 0) p.set("min_rpkm", f.minRpkm);
      if (f.includeIsoforms) p.set("include_isoforms", "1");
    } else {
      if (f.minScore > 0) p.set("min_score", f.minScore);
      if (f.maxScore < 1) p.set("max_score", f.maxScore);
      f.source.forEach((s) => p.append("source", s));
      f.experiment.forEach((e) => p.append("experiment", e));
      if (f.includeIsoforms) p.set("include_isoforms", "1");
    }
    const qs = p.toString();
    window.location.href = mlSplitsUrl + (qs ? "?" + qs : "");
  };

  const splitsButton = (
    <button
      onClick={handleGenerateSplits}
      style={{
        background: "var(--hippie-teal)",
        color: "#fff",
        border: "none",
        borderRadius: "var(--radius-md)",
        padding: ".45rem 1.1rem",
        fontWeight: 600,
        fontFamily: "var(--font-body)",
        fontSize: ".88rem",
        cursor: "pointer",
        whiteSpace: "nowrap",
      }}
    >
      <i className="bi bi-scissors me-1"></i> Generate ML Splits
    </button>
  );

  const countLabel = `${total.toLocaleString()} matching${loading ? " — loading…" : ""}`;
  const serverExport = { url: `${exportApiUrl}?${paramsFor(false)}`, disabled: loading || total === 0 };

  // Build only the active mode's row set — `rows` holds proteins XOR
  // interactions, so mapping the wrong shape would throw (e.g. reading
  // `r.protein_a` on a protein row).
  const proteinRows =
    mode === "proteins"
      ? rows.map((r) => ({
          key: r.id,
          id: r.id,
          symbol: r.symbol,
          uniprot: r.uniprot_id,
          entrez: r.entrez_id,
          degree: r.degree,
          avgScore: r.avg_score,
        }))
      : [];

  const interactionRows =
    mode === "interactions"
      ? rows.map((r, i) => ({
          key: `${r.is_noninteraction ? "ni" : "i"}-${r.id}-${i}`,
          a: { symbol: r.protein_a.symbol, uniprot: r.protein_a.uniprot_id, entrez: r.protein_a.entrez_id, isoform: null, is_reviewed: r.protein_a.is_reviewed },
          b: { symbol: r.protein_b.symbol, uniprot: r.protein_b.uniprot_id, entrez: r.protein_b.entrez_id, isoform: null, is_reviewed: r.protein_b.is_reviewed },
          score: r.score,
          // Non-interactions carry no evidence — show a dash rather than a 0 count.
          sourceCount: r.is_noninteraction ? null : r.source_count,
          experimentCount: r.is_noninteraction ? null : r.experiment_count,
          isNoninteraction: r.is_noninteraction,
          detailUrl: r.detail_url,
        }))
      : [];

  return (
    <div>
      <div className="hippie-hero">
        <h1>
          Browse
          <br />
          <em style={{ color: "var(--hippie-teal)" }}>HIPPIE</em>
        </h1>
        <p>
          Browse all human proteins or the full interaction table. Switch modes below; click any row to open it
          in the query pages.
        </p>
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

      {/* Unified search card: input, then examples-left / Filter + Search-right */}
      <div className="hippie-card mb-3">
        <input
          type="text"
          className="form-control mb-3"
          placeholder={
            mode === "proteins"
              ? "Search by gene symbol, UniProt ID, or Entrez ID…"
              : "Search interactions by a partner's gene symbol, UniProt ID, or Entrez ID…"
          }
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && applySearch()}
        />
        <div className="d-flex align-items-center justify-content-between flex-wrap gap-2">
          <div className="d-flex align-items-center gap-2 flex-wrap">
            <span className="text-muted-sm" style={{ fontFamily: "var(--font-mono)" }}>
              Try:
            </span>
            {EXAMPLES.map((ex) => (
              <span key={ex} className="tag-chip example-chip" onClick={() => runExample(ex)}>
                {ex}
              </span>
            ))}
          </div>
          <div className="d-flex align-items-center gap-2">
            <button
              className={`btn-filter-toggle${filtersOpen ? " active" : ""}`}
              onClick={() => setFiltersOpen((o) => !o)}
            >
              <i className={`bi bi-funnel${activeCount > 0 ? "-fill" : ""}`}></i>
              Filters
              {activeCount > 0 && (
                <span
                  style={{
                    background: "var(--hippie-teal)",
                    color: "#fff",
                    borderRadius: "100px",
                    fontSize: ".65rem",
                    padding: ".05rem .4rem",
                    marginLeft: ".1rem",
                  }}
                >
                  {activeCount}
                </span>
              )}
            </button>
            <button
              className="btn-hippie"
              onClick={applySearch}
              style={dirty ? { background: "var(--hippie-accent)", borderColor: "var(--hippie-accent)" } : undefined}
            >
              <i className="bi bi-search me-1"></i>Search
              {dirty && <span className="search-dirty-dot" title="Unapplied filter changes — click Search"></span>}
            </button>
          </div>
        </div>
      </div>

      {filtersOpen && (
        <FilterBox value={filters} onChange={setFilters} meta={meta} controls={controls} />
      )}

      {loadError && (
        <div
          className="hippie-card mb-3 text-center"
          style={{ borderColor: "var(--hippie-accent)", color: "var(--hippie-accent)" }}
        >
          <i className="bi bi-exclamation-circle fs-3 d-block mb-2"></i>
          <strong>{loadError}</strong>
        </div>
      )}

      {rows.length > 0 ? (
        mode === "proteins" ? (
          <ProteinTable
            rows={proteinRows}
            title="Proteins in HIPPIE"
            countLabel={countLabel}
            proteinQueryUrl={proteinQueryUrl}
            server={server}
            serverExport={serverExport}
            exportFilename="hippie_browse_proteins.tsv"
            headerExtra={splitsButton}
          />
        ) : (
          <InteractionTable
            rows={interactionRows}
            title="Interactions in HIPPIE"
            countLabel={countLabel}
            server={server}
            serverExport={serverExport}
            exportFilename="hippie_browse_interactions.tsv"
            headerExtra={splitsButton}
          />
        )
      ) : (
        !loading &&
        !loadError && (
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
