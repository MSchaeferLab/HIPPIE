import React, { useState, useRef, useCallback, useEffect } from "react";
import { createRoot } from "react-dom/client";
import { getCookie } from "./shared.jsx";
import { InteractionTable } from "./tables.jsx";
import {
  FilterBox,
  FILTER_DEFAULTS,
  filtersToBody,
  countActiveFilters,
  filtersEqual,
} from "./filters.jsx";

const { apiUrl, filterMetaUrl, maxPairs, batchSize } = window.HIPPIE_IQ_CONFIG;

const EXAMPLES = [
  { label: "Example 1 — tabs", text: "HTT\tTP53\nBRCA1\tTP53\nEGFR\tERBB2\nHTT\tHSP90AA1" },
  { label: "Example 2 — CSV", text: "P42858,P04637\nP38398,P04637\nO15350,P04637" },
  { label: "Example 3 — edge cases", text: "HTT;EGFR\nBRCA1;TP53\nFAKEPROT999;TP53\nHTT;BRCA1" },
];

function detectSep(line) {
  if (line.includes("\t")) return /\t/;
  if (line.includes(",")) return /,/;
  if (line.includes(";")) return /;/;
  return /\s+/;
}

function parseText(text) {
  const lines = text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  if (lines.length === 0) return { pairs: [], error: null };
  const sep = detectSep(lines[0]);
  const pairs = [];
  const bad = [];
  for (let i = 0; i < lines.length; i++) {
    const tokens = lines[i].split(sep).map((t) => t.trim()).filter(Boolean);
    if (tokens.length < 2) {
      bad.push(i + 1);
      continue;
    }
    pairs.push([tokens[0], tokens[1]]);
  }
  if (bad.length > 0 && pairs.length === 0)
    return { pairs: [], error: "Could not parse any pairs. Check your formatting." };
  return { pairs, error: null };
}

function parseFile(file) {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = (e) => resolve(parseText(e.target.result));
    reader.onerror = () => resolve({ pairs: [], error: "Could not read file." });
    reader.readAsText(file);
  });
}

// Map one interaction-query API row into the shared InteractionTable shape.
function mapRow(row, i) {
  return {
    key: `${row.input_order}-${i}`,
    a: {
      symbol: row.symbol_a,
      uniprot: row.uniprot_a || null,
      entrez: row.entrez_a ?? null,
      isoform: row.isoform_uniprot_a || null,
      is_reviewed: row.is_reviewed_a,
    },
    b: {
      symbol: row.symbol_b,
      uniprot: row.uniprot_b || null,
      entrez: row.entrez_b ?? null,
      isoform: row.isoform_uniprot_b || null,
      is_reviewed: row.is_reviewed_b,
    },
    score: row.score,
    sourceCount: row.source_count,
    experimentCount: row.experiment_count,
    isNoninteraction: !!row.is_noninteraction,
    detailUrl: row.detail_url || "",
    input_order: row.input_order,
  };
}

