import json as _json
from dataclasses import dataclass
from pathlib import Path

import networkx as nx
import numpy as np
from django.db.models import Exists, OuterRef, Q  # noqa: F401 (Q kept for back-compat)
from networkx.algorithms.community import kernighan_lin_bisection


@dataclass(frozen=True)
class SplitParams:
    # ── Interaction-level filters (gate which edges are allowed) ──────────
    min_score: float = 0.0
    max_score: float = 1.0
    source_ids: tuple = ()
    experiment_ids: tuple = ()
    type_ids: tuple = ()

    # ── Protein-level filters (gate which nodes are allowed) ──────────────
    # An interaction survives only if BOTH endpoints pass these.
    tissue_ids: tuple = ()
    min_rpkm: float = 0.0
    min_degree: int = 0
    min_avg_score: float = 0.0
    include_isoforms: bool = False

    # ── Negative sampling ─────────────────────────────────────────────────
    neg_ratio: float = 1.0
    seed: int = 78539105873


@dataclass
class SplitSummary:
    n_proteins: int
    n_positive_total: int
    n_negative_total: int
    n_discarded_edges: int
    n_discarded_nodes: int
    splits: list  # [{"name": str, "n_proteins": int, "n_pos": int, "n_neg": int}, …]


def allowed_protein_id_qs(params: SplitParams):
    """
    Return a lazy ``values("pk")`` queryset of Protein PKs passing the
    protein-level filters (tissue expression, min degree, min avg score), or
    ``None`` when no protein-level filter is active (so callers can skip node
    gating entirely).

    ``degree`` / ``avg_score`` are the denormalised columns on Protein
    (refreshed by ``recompute_protein_stats``) — global values, matching the
    Browse page. Isoform inclusion is handled at the *edge* level via the
    ``involves_isoform`` flag, so it is intentionally not applied here.
    """
    from hippie_website.models import Protein

    active = (
        bool(params.tissue_ids) or params.min_degree > 0 or params.min_avg_score > 0
    )
    if not active:
        return None

    qs = Protein.objects.all()
    if params.tissue_ids:
        qs = qs.expressed_in(
            list(params.tissue_ids),
            min_rpkm=params.min_rpkm if params.min_rpkm > 0 else None,
        )
    if params.min_degree > 0:
        qs = qs.filter(degree__gte=params.min_degree)
    if params.min_avg_score > 0:
        qs = qs.filter(avg_score__gte=params.min_avg_score)
    return qs.values("pk")


def build_interaction_queryset(params: SplitParams):
    """
    Build the filtered Interaction queryset shared by the graph builder and the
    statistics endpoint. Applies interaction-level filters (score range,
    sources, experiments, types), isoform exclusion, and protein-level node
    gating (both endpoints must pass the protein filters).

    M2M filters use ``Exists`` over the through tables so rows are never
    multiplied — keeping ``.count()`` accurate and edge iteration duplicate-free.
    """
    from hippie_website.models import Interaction

    qs = Interaction.objects.all()

    # ── Interaction-level filters ────────────────────────────────────────
    if params.min_score > 0:
        qs = qs.above_score(params.min_score)
    if params.max_score < 1.0:
        qs = qs.filter(score__lte=params.max_score)

    if params.source_ids:
        qs = qs.filter(
            Exists(
                Interaction.sources.through.objects.filter(
                    interaction_id=OuterRef("pk"),
                    source_id__in=list(params.source_ids),
                )
            )
        )
    if params.experiment_ids:
        qs = qs.filter(
            Exists(
                Interaction.experiments.through.objects.filter(
                    interaction_id=OuterRef("pk"),
                    experimenttype_id__in=list(params.experiment_ids),
                )
            )
        )
    if params.type_ids:
        qs = qs.filter(
            Exists(
                Interaction.interaction_types.through.objects.filter(
                    interaction_id=OuterRef("pk"),
                    interactiontype_id__in=list(params.type_ids),
                )
            )
        )

    # ── Isoform handling (denormalised flag — one indexed column) ────────
    if not params.include_isoforms:
        qs = qs.filter(involves_isoform=False)

    # ── Protein-level node gating ────────────────────────────────────────
    pid_qs = allowed_protein_id_qs(params)
    if pid_qs is not None:
        qs = qs.filter(protein_1_id__in=pid_qs, protein_2_id__in=pid_qs)

    return qs


