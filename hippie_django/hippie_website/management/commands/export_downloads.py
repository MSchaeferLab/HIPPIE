"""Generate the public HIPPIE download files.

Writes three gzip-compressed files into the directory given as the ``path``
argument:

* ``HIPPIE-current.mitab.txt.gz`` — PSI-MI TAB 2.5 (15 mandatory columns + 3
  HIPPIE extension columns), one binary interaction per row.
* ``HIPPIE-current.txt.gz``      — compact tab format keyed on uniprot_accession,
  uniprot_name retained for backwards compatibility.
* ``HIPPIE-current.stats.txt.gz`` — score quartiles and counts over a
  3x3 matrix: {interactions, non-interactions, both} x {all, isoforms-only,
  non-isoforms-only}.

The two download files cover Interactions only. NonInteractions carry no
evidence (sources / pmids / experiments), so they appear in the stats file only.
"""

import gzip
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from django.core.management.base import BaseCommand
from django.db.models import Count, QuerySet
from django.utils import timezone

from hippie_website.models import (
    ExperimentType,
    Interaction,
    InteractionType,
    Isoform,
    NonInteraction,
    Protein,
)
from hippie_website.stats_utils import compute_quartiles

MITAB_FILENAME = "HIPPIE-current.mitab.txt.gz"
TXT_FILENAME = "HIPPIE-current.txt.gz"
STATS_FILENAME = "HIPPIE-current.stats.txt.gz"

NULL = "-"
CHUNK = 10_000

MITAB_HEADER: list[str] = [
    "ID Interactor A",
    "ID Interactor B",
    "Alt IDs Interactor A",
    "Alt IDs Interactor B",
    "Aliases Interactor A",
    "Aliases Interactor B",
    "Interaction Detection Methods",
    "Publication 1st Author",
    "Publication Identifiers",
    "Taxid Interactor A",
    "Taxid Interactor B",
    "Interaction Types",
    "Source Databases",
    "Interaction Identifiers",
    "Confidence Value",
    "Presence In Other Species",
    "Gene Name Interactor A",
    "Gene Name Interactor B",
]

TXT_HEADER: list[str] = [
    "uniprot_accession_A",
    "uniprot_name_A",
    "entrez_A",
    "uniprot_accession_B",
    "uniprot_name_B",
    "entrez_B",
    "score",
    "info",
]


# ---------------------------------------------------------------------------
# PSI-MI TAB field helpers
# ---------------------------------------------------------------------------

_RESERVED = set("|():\t")


def _quote(part: str) -> str:
    """Wrap a field part in double quotes if it contains a reserved char."""
    if '"' in part:
        return '"' + part.replace('"', '\\"') + '"'
    if any(ch in part for ch in _RESERVED):
        return '"' + part + '"'
    return part


def _field(xref: str, value: str, desc: str = "") -> str:
    """Render ``<xref>:<value>(<desc>)`` with per-part reserved-char quoting."""
    out = f"{_quote(xref)}:{_quote(value)}"
    if desc:
        out += f"({_quote(desc)})"
    return out


TAXID_HUMAN = _field("taxid", "9606", "Homo sapiens")


def _cv_list(items: Iterable[ExperimentType | InteractionType]) -> str:
    """PSI-MI controlled-vocabulary list: ``psi-mi:"MI:xxxx"(name)`` per item.

    Items without a PSI-MI code fall back to ``psi-mi:<name>``.
    """
    parts: list[str] = []
    for it in items:
        if it.psi_mi_code:
            parts.append(_field("psi-mi", it.psi_mi_code, it.name))
        else:
            parts.append(_field("psi-mi", it.name))
    return "|".join(parts) if parts else NULL


def _protein_aliases(p: Protein) -> str:
    parts: list[str] = []
    if p.uniprot_name:
        parts.append(_field("uniprotkb", p.uniprot_name, "display_short"))
    if p.gene.entrez_name:
        parts.append(_field("entrezgene/locuslink", p.gene.entrez_name, "gene name"))
    for syn in p.synonyms.all():
        if syn.synonym:
            parts.append(_field("uniprotkb", syn.synonym))
    for syn in p.gene.synonyms.all():
        if syn.synonym:
            parts.append(_field("entrezgene/locuslink", syn.synonym, "gene synonym"))
    return "|".join(parts) if parts else NULL


