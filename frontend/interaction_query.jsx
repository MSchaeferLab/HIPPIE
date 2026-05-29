import React, { useState, useRef, useCallback, useEffect } from "react";
import { createRoot } from "react-dom/client";
import { ScoreBadge, Pagination } from "./shared.jsx";

const { apiUrl, maxPairs, batchSize } = window.HIPPIE_IQ_CONFIG;
const PAGE_SIZE = 25;

const EXAMPLES = [
  { label: "Example 1 — tabs", text: "HTT\tTP53\nBRCA1\tTP53\nEGFR\tERBB2\nHTT\tHSP90AA1" },
  { label: "Example 2 — CSV",  text: "P42858,P04637\nP38398,P04637\nO15350,P04637" },
  { label: "Example 3 — edge cases", text: "HTT;EGFR\nBRCA1;TP53\nFAKEPROT999;TP53\nHTT;BRCA1" },
];

function detectSep(line) {
  if (line.includes("\t")) return /\t/;
  if (line.includes(","))  return /,/;
  if (line.includes(";"))  return /;/;
  return /\s+/;
}

function parseText(text) {
  const lines = text.split(/\r?\n/).map(l => l.trim()).filter(Boolean);
  if (lines.length === 0) return { pairs: [], error: null };
  const sep = detectSep(lines[0]);
  const pairs = [], bad = [];
  for (let i = 0; i < lines.length; i++) {
    const tokens = lines[i].split(sep).map(t => t.trim()).filter(Boolean);
    if (tokens.length < 2) { bad.push(i + 1); continue; }
    pairs.push([tokens[0], tokens[1]]);
  }
  if (bad.length > 0 && pairs.length === 0)
    return { pairs: [], error: "Could not parse any pairs. Check your formatting." };
  return { pairs, error: null };
}

function parseFile(file) {
  return new Promise(resolve => {
    const reader = new FileReader();
    reader.onload  = e => resolve(parseText(e.target.result));
    reader.onerror = () => resolve({ pairs: [], error: "Could not read file." });
    reader.readAsText(file);
  });
}

function getCookie(name) {
  const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
  return m ? decodeURIComponent(m[1]) : "";
}