def get_interaction_graph(params: SplitParams) -> nx.Graph:
    """
    Build an undirected NetworkX graph of positive interactions matching
    the user's filters. Nodes are Protein primary keys (ints). Edge
    attributes: 'score'.
    """
    edge_tuples = (
        build_interaction_queryset(params)
        .values_list("protein_1_id", "protein_2_id", "score")
        .iterator(chunk_size=10_000)
    )

    G = nx.Graph()
    G.add_weighted_edges_from(edge_tuples, weight="score")
    return G


def test_partition_respects_disjoint_node_sets():
    params = SplitParams(seed=1)
    p = EdgePartition.__new__(EdgePartition)  # skip __init__
    p.params = params
    p.interaction_graph = nx.barbell_graph(10, 0)  # two cliques joined by an edge
    p.non_interaction_graph = nx.Graph()
    p.node_sets = []
    p.selected_sets = []
    p.discarded_edges = []
    p.discarded_nodes = set()

    p.generate_splits()
    a, b, c = p.node_sets
    assert a.isdisjoint(b) and b.isdisjoint(c) and a.isdisjoint(c), "Overlapping nodes!"


class EdgePartition:
    def __init__(self, params: SplitParams):
        self.params = params
        self.interaction_graph = get_interaction_graph(params)
        self.non_interaction_graph = (
            None  # get_non_interaction_graph() NOTE: method will be implemented
        )
        self.node_sets = []
        self.discarded_edges = []
        self.discarded_nodes = []
        self.selected_sets = []

    def single_split(self, graph):
        A, B = kernighan_lin_bisection(
            graph,
            seed=self.params.seed,
        )
        return A, B

    def generate_splits(self):
        node_set1, node_set_remaining = self.single_split(self.interaction_graph)
        node_set2, node_set3 = self.single_split(
            self.interaction_graph.subgraph(node_set_remaining)
        )

        self.node_sets = [node_set1, node_set2, node_set3]

    @staticmethod
    def _get_random_node(node_idx, cumulative_deg, max_degree_sum):
        # Degree-weighted pick: draw a point in [0, sum-of-degrees) and binary-
        # search the cumulative-degree array for the owning node — O(log V)
        # instead of a linear scan. side="left" reproduces the old behaviour of
        # returning the first node whose cumulative degree reaches idx.
        idx = np.random.randint(0, max_degree_sum)
        return node_idx[int(np.searchsorted(cumulative_deg, idx, side="left"))]

    def get_random_balanced_negative_complement(self, current_node_set):
        c_subgraph = self.interaction_graph.subgraph(current_node_set)
        node_idx, obs_degrees = zip(*c_subgraph.degree())
        cumulative_deg = np.cumsum(obs_degrees)
        max_degree_sum = int(cumulative_deg[-1])
        n_edges = c_subgraph.number_of_edges()

        # Sorted (u, v) tuples so undirected duplicates collapse; sets give O(1)
        # membership — the old list scans made this sampling loop O(E^2).
        positive_edges = {tuple(sorted(e)) for e in c_subgraph.edges()}
        selected_edges: set[tuple[int, int]] = set()
        tries_until_failure = 1000
        tries = 0

        # Draw degree-weighted node pairs until there are as many negatives as
        # positives, rejecting self-loops, duplicates, and true positive edges.
        while len(selected_edges) < n_edges:
            tries += 1
            u = self._get_random_node(node_idx, cumulative_deg, max_degree_sum)
            v = self._get_random_node(node_idx, cumulative_deg, max_degree_sum)
            edge = (u, v) if u <= v else (v, u)
            if u != v and edge not in selected_edges and edge not in positive_edges:
                selected_edges.add(edge)
                tries = 0
            if tries > tries_until_failure:
                raise ValueError(
                    f"No random edge selected in {tries_until_failure}, aborting."
                )

        g_negative = nx.Graph()
        g_negative.add_edges_from(selected_edges)

        # c_subgraph is a read-only NetworkX subgraph view (frozen); return a
        # mutable copy since drop_exclusive_nodes() removes nodes from it.
        return c_subgraph.copy(), g_negative

    def drop_exclusive_nodes(self):
        assert len(self.selected_sets) != 0, (
            "Can't remove exclusive nodes for missing graphs!"
        )
        dropped_nodes = set()
        for i, (pos_G, neg_G) in enumerate(self.selected_sets):
            while True:
                assert pos_G.number_of_edges() != 0 and neg_G.number_of_edges() != 0, (
                    "Removed all edges while balancing"
                )
                nodes_to_drop = {
                    node for node, degree in dict(pos_G.degree).items() if degree == 0
                }
                nodes_to_drop |= {
                    node for node, degree in dict(neg_G.degree).items() if degree == 0
                }

                if not nodes_to_drop:
                    break
                else:
                    pos_G.remove_nodes_from(nodes_to_drop)
                    neg_G.remove_nodes_from(nodes_to_drop)
                    dropped_nodes |= nodes_to_drop

            self.selected_sets[i] = (pos_G, neg_G)

        self.discarded_nodes = dropped_nodes

    def set_discarded_edges(self):
        selected_subgraphs = nx.compose_all([pair[0] for pair in self.selected_sets])
        self.discarded_edges = set(self.interaction_graph.edges()) - set(
            selected_subgraphs.edges()
        )


