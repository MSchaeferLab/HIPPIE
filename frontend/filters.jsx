// Shared, controlled FilterBox used by every query page (Batch 3+).
//
// The box is fully controlled: the *parent* owns the draft filter value and the
// draft-vs-applied semantics (nothing searches until the page commits the
// draft). FilterBox only renders controls and calls `onChange` with the next
// value. One component → one place to change filters everywhere.

import React, { useEffect, useState } from "react";
import { InfoPopover, DL } from "./shared.jsx";

// ── Unified filter state ────────────────────────────────────────────────────
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
export function CheckboxList({ items, selected, onToggle }) {
  const selSet = new Set(selected.map(String));
  return (
    <div
      style={{
        maxHeight: "160px",
        overflowY: "auto",
        border: "1px solid var(--hippie-border)",
        borderRadius: "var(--radius-md)",
        padding: ".4rem .6rem",
      }}
    >
      {items.length === 0 && <span className="text-muted-sm">None available</span>}
      {items.map((it) => (
        <label
          key={it.id}
          style={{ display: "flex", alignItems: "center", gap: ".4rem", cursor: "pointer", padding: ".15rem 0" }}
        >
          <input
            type="checkbox"
            checked={selSet.has(String(it.id))}
            onChange={() => onToggle(it.id)}
            style={{ cursor: "pointer" }}
          />
          <span className="text-muted-sm" style={{ color: "var(--hippie-ink)" }}>
            {it.name}
          </span>
        </label>
      ))}
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
export function countActiveFilters(f, controls = ALL_CONTROLS) {
  const on = new Set(controls);
  let n = 0;
  if (on.has("showMode") && f.showMode !== "interactions") n++;
  if (on.has("isoforms") && f.isoformMode !== "general") n++;
  if (on.has("score") && (f.minScore > 0 || f.maxScore < 1)) n++;
  if (on.has("source")) n += f.source.length;
  if (on.has("experiment")) n += f.experiment.length;
  if (on.has("interactionType")) n += f.interactionType.length;
  if (on.has("tissue")) n += f.tissue.length;
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
function _serialize(f) {
  const scalars = { show: f.showMode };
  const lists = {};
  if (f.isoformMode !== "general") scalars.isoform_mode = f.isoformMode;
  if (f.minScore > 0) scalars.min_score = f.minScore;
  if (f.maxScore < 1) scalars.max_score = f.maxScore;
  if (f.source.length) lists.source = f.source;
  if (f.experiment.length) lists.experiment = f.experiment;
  if (f.interactionType.length) lists.interaction_type = f.interactionType;
  if (f.tissue.length) {
    lists.tissue = f.tissue;
    if (f.minRpkm > 0) scalars.min_rpkm = f.minRpkm;
  }
  if (f.minDegree > 0) scalars.min_degree = f.minDegree;
  if (f.minAvgScore > 0) scalars.min_avg_score = f.minAvgScore;
  if (f.reviewed !== "both") scalars.reviewed = f.reviewed;
  return { scalars, lists };
}

// GET query string (Protein Query, Browse).
export function filtersToQuery(f) {
  const { scalars, lists } = _serialize(f);
  const p = new URLSearchParams();
  Object.entries(scalars).forEach(([k, v]) => p.set(k, v));
  Object.entries(lists).forEach(([k, arr]) => arr.forEach((v) => p.append(k, v)));
  return p;
}

// POST JSON body fields (Interaction Query, Network Query).
export function filtersToBody(f) {
  const { scalars, lists } = _serialize(f);
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
const SOURCE_HELP = DL([
  ["Source database", "Keep interactions reported by any selected source database (multiple selections = OR)."],
]);
const EXPERIMENT_HELP = DL([
  ["Experiment type", "Keep interactions detected by any selected experimental method."],
]);
const INTERACTION_TYPE_HELP = DL([
  ["Interaction type", "Keep interactions classified as any selected type."],
]);
const TISSUE_HELP = DL([
  ["Tissue expression", "Keep proteins expressed in any selected tissue."],
  ["Min. median RPKM ≥", "Minimum median expression (RPKM) required in the selected tissue(s). Appears once a tissue is selected."],
]);
const PROTEIN_FILTERS_HELP = DL([
  ["Min. degree ≥", "Minimum number of interaction partners (node degree) a protein must have."],
  ["Min. avg. score ≥", "Minimum mean confidence score across a protein's interactions."],
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
              onToggle={(id) => set({ source: toggleIn(f.source, id) })}
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
              onToggle={(id) => set({ experiment: toggleIn(f.experiment, id) })}
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
              onToggle={(id) => set({ interactionType: toggleIn(f.interactionType, id) })}
            />
          </div>
        )}

        {on.has("tissue") && (
          <div className={colCls}>
            <div className="filter-section-label">
              Tissue expression
              <InfoPopover title="Tissue expression" html={TISSUE_HELP} />
            </div>
            <label className="form-label">Expressed in any selected tissue</label>
            <CheckboxList
              items={meta.tissues || []}
              selected={f.tissue}
              onToggle={(id) => set({ tissue: toggleIn(f.tissue, id) })}
            />
            {f.tissue.length > 0 && (
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
              Min. degree ≥ <span className="mono">{f.minDegree || 0}</span>
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
