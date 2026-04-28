// Shared utilities and components used by all React pages.

export function scoreClass(s) {
  if (s >= 0.72) return "score-badge score-high";
  if (s >= 0.63) return "score-badge score-med";
  return "score-badge score-low";
}

export const uniprotUrl = (id) => id ? `https://www.uniprot.org/uniprot/${id}` : null;
export const entrezUrl  = (id) => id ? `https://www.ncbi.nlm.nih.gov/gene/${id}` : null;

export function ScoreBadge({ score }) {
  if (score < 0) return <span className="tag-chip" style={{color:"var(--hippie-accent)"}}>Not found</span>;
  return <span className={scoreClass(score)}>{score.toFixed(4)}</span>;
}

export function ExtLink({ href, children }) {
  return href
    ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>
    : <span>{children}</span>;
}

export function Pagination({ page, totalPages, onChange }) {
  if (totalPages <= 1) return null;
  const pages = [];
  const lo = Math.max(1, page - 2), hi = Math.min(totalPages, page + 2);
  if (lo > 1) { pages.push(1); if (lo > 2) pages.push("…"); }
  for (let i = lo; i <= hi; i++) pages.push(i);
  if (hi < totalPages) { if (hi < totalPages - 1) pages.push("…"); pages.push(totalPages); }
  return (
    <div className="hippie-pagination">
      <button disabled={page === 1} onClick={() => onChange(page - 1)}>‹</button>
      {pages.map((p, i) => p === "…"
        ? <span key={i} style={{padding:"0 .25rem",color:"var(--hippie-ink-muted)"}}>…</span>
        : <button key={p} className={p === page ? "active" : ""} onClick={() => onChange(p)}>{p}</button>
      )}
      <button disabled={page === totalPages} onClick={() => onChange(page + 1)}>›</button>
    </div>
  );
}

export function PaginationRow({ page, totalPages, totalItems, pageSize, onChange }) {
  if (totalPages <= 1) return null;
  return (
    <div className="d-flex justify-content-between align-items-center flex-wrap gap-2">
      <span className="text-muted-sm">
        Page {page} of {totalPages} — {(page-1)*pageSize+1}–{Math.min(page*pageSize, totalItems)} of {totalItems}
      </span>
      <Pagination page={page} totalPages={totalPages} onChange={onChange} />
    </div>
  );
}

export function SortableTh({ sortKey, currentKey, currentDir, onSort, children, className="" }) {
  const cls = [className, currentKey === sortKey ? `sorted-${currentDir}` : ""].join(" ").trim();
  return <th className={cls} onClick={() => onSort(sortKey)}>{children}</th>;
}