# ---------------------------------------------------------------------------
# Top-level entry point called by tasks.py
# ---------------------------------------------------------------------------

SPLIT_NAMES = ["train", "val", "test"]


def generate_splits(params: SplitParams, work_dir: Path, cb) -> SplitSummary:
    """
    Build the interaction graph, partition it into train/val/test splits with
    balanced negative sampling, write CSV files to work_dir, and return a
    SplitSummary.

    cb(step: str, frac: float) is called at key checkpoints for progress reporting.
    """
    cb("building_graph", 0.05)
    ep = EdgePartition(params)

    cb("partitioning", 0.15)
    ep.generate_splits()  # populates ep.node_sets

    n = len(ep.node_sets)
    for i, nodes_split in enumerate(ep.node_sets):
        cb("sampling_negatives", 0.20 + 0.40 * i / n)
        ep.selected_sets.append(ep.get_random_balanced_negative_complement(nodes_split))

    cb("pruning", 0.70)
    ep.drop_exclusive_nodes()
    ep.set_discarded_edges()

    cb("writing_files", 0.90)

    all_pks = set()
    for pos_G, neg_G in ep.selected_sets:
        all_pks |= set(pos_G.nodes()) | set(neg_G.nodes())

    from hippie_website.models import Protein

    accession_by_pk = dict(
        Protein.objects.filter(pk__in=all_pks).values_list("pk", "uniprot_accession")
    )

    split_stats = []
    for i, (pos_G, neg_G) in enumerate(ep.selected_sets):
        name = SPLIT_NAMES[i]
        with open(work_dir / f"{name}_pos.csv", "w") as f:
            f.write("protein_1_accession,protein_2_accession,score\n")
            for u, v, data in pos_G.edges(data=True):
                score = data.get("score", data.get("weight", ""))
                f.write(f"{accession_by_pk[u]},{accession_by_pk[v]},{score}\n")
        with open(work_dir / f"{name}_neg.csv", "w") as f:
            f.write("protein_1_accession,protein_2_accession\n")
            for u, v in neg_G.edges():
                f.write(f"{accession_by_pk[u]},{accession_by_pk[v]}\n")
        split_stats.append(
            {
                "name": name,
                "n_proteins": pos_G.number_of_nodes(),
                "n_pos": pos_G.number_of_edges(),
                "n_neg": neg_G.number_of_edges(),
            }
        )

    summary = SplitSummary(
        n_proteins=sum(s["n_proteins"] for s in split_stats),
        n_positive_total=sum(s["n_pos"] for s in split_stats),
        n_negative_total=sum(s["n_neg"] for s in split_stats),
        n_discarded_edges=len(ep.discarded_edges),
        n_discarded_nodes=len(ep.discarded_nodes),
        splits=split_stats,
    )
    with open(work_dir / "summary.json", "w") as f:
        _json.dump(summary.__dict__, f, indent=2)

    cb("done", 1.0)
    return summary
