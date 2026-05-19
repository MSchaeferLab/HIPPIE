import uuid

from django.db import models
from .managers import InteractionManager, ProteinManager

# =============================================================================
# Protein entities
# =============================================================================


class Gene(models.Model):
    """
    To map proteins that link to the same gene, we added a gene class that is defined by the entrez id.
    """

    entrez_id = models.PositiveIntegerField(db_index=True)
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


class ProteinTissue(models.Model):
    """
    Binary protein-tissue expression flag (protein is expressed in tissue).

    Could later be extended with an `expression_value` FloatField for
    RPKM/TPM quantitative expression.
    """

    protein = models.ForeignKey(
        Protein, on_delete=models.CASCADE, related_name="tissue_expression"
    )
    tissue = models.ForeignKey(
        Tissue, on_delete=models.CASCADE, related_name="expressed_proteins"
    )

    class Meta:
        db_table = "protein2tissue"
        constraints = [
            models.UniqueConstraint(
                fields=["protein", "tissue"],
                name="protein_tissue_unique",
            ),
        ]

    def __str__(self):
        return f"{self.protein.gene} expressed in {self.tissue.name}"


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
        constraints = [
            # Make psi_mi_code unique, but allow multiple empty strings
            models.UniqueConstraint(
                fields=["psi_mi_code"],
                condition=~models.Q(psi_mi_code=""),
                name="experiment_type_psi_mi_unique",
            )
        ]

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
        constraints = [
            models.UniqueConstraint(
                fields=["psi_mi_code"],
                condition=~models.Q(psi_mi_code=""),
                name="interaction_type_psi_mi_unique",
            )
        ]

    def __str__(self):
        return self.name


class Species(models.Model):
    """
    NCBI taxonomy species.
    """

    name = models.CharField(max_length=300, unique=True)

    class Meta:
        db_table = "species"
        verbose_name_plural = "species"

    def __str__(self):
        return self.name


# =============================================================================
# Ontology tables
# =============================================================================


class GOSlimTerm(models.Model):
    """
    GO slim term definitions with namespace.
    Primary key is the GO ID string (e.g. "GO:0008150").
    """

    class Namespace(models.TextChoices):
        BIOLOGICAL_PROCESS = "biological_process", "Biological Process"
        CELLULAR_COMPONENT = "cellular_component", "Cellular Component"
        MOLECULAR_FUNCTION = "molecular_function", "Molecular Function"

    id = models.CharField(max_length=20, primary_key=True)
    name = models.CharField(max_length=255)
    namespace = models.CharField(max_length=30, choices=Namespace.choices)

    parents = models.ManyToManyField(
        "self", related_name="children", symmetrical=False, db_table="GO_slim_term2term"
    )

    class Meta:
        db_table = "GO_slim_term"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    namespace__in=[
                        "biological_process",
                        "cellular_component",
                        "molecular_function",
                    ]
                ),
                name="go_slim_term_namespace_valid",
            )
        ]

    def __str__(self):
        return f"{self.id} {self.name}"


class MeSHTerm(models.Model):
    """
    MeSH term definitions.
    Hierarchy is encoded in the `number` field via string prefix
    matching (e.g. "C01.252.400" is a child of "C01.252").
    Django's `__startswith` lookup maps to the same SQL pattern.
    """

    number = models.CharField(max_length=255, primary_key=True)
    name = models.CharField(max_length=512, db_index=True)

    class Meta:
        db_table = "mesh_term"

    def __str__(self):
        return f"{self.number} {self.name[:60]}"


# =============================================================================
# Core interaction
# =============================================================================


