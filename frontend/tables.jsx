// Shared result tables (Batch 3+): one InteractionTable used by Protein Query,
// Interaction Query, Browse Interactions and (Batch 5) Network Query, and one
// ProteinTable used by Browse Proteins.
//
// Both tables run in two modes:
//   • client mode  — parent passes the FULL result set as `rows`; the table
//     sorts, paginates and exports it in-browser (Protein/Interaction Query).
//   • server mode  — parent passes a `server` object; `rows` is the current
//     page, and sort/pagination are delegated back via callbacks (Browse).
//
// Rows are a normalised shape so every page maps its own API payload once:
//   Interaction: { key, a:{symbol,uniprot,entrez,isoform}, b:{...},
//                  score, sourceCount, experimentCount, isNoninteraction,
//                  detailUrl, seedInteraction? }
//   Protein:     { key, id, symbol, uniprot, entrez, degree, avgScore }
// A `score < 0` interaction row is "not found" (Interaction Query only).

import React, { useState } from "react";
import { scoreClass, uniprotUrl, entrezUrl, PaginationRow, PageSizeSelect } from "./shared.jsx";

export const DEFAULT_PAGE_SIZE = 25;

// Numeric columns default to descending on first sort; everything else ascending.
// Shared by the client-mode table below and Browse's server-mode onSort.
export function defaultSortDir(key) {
  return key === "score" || key === "degree" || key === "avg_score"
    ? "desc"
    : "asc";
}

// ── Export bars ─────────────────────────────────────────────────────────────
function ClientExportBar({ header, lines, filename }) {
  const tsv = () => [header.join("\t"), ...lines.map((c) => c.join("\t"))].join("\n");
  const copy = () => navigator.clipboard.writeText(tsv());
  const download = () => {
    const a = Object.assign(document.createElement("a"), {
      href: URL.createObjectURL(new Blob([tsv()], { type: "text/tab-separated-values" })),
      download: filename,
    });
    a.click();
    URL.revokeObjectURL(a.href);
  };
  return (
    <div className="export-bar">
      <button onClick={copy}>
        <i className="bi bi-clipboard me-1"></i>Copy
      </button>
      <button onClick={download}>
        <i className="bi bi-download me-1"></i>TSV
      </button>
    </div>
  );
}

// Server-side export (Browse): fetch a capped TSV built by the backend so the
// export matches every matching row, not just the page in memory.
export function ServerExportBar({ url, filename, disabled }) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const flash = (m) => {
    setMsg(m);
    if (m) setTimeout(() => setMsg(""), 4000);
  };
  const run = async (action) => {
    setBusy(true);
    setMsg("");
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
          download: filename,
        });
        a.click();
        URL.revokeObjectURL(a.href);
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

// ── Small shared bits ───────────────────────────────────────────────────────
const RowArrow = ({ href, stop }) =>
  href ? (
    <a href={href} onClick={stop} title="View details" style={{ color: "var(--hippie-teal)", fontSize: "1.3rem", lineHeight: 1 }}>
      <i className="bi bi-arrow-right-circle"></i>
    </a>
  ) : (
    <span className="text-muted-sm">—</span>
  );

const ExtA = ({ href, children, stop }) =>
  href ? (
    <a href={href} target="_blank" rel="noopener noreferrer" onClick={stop}>
      {children}
    </a>
  ) : (
    <span>{children}</span>
  );

// Generic client/server sort+paginate controller shared by both tables.
function useTableState(rows, isServer, server, defaultSortKey, defaultSortDir, sortValues) {
  const [cSortKey, setCSortKey] = useState(defaultSortKey);
  const [cSortDir, setCSortDir] = useState(defaultSortDir);
  const [cPage, setCPage] = useState(1);
  const [cPageSize, setCPageSize] = useState(DEFAULT_PAGE_SIZE);

  const sortKey = isServer ? server.sort?.key : cSortKey;
  const sortDir = isServer ? server.sort?.dir : cSortDir;

  const handleSort = (key) => {
    if (isServer) {
      server.onSort(key);
      return;
    }
    if (cSortKey === key) setCSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else {
      setCSortKey(key);
      setCSortDir(defaultSortDir(key));
    }
    setCPage(1);
  };

  const page = isServer ? server.page : cPage;
  const pageSize = isServer ? server.pageSize : cPageSize;
  const total = isServer ? server.total : rows.length;
  const totalPages = isServer ? server.totalPages : Math.max(1, Math.ceil(rows.length / pageSize));

  let sortedAll = rows;
  let pageRows = rows;
  if (!isServer) {
    const val = sortValues[sortKey] || (() => 0);
    sortedAll = [...rows].sort((a, b) => {
      const va = val(a);
      const vb = val(b);
      if (typeof va === "string") return sortDir === "asc" ? va.localeCompare(vb) : vb.localeCompare(va);
      return sortDir === "asc" ? va - vb : vb - va;
    });
    pageRows = sortedAll.slice((cPage - 1) * cPageSize, cPage * cPageSize);
  }

  const onPageChange = (p) => {
    if (isServer) server.onPageChange(p);
    else setCPage(p);
    window.scrollTo({ top: 0, behavior: "smooth" });
  };
  const onPageSizeChange = (s) => {
    if (isServer) server.onPageSizeChange(s);
    else {
      setCPageSize(s);
      setCPage(1);
    }
  };

  const thCls = (k) => (sortKey === k ? `sorted-${sortDir}` : "");
  return { handleSort, thCls, page, pageSize, total, totalPages, sortedAll, pageRows, onPageChange, onPageSizeChange };
}