def _mitab_row(inter: Interaction) -> list[str]:
    a = inter.protein_1
    b = inter.protein_2

    pubs = (
        "|".join(_field("pubmed", str(pub.pmid)) for pub in inter.publications.all())
        or NULL
    )
    sources = "|".join(_quote(s.name) for s in inter.sources.all()) or NULL
    xrefs = (
        "|".join(_field(x.source.name, x.link) for x in inter.cross_references.all())
        or NULL
    )
    species = (
        "|".join(
            _field("taxid", str(sp.NCBI_tax_id), sp.name)
            for sp in inter.conserved_species.all()
        )
        or NULL
    )

    return [
        _field("entrez gene", str(a.gene.entrez_id)),
        _field("entrez gene", str(b.gene.entrez_id)),
        _field("uniprotkb", a.uniprot_accession),
        _field("uniprotkb", b.uniprot_accession),
        _protein_aliases(a),
        _protein_aliases(b),
        _cv_list(inter.experiments.all()),
        NULL,  # Publication 1st Author — not stored
        pubs,
        TAXID_HUMAN,
        TAXID_HUMAN,
        _cv_list(inter.interaction_types.all()),
        sources,
        xrefs,
        _field("hippie", f"{inter.score:.2f}"),
        species,
        a.gene.entrez_name or NULL,
        b.gene.entrez_name or NULL,
    ]


def _txt_row(inter: Interaction) -> list[str]:
    a = inter.protein_1
    b = inter.protein_2
    info = (
        "experiments:"
        + ",".join(e.name for e in inter.experiments.all())
        + ";pmids:"
        + ",".join(str(pub.pmid) for pub in inter.publications.all())
        + ";sources:"
        + ",".join(s.name for s in inter.sources.all())
    )
    return [
        a.uniprot_accession,
        a.uniprot_name,
        str(a.gene.entrez_id),
        b.uniprot_accession,
        b.uniprot_name,
        str(b.gene.entrez_id),
        f"{inter.score:.2f}",
        info,
    ]


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

SPLITS = ("all", "isoforms-only", "non-isoforms-only")


@dataclass
class Bucket:
    """Accumulated values for one (dataset, split) cell."""

    scores: list[float] = field(default_factory=list)
    proteins: set[int] = field(default_factory=set)
    genes: set[int] = field(default_factory=set)

    def merge(self, other: "Bucket") -> "Bucket":
        out = Bucket()
        out.scores = self.scores + other.scores
        out.proteins = self.proteins | other.proteins
        out.genes = self.genes | other.genes
        return out


def _add(bucket: Bucket, p1: int, p2: int, score: float, p2g: dict[int, int]) -> None:
    bucket.scores.append(score)
    bucket.proteins.add(p1)
    bucket.proteins.add(p2)
    g1 = p2g.get(p1)
    g2 = p2g.get(p2)
    if g1 is not None:
        bucket.genes.add(g1)
    if g2 is not None:
        bucket.genes.add(g2)


def _fmt_quartiles(scores: list[float]) -> str:
    if not scores:
        return "score: (no rows)"
    q = compute_quartiles(scores)
    return (
        f"score: min={q['min']:.4f} Q1={q['q1']:.4f} median={q['median']:.4f} "
        f"Q3={q['q3']:.4f} max={q['max']:.4f} mean={q['mean']:.4f} std={q['std']:.4f}"
    )


def _evidence_line(flt: dict[str, bool]) -> list[str]:
    """Distinct + average evidence counts for an Interaction split."""
    qs = Interaction.objects.filter(**flt)
    rows = qs.count()
    if rows == 0:
        return ["evidence: (no rows)"]
    # Separate aggregates: combining multiple M2M Counts in one query fans out.
    n_pubs = qs.aggregate(n=Count("publications", distinct=True))["n"]
    n_srcs = qs.aggregate(n=Count("sources", distinct=True))["n"]
    n_exps = qs.aggregate(n=Count("experiments", distinct=True))["n"]
    t_pubs = qs.aggregate(n=Count("publications"))["n"]
    t_srcs = qs.aggregate(n=Count("sources"))["n"]
    t_exps = qs.aggregate(n=Count("experiments"))["n"]
    return [
        f"evidence: distinct publications={n_pubs} sources={n_srcs} "
        f"experiment types={n_exps}",
        f"          avg per interaction: pubs={t_pubs / rows:.2f} "
        f"sources={t_srcs / rows:.2f} experiments={t_exps / rows:.2f}",
    ]


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