function App() {
  // Input (draft) ------------------------------------------------------------
  const [mode, setMode] = useState("text"); // text | file
  const [text, setText] = useState("");
  const [file, setFile] = useState(null);
  const [dragOver, setDragOver] = useState(false);
  const [inputError, setInputError] = useState(null);
  const fileRef = useRef(null);

  // Filters (draft vs applied) ----------------------------------------------
  const [filters, setFilters] = useState(FILTER_DEFAULTS);
  const [appliedFilters, setAppliedFilters] = useState(FILTER_DEFAULTS);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [meta, setMeta] = useState({ tissues: [], sources: [], experiments: [], interaction_types: [] });

  // Results ------------------------------------------------------------------
  const [rows, setRows] = useState([]);
  const [streaming, setStreaming] = useState(false);
  const [progress, setProgress] = useState(0);
  const [globalErr, setGlobalErr] = useState(null);
  const abortRef = useRef(null);

  useEffect(() => {
    if (filterMetaUrl) fetch(filterMetaUrl).then((r) => r.json()).then(setMeta).catch(() => {});
  }, []);

  const runQuery = useCallback(async (pairs, f) => {
    if (abortRef.current) abortRef.current.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setRows([]);
    setGlobalErr(null);
    setStreaming(true);
    setProgress(0);
    setAppliedFilters(f);

    const totalBatches = Math.ceil(pairs.length / batchSize);
    const allRows = [];
    const bodyFilters = filtersToBody(f);

    try {
      for (let b = 0; b < totalBatches; b++) {
        if (controller.signal.aborted) break;
        const slice = pairs
          .slice(b * batchSize, (b + 1) * batchSize)
          .map((p, i) => ({ a: p[0], b: p[1], input_order: b * batchSize + i }));
        const res = await fetch(apiUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRFToken": getCookie("csrftoken") },
          body: JSON.stringify({ pairs: slice, ...bodyFilters }),
          signal: controller.signal,
        });
        if (!res.ok) throw new Error(`Server error ${res.status}: ${await res.text()}`);
        const data = await res.json();
        allRows.push(...data.results);
        setRows(allRows.map(mapRow));
        setProgress((b + 1) / totalBatches);
      }
    } catch (err) {
      if (err.name !== "AbortError") setGlobalErr(err.message);
    } finally {
      setStreaming(false);
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

  // Search: parse the current input, then run with the current (draft) filters.
  const submit = async () => {
    setInputError(null);
    let pairs;
    let error;
    if (mode === "text") {
      ({ pairs, error } = parseText(text));
    } else {
      if (!file) return setInputError("Please select or drop a file.");
      ({ pairs, error } = await parseFile(file));
    }
    if (error) return setInputError(error);
    if (!pairs || pairs.length === 0) return setInputError("Please enter at least one protein pair.");
    if (pairs.length > maxPairs)
      return setInputError(`Too many pairs. Maximum is ${maxPairs.toLocaleString()}.`);
    setFiltersOpen(false);
    runQuery(pairs, filters);
  };

  const onExampleClick = (exText) => {
    setMode("text");
    setFile(null);
    setText(exText);
    setInputError(null);
    const { pairs, error } = parseText(exText);
    if (error) return setInputError(error);
    if (pairs.length > 0) runQuery(pairs, filters);
  };

  const canSubmit = !streaming && (mode === "text" ? text.trim().length > 0 : file !== null);
  const activeCount = countActiveFilters(filters);
  const dirty = !filtersEqual(filters, appliedFilters);

  return (
    <div>
      <div className="hippie-hero">
        <h1>
          Interaction
          <br />
          <em style={{ color: "var(--hippie-teal)" }}>Query</em>
        </h1>
        <p>
          Look up specific protein pairs by any combination of gene symbol, UniProt ID, UniProt accession, or Entrez ID.
          Pairs not found in HIPPIE are reported with a score of −1.
        </p>
      </div>

      <div className="hippie-card mb-4">
        <div className="mode-toggle mb-3">
          <button className={mode === "text" ? "active" : ""} onClick={() => switchMode("text")}>
            <i className="bi bi-input-cursor-text me-1"></i>Text
          </button>
          <button className={mode === "file" ? "active" : ""} onClick={() => switchMode("file")}>
            <i className="bi bi-file-earmark-arrow-up me-1"></i>File upload
          </button>
        </div>

        {mode === "text" && (
          <textarea
            className="pair-textarea mb-2"
            placeholder={"One pair per line — separator auto-detected:\n\nHTT\tBRCA1\nTP53, MDM2\nEGFR ERBB2"}
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
                  <span className="text-muted-sm">TSV, CSV, TXT — no header, max {maxPairs.toLocaleString()} pairs</span>
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
            className="d-flex align-items-start gap-2 mb-3 p-2 rounded"
            style={{ background: "var(--hippie-accent-soft)", color: "var(--hippie-accent)", fontSize: ".85rem" }}
          >
            <i className="bi bi-exclamation-circle mt-1" style={{ flexShrink: 0 }}></i>
            <span>{inputError}</span>
          </div>
        )}

        <div className="d-flex align-items-center justify-content-between flex-wrap gap-2 mt-2">
          <div className="d-flex align-items-center gap-2 flex-wrap">
            <span className="text-muted-sm" style={{ fontFamily: "var(--font-mono)" }}>
              Try:
            </span>
            {EXAMPLES.map((ex, i) => (
              <span key={i} className="tag-chip example-chip" onClick={() => !streaming && onExampleClick(ex.text)}>
                {ex.label}
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
              onClick={submit}
              disabled={!canSubmit}
              style={dirty ? { background: "var(--hippie-accent)", borderColor: "var(--hippie-accent)" } : undefined}
            >
              {streaming ? (
                <>
                  <span className="spinner me-2"></span>Querying…
                </>
              ) : (
                <>
                  <i className="bi bi-search me-1"></i>Search
                </>
              )}
              {dirty && !streaming && <span className="search-dirty-dot" title="Unapplied filter changes — click Search"></span>}
            </button>
          </div>
        </div>
      </div>

      {filtersOpen && <FilterBox value={filters} onChange={setFilters} meta={meta} />}

      {globalErr && (
        <div className="hippie-card mb-4 text-center" style={{ borderColor: "var(--hippie-accent)", color: "var(--hippie-accent)" }}>
          <i className="bi bi-exclamation-circle fs-3 d-block mb-2"></i>
          <strong>{globalErr}</strong>
        </div>
      )}

      {rows.length > 0 && (
        <InteractionTable
          rows={rows}
          title="Interaction results"
          countLabel={`${rows.length.toLocaleString()} pair${rows.length !== 1 ? "s" : ""}`}
          exportFilename="hippie_interactions.tsv"
          defaultSortKey="input_order"
          defaultSortDir="asc"
          streaming={streaming}
          progress={progress}
        />
      )}

      {!streaming && rows.length === 0 && !globalErr && (
        <div className="state-box">
          <i className="bi bi-intersect state-icon"></i>
          <p className="mb-0">Enter protein pairs above to query their interactions.</p>
        </div>
      )}
    </div>
  );
}

createRoot(document.getElementById("hippie-iq-app")).render(<App />);