// ── Interaction table ───────────────────────────────────────────────────────
const INT_SORT_VALUES = {
  input_order: (r) => r.input_order ?? 0,
  score: (r) => r.score,
  symbol_a: (r) => r.a.symbol || "",
  uniprot_a: (r) => r.a.isoform || r.a.uniprot || "",
  entrez_a: (r) => r.a.entrez ?? -1,
  symbol_b: (r) => r.b.symbol || "",
  uniprot_b: (r) => r.b.isoform || r.b.uniprot || "",
  entrez_b: (r) => r.b.entrez ?? -1,
  sources: (r) => r.sourceCount ?? -1,
  experiments: (r) => r.experimentCount ?? -1,
};

const INT_TSV_HEADER = [
  "Gene A", "UniProt A", "Entrez A", "Gene B", "UniProt B", "Entrez B",
  "Score", "Sources", "Experiments", "Type", "Review Type A", "Review Type B",
];

function intRowType(r) {
  if (r.score < 0) return "Not found";
  return r.isNoninteraction ? "Non-Interaction" : "Interaction";
}

function intTsvRow(r) {
  const nf = r.score < 0;
  return [
    r.a.symbol,
    r.a.isoform || r.a.uniprot || "",
    r.a.entrez ?? "",
    r.b.symbol,
    r.b.isoform || r.b.uniprot || "",
    r.b.entrez ?? "",
    nf ? -1 : r.score,
    nf ? "Not found" : r.sourceCount ?? "",
    nf ? "Not found" : r.experimentCount ?? "",
    intRowType(r),
    r.a.is_reviewed ? "reviewed" : "unreviewed",
    r.b.is_reviewed ? "reviewed" : "unreviewed",
  ].map((c) => String(c).replace(/[\t\r\n]/g, " "));
}

function ProteinCells({ p, stop }) {
  const acc = p.isoform || p.uniprot;
  return (
    <>
      <td>
        <strong>{p.symbol || "—"} </strong>
        {p.is_reviewed === false && (
          <span className="score-badge score-low">unreviewed</span>
        )}
      </td>
      <td>
        {acc ? (
          <ExtA href={uniprotUrl(acc)} stop={stop}>
            <span className="mono">{acc}</span>
          </ExtA>
        ) : (
          <span className="text-muted-sm">—</span>
        )}
      </td>
      <td>
        {p.entrez != null ? (
          <ExtA href={entrezUrl(p.entrez)} stop={stop}>
            <span className="mono">{p.entrez}</span>
          </ExtA>
        ) : (
          <span className="text-muted-sm">—</span>
        )}
      </td>
    </>
  );
}

function InteractionRow({ r, showSeed }) {
  const nf = r.score < 0;
  const stop = (e) => e.stopPropagation();
  const go = () => {
    if (r.detailUrl) window.location.href = r.detailUrl;
  };
  const rowCls = nf ? "row-not-found" : r.isNoninteraction ? "row-noninteraction" : "";
  return (
    <tr className={rowCls} onClick={go} style={{ cursor: r.detailUrl ? "pointer" : "default" }}>
      <ProteinCells p={r.a} stop={stop} />
      <ProteinCells p={r.b} stop={stop} />
      <td>
        {nf ? (
          <span className="tag-chip" style={{ color: "var(--hippie-accent)" }}>-1</span>
        ) : (
          <span className={scoreClass(r.score)}>{r.score.toFixed(4)}</span>
        )}
      </td>
      <td>
        {nf ? (
          <span className="text-muted-sm" style={{ color: "var(--hippie-accent)" }}>Not found</span>
        ) : r.sourceCount != null ? (
          <span className="tag-chip">{r.sourceCount}</span>
        ) : (
          <span className="text-muted-sm">—</span>
        )}
      </td>
      <td>
        {nf ? (
          <span className="text-muted-sm" style={{ color: "var(--hippie-accent)" }}>Not found</span>
        ) : r.experimentCount != null ? (
          <span className="tag-chip">{r.experimentCount}</span>
        ) : (
          <span className="text-muted-sm">—</span>
        )}
      </td>
      {showSeed && (
        <td>
          {r.seedInteraction ? (
            <i className="bi bi-check-circle-fill" style={{ color: "var(--hippie-teal)" }} title="Seed interaction"></i>
          ) : (
            <i className="bi bi-x-circle text-muted-sm" title="Not a seed interaction"></i>
          )}
        </td>
      )}
      <td onClick={stop}>
        <RowArrow href={r.detailUrl} stop={stop} />
      </td>
    </tr>
  );
}

