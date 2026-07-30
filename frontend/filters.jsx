// Shared, controlled FilterBox used by every query page (Batch 3+).
//
// The box is fully controlled: the *parent* owns the draft filter value and the
// draft-vs-applied semantics (nothing searches until the page commits the
// draft). FilterBox only renders controls and calls `onChange` with the next
// value. One component → one place to change filters everywhere.

import React, { useEffect, useRef, useState } from "react";
import { InfoPopover, DL } from "./shared.jsx";

// ── Unified filter state ────────────────────────────────────────────────────
// The multi-select lists (source / experiment / interactionType / tissue) start
// empty and are seeded to "everything ticked" by useAllSelectedDefaults as soon
// as the option lists arrive — we cannot list ids we have not fetched yet.
// Empty and full are equivalent on the wire (see _serialize): the backend
// applies no predicate for an omitted list, so both mean "all rows pass".
export const FILTER_DEFAULTS = {
  showMode: "interactions", // interactions | noninteractions | both
  isoformMode: "general", // general | isoforms | both
  minScore: 0,
  maxScore: 1,
  source: [], // ids
  experiment: [], // ids
  interactionType: [], // ids
  tissue: [], // ids
  minRpkm: 0,
  minDegree: 0,
  minAvgScore: 0,
  reviewed: "both", // both | reviewed | unreviewed
};

// All controls in display order. Pages pass `controls` to pick a subset;
// omit to show them all (full parity — Protein Query & Interaction Query).
export const ALL_CONTROLS = [
  "showMode",
  "score",
  "source",
  "experiment",
  "interactionType",
  "tissue",
  "protein",
  "reviewed",
  "isoforms",
];

// Empty filter-metadata shape + loader hook. Every query page fetches the same
// tissue/source/experiment/interaction-type option lists from its filterMetaUrl;
// this centralises the empty default and the fetch effect.
export const EMPTY_META = {
  tissues: [],
  sources: [],
  experiments: [],
  interaction_types: [],
};

export function useFilterMeta(url) {
  const [meta, setMeta] = useState(EMPTY_META);
  useEffect(() => {
    if (url)
      fetch(url)
        .then((r) => r.json())
        .then(setMeta)
        .catch(() => {});
  }, [url]);
  return meta;
}

// Which filter-state key each option list seeds.
const LIST_KEYS = [
  ["sources", "source"],
  ["experiments", "experiment"],
  ["interaction_types", "interactionType"],
  ["tissues", "tissue"],
];

// FILTER_DEFAULTS with every option ticked. Use this instead of FILTER_DEFAULTS
// wherever a page resets its filters after the option lists have loaded.
export function defaultFilters(meta = {}) {
  const f = { ...FILTER_DEFAULTS };
  LIST_KEYS.forEach(([metaKey, filterKey]) => {
    f[filterKey] = allOptionIds(meta[metaKey] || []);
  });
  return f;
}

// Tick everything once the option lists land.
//
// Starting empty reads as "nothing included" even though the backend treats it
// as "no filter", so every box starts ticked instead. Seeding is safe precisely
// because the two are equivalent: a list that is still empty when the options
// arrive was not filtering anything, so filling it in changes no results.
//
// Pass every state setter that holds a filter object — typically the draft *and*
// the applied copy. Seeding both keeps them equal, so the page does not come up
// looking dirty with an Apply button lit for a change the user never made.
//
// Runs once (guarded by a ref) so a later meta refresh cannot resurrect boxes
// the user has since unticked. Lists already populated — from URL params or an
// ML-splits hand-off — are left exactly as they came in.
export function useAllSelectedDefaults(meta, setters) {
  const seeded = useRef(false);
  useEffect(() => {
    if (seeded.current) return;
    const lists = {};
    LIST_KEYS.forEach(([metaKey, filterKey]) => {
      const items = meta[metaKey] || [];
      if (items.length) lists[filterKey] = allOptionIds(items);
    });
    // Meta has not arrived yet — try again on the next render.
    if (!Object.keys(lists).length) return;
    seeded.current = true;
    setters.forEach((setState) =>
      setState((prev) => {
        const patch = {};
        Object.entries(lists).forEach(([key, ids]) => {
          if ((prev[key] || []).length === 0) patch[key] = ids;
        });
        return Object.keys(patch).length ? { ...prev, ...patch } : prev;
      }),
    );
  });
}

// ── Multi-select selection helpers ──────────────────────────────────────────
// Sorted by id, the same canonical order `toggleIn` and `setIds` produce. The
// dirty check (`filtersEqual`) is a JSON compare and therefore order-sensitive,
// so a seeded list left in the backend's name order would read as "changed" on
// the user's first click even though the set of ticked boxes still matched.
export function allOptionIds(items) {
  return (items || [])
    .map((it) => it.id)
    .sort((a, b) => String(a).localeCompare(String(b)));
}

export function isFullSelection(selected, items) {
  if (!items || !items.length) return false;
  const sel = new Set((selected || []).map(String));
  return items.every((it) => sel.has(String(it.id)));
}

// True only when the list actually narrows the result set. All-ticked and
// none-ticked both mean "no filter" and are omitted from the request.
export function listNarrows(selected, items) {
  return (selected || []).length > 0 && !isFullSelection(selected, items);
}