class Command(BaseCommand):
    help = "Generate the HIPPIE download files (.mitab.txt, .txt) and a stats file."

    def add_arguments(self, parser: object) -> None:
        parser.add_argument(  # type: ignore[attr-defined]
            "path",
            help="Output directory for the generated files (created if missing).",
        )

    def handle(self, *_args: object, **options: object) -> None:
        out_dir = Path(str(options["path"]))
        out_dir.mkdir(parents=True, exist_ok=True)

        self._write_downloads(out_dir)
        self._write_stats(out_dir)
        self.stdout.write(self.style.SUCCESS(f"Wrote download files to {out_dir}"))

    # -- download files (single streaming pass over Interactions) ----------
    def _write_downloads(self, out_dir: Path) -> None:
        qs: QuerySet[Interaction] = (
            Interaction.objects.select_related("protein_1__gene", "protein_2__gene")
            .prefetch_related(
                "protein_1__synonyms",
                "protein_2__synonyms",
                "protein_1__gene__synonyms",
                "protein_2__gene__synonyms",
                "experiments",
                "publications",
                "interaction_types",
                "sources",
                "conserved_species",
                "cross_references__source",
            )
            .order_by("pk")
        )

        mitab_path = out_dir / MITAB_FILENAME
        txt_path = out_dir / TXT_FILENAME
        written = 0
        with (
            gzip.open(mitab_path, "wt", encoding="utf-8") as mitab,
            gzip.open(txt_path, "wt", encoding="utf-8") as txt,
        ):
            mitab.write("\t".join(MITAB_HEADER) + "\n")
            txt.write("\t".join(TXT_HEADER) + "\n")
            for inter in qs.iterator(chunk_size=CHUNK):
                mitab.write("\t".join(_mitab_row(inter)) + "\n")
                txt.write("\t".join(_txt_row(inter)) + "\n")
                written += 1
                if written % 100_000 == 0:
                    self.stdout.write(f"  ...{written:,} interactions written")
        self.stdout.write(
            f"  {written:,} interactions -> {MITAB_FILENAME}, {TXT_FILENAME}"
        )

    # -- statistics file ---------------------------------------------------
    def _write_stats(self, out_dir: Path) -> None:
        p2g: dict[int, int] = dict(Protein.objects.values_list("pk", "gene_id"))
        iso_pks: set[int] = set(Isoform.objects.values_list("pk", flat=True))

        inter = {s: Bucket() for s in SPLITS}
        for p1, p2, score, iso in Interaction.objects.values_list(
            "protein_1_id", "protein_2_id", "score", "involves_isoform"
        ).iterator(chunk_size=CHUNK):
            _add(inter["all"], p1, p2, score, p2g)
            _add(
                inter["isoforms-only" if iso else "non-isoforms-only"],
                p1,
                p2,
                score,
                p2g,
            )

        noninter = {s: Bucket() for s in SPLITS}
        for p1, p2, score in NonInteraction.objects.values_list(
            "protein_1_id", "protein_2_id", "score"
        ).iterator(chunk_size=CHUNK):
            iso = p1 in iso_pks or p2 in iso_pks
            _add(noninter["all"], p1, p2, score, p2g)
            _add(
                noninter["isoforms-only" if iso else "non-isoforms-only"],
                p1,
                p2,
                score,
                p2g,
            )

        both = {s: inter[s].merge(noninter[s]) for s in SPLITS}

        inter_filters: dict[str, dict[str, bool]] = {
            "all": {},
            "isoforms-only": {"involves_isoform": True},
            "non-isoforms-only": {"involves_isoform": False},
        }

        path = out_dir / STATS_FILENAME
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write("HIPPIE downloadable data - summary statistics\n")
            f.write(f"Generated: {timezone.now():%Y-%m-%d %H:%M %Z}\n")

            self._stats_section(f, "interactions", inter, inter_filters)
            self._stats_section(f, "non-interactions", noninter, None)
            self._stats_section(f, "both", both, inter_filters)
        self.stdout.write(f"  stats -> {STATS_FILENAME}")

    def _stats_section(
        self,
        f: TextIO,
        title: str,
        buckets: dict[str, Bucket],
        evidence_filters: dict[str, dict[str, bool]] | None,
    ) -> None:
        f.write("\n" + "=" * 64 + "\n")
        f.write(f"DATASET: {title}\n")
        for split in SPLITS:
            b = buckets[split]
            f.write("-" * 64 + "\n")
            f.write(f"[{split}]\n")
            f.write(f"  rows: {len(b.scores)}\n")
            f.write(f"  distinct proteins: {len(b.proteins)}\n")
            f.write(f"  distinct genes: {len(b.genes)}\n")
            f.write(f"  {_fmt_quartiles(b.scores)}\n")
            if evidence_filters is not None:
                for line in _evidence_line(evidence_filters[split]):
                    f.write(f"  {line}\n")
            else:
                f.write("  evidence: N/A (non-interactions carry no evidence)\n")
