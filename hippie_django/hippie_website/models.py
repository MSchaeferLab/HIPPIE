import uuid

from django.core.exceptions import ValidationError
from django.db import models
from .managers import InteractionManager, ProteinManager

# =============================================================================
# Protein entities
# =============================================================================


class Gene(models.Model):
    """
    To map proteins that link to the same gene, we added a gene class that is defined by the entrez id.
    """

    entrez_id = models.PositiveIntegerField(db_index=True, unique=True)
    entrez_name = models.CharField(max_length=40, blank=True, default="", db_index=True)

    class Meta:
        db_table = "gene"

    def __str__(self):
        if self.entrez_name:
            return f"{self.entrez_name} ({self.entrez_id})"
        return str(self.entrez_id)


class GeneSynonym(models.Model):
    """
    Alternative names / symbols for a Gene.
    Examples: 'TP53', 'P53', 'BCC7'
    """

    gene = models.ForeignKey(
        Gene,
        on_delete=models.CASCADE,
        related_name="synonyms",
    )
    synonym = models.CharField(
        max_length=60,
        db_index=True,
        help_text="Alternative name or symbol for the gene",
    )
    additional_information = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        db_table = "gene_synonym"
        constraints = [
            models.UniqueConstraint(
                fields=["gene", "synonym"],
                name="unique_gene_synonym",
            )
        ]

    def __str__(self):
        return f"{self.synonym} -> {self.gene}"


class Protein(models.Model):
    """
    Central entity — one row per unique protein in HIPPIE.
    """

    gene = models.ForeignKey(Gene, on_delete=models.CASCADE, related_name="proteins")
    uniprot_accession = models.CharField(max_length=20, db_index=True, unique=True)
    uniprot_name = models.CharField(
        max_length=16, db_index=True, default="", blank=True
    )
    # True for reviewed Swiss-Prot entries, False for TrEMBL. Defaults True;
    # populated by the update routine (not yet wired), so TrEMBL is empty for now.
    is_swissprot = models.BooleanField(default=True, db_index=True)

    # Denormalised browse stats — refreshed by `recompute_protein_stats`.
    # degree = number of interactions touching this protein (either side);
    # avg_score = mean confidence score across those interactions.
    degree = models.PositiveIntegerField(default=0, db_index=True)
    avg_score = models.FloatField(null=True, blank=True, db_index=True)

    objects = ProteinManager()

    class Meta:
        db_table = "protein"

    def __str__(self):
        return self.uniprot_accession


class ProteinSynonym(models.Model):
    """
    Alternative names / identifiers for a Protein.
    Examples: alternative gene symbols, legacy protein names, etc.
    """

    protein = models.ForeignKey(
        Protein,
        on_delete=models.CASCADE,
        related_name="synonyms",
    )
    synonym = models.CharField(
        max_length=60,
        db_index=True,
        help_text="Alternative name or identifier for the protein",
    )
    additional_information = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        db_table = "protein_synonym"
        constraints = [
            models.UniqueConstraint(
                fields=["protein", "synonym"],
                name="unique_protein_synonym",
            )
        ]

    def __str__(self):
        return f"{self.synonym} -> {self.protein}"


class Isoform(Protein):
    """
    Protein isoform — MTI subclass of Protein.
    Every Isoform IS a Protein row (shares the same pk).
    The isoform-specific UniProt accession (e.g. "P38398-2") is stored on
    this model's inherited `uniprot_accession` field.
    `general_protein` points to the canonical parent protein entry.
    """

    general_protein = models.ForeignKey(
        Protein,
        on_delete=models.CASCADE,
        related_name="isoforms",
        help_text="Canonical parent protein for this isoform",
    )

    class Meta:
        db_table = "isoform"

    def __str__(self):
        return self.uniprot_accession


# =============================================================================
# Tissue expression
# =============================================================================


class Tissue(models.Model):
    """
    GTEx tissue types.
    IDs are preserved from the original schema for compatibility.
    """

    name = models.CharField(max_length=100, unique=True)

    class Meta:
        db_table = "tissue"

    def __str__(self):
        return self.name


class GeneTissue(models.Model):
    """
    Quantitative gene-tissue expression record from GTEx.

    Stores the median RPKM measured for one gene in one tissue.
    """

    gene = models.ForeignKey(
        Gene, on_delete=models.CASCADE, related_name="tissue_expression"
    )
    tissue = models.ForeignKey(
        Tissue, on_delete=models.CASCADE, related_name="expressed_genes"
    )
    median_rpkm = models.FloatField(verbose_name="rpkm")

    class Meta:
        db_table = "gene2tissue"
        constraints = [
            models.UniqueConstraint(
                fields=["gene", "tissue"],
                name="gene_tissue_unique",
            ),
        ]

    def __str__(self):
        return f"{self.gene.entrez_name} expressed in {self.tissue.name}"