// The funnel "Filters" toggle button with its active-count badge, shared by the
// Protein / Interaction / Browse query pages (byte-identical markup).
export function FilterToggleButton({ activeCount, filtersOpen, onClick }) {
  return (
    <button
      className={`btn-filter-toggle${filtersOpen ? " active" : ""}`}
      onClick={onClick}
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
  );
}

const _REL = (typeof window !== "undefined" && window.HIPPIE_RELEASE) || {};

// " v11_2025-08-22" when base.html injected the active release's GTEx version,
// "" otherwise — so the tissue help text names the release without hardcoding it.
export function gtexVersionSuffix() {
  return _REL.gtexVersion ? ` ${_REL.gtexVersion}` : "";
}

// Q2 (medium) / Q3 (high) thresholds for the currently selected result type,
// read from the active release injected by base.html (fallbacks documented).
export function confThresholds(showMode) {
  if (showMode === "noninteractions")
    return { med: _REL.nonintMedian ?? 0.00, high: _REL.nonintQ3 ?? 0.00 };
  if (showMode === "both")
    return { med: _REL.bothMedian ?? 0.00, high: _REL.bothQ3 ?? 0.00 };
  return { med: _REL.intMedian ?? 0.00, high: _REL.intQ3 ?? 0.00 };
}

// Tint for the confidence preset chips — mirrors the score-badge colors a score
// earns once it crosses each threshold (see scoreClass in shared.jsx / hippie.css).
export const CONF_CHIP_STYLE = {
  med: {
    background: "#fef3e2",
    color: "var(--hippie-score-med)",
    borderColor: "var(--hippie-score-med)",
  },
  high: {
    background: "var(--hippie-teal-soft)",
    color: "var(--hippie-score-high)",
    borderColor: "var(--hippie-score-high)",
  },
};

// ── Reusable multi-select checkbox list ─────────────────────────────────────
const UNGROUPED = "__ungrouped__";
const OTHER_CATEGORY = "Other";

// Everything is ordered by name: a user looking for a term they already know
// ("pull down") can scan for it. Ordering by interaction count instead sorts on
// a number that is not what the eye is searching for.
const byName = (a, b) => a.name.localeCompare(b.name);

const sumCounts = (items) =>
  items.some((it) => it.count != null)
    ? items.reduce((n, it) => n + (it.count || 0), 0)
    : null;

// Compact volume for a group header: 1050289 → "1,050k", 866 → "866".
function fmtVolume(n) {
  if (n == null) return "";
  return n >= 1000 ? `${Math.round(n / 1000).toLocaleString()}k` : String(n);
}

/**
 * Bucket options into `[{name, items, subgroups, loose, count}]`.
 *
 * Two levels: `category` makes the group, an optional `subcategory` makes a
 * sub-heading inside it (13 GTEx brain regions collapse behind one "Brain"
 * rather than filling the Nervous system group). Options with no subcategory
 * sit directly under the group, after the sub-headings.
 *
 * Every level — categories, sub-headings, options — is ordered by name, so a
 * category the backend invented that the frontend has not been taught about
 * still lands in its alphabetical place rather than being appended or
 * vanishing. "Other" is pinned last whatever happens.
 */
function buildGroups(items) {
  const buckets = new Map();
  items.forEach((it) => {
    const key = it.category || UNGROUPED;
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(it);
  });

  const names = [...buckets.keys()]
    .filter((c) => c !== OTHER_CATEGORY)
    .sort((a, b) => a.localeCompare(b));
  if (buckets.has(OTHER_CATEGORY)) names.push(OTHER_CATEGORY);

  return names.map((name) => {
    const all = buckets.get(name);
    const subMap = new Map();
    const loose = [];
    all.forEach((it) => {
      if (!it.subcategory) return loose.push(it);
      if (!subMap.has(it.subcategory)) subMap.set(it.subcategory, []);
      subMap.get(it.subcategory).push(it);
    });
    let subgroups = [...subMap.entries()]
      .map(([subName, subItems]) => ({
        name: subName,
        items: subItems.slice().sort(byName),
        count: sumCounts(subItems),
      }))
      .sort(byName);
    // A lone sub-heading covering the whole group ("Cultured cells › Cells")
    // adds a click and says nothing — show its options directly instead.
    let flat = loose;
    if (subgroups.length === 1 && !loose.length) {
      flat = subgroups[0].items;
      subgroups = [];
    }
    return {
      name,
      items: all,
      subgroups,
      loose: flat.slice().sort(byName),
      count: sumCounts(all),
    };
  });
}

const CL_BTN = {
  border: "1px solid var(--hippie-border)",
  background: "var(--hippie-surface)",
  color: "var(--hippie-ink-muted)",
  borderRadius: "100px",
  fontSize: ".62rem",
  fontFamily: "var(--font-mono)",
  padding: ".05rem .45rem",
  cursor: "pointer",
  lineHeight: 1.6,
};

function MiniBtn({ onClick, children, title }) {
  return (
    <button type="button" style={CL_BTN} title={title} onClick={onClick}>
      {children}
    </button>
  );
}

