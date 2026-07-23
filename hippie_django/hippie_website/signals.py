"""Keep Interaction.n_sources/n_experiments in sync outside of bulk imports.

``recompute_interaction_flags`` handles full-table refresh after data loads.
Admin edits (filter_horizontal on Interaction) and Source/ExperimentType
deletions mutate the same M2M through tables but never go through that
command, so the denormalised counts drift unless these signals recompute
them too.
"""

from django.db.models.signals import m2m_changed, post_delete, pre_delete
from django.dispatch import receiver

from .models import ExperimentType, Interaction, Source
from .query_filters import recompute_evidence_counts


def _recompute(interaction_ids):
    ids = [pk for pk in interaction_ids if pk is not None]
    if not ids:
        return
    recompute_evidence_counts(Interaction.objects.filter(pk__in=ids))


@receiver(m2m_changed, sender=Interaction.sources.through)
@receiver(m2m_changed, sender=Interaction.experiments.through)
def _on_evidence_m2m_changed(sender, instance, action, reverse, pk_set, **kwargs):
    if action not in ("post_add", "post_remove", "post_clear"):
        return
    if reverse:
        # instance is a Source/ExperimentType; pk_set holds the affected
        # Interaction pks (None on post_clear — no reverse-clear caller
        # exists in this codebase today, so there is nothing to recompute).
        _recompute(pk_set or [])
    else:
        _recompute([instance.pk])


@receiver(pre_delete, sender=Source)
@receiver(pre_delete, sender=ExperimentType)
def _stash_affected_interactions(sender, instance, **kwargs):
    # Cascade deletes remove the through-table rows without ever firing
    # m2m_changed, so the affected Interaction pks must be captured here,
    # before the cascade runs, and recomputed in post_delete below.
    instance._affected_interaction_pks = list(
        instance.interactions.values_list("pk", flat=True)
    )


@receiver(post_delete, sender=Source)
@receiver(post_delete, sender=ExperimentType)
def _resync_after_evidence_delete(sender, instance, **kwargs):
    _recompute(getattr(instance, "_affected_interaction_pks", []))
