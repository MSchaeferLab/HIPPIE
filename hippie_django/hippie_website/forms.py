from django import forms
from .models import Tissue, InteractionType


# ---------------------------------------------------------------------------
# Choice constants
# ---------------------------------------------------------------------------

SCORE_PRESETS = [
    ("", "Custom"),
    ("0", "No filter (0.0)"),
    ("0.63", "Medium (0.63)"),
    ("0.72", "High (0.72)"),
]

OUTPUT_CHOICES = [
    ("browser_text", "Browser – plain text"),
    ("browser_vis", "Browser – network visualization"),
    ("hippie_tab", "Download – HIPPIE TAB file"),
    ("psimitab", "Download – PSI-MI TAB 2.5"),
]

DIRECTION_CHOICES = [
    ("none", "Do not show direction"),
    ("unweighted_sp", "Unweighted shortest paths"),
    ("weighted_sp", "Confidence-weighted shortest paths"),
    ("kegg", "KEGG direction"),
]

EFFECT_CHOICES = [
    ("none", "Do not show effect"),
    ("predicted", "Show predicted effect"),
    ("kegg", "Show KEGG effect"),
]

NEGATOME_EDGES_CHOICES = [
    ("none", "Only interactions"),
    ("exclusively", "Only non-interactions"),
    ("both", "Any edges"),
]


# ---------------------------------------------------------------------------
# Form
# ---------------------------------------------------------------------------


class NetworkQueryForm(forms.Form):
    # -----------------------------------------------------------------------
    # 1. Query set
    # -----------------------------------------------------------------------

    proteins = forms.CharField(
        label="Protein / interaction list",
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": "e.g. DNMT3A DNMT3B"}),
        required=False,
        help_text=(
            "Space- or newline-separated gene symbols, UniProt accessions, "
            "or Entrez IDs."
        ),
    )
    proteins_file = forms.FileField(
        label="…or upload a list file",
        required=False,
    )

    # -----------------------------------------------------------------------
    # 2. Output parameters
    # -----------------------------------------------------------------------

    output_type = forms.ChoiceField(
        label="Output type",
        choices=OUTPUT_CHOICES,
        initial="browser_vis",
    )

    layer_0 = forms.BooleanField(
        label="Layer 0 – within input set",
        required=False,
        initial=False,
    )
    layer_1 = forms.BooleanField(
        label="Layer 1 – between input set and HIPPIE",
        required=False,
        initial=True,
    )
    include_isoforms = forms.BooleanField(
        label="Include isoforms",
        required=False,
        initial=False,
        help_text="Expand canonical proteins to all known isoforms.",
    )
    min_ppi = forms.IntegerField(
        label="Minimum PPIs to query set",
        min_value=1,
        initial=1,
        required=False,
    )

    score_preset = forms.ChoiceField(
        label="Score filter preset",
        choices=SCORE_PRESETS,
        required=False,
    )
    score_min = forms.FloatField(
        label="Minimum confidence score",
        min_value=0.0,
        max_value=1.0,
        initial=0.0,
        required=False,
    )

    interaction_types = forms.ModelMultipleChoiceField(
        label="Interaction types",
        queryset=InteractionType.objects.all(),
        widget=forms.CheckboxSelectMultiple,
        required=False,
        help_text="Leave empty to include all types.",
    )

    tissue = forms.ModelChoiceField(
        label="Tissue filter",
        queryset=Tissue.objects.all().order_by("name"),
        empty_label="Any tissues",
        required=False,
    )
    min_rpkm = forms.FloatField(
        label="Min. median RPKM",
        required=False,
        min_value=0,
        widget=forms.NumberInput(attrs={"placeholder": "0", "step": "1"}),
    )
    tissue_file = forms.FileField(
        label="…or upload a custom tissue filter",
        required=False,
    )

    go_terms = forms.CharField(
        label="GO slim terms",
        widget=forms.HiddenInput,
        required=False,
        help_text="Comma-separated GO IDs, e.g. GO:0008150,GO:0005575",
    )
    mesh_terms = forms.CharField(
        label="MeSH terms",
        widget=forms.HiddenInput,
        required=False,
        help_text="Comma-separated MeSH numbers, e.g. C01.252,C01.252.400",
    )

    direction = forms.ChoiceField(
        label="Edge directionality",
        choices=DIRECTION_CHOICES,
        initial="none",
        widget=forms.RadioSelect,
    )
    sources = forms.CharField(
        label="Sources",
        required=False,
        help_text=(
            "Space-separated gene symbols. "
            "Leave empty to use all receptors from SignalingEndpoint."
        ),
    )
    sinks = forms.CharField(
        label="Sinks",
        required=False,
        help_text=(
            "Space-separated gene symbols. "
            "Leave empty to use all transcription factors from SignalingEndpoint."
        ),
    )

    effect = forms.ChoiceField(
        label="Effect display",
        choices=EFFECT_CHOICES,
        initial="none",
        widget=forms.RadioSelect,
    )

    negatome_edges = forms.ChoiceField(
        label="Return experimental non-interactions",
        choices=NEGATOME_EDGES_CHOICES,
        initial="none",
        widget=forms.RadioSelect,
    )

    def clean(self):
        cleaned = super().clean()

        if not cleaned.get("proteins") and not cleaned.get("proteins_file"):
            raise forms.ValidationError(
                "Provide either a protein list or upload a file."
            )

        # A chosen preset overwrites whatever was typed in score_min
        preset = cleaned.get("score_preset")
        if preset:
            cleaned["score_min"] = float(preset)

        return cleaned