// Collapsible heading for a group (depth 0) or a sub-group (depth 1). Carries
// selected/total, the group's summed evidence volume, and its own all/none so a
// whole family can be toggled without opening it.
function Heading({ label, open, nSel, total, volume, onToggle, onAll, onNone, depth }) {
  return (
    <div
      className="d-flex align-items-center gap-2"
      style={{
        padding: ".2rem 0",
        paddingLeft: depth ? ".9rem" : 0,
        borderTop: depth ? "none" : "1px solid var(--hippie-border)",
      }}
    >
      <button
        type="button"
        onClick={onToggle}
        style={{
          border: "none",
          background: "none",
          padding: 0,
          cursor: "pointer",
          display: "flex",
          alignItems: "center",
          gap: ".3rem",
          flex: "1 1 auto",
          minWidth: 0,
          textAlign: "left",
          color: "var(--hippie-ink)",
          fontSize: depth ? ".74rem" : ".78rem",
          fontWeight: 400,
        }}
      >
        <i
          className={`bi bi-chevron-${open ? "down" : "right"}`}
          style={{ fontSize: ".6rem" }}
        ></i>
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {label}
        </span>
        <span
          className="text-muted-sm"
          style={{ fontFamily: "var(--font-mono)", fontSize: ".62rem", fontWeight: 400 }}
        >
          {nSel}/{total}
        </span>
      </button>
      {volume != null && (
        <span
          className="text-muted-sm"
          style={{ fontFamily: "var(--font-mono)", fontSize: ".62rem", whiteSpace: "nowrap" }}
          title={`${volume.toLocaleString()} in total`}
        >
          {fmtVolume(volume)}
        </span>
      )}
      <MiniBtn onClick={onAll} title={`Select all in ${label}`}>
        all
      </MiniBtn>
      <MiniBtn onClick={onNone} title={`Clear all in ${label}`}>
        none
      </MiniBtn>
    </div>
  );
}

/**
 * Multi-select list with search, per-option evidence counts, All/None, and
 * collapsible category groups.
 *
 * `noun` names the options in the empty-state hint ("experiment types"), which
 * spells out the one genuinely surprising behaviour: unticking everything is
 * not "exclude everything", it removes the filter.
 */