# =============================================================================
# Reference / lookup tables
# =============================================================================


class Publication(models.Model):
    """
    Publications that are connected to any of the other classes..
    """

    pmid = models.PositiveBigIntegerField(unique=True, db_index=True)

    def __str__(self):
        return str(self.pmid)


class Source(models.Model):
    """
    Source databases providing interaction evidence.
    """

    name = models.CharField(max_length=100, unique=True)
    url = models.URLField(blank=True, default="")
    n_connected_interactions = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "source"

    def __str__(self):
        return self.name


class ExperimentType(models.Model):
    """
    Experimental techniques for detecting protein interactions
    with PSI-MI ontology codes and quality scores used
    in HIPPIE's confidence scoring formula.
    """

    name = models.CharField(max_length=100, unique=True)
    psi_mi_code = models.CharField(max_length=30, blank=True, default="")
    quality_score = models.FloatField(
        help_text="Weight in HIPPIE confidence scoring", null=True, blank=True
    )

    class Meta:
        db_table = "experiment_type"

    def clean(self) -> None:
        if (
            self.psi_mi_code
            and ExperimentType.objects.exclude(pk=self.pk)
            .filter(psi_mi_code=self.psi_mi_code)
            .exists()
        ):
            raise ValidationError(
                {"psi_mi_code": "PSI-MI code must be unique among non-empty values."}
            )

    def __str__(self):
        return self.name


class InteractionType(models.Model):
    """
    PSI-MI interaction type classification.
    """

    name = models.CharField(max_length=100, unique=True)
    psi_mi_code = models.CharField(max_length=30, blank=True, default="")

    class Meta:
        db_table = "interaction_type"

    def clean(self) -> None:
        if (
            self.psi_mi_code
            and InteractionType.objects.exclude(pk=self.pk)
            .filter(psi_mi_code=self.psi_mi_code)
            .exists()
        ):
            raise ValidationError(
                {"psi_mi_code": "PSI-MI code must be unique among non-empty values."}
            )

    def __str__(self):
        return self.name


class Species(models.Model):
    """
    NCBI taxonomy species.
    """

    name = models.CharField(max_length=255, unique=True)
    NCBI_tax_id = models.PositiveIntegerField(unique=True)

    class Meta:
        db_table = "species"
        verbose_name_plural = "species"

    def __str__(self):
        return self.name


# =============================================================================
# Core interaction
# =============================================================================


