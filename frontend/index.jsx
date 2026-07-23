import React, { useState, useCallback, useEffect } from "react";
import { createRoot } from "react-dom/client";
import { InteractionTable } from "./tables.jsx";
import { parseIdentifiers, MAX_QUERY_PROTEINS } from "./shared.jsx";
import {
  FilterBox,
  FilterToggleButton,
  FILTER_DEFAULTS,
  filtersToQuery,
  countActiveFilters,
  filtersEqual,
  useFilterMeta,
} from "./filters.jsx";

const { apiUrl, filterMetaUrl } = window.HIPPIE_CONFIG;
const EXAMPLES = ["HTT", "P42858", "3064", "HD_HUMAN", "ENSG00000197386"];
const INITIAL_Q = new URLSearchParams(window.location.search).get("q") || "";

// Short label for the resolved query proteins: list up to three symbols, then
// "+N" for the remainder (used in the results title and empty state).
function queryLabel(proteins) {
  const syms = (proteins || []).map((p) => p?.symbol).filter(Boolean);
  if (syms.length === 0) return "";
  if (syms.length <= 3) return syms.join(", ");
  return `${syms.slice(0, 3).join(", ")} +${syms.length - 3}`;
}

// Map the protein-query API payload into the shared InteractionTable row shape.
// Each row's query_side is the queried protein for that edge (side A); the
// partner is side B. qs is a mild fallback (first resolved protein).
function mapRows(data) {
  const qs = (data.query_proteins && data.query_proteins[0]) || {};
  return data.interactions.map((row, i) => ({
    key: `${row.is_noninteraction ? "ni" : "i"}-${row.id}-${i}`,
    a: {
      symbol: row.query_side?.symbol ?? qs.symbol ?? "",
      uniprot: row.query_side?.uniprot_id ?? qs.uniprot_id ?? null,
      entrez: row.query_side?.gene_id ?? qs.gene_id ?? null,
      isoform: row.query_side?.isoform_uniprot_id ?? null,
      is_reviewed: row.query_side?.is_reviewed ?? qs.is_reviewed,
    },
    b: {
      symbol: row.partner?.symbol ?? "",
      uniprot: row.partner?.uniprot_id ?? null,
      entrez: row.partner?.gene_id ?? null,
      isoform: row.partner?.isoform_uniprot_id ?? null,
      is_reviewed: row.partner?.is_reviewed,
    },
    score: row.score,
    sourceCount: row.source_count,
    experimentCount: row.experiment_count,
    isNoninteraction: !!row.is_noninteraction,
    detailUrl: row.detail_url || "",
  }));
}