function ExportBar({ rows }) {
  const tsv = () => {
    const h = ["Protein A","Protein B","Score","Sources","Experiments"].join("\t");
    const data = rows.map(r => [
      r.symbol_a, r.symbol_b, r.score,
      r.score < 0 ? "" : r.source_count,
      r.score < 0 ? "" : r.experiment_count,
    ].join("\t"));
    return [h, ...data].join("\n");
  };
  const copy     = () => navigator.clipboard.writeText(tsv());
  const download = () => {
    const a = Object.assign(document.createElement("a"), {
      href: URL.createObjectURL(new Blob([tsv()], {type:"text/tab-separated-values"})),
      download: "hippie_interactions.tsv",
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

function ResultsTable({ rows, streaming, progress }) {
  const [sortKey, setSortKey] = useState("input_order");
  const [sortDir, setSortDir] = useState("asc");
  const [page,    setPage]    = useState(1);

  const handleSort = (key) => {
    if (sortKey === key) setSortDir(d => d === "asc" ? "desc" : "asc");
    else { setSortKey(key); setSortDir(key === "score" ? "desc" : "asc"); }
    setPage(1);
  };
  const thCls = (k) => sortKey === k ? `sorted-${sortDir}` : "";

  const sorted = [...rows].sort((a, b) => {
    const vals = {
      input_order: [a.input_order, b.input_order],
      symbol_a:    [a.symbol_a,    b.symbol_a],
      symbol_b:    [a.symbol_b,    b.symbol_b],
      score:       [a.score,       b.score],
      sources:     [a.source_count, b.source_count],
      experiments: [a.experiment_count, b.experiment_count],
    };
    const [va, vb] = vals[sortKey] ?? [0, 0];
    if (typeof va === "string") return sortDir === "asc" ? va.localeCompare(vb) : vb.localeCompare(va);
    return sortDir === "asc" ? va - vb : vb - va;
  });

  const totalPages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
  const pageRows   = sorted.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
  const notFound   = rows.filter(r => r.score < 0).length;

  return (
    <div>
      <div className="d-flex justify-content-between align-items-start flex-wrap gap-3 mb-3">
        <div>
          <h2 className="results-title">Interaction results</h2>
          <div className="d-flex gap-3 mt-1 flex-wrap">
            <span className="text-muted-sm">{rows.length.toLocaleString()} pair{rows.length !== 1 ? "s" : ""}</span>
            {notFound > 0 && (
              <span style={{fontSize:".8rem",color:"var(--hippie-accent)"}}>
                <i className="bi bi-exclamation-circle me-1"></i>{notFound} not found
              </span>
            )}
            {streaming && (
              <span className="text-muted-sm">
                <span className="spinner me-1"></span>Loading… {Math.round(progress * 100)}%
              </span>
            )}
          </div>
        </div>
        {!streaming && <ExportBar rows={sorted} />}
      </div>

      {streaming && (
        <div className="batch-progress mb-3">
          <div className="batch-progress-fill" style={{width:`${Math.round(progress*100)}%`}} />
        </div>
      )}

      <div className="hippie-card p-0 overflow-hidden">
        <div style={{overflowX:"auto"}}>
          <table className="hippie-table">
            <thead>
              <tr>
                <th onClick={() => handleSort("symbol_a")}    className={thCls("symbol_a")}>Protein A</th>
                <th onClick={() => handleSort("symbol_b")}    className={thCls("symbol_b")}>Protein B</th>
                <th onClick={() => handleSort("score")}       className={thCls("score")}>Score</th>
                <th onClick={() => handleSort("sources")}     className={thCls("sources")}>Sources</th>
                <th onClick={() => handleSort("experiments")} className={thCls("experiments")}>Experiments</th>
                <th style={{cursor:"default"}}>Evidence</th>
              </tr>
            </thead>
            <tbody>
              {pageRows.map((row, i) => (
                <tr key={i} className={row.score < 0 ? "row-not-found" : ""}>
                  <td>
                    {row.uniprot_a
                      ? <a href={`https://www.uniprot.org/uniprot/${row.uniprot_a}`} target="_blank" rel="noopener noreferrer">
                          <strong>{row.symbol_a}</strong></a>
                      : <strong>{row.symbol_a}</strong>}
                    {row.isoform_uniprot_a && <span className="mono text-muted-sm ms-1">({row.isoform_uniprot_a})</span>}
                    {!row.isoform_uniprot_a && row.input_a !== row.symbol_a && <span className="text-muted-sm ms-1">({row.input_a})</span>}
                  </td>
                  <td>
                    {row.uniprot_b
                      ? <a href={`https://www.uniprot.org/uniprot/${row.uniprot_b}`} target="_blank" rel="noopener noreferrer">
                          <strong>{row.symbol_b}</strong></a>
                      : <strong>{row.symbol_b}</strong>}
                    {row.isoform_uniprot_b && <span className="mono text-muted-sm ms-1">({row.isoform_uniprot_b})</span>}
                    {!row.isoform_uniprot_b && row.input_b !== row.symbol_b && <span className="text-muted-sm ms-1">({row.input_b})</span>}
                  </td>
                  <td><ScoreBadge score={row.score} /></td>
                  <td>{row.score >= 0 && row.source_count != null ? <span className="tag-chip">{row.source_count}</span> : <span className="text-muted-sm">—</span>}</td>
                  <td>{row.score >= 0 && row.experiment_count != null ? <span className="tag-chip">{row.experiment_count}</span> : <span className="text-muted-sm">—</span>}</td>
                  <td>
                    {row.score >= 0 && row.interaction_id
                      ? <a href={row.detail_url}><i className="bi bi-journal-text me-1"></i>View</a>
                      : <span className="text-muted-sm">—</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {totalPages > 1 && (
        <div className="mt-3 d-flex justify-content-between align-items-center flex-wrap gap-2">
          <span className="text-muted-sm">
            Page {page} of {totalPages} — {(page-1)*PAGE_SIZE+1}–{Math.min(page*PAGE_SIZE, sorted.length)} of {sorted.length}
          </span>
          <Pagination page={page} totalPages={totalPages}
            onChange={p => { setPage(p); window.scrollTo({top:0,behavior:"smooth"}); }} />
        </div>
      )}
    </div>
  );
}

function InputPanel({ onSubmit, disabled, includeIsoforms, onIsoformChange, show, onShowChange }) {
  const [mode,       setMode]       = useState("text");
  const [text,       setText]       = useState("");
  const [file,       setFile]       = useState(null);
  const [dragOver,   setDragOver]   = useState(false);
  const [inputError, setInputError] = useState(null);
  const fileRef = useRef(null);

  const switchMode = (m) => { setMode(m); setInputError(null); if (m === "text") setFile(null); else setText(""); };

  const handleDrop = (e) => { e.preventDefault(); setDragOver(false); const f = e.dataTransfer.files[0]; if (f) setFile(f); };

  const handleSubmit = async () => {
    setInputError(null);
    if (mode === "text") {
      const { pairs, error } = parseText(text);
      if (error) return setInputError(error);
      if (pairs.length === 0) return setInputError("Please enter at least one protein pair.");
      if (pairs.length > maxPairs) return setInputError(`Too many pairs. Maximum is ${maxPairs.toLocaleString()}.`);
      onSubmit(pairs);
    } else {
      if (!file) return setInputError("Please select or drop a file.");
      const { pairs, error } = await parseFile(file);
      if (error) return setInputError(error);
      if (pairs.length === 0) return setInputError("No valid pairs found in file.");
      if (pairs.length > maxPairs) return setInputError(`Too many pairs. Maximum is ${maxPairs.toLocaleString()}.`);
      onSubmit(pairs);
    }
  };

  const canSubmit = !disabled && (mode === "text" ? text.trim().length > 0 : file !== null);

  const onExampleClick = (exText) => {
    setMode("text"); setFile(null); setText(exText); setInputError(null);
    const { pairs, error } = parseText(exText);
    if (error) { setInputError(error); return; }
    if (pairs.length > 0) onSubmit(pairs);
  };

  return (
    <div className="hippie-card mb-4">
      <div className="d-flex align-items-center justify-content-between flex-wrap gap-2 mb-3">
        <span className="text-muted-sm">Input method</span>
        <div className="mode-toggle">
          <button className={mode === "text" ? "active" : ""} onClick={() => switchMode("text")}>
            <i className="bi bi-input-cursor-text me-1"></i>Text
          </button>
          <button className={mode === "file" ? "active" : ""} onClick={() => switchMode("file")}>
            <i className="bi bi-file-earmark-arrow-up me-1"></i>File upload
          </button>
        </div>
      </div>

      {mode === "text" && (
        <>
          <textarea className="pair-textarea mb-2"
            placeholder={"One pair per line — separator auto-detected:\n\nHTT\tBRCA1\nTP53, MDM2\nEGFR ERBB2"}
            value={text} onChange={e => setText(e.target.value)} />
          <p className="text-muted-sm mb-2">Accepts gene symbols, UniProt IDs, UniProt accessions, or Entrez IDs.</p>
          <div className="d-flex align-items-center gap-2 flex-wrap mb-3">
            <span className="text-muted-sm" style={{fontFamily:"var(--font-mono)"}}>Try:</span>
            {EXAMPLES.map((ex, i) => (
              <span key={i} className="tag-chip example-chip"
                    onClick={() => !disabled && onExampleClick(ex.text)}>{ex.label}</span>
            ))}
          </div>
        </>
      )}

      {mode === "file" && (
        <>
          <div className={`file-dropzone mb-2${dragOver ? " drag-over" : ""}${file ? " has-file" : ""}`}
               onClick={() => fileRef.current.click()}
               onDragOver={e => { e.preventDefault(); setDragOver(true); }}
               onDragLeave={() => setDragOver(false)}
               onDrop={handleDrop}>
            <i className={file ? "bi bi-file-earmark-check" : "bi bi-file-earmark-arrow-up"}></i>
            {file
              ? <><strong style={{fontSize:".875rem"}}>{file.name}</strong>
                  <span className="text-muted-sm">{(file.size/1024).toFixed(1)} KB — click to change</span></>
              : <><span style={{fontSize:".875rem",color:"var(--hippie-ink-muted)"}}>Drop a file here or click to browse</span>
                  <span className="text-muted-sm">TSV, CSV, TXT — no header, max {maxPairs.toLocaleString()} pairs</span></>}
          </div>
          <input ref={fileRef} type="file" accept=".tsv,.csv,.txt,.tab" style={{display:"none"}}
                 onChange={e => { if (e.target.files[0]) setFile(e.target.files[0]); }} />
          <p className="text-muted-sm mb-3">Any delimiter (tab, comma, semicolon, space) is auto-detected. No header row.</p>
        </>
      )}

      {inputError && (
        <div className="d-flex align-items-start gap-2 mb-3 p-2 rounded"
             style={{background:"var(--hippie-accent-soft)",color:"var(--hippie-accent)",fontSize:".85rem"}}>
          <i className="bi bi-exclamation-circle mt-1" style={{flexShrink:0}}></i>
          <span>{inputError}</span>
        </div>
      )}

      <div className="d-flex align-items-center gap-2 flex-wrap mb-3">
        <span className="text-muted-sm">Show</span>
        <div className="mode-toggle">
          <button className={show === "interactions" ? "active" : ""}
                  onClick={() => onShowChange("interactions")}>Interactions</button>
          <button className={show === "noninteractions" ? "active" : ""}
                  onClick={() => onShowChange("noninteractions")}>Non-interactions</button>
          <button className={show === "both" ? "active" : ""}
                  onClick={() => onShowChange("both")}>Both</button>
        </div>
      </div>

      <div className="d-flex justify-content-between align-items-center flex-wrap gap-2">
        <label style={{display:"inline-flex",alignItems:"center",gap:".4rem",cursor:"pointer",userSelect:"none"}}>
          <input type="checkbox" checked={includeIsoforms}
                 onChange={e => onIsoformChange(e.target.checked)} style={{cursor:"pointer"}} />
          <span className="text-muted-sm">Include isoforms</span>
        </label>
        <button className="btn-submit" onClick={handleSubmit} disabled={!canSubmit}>
          {disabled
            ? <><span className="spinner me-2"></span>Querying…</>
            : <><i className="bi bi-arrow-right-circle me-2"></i>Query interactions</>}
        </button>
      </div>
    </div>
  );
}

function App() {
  const [rows,            setRows]           = useState([]);
  const [streaming,       setStreaming]       = useState(false);
  const [progress,        setProgress]       = useState(0);
  const [globalErr,       setGlobalErr]      = useState(null);
  const [includeIsoforms, setIncludeIsoforms]= useState(false);
  const [show,            setShow]           = useState("interactions");
  const abortRef = useRef(null);
  const lastPairsRef = useRef(null);

  const runQuery = useCallback(async (pairs) => {
    lastPairsRef.current = pairs;
    if (abortRef.current) abortRef.current.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setRows([]); setGlobalErr(null); setStreaming(true); setProgress(0);

    const totalBatches = Math.ceil(pairs.length / batchSize);
    const allRows = [];

    try {
      for (let b = 0; b < totalBatches; b++) {
        if (controller.signal.aborted) break;
        const slice = pairs.slice(b * batchSize, (b + 1) * batchSize)
          .map((p, i) => ({ a: p[0], b: p[1], input_order: b * batchSize + i }));
        const res = await fetch(apiUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRFToken": getCookie("csrftoken") },
          body: JSON.stringify({ pairs: slice, include_isoforms: includeIsoforms, show }),
          signal: controller.signal,
        });
        if (!res.ok) throw new Error(`Server error ${res.status}: ${await res.text()}`);
        const data = await res.json();
        allRows.push(...data.results);
        setRows([...allRows]);
        setProgress((b + 1) / totalBatches);
      }
    } catch (err) {
      if (err.name !== "AbortError") setGlobalErr(err.message);
    } finally {
      setStreaming(false);
    }
  }, [includeIsoforms, show]);

  // Toggling "include isoforms" or the show-mode re-runs the last query
  // automatically, so the user need not resubmit the pairs.
  const togglesInited = useRef(false);
  useEffect(() => {
    if (!togglesInited.current) { togglesInited.current = true; return; }
    if (lastPairsRef.current && lastPairsRef.current.length > 0) {
      runQuery(lastPairsRef.current);
    }
  }, [includeIsoforms, show]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div>
      <div className="hippie-hero">
        <h1>Interaction<br /><em style={{color:"var(--hippie-teal)"}}>Query</em></h1>
        <p>Look up specific protein pairs by any combination of gene symbol, UniProt ID,
           UniProt accession, or Entrez ID. Pairs not found in HIPPIE are reported with a score of −1.</p>
      </div>

      <InputPanel onSubmit={runQuery} disabled={streaming}
                  includeIsoforms={includeIsoforms} onIsoformChange={setIncludeIsoforms}
                  show={show} onShowChange={setShow} />

      {globalErr && (
        <div className="hippie-card mb-4 text-center"
             style={{borderColor:"var(--hippie-accent)",color:"var(--hippie-accent)"}}>
          <i className="bi bi-exclamation-circle fs-3 d-block mb-2"></i>
          <strong>{globalErr}</strong>
        </div>
      )}

      {rows.length > 0 && <ResultsTable rows={rows} streaming={streaming} progress={progress} />}

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
