from django.test import TestCase

from .models import Gene, Protein, Interaction, NonInteraction
from . import views


def _p(name, acc):
    g, _ = Gene.objects.get_or_create(entrez_id=0, defaults={"entrez_name": ""})
    return Protein.objects.create(gene=g, uniprot_name=name, uniprot_accession=acc)


class UnionTieTest(TestCase):
    def test_both_union_tie_pagination(self):
        a = _p("A", "P_A")
        b = _p("B", "P_B")
        p1, p2 = (a, b) if a.pk <= b.pk else (b, a)
        ix = Interaction.objects.create(protein_1=p1, protein_2=p2, score=0.5)
        ni = NonInteraction.objects.create(protein_1=p1, protein_2=p2, score=0.5)

        legs = [
            views._interaction_values_qs(Interaction.objects.all()),
            views._noninteraction_values_qs(NonInteraction.objects.all()),
        ]
        union = legs[0].union(legs[1], all=True).order_by("-score", "id")
        sql = str(union.query)
        lines = [
            f"IX_ID={ix.pk} NI_ID={ni.pk} OVERLAP={ix.pk == ni.pk}",
            "ORDERBY=" + sql.split("ORDER BY")[-1].strip(),
            "KIND_IN_ORDERBY=" + str("kind" in sql.split("ORDER BY")[-1].lower()),
            "PAGE0=" + str([(r["kind"], r["id"]) for r in union[0:1]]),
            "PAGE1=" + str([(r["kind"], r["id"]) for r in union[1:2]]),
        ]
        raise AssertionError("DIAG\n" + "\n".join(lines))
