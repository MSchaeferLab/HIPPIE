from django.contrib import admin

from .models import (
    BaitPreyAssociation,
    ExperimentType,
    Gene,
    GeneSynonym,
    GOSlimTerm,
    Interaction,
    InteractionCrossReference,
    InteractionType,
    Isoform,
    MeSHTerm,
    NonInteraction,
    OrthologInteraction,
    Protein,
    ProteinSynonym,
    GeneTissue,
    Publication,
    SignalingEndpoint,
    Source,
    Species,
    Tissue,
    BaitPreyTest,
)


@admin.register(Gene)
class GeneAdmin(admin.ModelAdmin):
    list_display = ("id", "entrez_id", "entrez_name")
    search_fields = ("=entrez_id", "entrez_name")
    ordering = ("entrez_id",)


@admin.register(GeneSynonym)
class GeneSynonymAdmin(admin.ModelAdmin):
    list_display = ("id", "gene", "synonym")
    search_fields = ("synonym", "gene__entrez_name")
    list_select_related = ("gene",)
    autocomplete_fields = ("gene",)


@admin.register(Protein)
class ProteinAdmin(admin.ModelAdmin):
    list_display = ("id", "uniprot_accession", "uniprot_name", "gene")
    search_fields = ("uniprot_accession", "uniprot_name", "gene__entrez_name")
    ordering = ("uniprot_accession",)
    list_select_related = ("gene",)
    autocomplete_fields = ("gene",)


@admin.register(ProteinSynonym)
class ProteinSynonymAdmin(admin.ModelAdmin):
    list_display = ("id", "protein", "synonym")
    search_fields = ("synonym", "protein__gene__entrez_name")
    list_select_related = ("protein",)
    autocomplete_fields = ("protein",)


@admin.register(Isoform)
class IsoformAdmin(admin.ModelAdmin):
    list_display = ("id", "uniprot_accession", "general_protein")
    search_fields = ("uniprot_accession", "general_protein__uniprot_accession")
    list_select_related = ("general_protein",)

    @admin.display(description="Parent protein")
    def parent_symbol(self, obj):
        return obj.general_protein.uniprot_accession


@admin.register(Publication)
class PublicationAdmin(admin.ModelAdmin):
    list_display = ("id", "pmid")
    search_fields = ("=pmid",)
    ordering = ("pmid",)


@admin.register(Tissue)
class TissueAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    search_fields = ("name",)
    ordering = ("name",)


@admin.register(GeneTissue)
class GeneTissueAdmin(admin.ModelAdmin):
    list_display = ("id", "gene", "tissue", "median_rpkm")
    search_fields = ("gene__entrez_name", "tissue__name")
    list_filter = ("tissue",)
    list_select_related = ("gene", "tissue")
    autocomplete_fields = ("gene", "tissue")


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "url")
    search_fields = ("name", "url")
    ordering = ("name",)


@admin.register(ExperimentType)
class ExperimentTypeAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "psi_mi_code", "quality_score")
    search_fields = ("name", "psi_mi_code")
    list_filter = (("psi_mi_code", admin.EmptyFieldListFilter),)
    ordering = ("name",)


@admin.register(InteractionType)
class InteractionTypeAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "psi_mi_code")
    search_fields = ("name", "psi_mi_code")
    list_filter = (("psi_mi_code", admin.EmptyFieldListFilter),)
    ordering = ("name",)


@admin.register(Species)
class SpeciesAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    search_fields = ("name",)
    ordering = ("name",)


@admin.register(GOSlimTerm)
class GOSlimTermAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "namespace")
    search_fields = ("id", "name")
    list_filter = ("namespace",)
    ordering = ("id",)
    autocomplete_fields = ("parents",)


@admin.register(MeSHTerm)
class MeSHTermAdmin(admin.ModelAdmin):
    list_display = ("number", "name")
    search_fields = ("number", "name")
    ordering = ("number",)


@admin.register(Interaction)
class InteractionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "protein_1",
        "protein_2",
        "score",
        "kegg_direction",
        "effect_type",
        "effect_source",
    )
    search_fields = ("=id", "protein_1__gene__entrez_name", "protein_2__gene__entrez_name")
    list_filter = ("kegg_direction", "effect_type", "effect_source")
    ordering = ("-score", "id")
    list_select_related = ("protein_1", "protein_2")
    autocomplete_fields = ("protein_1", "protein_2")
    filter_horizontal = (
        "sources",
        "experiments",
        "conserved_species",
        "interaction_types",
        "go_terms",
        "mesh_terms",
        "publications",
    )


@admin.register(InteractionCrossReference)
class InteractionCrossReferenceAdmin(admin.ModelAdmin):
    list_display = ("id", "interaction", "link", "source", "species")
    search_fields = (
        "link",
        "=interaction__id",
        "source__name",
        "species__name",
        "interaction__protein_1__gene__entrez_name",
        "interaction__protein_2__gene__entrez_name",
    )
    list_filter = ("source", "species")
    list_select_related = (
        "interaction",
        "interaction__protein_1",
        "interaction__protein_2",
        "source",
        "species",
    )
    autocomplete_fields = ("interaction", "source", "species")


@admin.register(SignalingEndpoint)
class SignalingEndpointAdmin(admin.ModelAdmin):
    list_display = ("id", "uniprot_id", "type")
    search_fields = ("uniprot_id",)
    list_filter = ("type",)
    ordering = ("uniprot_id",)


@admin.register(OrthologInteraction)
class OrthologInteractionAdmin(admin.ModelAdmin):
    list_display = ("id", "gene_1", "gene_2")
    search_fields = ("=id", "gene_1__entrez_id", "gene_2__entrez_id")
    list_select_related = ("gene_1", "gene_2")
    autocomplete_fields = ("gene_1", "gene_2")


@admin.register(BaitPreyAssociation)
class BaitPreyAssociationAdmin(admin.ModelAdmin):
    list_display = ("id", "interaction", "direction")
    search_fields = (
        "=interaction__id",
        "interaction__protein_1__gene__entrez_name",
        "interaction__protein_2__gene__entrez_name",
        "=tests_performed__publication__pmid",
    )
    list_filter = ("direction",)
    list_select_related = (
        "interaction",
        "interaction__protein_1",
        "interaction__protein_2",
    )
    autocomplete_fields = ("interaction",)


@admin.register(NonInteraction)
class NonInteractionAdmin(admin.ModelAdmin):
    list_display = ("id", "protein_1", "protein_2", "score")
    search_fields = ("=id", "protein_1__gene__entrez_name", "protein_2__gene__entrez_name")
    ordering = ("-score", "id")
    list_select_related = ("protein_1", "protein_2")
    autocomplete_fields = ("protein_1", "protein_2")


@admin.register(BaitPreyTest)
class BaitPreyTestAdmin(admin.ModelAdmin):
    list_display = ("id", "detection", "publication", "method")
    search_fields = ("detection", "=publication__pmid", "method")
    list_filter = ("detection",)
    ordering = ("-publication__pmid",)
    autocomplete_fields = ("publication",)