export function InteractionTable({
  rows,
  title = "Results",
  countLabel,
  defaultSortKey = "score",
  defaultSortDir = "desc",
  exportFilename = "hippie_interactions.tsv",
  serverExport, // { url, disabled } → use server-side export
  headerExtra, // extra toolbar node (e.g. Generate Splits button)
  showSeed = false, // Network Query "Seed Interaction" column
  server, // server-mode controller
  streaming,
  progress,
}) {
  const isServer = !!server;
  const st = useTableState(rows, isServer, server, defaultSortKey, defaultSortDir, INT_SORT_VALUES);
  const notFound = rows.filter((r) => r.score < 0).length;
  const exportLines = (isServer ? rows : st.sortedAll).map(intTsvRow);

  return (
    <div>
      <div className="d-flex justify-content-between align-items-start flex-wrap gap-3 mb-3">
        <div>
          <h2 className="results-title">{title}</h2>
          <div className="d-flex gap-3 mt-1 flex-wrap">
            <span className="text-muted-sm">
              {countLabel ?? `${st.total.toLocaleString()} result${st.total !== 1 ? "s" : ""}`}
            </span>
            {notFound > 0 && (
              <span style={{ fontSize: ".8rem", color: "var(--hippie-accent)" }}>
                <i className="bi bi-exclamation-circle me-1"></i>
                {notFound} not found
              </span>
            )}
            {streaming && (
              <span className="text-muted-sm">
                <span className="spinner me-1"></span>Loading… {Math.round((progress || 0) * 100)}%
              </span>
            )}
          </div>
        </div>
        <div className="d-flex align-items-center gap-3">
          <PageSizeSelect pageSize={st.pageSize} onChange={st.onPageSizeChange} />
          {!streaming &&
            (serverExport ? (
              <ServerExportBar url={serverExport.url} filename={exportFilename} disabled={serverExport.disabled} />
            ) : (
              <ClientExportBar header={INT_TSV_HEADER} lines={exportLines} filename={exportFilename} />
            ))}
          {headerExtra}
        </div>
      </div>

      {streaming && (
        <div className="batch-progress mb-3">
          <div className="batch-progress-fill" style={{ width: `${Math.round((progress || 0) * 100)}%` }} />
        </div>
      )}

      <div className="hippie-card p-0 overflow-hidden">
        <div style={{ overflowX: "auto" }}>
          <table className="hippie-table">
            <thead>
              <tr>
                <th onClick={() => st.handleSort("symbol_a")} className={st.thCls("symbol_a")}>Gene A</th>
                <th onClick={() => st.handleSort("uniprot_a")} className={st.thCls("uniprot_a")}>UniProt A</th>
                <th onClick={() => st.handleSort("entrez_a")} className={st.thCls("entrez_a")}>Entrez A</th>
                <th onClick={() => st.handleSort("symbol_b")} className={st.thCls("symbol_b")}>Gene B</th>
                <th onClick={() => st.handleSort("uniprot_b")} className={st.thCls("uniprot_b")}>UniProt B</th>
                <th onClick={() => st.handleSort("entrez_b")} className={st.thCls("entrez_b")}>Entrez B</th>
                <th onClick={() => st.handleSort("score")} className={st.thCls("score")}>Score</th>
                <th onClick={() => st.handleSort("sources")} className={st.thCls("sources")}>Sources</th>
                <th onClick={() => st.handleSort("experiments")} className={st.thCls("experiments")}>Experiments</th>
                {showSeed && <th style={{ cursor: "default" }}>Seed</th>}
                <th style={{ cursor: "default" }}></th>
              </tr>
            </thead>
            <tbody>
              {st.pageRows.map((r) => (
                <InteractionRow key={r.key} r={r} showSeed={showSeed} />
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="mt-3">
        <PaginationRow
          page={st.page}
          totalPages={st.totalPages}
          totalItems={st.total}
          pageSize={st.pageSize}
          onChange={st.onPageChange}
        />
      </div>
    </div>
  );
}

// ── Protein table (Browse Proteins) ─────────────────────────────────────────
const PROT_SORT_VALUES = {
  symbol: (r) => r.symbol || "",
  uniprot_id: (r) => r.uniprot || "",
  entrez_id: (r) => r.entrez ?? -1,
  degree: (r) => r.degree ?? 0,
  avg_score: (r) => r.avgScore ?? -1,
};

const PROT_TSV_HEADER = ["Gene Symbol", "UniProt", "Entrez", "Degree", "Avg. Score"];

function protTsvRow(r) {
  return [r.symbol, r.uniprot || "", r.entrez ?? "", r.degree ?? "", r.avgScore != null ? r.avgScore : ""].map((c) =>
    String(c).replace(/[\t\r\n]/g, " "),
  );
}

function ProteinRow({ r, queryUrl }) {
  const href = queryUrl ? `${queryUrl}?q=${encodeURIComponent(r.symbol)}` : "";
  const stop = (e) => e.stopPropagation();
  const go = () => {
    if (href) window.location.href = href;
  };
  return (
    <tr onClick={go} style={{ cursor: href ? "pointer" : "default" }} title={`Query ${r.symbol}`}>
      <td>
        <strong>{r.symbol || "—"}</strong>
      </td>
      <td>
        {r.uniprot ? (
          <ExtA href={uniprotUrl(r.uniprot)} stop={stop}>
            <span className="mono">{r.uniprot}</span>
          </ExtA>
        ) : (
          <span className="text-muted-sm">—</span>
        )}
      </td>
      <td>
        {r.entrez != null ? (
          <ExtA href={entrezUrl(r.entrez)} stop={stop}>
            <span className="mono">{r.entrez}</span>
          </ExtA>
        ) : (
          <span className="text-muted-sm">—</span>
        )}
      </td>
      <td>
        <span className="tag-chip">{r.degree}</span>
      </td>
      <td>
        {r.avgScore != null ? (
          <span className={scoreClass(r.avgScore)}>{r.avgScore.toFixed(4)}</span>
        ) : (
          <span className="text-muted-sm">—</span>
        )}
      </td>
      <td onClick={stop}>
        <RowArrow href={href} stop={stop} />
      </td>
    </tr>
  );
}

export function ProteinTable({
  rows,
  title = "Proteins",
  countLabel,
  proteinQueryUrl,
  defaultSortKey = "symbol",
  defaultSortDir = "asc",
  exportFilename = "hippie_proteins.tsv",
  serverExport,
  headerExtra,
  server,
}) {
  const isServer = !!server;
  const st = useTableState(rows, isServer, server, defaultSortKey, defaultSortDir, PROT_SORT_VALUES);
  const exportLines = (isServer ? rows : st.sortedAll).map(protTsvRow);

  return (
    <div>
      <div className="d-flex justify-content-between align-items-start flex-wrap gap-3 mb-3">
        <div>
          <h2 className="results-title">{title}</h2>
          <span className="text-muted-sm">
            {countLabel ?? `${st.total.toLocaleString()} result${st.total !== 1 ? "s" : ""}`}
          </span>
        </div>
        <div className="d-flex align-items-center gap-3">
          <PageSizeSelect pageSize={st.pageSize} onChange={st.onPageSizeChange} />
          {serverExport ? (
            <ServerExportBar url={serverExport.url} filename={exportFilename} disabled={serverExport.disabled} />
          ) : (
            <ClientExportBar header={PROT_TSV_HEADER} lines={exportLines} filename={exportFilename} />
          )}
          {headerExtra}
        </div>
      </div>

      <div className="hippie-card p-0 overflow-hidden">
        <div style={{ overflowX: "auto" }}>
          <table className="hippie-table">
            <thead>
              <tr>
                <th onClick={() => st.handleSort("symbol")} className={st.thCls("symbol")}>Gene Symbol</th>
                <th onClick={() => st.handleSort("uniprot_id")} className={st.thCls("uniprot_id")}>UniProt ID</th>
                <th onClick={() => st.handleSort("entrez_id")} className={st.thCls("entrez_id")}>Entrez Gene ID</th>
                <th onClick={() => st.handleSort("degree")} className={st.thCls("degree")}>Degree</th>
                <th onClick={() => st.handleSort("avg_score")} className={st.thCls("avg_score")}>Avg. Score</th>
                <th style={{ cursor: "default" }}></th>
              </tr>
            </thead>
            <tbody>
              {st.pageRows.map((r) => (
                <ProteinRow key={r.key ?? r.id} r={r} queryUrl={proteinQueryUrl} />
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="mt-3">
        <PaginationRow
          page={st.page}
          totalPages={st.totalPages}
          totalItems={st.total}
          pageSize={st.pageSize}
          onChange={st.onPageChange}
        />
      </div>
    </div>
  );
}