export function CheckboxList({
  items = [],
  selected = [],
  onChange,
  noun = "options",
  maxHeight = "220px",
}) {
  const [search, setSearch] = useState("");
  const [overrides, setOverrides] = useState({});
  const selSet = new Set(selected.map(String));
  const isOn = (it) => selSet.has(String(it.id));

  const needle = search.trim().toLowerCase();
  const visible = needle
    ? items.filter((it) => it.name.toLowerCase().includes(needle))
    : items;
  const grouped = items.some((it) => it.category);
  const groups = grouped ? buildGroups(visible) : [];
  // The backend already sends options alphabetically, but that leans on the DB
  // collation — sort here too so the flat list matches the grouped one.
  const flat = grouped ? [] : visible.slice().sort(byName);

  const setIds = (ids) =>
    onChange([...ids].sort((a, b) => String(a).localeCompare(String(b))));
  const addAll = (list) => setIds(new Set([...selected, ...allOptionIds(list)]));
  const removeAll = (list) => {
    const drop = new Set(allOptionIds(list).map(String));
    setIds(selected.filter((id) => !drop.has(String(id))));
  };

  // Groups start closed — with everything ticked by default, ten headers beat
  // 190 rows. A partly-selected group opens itself so the choice stays visible,
  // and searching opens everything that matched. `key` is the group name, or
  // "group/sub" for a sub-heading.
  const isExpanded = (key, list) => {
    if (needle) return true;
    if (key in overrides) return overrides[key];
    const n = list.filter(isOn).length;
    return n > 0 && n < list.length;
  };
  const toggleOpen = (key, list) =>
    setOverrides((o) => ({ ...o, [key]: !isExpanded(key, list) }));

  // depth 0 = ungrouped list, 1 = directly under a group, 2 = inside a subgroup.
  const renderOption = (it, depth) => (
    <label
      key={it.id}
      style={{
        display: "flex",
        alignItems: "center",
        gap: ".4rem",
        cursor: "pointer",
        padding: ".15rem 0",
        paddingLeft: `${depth * 0.9}rem`,
      }}
    >
      <input
        type="checkbox"
        checked={isOn(it)}
        onChange={() => onChange(toggleIn(selected, it.id))}
        style={{ cursor: "pointer" }}
      />
      <span
        className="text-muted-sm"
        style={{ color: "var(--hippie-ink)", flex: "1 1 auto" }}
      >
        {it.name}
      </span>
      {it.count != null && (
        <span
          className="text-muted-sm"
          style={{ fontFamily: "var(--font-mono)", fontSize: ".62rem", whiteSpace: "nowrap" }}
        >
          {it.count.toLocaleString()}
        </span>
      )}
    </label>
  );

  return (
    <div>
      <div className="d-flex align-items-center gap-2 mb-1 flex-wrap">
        <span
          className="text-muted-sm"
          style={{ fontFamily: "var(--font-mono)", fontSize: ".65rem" }}
        >
          {selected.length} / {items.length}
        </span>
        <MiniBtn onClick={() => setIds(allOptionIds(items))} title="Select every option">
          All
        </MiniBtn>
        <MiniBtn onClick={() => onChange([])} title="Clear every option">
          None
        </MiniBtn>
      </div>

      {items.length > 8 && (
        <input
          type="search"
          className="form-control mb-1"
          style={{ fontSize: ".8rem", padding: ".25rem .5rem" }}
          placeholder="Search…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      )}

      <div
        style={{
          maxHeight,
          overflowY: "auto",
          border: "1px solid var(--hippie-border)",
          borderRadius: "var(--radius-md)",
          padding: ".4rem .6rem",
        }}
      >
        {items.length === 0 && <span className="text-muted-sm">None available</span>}
        {items.length > 0 && visible.length === 0 && (
          <span className="text-muted-sm">No match for “{search}”.</span>
        )}

        {/* Ungrouped vocabulary (no categories at all): a plain flat list. It
            has no header to click, so it must never be collapsible. */}
        {!grouped && flat.map((it) => renderOption(it, 0))}

        {groups.map((group) => {
          if (!group.items.length) return null;
          const open = isExpanded(group.name, group.items);
          const nSel = group.items.filter(isOn).length;
          return (
            <div key={group.name}>
              <Heading
                label={group.name}
                open={open}
                nSel={nSel}
                total={group.items.length}
                volume={group.count}
                onToggle={() => toggleOpen(group.name, group.items)}
                onAll={() => addAll(group.items)}
                onNone={() => removeAll(group.items)}
                depth={0}
              />
              {open && (
                <>
                  {group.subgroups.map((sub) => {
                    const subKey = `${group.name}/${sub.name}`;
                    const subOpen = isExpanded(subKey, sub.items);
                    return (
                      <div key={subKey}>
                        <Heading
                          label={sub.name}
                          open={subOpen}
                          nSel={sub.items.filter(isOn).length}
                          total={sub.items.length}
                          volume={sub.count}
                          onToggle={() => toggleOpen(subKey, sub.items)}
                          onAll={() => addAll(sub.items)}
                          onNone={() => removeAll(sub.items)}
                          depth={1}
                        />
                        {subOpen && sub.items.map((it) => renderOption(it, 2))}
                      </div>
                    );
                  })}
                  {group.loose.map((it) => renderOption(it, 1))}
                </>
              )}
            </div>
          );
        })}
      </div>

      {items.length > 0 && selected.length === 0 && (
        <div className="text-muted-sm mt-1" style={{ fontSize: ".72rem" }}>
          <i className="bi bi-info-circle me-1"></i>
          Nothing ticked — no filter is applied, all {items.length} {noun} are
          included.
        </div>
      )}
    </div>
  );
}

export function toggleIn(arr, id) {
  const next = arr.map(String).includes(String(id))
    ? arr.filter((x) => String(x) !== String(id))
    : [...arr, id];
  // Canonical order so the draft-vs-applied dirty check (a JSON compare) is
  // insensitive to the order in which items were (re)selected.
  return next.sort((a, b) => String(a).localeCompare(String(b)));
}

// Count active (non-default) filters — drives the Filter button badge.
//
// A list counts only when it actually narrows the results: with every box
// ticked by default, counting ticks would show "190 filters" on an unfiltered
// page. `meta` is optional so a caller without option lists still gets a
// sensible (if slightly generous) count.
export function countActiveFilters(f, controls = ALL_CONTROLS, meta = {}) {
  const on = new Set(controls);
  const listCount = (selected, items) =>
    listNarrows(selected, items) ? selected.length : 0;
  let n = 0;
  if (on.has("showMode") && f.showMode !== "interactions") n++;
  if (on.has("isoforms") && f.isoformMode !== "general") n++;
  if (on.has("score") && (f.minScore > 0 || f.maxScore < 1)) n++;
  if (on.has("source")) n += listCount(f.source, meta.sources);
  if (on.has("experiment")) n += listCount(f.experiment, meta.experiments);
  if (on.has("interactionType"))
    n += listCount(f.interactionType, meta.interaction_types);
  if (on.has("tissue")) n += listCount(f.tissue, meta.tissues);
  if (on.has("protein") && f.minDegree > 0) n++;
  if (on.has("protein") && f.minAvgScore > 0) n++;
  if (on.has("reviewed") && f.reviewed !== "both") n++;
  return n;
}

export function filtersEqual(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

// Serialise to backend param names (single source of truth). Returns
// { scalars, lists } which the GET / POST adapters below flatten.
//
// A list is sent only when it is a proper subset of the available options. Both
// extremes — every box ticked, no box ticked — omit the parameter, which is what
// the backend already treats as "no filter" (query_filters.py: `if source_ids:`).
// That keeps the API contract untouched and keeps requests small: an unfiltered
// page would otherwise carry ~190 experiment ids on every keystroke.
function _serialize(f, meta = {}) {
  const scalars = { show: f.showMode };
  const lists = {};
  if (f.isoformMode !== "general") scalars.isoform_mode = f.isoformMode;
  if (f.minScore > 0) scalars.min_score = f.minScore;
  if (f.maxScore < 1) scalars.max_score = f.maxScore;
  if (listNarrows(f.source, meta.sources)) lists.source = f.source;
  if (listNarrows(f.experiment, meta.experiments)) lists.experiment = f.experiment;
  if (listNarrows(f.interactionType, meta.interaction_types))
    lists.interaction_type = f.interactionType;
  if (listNarrows(f.tissue, meta.tissues)) {
    lists.tissue = f.tissue;
    if (f.minRpkm > 0) scalars.min_rpkm = f.minRpkm;
  }
  if (f.minDegree > 0) scalars.min_degree = f.minDegree;
  if (f.minAvgScore > 0) scalars.min_avg_score = f.minAvgScore;
  if (f.reviewed !== "both") scalars.reviewed = f.reviewed;
  return { scalars, lists };
}

// GET query string (Protein Query, Browse).
export function filtersToQuery(f, meta = {}) {
  const { scalars, lists } = _serialize(f, meta);
  const p = new URLSearchParams();
  Object.entries(scalars).forEach(([k, v]) => p.set(k, v));
  Object.entries(lists).forEach(([k, arr]) => arr.forEach((v) => p.append(k, v)));
  return p;
}

// POST JSON body fields (Interaction Query, Network Query).
export function filtersToBody(f, meta = {}) {
  const { scalars, lists } = _serialize(f, meta);
  return { ...scalars, ...lists };
}

// ── The FilterBox ───────────────────────────────────────────────────────────
// ── Filter help text (definition lists shown in section-header pop-ups) ──────
const RESULT_TYPE_HELP = DL([
  ["Interactions", "Positive, experimentally supported protein pairs."],
  ["Non-interactions", "Sampled negative pairs (no known interaction), rows are shown with a grey background."],
  ["Both", "Show positives and sampled negatives together."],
]);
const CONFIDENCE_HELP = DL([
  ["Min. score ≥", "Keep interactions with confidence ≥ this value (0–1)."],
  ["Max. score ≤", "Keep interactions with confidence ≤ this value. Sliders clamp so min ≤ max."],
  ["Medium / High conf.", "One-click presets snapping Min. score to the release's median (medium) or Q3 (high) confidence threshold."],
]);
// Every multi-select shares the same two surprises, so both are stated in each
// popover rather than assumed: selections are OR-ed, and clearing the list
// removes the filter instead of excluding everything.
const NOTHING_TICKED = [
  "Nothing ticked",
  "Same as everything ticked — the filter is dropped and all options are included.",
];
// The number beside each option is a denormalised whole-database column, never
// recomputed against the active filters — say so, or it reads as a stale count.
const GLOBAL_COUNTS = [
  "Counts are global",
  "The number beside each option counts across all of HIPPIE. It is not recomputed for the subset your current filters leave, so it does not change as you narrow the query.",
];
const TISSUE_COUNTS = [
  "Counts are global",
  "The number beside each tissue is how many genes GTEx measures as expressed there, across all of HIPPIE — not interactions, and not the subset your current filters leave. It does not change as you narrow the query.",
];
const SOURCE_HELP = DL([
  ["Source database", "Keep interactions reported by any selected source database (multiple selections = OR)."],
  ["Grouping", "Options are grouped by resource type; groups and the sources inside them are listed alphabetically. Sources no interaction uses are not listed."],
  GLOBAL_COUNTS,
  NOTHING_TICKED,
]);
const EXPERIMENT_HELP = DL([
  ["Experiment type", "Keep interactions detected by any selected experimental method (multiple selections = OR)."],
  ["Grouping", "Options are grouped by method family (affinity purification, mass spectrometry, two-hybrid, …); groups and the methods inside them are listed alphabetically. Methods no interaction uses are not listed."],
  GLOBAL_COUNTS,
  NOTHING_TICKED,
]);
const INTERACTION_TYPE_HELP = DL([
  ["Interaction type", "Keep interactions classified as any selected type (multiple selections = OR)."],
  ["Grouping", "Options are grouped by kind (association & complex membership, direct interaction, spatial proximity, enzymatic & post-translational reactions, cleavage); groups and the types inside them are listed alphabetically."],
  GLOBAL_COUNTS,
  NOTHING_TICKED,
]);
const TISSUE_HELP = DL([
  ["Tissue expression", "Keep proteins expressed in any selected tissue (multiple selections = OR)."],
  ["Data source", `Median gene-level expression from GTEx${gtexVersionSuffix()}. Only genes reaching a median of 1.0 RPKM in at least one tissue were imported, so a threshold below 1.0 has no additional effect.`],
  ["Grouping", "Tissues are grouped by body system, then by organ; groups and the tissues inside them are listed alphabetically."],
  TISSUE_COUNTS,
  ["Min. median RPKM ≥", "Minimum median expression (RPKM) required in the selected tissue(s). Appears once the tissue list is narrowed."],
  NOTHING_TICKED,
]);
const PROTEIN_FILTERS_HELP = DL([
  ["Min. degree in all of HIPPIE ≥", "Minimum number of interaction partners a protein has across the whole database. Counted over every HIPPIE interaction, ignoring the source, score and type filters — so a protein can pass this and still have very few partners left once those filters are applied."],
  ["Min. avg. score ≥", "Minimum mean confidence score across a protein's interactions, again computed over all of HIPPIE."],
]);
const REVIEWED_HELP = DL([
  ["Reviewed", "UniProt-reviewed (Swiss-Prot) proteins."],
  ["Unreviewed", "Unreviewed (TrEMBL) proteins."],
  ["Both", "No curation-status filter."],
]);
const ISOFORMS_HELP = DL([
  ["General", "Proteins without any isoform-level information."],
  ["Isoforms", "Only pairs where an endpoint is an isoform."],
  ["Both", "Showing everything connected to your keyword, whether it contains isoforms or not."],
]);

export function FilterBox({ value, onChange, meta = {}, controls = ALL_CONTROLS, layout = "collapsible" }) {
  const f = value;
  const on = new Set(controls);
  const set = (patch) => onChange({ ...f, ...patch });
  const { med, high } = confThresholds(f.showMode);
  const colCls = layout === "vertical" ? "col-12" : "col-md-6 col-lg-4";

  return (
    <div className={layout === "vertical" ? "" : "filter-panel mb-3"}>
      <div className="row g-3">
        {on.has("showMode") && (
          <div className={colCls}>
            <div className="filter-section-label">
              Result type
              <InfoPopover title="Result type" html={RESULT_TYPE_HELP} />
            </div>
            <div className="mode-toggle">
              {[
                ["interactions", "Interactions"],
                ["noninteractions", "Non-interactions"],
                ["both", "Both"],
              ].map(([k, label]) => (
                <button key={k} className={f.showMode === k ? "active" : ""} onClick={() => set({ showMode: k })}>
                  {label}
                </button>
              ))}
            </div>
            <div className="text-muted-sm mt-1">Non-interactions shown with a grey background.</div>
          </div>
        )}

        {on.has("score") && (
          <div className={colCls}>
            <div className="filter-section-label">
              Confidence score
              <InfoPopover title="Confidence score" html={CONFIDENCE_HELP} />
            </div>
            <label className="form-label">
              Min. score ≥ <span className="mono">{f.minScore.toFixed(2)}</span>
            </label>
            <input
              type="range"
              className="form-range"
              min="0"
              max="1"
              step="0.01"
              value={f.minScore}
              onChange={(e) => set({ minScore: Math.min(parseFloat(e.target.value), f.maxScore) })}
            />
            <label className="form-label">
              Max. score ≤ <span className="mono">{f.maxScore.toFixed(2)}</span>
            </label>
            <input
              type="range"
              className="form-range mb-2"
              min="0"
              max="1"
              step="0.01"
              value={f.maxScore}
              onChange={(e) => set({ maxScore: Math.max(parseFloat(e.target.value), f.minScore) })}
            />
            <div className="d-flex gap-2 flex-wrap">
              <button
                className="tag-chip example-chip"
                style={CONF_CHIP_STYLE.med}
                onClick={() => {
                  const v = parseFloat(med.toFixed(2));
                  set({ minScore: v, maxScore: f.maxScore < v ? 1 : f.maxScore });
                }}
              >
                Medium conf. ≥ {med.toFixed(2)}
              </button>
              <button
                className="tag-chip example-chip"
                style={CONF_CHIP_STYLE.high}
                onClick={() => {
                  const v = parseFloat(high.toFixed(2));
                  set({ minScore: v, maxScore: f.maxScore < v ? 1 : f.maxScore });
                }}
              >
                High conf. ≥ {high.toFixed(2)}
              </button>
            </div>
          </div>
        )}

        {on.has("source") && (
          <div className={colCls}>
            <div className="filter-section-label">
              Source database
              <InfoPopover title="Source database" html={SOURCE_HELP} />
            </div>
            <label className="form-label">In any selected source</label>
            <CheckboxList
              items={meta.sources || []}
              selected={f.source}
              onChange={(ids) => set({ source: ids })}
              noun="source databases"
            />
          </div>
        )}

        {on.has("experiment") && (
          <div className={colCls}>
            <div className="filter-section-label">
              Experiment type
              <InfoPopover title="Experiment type" html={EXPERIMENT_HELP} />
            </div>
            <label className="form-label">Detected by any selected method</label>
            <CheckboxList
              items={meta.experiments || []}
              selected={f.experiment}
              onChange={(ids) => set({ experiment: ids })}
              noun="experiment types"
            />
          </div>
        )}

        {on.has("interactionType") && (
          <div className={colCls}>
            <div className="filter-section-label">
              Interaction type
              <InfoPopover title="Interaction type" html={INTERACTION_TYPE_HELP} />
            </div>
            <label className="form-label">Classified as any selected type</label>
            <CheckboxList
              items={meta.interaction_types || []}
              selected={f.interactionType}
              onChange={(ids) => set({ interactionType: ids })}
              noun="interaction types"
            />
          </div>
        )}

        {on.has("tissue") && (
          <div className={colCls}>
            <div className="filter-section-label">
              Tissue expression (GTEx)
              <InfoPopover title="Tissue expression (GTEx)" html={TISSUE_HELP} />
            </div>
            <label className="form-label">Expressed in any selected tissue</label>
            <CheckboxList
              items={meta.tissues || []}
              selected={f.tissue}
              onChange={(ids) => set({ tissue: ids })}
              noun="tissues"
            />
            {listNarrows(f.tissue, meta.tissues) && (
              <>
                <label className="form-label mt-2">Min. median RPKM ≥</label>
                <input
                  type="number"
                  className="form-control"
                  min="0"
                  step="1"
                  placeholder="0"
                  value={f.minRpkm || ""}
                  onChange={(e) => set({ minRpkm: parseFloat(e.target.value) || 0 })}
                />
              </>
            )}
          </div>
        )}

        {on.has("protein") && (
          <div className={colCls}>
            <div className="filter-section-label">
              Protein filters
              <InfoPopover title="Protein filters" html={PROTEIN_FILTERS_HELP} />
            </div>
            <label className="form-label">
              Min. degree in all of HIPPIE ≥{" "}
              <span className="mono">{f.minDegree || 0}</span>
            </label>
            <input
              type="range"
              className="form-range mb-2"
              min="0"
              max="500"
              step="5"
              value={f.minDegree || 0}
              onChange={(e) => set({ minDegree: parseInt(e.target.value) })}
            />
            <label className="form-label">
              Min. avg. score ≥ <span className="mono">{(f.minAvgScore || 0).toFixed(2)}</span>
            </label>
            <input
              type="range"
              className="form-range"
              min="0"
              max="1"
              step="0.01"
              value={f.minAvgScore || 0}
              onChange={(e) => set({ minAvgScore: parseFloat(e.target.value) })}
            />
          </div>
        )}

        {on.has("reviewed") && (
          <div className={colCls}>
            <div className="filter-section-label">
              Protein review status
              <InfoPopover title="Protein review status" html={REVIEWED_HELP} />
            </div>
            <div className="mode-toggle">
              {[
                ["both", "Both"],
                ["reviewed", "Reviewed"],
                ["unreviewed", "Unreviewed"],
              ].map(([k, label]) => (
                <button key={k} className={f.reviewed === k ? "active" : ""} onClick={() => set({ reviewed: k })}>
                  {label}
                </button>
              ))}
            </div>
          </div>
        )}

        {on.has("isoforms") && (
          <div className={colCls}>
            <div className="filter-section-label">
              Isoforms
              <InfoPopover title="Isoforms" html={ISOFORMS_HELP} />
            </div>
            <div className="mode-toggle">
              {[
                ["general", "General"],
                ["isoforms", "Isoforms"],
                ["both", "Both"],
              ].map(([k, label]) => (
                <button key={k} className={f.isoformMode === k ? "active" : ""} onClick={() => set({ isoformMode: k })}>
                  {label}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── ML Splits filter panels ─────────────────────────────────────────────────
// The ML Splits page groups the same controls differently (protein-level vs
// interaction-level, two side-by-side cards next to their statistics boxes) and
// omits the ones that make no sense for a split (result type, review status).
// The panels live here rather than in ml_splits.jsx so every filter affordance —
// grouping, search, counts, All/None, the empty-state hint — is written once.
//
// Their filter objects are flat and page-local (see PROTEIN_INIT /
// INTERACTION_INIT in ml_splits.jsx); notably the interaction-type key is `type`
// here and `interactionType` in FILTER_DEFAULTS.

const ML_PROTEIN_HELP = DL([
  ["Expressed in any selected tissue", `Keep only proteins expressed in any of the selected tissues. Median gene-level expression from GTEx${gtexVersionSuffix()}.`],
  ["Min. median RPKM ≥", "Minimum median expression (RPKM) required in the selected tissue(s). Appears once the tissue list is narrowed. Only genes reaching 1.0 RPKM in at least one tissue were imported, so a lower threshold has no additional effect."],
  ["Min. degree in all of HIPPIE ≥", "Minimum number of interaction partners a protein has across the whole database. Counted over every HIPPIE interaction, ignoring the interaction filters on the right — so the median degree reported in the statistics box, which counts only surviving edges, is often much lower than this threshold."],
  ["Min. avg score ≥", "Minimum mean confidence score across a protein's interactions, again over all of HIPPIE."],
  ["Isoforms", "General = canonical entries (plus any isoform you queried); Isoforms = only isoform entries; Both = no isoform filter."],
  TISSUE_COUNTS,
  NOTHING_TICKED,
]);

const ML_INTERACTION_HELP = DL([
  ["Min. score ≥", "Keep interactions with confidence ≥ this value (0–1)."],
  ["Max. score ≤", "Keep interactions with confidence ≤ this value. Sliders clamp so min ≤ max."],
  ["Medium / High conf.", "One-click presets snapping Min. score to the release's median (medium) or Q3 (high) confidence threshold."],
  ["Experiment type", "Keep interactions detected by any selected experimental method."],
  ["Interaction type", "Keep interactions classified as any selected interaction type."],
  ["Source database", "Keep interactions reported by any selected source database."],
  ["Grouping", "Each list is grouped by kind, with groups and the options inside them listed alphabetically. Options no interaction uses are not listed."],
  GLOBAL_COUNTS,
  NOTHING_TICKED,
]);

export function MLProteinFilterPanel({ meta, filters, onChange }) {
  const set = (patch) => onChange({ ...filters, ...patch });
  return (
    <div className="hippie-card mb-0" style={{ height: "100%" }}>
      <div className="filter-section-label">
        Protein Filters
        <InfoPopover title="Protein Filters" html={ML_PROTEIN_HELP} />
      </div>
      <label className="form-label">
        Expressed in any selected tissue <span className="text-muted-sm">(GTEx)</span>
      </label>
      <CheckboxList
        items={meta.tissues || []}
        selected={filters.tissue}
        onChange={(ids) => set({ tissue: ids })}
        noun="tissues"
      />
      {listNarrows(filters.tissue, meta.tissues) && (
        <>
          <label className="form-label mt-2">Min. median RPKM ≥</label>
          <input
            type="number"
            className="form-control"
            min="0"
            step="1"
            placeholder="0"
            value={filters.minRpkm || ""}
            onChange={(e) => set({ minRpkm: parseFloat(e.target.value) || 0 })}
          />
        </>
      )}
      <label className="form-label mt-3">
        Min. degree in all of HIPPIE ≥{" "}
        <span className="mono">{filters.minDegree || 0}</span>
      </label>
      <input
        type="range"
        className="form-range mb-2"
        min="0"
        max="500"
        step="5"
        value={filters.minDegree || 0}
        onChange={(e) => set({ minDegree: parseInt(e.target.value) })}
      />
      <label className="form-label">
        Min. avg score ≥{" "}
        <span className="mono">{(filters.minAvgScore || 0).toFixed(2)}</span>
      </label>
      <input
        type="range"
        className="form-range mb-3"
        min="0"
        max="1"
        step="0.01"
        value={filters.minAvgScore || 0}
        onChange={(e) => set({ minAvgScore: parseFloat(e.target.value) })}
      />
      <div className="filter-section-label mt-3">Isoforms</div>
      <div className="mode-toggle">
        {[
          ["general", "General"],
          ["isoforms", "Isoforms"],
          ["both", "Both"],
        ].map(([k, label]) => (
          <button
            key={k}
            className={filters.isoformMode === k ? "active" : ""}
            onClick={() => set({ isoformMode: k })}
          >
            {label}
          </button>
        ))}
      </div>
    </div>
  );
}

export function MLInteractionFilterPanel({ meta, filters, onChange }) {
  const set = (patch) => onChange({ ...filters, ...patch });
  const { med, high } = confThresholds("interactions");
  const preset = (v) =>
    set({ minScore: v, maxScore: (filters.maxScore ?? 1) < v ? 1 : filters.maxScore });
  return (
    <div className="hippie-card mb-0" style={{ height: "100%" }}>
      <div className="filter-section-label">
        Interaction Filters
        <InfoPopover title="Interaction Filters" html={ML_INTERACTION_HELP} />
      </div>
      <label className="form-label">
        Min. score ≥ <span className="mono">{(filters.minScore || 0).toFixed(2)}</span>
      </label>
      <input
        type="range"
        className="form-range mb-2"
        min="0"
        max="1"
        step="0.01"
        value={filters.minScore || 0}
        onChange={(e) =>
          set({ minScore: Math.min(parseFloat(e.target.value), filters.maxScore ?? 1) })
        }
      />
      <label className="form-label">
        Max. score ≤ <span className="mono">{(filters.maxScore ?? 1).toFixed(2)}</span>
      </label>
      <input
        type="range"
        className="form-range mb-2"
        min="0"
        max="1"
        step="0.01"
        value={filters.maxScore ?? 1}
        onChange={(e) =>
          set({ maxScore: Math.max(parseFloat(e.target.value), filters.minScore ?? 0) })
        }
      />
      <div className="d-flex gap-2 flex-wrap mb-3">
        <button
          className="tag-chip example-chip"
          style={CONF_CHIP_STYLE.med}
          onClick={() => preset(parseFloat(med.toFixed(2)))}
        >
          Medium conf. ≥ {med.toFixed(2)}
        </button>
        <button
          className="tag-chip example-chip"
          style={CONF_CHIP_STYLE.high}
          onClick={() => preset(parseFloat(high.toFixed(2)))}
        >
          High conf. ≥ {high.toFixed(2)}
        </button>
      </div>
      {/* Stacked, not three-across: this panel is already only half the page
          wide, and option names ("anti tag coimmunoprecipitation") do not fit a
          30%-wide column. Shorter scroll boxes keep all three on screen. */}
      <div className="row g-3">
        <div className="col-12">
          <label className="form-label">Experiment type</label>
          <CheckboxList
            items={meta.experiments || []}
            selected={filters.experiment}
            onChange={(ids) => set({ experiment: ids })}
            noun="experiment types"
            maxHeight="150px"
          />
        </div>
        <div className="col-12">
          <label className="form-label">Interaction type</label>
          <CheckboxList
            items={meta.interaction_types || []}
            selected={filters.type}
            onChange={(ids) => set({ type: ids })}
            noun="interaction types"
            maxHeight="150px"
          />
        </div>
        <div className="col-12">
          <label className="form-label">Source database</label>
          <CheckboxList
            items={meta.sources || []}
            selected={filters.source}
            onChange={(ids) => set({ source: ids })}
            noun="source databases"
            maxHeight="150px"
          />
        </div>
      </div>
    </div>
  );
}