class Interaction(models.Model):
    """
    Central interaction table — the heart of HIPPIE.

    Every row always has `protein_1` and `protein_2` set.
    """

    # -- Required: protein-level interactors
    protein_1 = models.ForeignKey(
        Protein,
        on_delete=models.CASCADE,
        related_name="interactions_as_1",
    )
    protein_2 = models.ForeignKey(
        Protein,
        on_delete=models.CASCADE,
        related_name="interactions_as_2",
    )

    # -- Confidence score (0.0–1.0)
    score = models.FloatField(db_index=True)

    # Denormalised browse flag — True when either interactor is an isoform.
    # Lets the default browse view (canonical-only) filter on one indexed
    # column instead of two `protein_*__isoform__isnull` anti-joins over the
    # full table. Refreshed by `recompute_interaction_flags`.
    involves_isoform = models.BooleanField(default=False, db_index=True)

    # Denormalised evidence counts — refreshed by `recompute_interaction_flags`
    # and kept in sync on ad-hoc edits by `signals.py`. Let the browse
    # interaction table sort by evidence volume with an indexed scalar column
    # instead of a GROUP BY/Count over the M2M through tables (the browse perf
    # guidance forbids annotating counts over the 1.15M-row table). Composite
    # `(n_*, id)` indexes below speed up the per-table scan/sort on this
    # column; they're plain ascending, so a descending sort (the browse
    # default) or the cross-table union merge still needs a sort step — they
    # do not guarantee an index-only ordered scan across the union.
    n_sources = models.PositiveIntegerField(default=0)
    n_experiments = models.PositiveIntegerField(default=0)

    sources = models.ManyToManyField(
        Source, related_name="interactions", db_table="interaction2source"
    )
    publications = models.ManyToManyField(
        Publication, related_name="interactions", db_table="interaction2pubmed"
    )
    experiments = models.ManyToManyField(
        ExperimentType, related_name="interactions", db_table="interaction2experiment"
    )
    conserved_species = models.ManyToManyField(
        Species, related_name="conserved_interactions", db_table="interaction2species"
    )
    interaction_types = models.ManyToManyField(
        InteractionType, related_name="interactions", db_table="interaction2type"
    )

    objects = InteractionManager()

    class Meta:
        db_table = "interaction"
        indexes = [
            models.Index(fields=["protein_1", "score"]),
            models.Index(fields=["protein_2", "score"]),
            models.Index(fields=["score", "id"]),
            models.Index(fields=["n_sources", "id"]),
            models.Index(fields=["n_experiments", "id"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(protein_1_id__lte=models.F("protein_2_id")),
                name="interaction_canonical_order",
            ),
            models.CheckConstraint(
                condition=models.Q(score__gte=0.0) & models.Q(score__lte=1.0),
                name="interaction_score_range",
            ),
            models.UniqueConstraint(
                fields=["protein_1", "protein_2"],
                name="interaction_unique_pair",
            ),
        ]

    def __str__(self):
        return f"{self.protein_1.uniprot_accession}–{self.protein_2.uniprot_accession} ({self.score})"


class NonInteraction(models.Model):
    """
    Central noninteraction table — the heart of HIPPIE.

    Every row always has `protein_1` and `protein_2` set.
    """

    # -- Required: protein-level interactors
    protein_1 = models.ForeignKey(
        Protein,
        on_delete=models.CASCADE,
        related_name="noninteractions_as_1",
    )
    protein_2 = models.ForeignKey(
        Protein,
        on_delete=models.CASCADE,
        related_name="noninteractions_as_2",
    )

    # -- Confidence score (0.0–1.0)
    score = models.FloatField(db_index=True)

    class Meta:
        db_table = "noninteraction"
        indexes = [
            models.Index(fields=["protein_1", "score"]),
            models.Index(fields=["protein_2", "score"]),
            models.Index(fields=["score", "id"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(protein_1_id__lte=models.F("protein_2_id")),
                name="noninteraction_canonical_order",
            ),
            models.CheckConstraint(
                condition=models.Q(score__gte=0.0) & models.Q(score__lte=1.0),
                name="noninteraction_score_range",
            ),
            models.UniqueConstraint(
                fields=["protein_1", "protein_2"],
                name="noninteraction_unique_pair",
            ),
        ]

    def __str__(self):
        return (
            f"NonInteraction {self.protein_1.gene}–{self.protein_2.gene} ({self.score})"
        )


# =============================================================================
# Cross-references
# =============================================================================


class InteractionCrossReference(models.Model):
    """
    External database cross-references for interactions.
    Consolidates the old `interaction2link` and `interaction2homomint_link`
    tables (the latter was a byte-identical subset where source_id=6).
    """

    interaction = models.ForeignKey(
        Interaction,
        on_delete=models.CASCADE,
        related_name="cross_references",
    )
    link = models.CharField(max_length=40, help_text='e.g. "MINT-10096"')
    source = models.ForeignKey(
        Source, on_delete=models.CASCADE, related_name="cross_references"
    )
    species = models.ForeignKey(
        Species, on_delete=models.CASCADE, related_name="cross_references", null=True
    )

    objects = models.Manager()

    class Meta:
        db_table = "interaction2link"
        indexes = [
            models.Index(fields=["interaction", "source"]),
        ]

    def __str__(self):
        return f"{self.interaction_id} is referenced by {self.link}"


# =============================================================================
# Ortholog interactions
# =============================================================================


class OrthologInteraction(models.Model):
    gene_1 = models.ForeignKey(
        Gene, on_delete=models.CASCADE, related_name="ortholog_interactions_as_1"
    )
    gene_2 = models.ForeignKey(
        Gene, on_delete=models.CASCADE, related_name="ortholog_interactions_as_2"
    )

    ortholog_species = models.ManyToManyField(
        Species,
        related_name="ortholog_interactions",
        db_table="ortholog_interaction_species",
    )

    class Meta:
        db_table = "ortholog_interaction"
        indexes = [
            models.Index(fields=["gene_1", "gene_2"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(gene_1_id__lte=models.F("gene_2_id")),
                name="ortholog_interaction_canonical_order",
            ),
            models.UniqueConstraint(
                fields=["gene_1", "gene_2"],
                name="ortholog_interaction_unique",
            ),
        ]

    def __str__(self):
        return f"{self.gene_1.entrez_name}–{self.gene_2.entrez_name}"


class BaitPreyAssociation(models.Model):
    interaction = models.ForeignKey(
        Interaction,
        on_delete=models.CASCADE,
        related_name="bait_prey",
        null=True,
        blank=True,
    )

    noninteraction = models.ForeignKey(
        NonInteraction,
        on_delete=models.SET_NULL,
        related_name="bait_prey",
        null=True,
        blank=True,
    )

    publications = models.ManyToManyField(
        Publication,
        related_name="bait_prey_associations",
        db_table="bait_prey_assoc2publication",
    )
    number_of_observed = models.PositiveIntegerField(default=0)
    number_of_tests = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "bait_prey_assoc"
        constraints = [
            models.UniqueConstraint(
                fields=["interaction"],
                condition=models.Q(interaction__isnull=False),
                name="bait_prey_unique_interaction",
            ),
            models.UniqueConstraint(
                fields=["noninteraction"],
                condition=models.Q(noninteraction__isnull=False),
                name="bait_prey_unique_noninteraction",
            ),
            models.CheckConstraint(
                condition=models.Q(interaction__isnull=True)
                | models.Q(noninteraction__isnull=True),
                name="max_one_link_to_interaction_or_noninteraction",
            ),
        ]

    def __str__(self):
        return f"{self.interaction} number_of_tests={self.number_of_tests}"


class SplitJob(models.Model):
    STATUS = [
        ("PENDING", "PENDING"),
        ("RUNNING", "RUNNING"),
        ("DONE", "DONE"),
        ("FAILED", "FAILED"),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    status = models.CharField(max_length=10, choices=STATUS, default="PENDING")
    params = models.JSONField()
    progress = models.FloatField(default=0.0)
    step = models.CharField(max_length=40, blank=True)
    zip_path = models.CharField(max_length=512, blank=True)
    summary = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["status", "created_at"], name="splitjob_status_created_idx"
            ),
        ]


class ReleaseMeta(models.Model):
    """
    Metadata for one HIPPIE data release.

    Holds the release version/date, the per-dataset score quartiles used to
    derive the *medium* (Q2 / median) and *high* (Q3) confidence thresholds, and
    a free-form map of integrated resource versions. The most recent release
    (``current()``) drives the app-wide dynamic thresholds and the /information/
    "Resource Versions" block.

    Quartiles are stored per dataset: ``int_*`` (interactions), ``nonint_*``
    (non-interactions), ``both_*`` (union of both). All are nullable so a partial
    release (e.g. before non-interaction scoring exists) can still be recorded.
    """

    release_number = models.PositiveIntegerField(default=0, db_index=True)
    version_label = models.CharField(max_length=32, blank=True, default="")
    release_date = models.DateField(null=True, blank=True, db_index=True)

    int_q1 = models.FloatField(null=True, blank=True)
    int_median = models.FloatField(null=True, blank=True)
    int_q3 = models.FloatField(null=True, blank=True)

    nonint_q1 = models.FloatField(null=True, blank=True)
    nonint_median = models.FloatField(null=True, blank=True)
    nonint_q3 = models.FloatField(null=True, blank=True)

    both_q1 = models.FloatField(null=True, blank=True)
    both_median = models.FloatField(null=True, blank=True)
    both_q3 = models.FloatField(null=True, blank=True)

    resource_versions = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "release_meta"
        ordering = ["-release_date", "-release_number"]

    def __str__(self) -> str:
        return self.version_label or f"release {self.release_number}"

    @classmethod
    def current(cls) -> "ReleaseMeta | None":
        """Most recent release by date (then number), or None if none exist."""
        return cls.objects.order_by("-release_date", "-release_number").first()

    def set_quartiles(self, dataset: str, stats: dict[str, float]) -> None:
        """
        Store q1/median/q3 for one dataset from a ``compute_quartiles`` dict.

        ``dataset`` is one of ``"int"``, ``"nonint"``, ``"both"``. Does not save.
        """
        if dataset not in ("int", "nonint", "both"):
            raise ValueError(f"unknown dataset {dataset!r}")
        setattr(self, f"{dataset}_q1", stats["q1"])
        setattr(self, f"{dataset}_median", stats["median"])
        setattr(self, f"{dataset}_q3", stats["q3"])

    @classmethod
    def record_resources(cls, mapping: dict[str, str]) -> "ReleaseMeta | None":
        """
        Merge ``mapping`` into the current release's ``resource_versions`` and
        save. No-op (returns None) when no release row exists yet.
        """
        rel = cls.current()
        if rel is None:
            return None
        rel.resource_versions = {**(rel.resource_versions or {}), **mapping}
        rel.save(update_fields=["resource_versions", "updated_at"])
        return rel