class Interaction(models.Model):
    """
    Central interaction table — the heart of HIPPIE.

    Every row always has `protein_1` and `protein_2` set.

    `kegg_direction` and `effect_type`/`effect_source` are inlined from
    the old `interaction2keggDirection` and `interaction2effect` tables.
    """

    class EffectType(models.IntegerChoices):
        ACTIVATION = 1, "Activation"
        INHIBITION = -1, "Inhibition"

    class EffectSource(models.IntegerChoices):
        SURATANEE = 1, "Suratanee"
        KEGG = 25, "KEGG"

    class DirectionType(models.IntegerChoices):
        FORWARD = 1, "protein_1 -> protein_2"
        BACKWARD = -1, "protein_2 -> protein_1"

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

    # -- Inlined from interaction2keggDirection
    #    1 = protein_1 → protein_2, -1 = protein_2 → protein_1
    kegg_direction = models.SmallIntegerField(
        choices=DirectionType.choices, null=True, blank=True
    )

    # -- Inlined from interaction2effect
    effect_type = models.SmallIntegerField(
        choices=EffectType.choices, null=True, blank=True
    )
    effect_source = models.SmallIntegerField(
        choices=EffectSource.choices,
        null=True,
        blank=True,
        help_text="Source of effect prediction: 1=Suratanee, 25=KEGG",
    )

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
    go_terms = models.ManyToManyField(
        GOSlimTerm, related_name="interactions", db_table="interaction2GO"
    )
    mesh_terms = models.ManyToManyField(
        MeSHTerm, related_name="interactions", db_table="interaction2mesh"
    )

    objects = InteractionManager()

    class Meta:
        db_table = "interaction"
        indexes = [
            models.Index(fields=["protein_1", "score"]),
            models.Index(fields=["protein_2", "score"]),
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


class HomoMINTLinkManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(source_id=6)


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

    objects = models.Manager()  # default manager — all rows
    homomint = HomoMINTLinkManager()  # filtered manager — source_id=6 only

    class Meta:
        db_table = "interaction2link"
        indexes = [
            models.Index(fields=["interaction", "source"]),
        ]

    def __str__(self):
        return f"{self.interaction_id} is referenced by {self.link}"


# =============================================================================
# Shortest path support
# =============================================================================


class SignalingEndpoint(models.Model):
    """
    Pre-computed receptor / transcription factor classification for
    shortest path analysis.

    Used by the shortest path algorithm to identify default sources
    (receptors) and sinks (transcription factors).
    """

    class Types(models.TextChoices):
        RECEPTOR = "rec", "Receptor"
        TRANSCRIPTION_FACTOR = "tf", "Transcription factor"
        BOTH = "b", "Both"

    # TODO: Foreign Key to ProteinUniProt or Protein?
    uniprot_id = models.CharField(max_length=16, unique=True)
    type = models.CharField(max_length=3, choices=Types.choices)

    class Meta:
        db_table = "sp_analysis_end_nodes"

    def __str__(self):
        return f"{self.uniprot_id} ({self.get_type_display()})"


# =============================================================================
# Ortholog interactions
# =============================================================================


class OrthologInteraction(models.Model):
    """
    Cross-species ortholog interactions.
    Consolidated from three identically-structured table pairs:
      - homomint_interaction
      - i2d_interaction
      - ortho_interaction

    """

    class OrthologSource(models.TextChoices):
        HOMOMINT = "homomint", "HomoMINT"
        I2D = "i2d", "I2D"
        ORTHO = "ortho", "Ortho"

    protein_1 = models.ForeignKey(
        Protein, on_delete=models.CASCADE, related_name="ortholog_interactions_as_1"
    )
    protein_2 = models.ForeignKey(
        Protein, on_delete=models.CASCADE, related_name="ortholog_interactions_as_2"
    )
    source = models.CharField(
        max_length=20, choices=OrthologSource.choices, db_index=True
    )
    ortholog_species = models.ManyToManyField(
        Species,
        related_name="ortholog_interactions",
        db_table="ortholog_interaction_species",
    )

    class Meta:
        db_table = "ortholog_interaction"
        indexes = [
            models.Index(fields=["protein_1", "protein_2"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(protein_1_id__lte=models.F("protein_2_id")),
                name="ortholog_interaction_canonical_order",
            ),
            models.UniqueConstraint(
                fields=["protein_1", "protein_2", "source"],
                name="ortholog_interaction_unique",
            ),
        ]

    def __str__(self):
        return f"{self.protein_1.gene}–{self.protein_2.gene} ({self.source})"


class BaitPreyTest(models.Model):
    detection = models.BooleanField(
        help_text="True if bait-prey association is detected, False if tested but not detected"
    )
    publication = models.ForeignKey(
        Publication, on_delete=models.CASCADE, related_name="bait_prey_tests"
    )
    method = models.ForeignKey(ExperimentType, on_delete=models.CASCADE)

    class Meta:
        db_table = "bait_prey_test"
        constraints = [
            models.UniqueConstraint(
                fields=["detection", "publication", "method"],
                name="bait_prey_test_unique",
            ),
        ]

    def __str__(self):
        return f"PMID:{self.publication} {self.method.name} detected={self.detection}"


class BaitPreyAssociation(models.Model):
    class Directions(models.IntegerChoices):
        PROTEIN_ONE_BAIT = 1, "Protein 1 Bait"
        PROTEIN_TWO_BAIT = -1, "Protein 2 Bait"

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
    direction = models.SmallIntegerField(
        choices=Directions.choices,
        help_text="1 = protein_1 is bait, -1 = protein_2 is bait",
    )

    tests_performed = models.ManyToManyField(
        BaitPreyTest,
        related_name="bait_prey_associations",
        db_table="bait_prey_assoc2test",
    )

    class Meta:
        db_table = "bait_prey_assoc"
        constraints = [
            models.UniqueConstraint(
                fields=["interaction", "direction"],
                name="bait_pray_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(interaction__isnull=True)
                | models.Q(noninteraction__isnull=True),
                name="max_one_link_to_interaction_or_noninteraction",
            ),
        ]

    def __str__(self):
        return f"{self.interaction} direction={self.get_direction_display()} tests={self.tests_performed.count()}"


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
