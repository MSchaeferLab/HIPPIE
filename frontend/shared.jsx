// Shared utilities and components used by all React pages.

// Confidence thresholds come from the active release (window.HIPPIE_RELEASE,
// injected by base.html); fall back to the documented v3.0 values.
const _REL = (typeof window !== "undefined" && window.HIPPIE_RELEASE) || {};
const MED_THRESHOLD = _REL.intMedian ?? 0.00;
const HIGH_THRESHOLD = _REL.intQ3 ?? 0.00;

export function scoreClass(s) {
  if (s >= HIGH_THRESHOLD) return "score-badge score-high";
  if (s >= MED_THRESHOLD) return "score-badge score-med";
  return "score-badge score-low";
}

export const uniprotUrl = (id) => id ? `https://www.uniprot.org/uniprot/${id}` : null;
export const entrezUrl  = (id) => id ? `https://www.ncbi.nlm.nih.gov/datasets/gene/${id}` : null;

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

const _PAGE_SIZES = [10, 25, 50, 100];

export function PageSizeSelect({ pageSize, onChange }) {
  return (
    <label className="text-muted-sm d-inline-flex align-items-center gap-1">
      Per page
      <select className="form-select form-select-sm" style={{width:"auto",display:"inline-block"}}
              value={pageSize} onChange={e => onChange(parseInt(e.target.value))}>
        {_PAGE_SIZES.map(s => <option key={s} value={s}>{s}</option>)}
      </select>
    </label>
  );
}

export function SortableTh({ sortKey, currentKey, currentDir, onSort, children, className="" }) {
  const cls = [className, currentKey === sortKey ? `sorted-${currentDir}` : ""].join(" ").trim();
  return <th className={cls} onClick={() => onSort(sortKey)}>{children}</th>;
}

// Read a cookie value by name (used for the Django CSRF token in POST fetches).
export function getCookie(name) {
  const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
  return m ? decodeURIComponent(m[1]) : "";
}
