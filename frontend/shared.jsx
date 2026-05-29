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

function _range(start, end) {
  const out = [];
  for (let i = start; i <= end; i++) out.push(i);
  return out;
}

// Build the page-button items: always show first + last, a sibling window
// around the current page, and a single "…" only when it would hide ≥2 pages
// (otherwise the hidden single page is rendered as a normal button). This
// guarantees no duplicate page numbers and a stable button count.
export function paginationItems(page, totalPages, siblingCount = 1, boundaryCount = 1) {
  if (totalPages <= 1) return totalPages === 1 ? [1] : [];

  const startPages = _range(1, Math.min(boundaryCount, totalPages));
  const endPages = _range(
    Math.max(totalPages - boundaryCount + 1, boundaryCount + 1),
    totalPages,
  );

  const siblingsStart = Math.max(
    Math.min(page - siblingCount, totalPages - boundaryCount - siblingCount * 2 - 1),
    boundaryCount + 2,
  );
  const siblingsEnd = Math.min(
    Math.max(page + siblingCount, boundaryCount + siblingCount * 2 + 2),
    endPages.length > 0 ? endPages[0] - 2 : totalPages - 1,
  );

  return [
    ...startPages,
    ...(siblingsStart > boundaryCount + 2
      ? ["ellipsis"]
      : boundaryCount + 1 < totalPages - boundaryCount
        ? [boundaryCount + 1]
        : []),
    ..._range(siblingsStart, siblingsEnd),
    ...(siblingsEnd < totalPages - boundaryCount - 1
      ? ["ellipsis"]
      : totalPages - boundaryCount > boundaryCount
        ? [totalPages - boundaryCount]
        : []),
    ...endPages,
  ];
}

export function Pagination({ page, totalPages, onChange }) {
  if (totalPages <= 1) return null;
  const items = paginationItems(page, totalPages);
  return (
    <div className="hippie-pagination">
      <button disabled={page === 1} onClick={() => onChange(page - 1)}>‹</button>
      {items.map((p, i) => p === "ellipsis"
        ? <span key={`e${i}`} style={{padding:"0 .25rem",color:"var(--hippie-ink-muted)"}}>…</span>
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
