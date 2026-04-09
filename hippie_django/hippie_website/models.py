from django.db import models


# =============================================================================
# Protein entities
# =============================================================================


class Protein(models.Model):
    """
    Central entity — one row per unique protein in HIPPIE.
    The `name` field stores the primary gene symbol (e.g. "BRCA1").
    """

    name = models.CharField(max_length=30, unique=True)

    class Meta:
        db_table = "protein"

    def __str__(self):
        return self.name


class Isoform(models.Model):
    """
    Protein isoform — for future isoform-level interaction data.
    Each isoform belongs to exactly one protein.
    """

    protein = models.ForeignKey(
        Protein, on_delete=models.CASCADE, related_name="isoforms"
    )
    uniprot_id = models.CharField(
        max_length=20, db_index=True, help_text='e.g. "P38398-2"'
    )
    name = models.CharField(max_length=100, blank=True, default="")

    class Meta:
        db_table = "isoform"
        constraints = [
            models.UniqueConstraint(
                fields=["protein", "uniprot_id"],
                name="isoform_protein_uniprot_unique",
            ),
        ]

    def __str__(self):
        return self.uniprot_id


# =============================================================================
# Identifier mappings
# =============================================================================


class ProteinUniProt(models.Model):
    """
    Maps a protein to its UniProt entry ID (e.g. "BRCA1_HUMAN").
    `version` is used to pick the latest mapping when multiple exist
    (ORDER BY version DESC LIMIT 1).
    """

    protein = models.ForeignKey(
        Protein, on_delete=models.CASCADE, related_name="uniprot_ids"
    )
    uniprot_id = models.CharField(max_length=16, db_index=True)
    version = models.IntegerField(default=0)

    class Meta:
        db_table = "protein2uniprot"
        constraints = [
            models.UniqueConstraint(
                fields=["protein", "uniprot_id"],
                name="protein_to_uniprot_unique",
            ),
        ]

    def __str__(self):
        return self.uniprot_id


class ProteinEntrez(models.Model):
    """
    Maps a protein to its NCBI Entrez Gene ID + gene symbol.
    """

    protein = models.ForeignKey(
        Protein, on_delete=models.CASCADE, related_name="entrez_ids"
    )
    gene_id = models.PositiveIntegerField(db_index=True)
    name = models.CharField(
        max_length=40, blank=True, default="", db_index=True
    )

    class Meta:
        db_table = "protein_entrez"
        constraints = [
            models.UniqueConstraint(
                fields=["protein", "gene_id"],
                name="protein_entrez_unique",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.gene_id})"


class UniProtAccession(models.Model):
    """
    Maps UniProt accessions (e.g. "P38398") to UniProt entry IDs
    (e.g. "BRCA1_HUMAN").  Multiple accessions can map to the same ID.

    Resolution path: accession → uniprot_id → ProteinUniProt → Protein.
    We keep the indirect link rather than adding a direct protein FK to
    avoid denormalization and sync risk when UniProt updates accession
    mappings.
    """

    accession = models.CharField(max_length=20, db_index=True)
    uniprot_id = models.CharField(max_length=16, db_index=True)

    class Meta:
        db_table = "uniprot_accession2id"
        constraints = [
            models.UniqueConstraint(
                fields=["accession", "uniprot_id"],
                name="uniprot_accession_unique",
            ),
        ]

    def __str__(self):
        return f"{self.accession} -> {self.uniprot_id}"


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
        return f"{self.protein.name} expressed in {self.tissue.name}"


# =============================================================================
# Reference / lookup tables
# =============================================================================

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
        help_text="Weight in HIPPIE confidence scoring"
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
                check=models.Q(namespace__in=[
                    "biological_process",
                    "cellular_component",
                    "molecular_function",
                ]),
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
        choices=EffectSource.choices, null=True, blank=True,
        help_text="Source of effect prediction: 1=Suratanee, 25=KEGG",
    )

    sources = models.ManyToManyField(
        Source, related_name="interactions", db_table="interaction2source"
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

    class Meta:
        db_table = "interaction"
        indexes = [
            models.Index(fields=["protein_1", "score"]),
            models.Index(fields=["protein_2", "score"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(protein_1_id__lte=models.F("protein_2_id")),
                name="interaction_canonical_order",
            ),
            models.CheckConstraint(
                check=models.Q(score__gte=0.0) & models.Q(score__lte=1.0),
                name="interaction_score_range",
            ),
            models.UniqueConstraint(
                fields=["protein_1", "protein_2"],
                name="interaction_unique_pair",
            ),
        ]

    def __str__(self):
        return f"{self.protein_1.name}–{self.protein_2.name} ({self.score})"


# =============================================================================
# Evidence junction tables
# =============================================================================



class InteractionPublication(models.Model):
    """
    PubMed citations supporting an interaction.
    """

    interaction = models.ForeignKey(
        Interaction, on_delete=models.CASCADE, related_name="publications"
    )
    pmid = models.PositiveIntegerField(db_index=True)

    class Meta:
        db_table = "interaction2pubmed"
        constraints = [
            models.UniqueConstraint(
                fields=["interaction", "pmid"],
                name="interaction_publication_unique",
            ),
        ]

    def __str__(self):
        return f"{self.interaction_id} PMID:{self.pmid}"


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
    link = models.CharField(
        max_length=40, help_text='e.g. "MINT-10096"'
    )
    source = models.ForeignKey(
        Source, on_delete=models.CASCADE, related_name="cross_references"
    )
    species = models.ForeignKey(
        Species,
        on_delete=models.CASCADE,
        related_name="cross_references",
        null=True
    )


    objects = models.Manager()          # default manager — all rows
    homomint = HomoMINTLinkManager()    # filtered manager — source_id=6 only

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
        Species, related_name="ortholog_interactions", db_table="ortholog_interaction_species"
    )

    class Meta:
        db_table = "ortholog_interaction"
        indexes = [
            models.Index(fields=["protein_1", "protein_2"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(protein_1_id__lte=models.F("protein_2_id")),
                name="ortholog_interaction_canonical_order",
            ),
            models.UniqueConstraint(
                fields=["protein_1", "protein_2", "source"],
                name="ortholog_interaction_unique",
            ),
        ]

    def __str__(self):
        return f"{self.protein_1.name}–{self.protein_2.name} ({self.source})"


class BaitPreyAssociation(models.Model):
    class Directions(models.IntegerChoices):
        PROTEIN_ONE_BAIT = 1, "Protein 1 Bait"
        PROTEIN_TWO_BAIT = -1, "Protein 2 Bait"

    interaction = models.ForeignKey(
        Interaction, on_delete=models.CASCADE, related_name="bait_prey"
    )
    pmid = models.PositiveIntegerField(db_index=True)
    direction = models.SmallIntegerField(
        choices=Directions.choices,
        help_text="1 = protein_1 is bait, -1 = protein_2 is bait"
    )

    class Meta:
        db_table = "bait_prey_assoc"
        constraints = [
            models.UniqueConstraint(
                fields=["interaction", "pmid", "direction"],
                name="bait_pray_unique",
            ),
        ]