function App() {
  const [query, setQuery] = useState(INITIAL_Q);
  const [appliedQuery, setAppliedQuery] = useState("");
  const [filters, setFilters] = useState(FILTER_DEFAULTS);
  const [appliedFilters, setAppliedFilters] = useState(FILTER_DEFAULTS);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const meta = useFilterMeta(filterMetaUrl);

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);

  // Nothing searches until the user commits: Search button, Enter, or an example.
  const runSearch = useCallback(async (q, f) => {
    const qq = (q ?? "").trim();
    if (!qq) return;
    const ids = parseIdentifiers(qq);
    if (ids.length > MAX_QUERY_PROTEINS) {
      setResult(null);
      setError(
        `Too many proteins: ${ids.length} (max ${MAX_QUERY_PROTEINS} per query). Please remove some identifiers.`,
      );
      return;
    }
    setLoading(true);
    setError(null);
    setResult(null);
    setAppliedQuery(qq);
    setAppliedFilters(f);
    try {
      const params = filtersToQuery(f);
      params.set("q", qq);
      const data = await fetch(`${apiUrl}?${params.toString()}`).then((r) => r.json());
      if (data.error) setError(data.error);
      else setResult(data);
    } catch {
      setError("Network error — could not reach the server.");
    } finally {
      setLoading(false);
    }
  }, []);

  // Deep-link ?q= autoruns once on mount with default filters.
  useEffect(() => {
    if (INITIAL_Q) runSearch(INITIAL_Q, FILTER_DEFAULTS);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const submit = () => {
    setFiltersOpen(false);
    runSearch(query, filters);
  };
  const runExample = (ex) => {
    setQuery(ex);
    runSearch(ex, filters);
  };

  const activeCount = countActiveFilters(filters);
  const dirty = !filtersEqual(filters, appliedFilters);
  const rows = result ? mapRows(result) : [];

  return (
    <div>
      <div className="hippie-hero">
        <h1>
          Protein
          <br />
          <em style={{ color: "var(--hippie-teal)" }}>Query</em>
        </h1>
        <p>
          Enter one or more proteins — UniProt ID or accession, Entrez gene ID, gene symbol, or Ensembl ID,
          separated by comma, space or tab (up to {MAX_QUERY_PROTEINS}) — to retrieve all known human
          protein–protein interactions (or non-interactions) and their HIPPIE confidence scores.
        </p>
      </div>

      <div className="hippie-card mb-3">
        <input
          type="text"
          className="form-control mb-3"
          placeholder="e.g. HTT, P42858, 3064, HD_HUMAN, ENSG00000197386 — up to 50, comma/space/tab separated"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit()}
          autoFocus
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
            <FilterToggleButton
              activeCount={activeCount}
              filtersOpen={filtersOpen}
              onClick={() => setFiltersOpen((o) => !o)}
            />
            <button
              className="btn-hippie"
              onClick={submit}
              disabled={loading || !query.trim()}
              style={dirty ? { background: "var(--hippie-accent)", borderColor: "var(--hippie-accent)" } : undefined}
            >
              {loading ? (
                <span className="spinner" style={{ width: 16, height: 16, borderWidth: 2, verticalAlign: "middle" }}></span>
              ) : (
                <>
                  <i className="bi bi-search me-1"></i>Search
                </>
              )}
              {dirty && !loading && <span className="search-dirty-dot" title="Unapplied filter changes — click Search"></span>}
            </button>
          </div>
        </div>
      </div>

      {filtersOpen && <FilterBox value={filters} onChange={setFilters} meta={meta} />}

      {loading && (
        <div className="state-box">
          <span className="spinner d-block mx-auto mb-3"></span>
          Resolving identifiers and loading interactions…
        </div>
      )}
      {!loading && error && (
        <div className="hippie-card text-center" style={{ borderColor: "var(--hippie-accent)", color: "var(--hippie-accent)" }}>
          <i className="bi bi-exclamation-circle fs-3 d-block mb-2"></i>
          <strong>{error}</strong>
          <p className="text-muted-sm mt-2 mb-0">
            Supported formats: gene symbol, UniProt ID, UniProt accession, Entrez gene ID.
          </p>
        </div>
      )}
      {!loading && result && result.unresolved?.length > 0 && (
        <div className="hippie-card mb-3" style={{ borderColor: "var(--hippie-accent)" }}>
          <i className="bi bi-exclamation-triangle me-2" style={{ color: "var(--hippie-accent)" }}></i>
          Not found: <strong>{result.unresolved.join(", ")}</strong>
        </div>
      )}
      {!loading && result && rows.length === 0 && (
        <div className="state-box">
          <i className="bi bi-inbox state-icon"></i>
          No results found for <strong>{queryLabel(result.query_proteins)}</strong>.
        </div>
      )}
      {!loading && result && rows.length > 0 && (
        <InteractionTable
          rows={rows}
          title={
            <>
              Results for <em>{queryLabel(result.query_proteins)}</em>
            </>
          }
          exportFilename={`hippie_${result.query_proteins?.length === 1 ? result.query_proteins[0].symbol : "query"}.tsv`}
        />
      )}

      {!result && !loading && (
        <div id="about" className="mt-5">
          <div className="hippie-card">
            <h3 className="mb-3">About HIPPIE</h3>
            <p className="mb-2">
              HIPPIE (<strong>H</strong>uman <strong>I</strong>ntegrated <strong>P</strong>rotein–<strong>P</strong>rotein
              <strong> I</strong>nteraction r<strong>E</strong>ference) is a resource of confidence-scored, functionally
              annotated human protein–protein interactions aggregated from multiple experimental databases. You can query
              single proteins, construct interaction networks, or browse the entire HIPPIE proteome.
            </p>
            <p className="mb-0">
              Every interaction carries a confidence score from 0 to 1, computed as a weighted sum of experimental
              technique quality, the number of supporting studies, and cross-species conservation. An interaction
              mentioned at least once in the literature receives a score of ≥ 0.49, so 0.49 is the minimum interaction
              score in the database. Scores ≥ {(window.HIPPIE_RELEASE?.intMedian ?? 0.00).toFixed(2)} are considered{" "}
              <em>medium confidence</em>; ≥ {(window.HIPPIE_RELEASE?.intQ3 ?? 0.00).toFixed(2)} <em>high confidence</em>.
            </p>
          </div>
        </div>
      )}
    </div>
  );
}

createRoot(document.getElementById("hippie-app")).render(<App />);
