import json as _json
from dataclasses import dataclass
from pathlib import Path

import networkx as nx
import numpy as np
from django.db.models import Q
from networkx.algorithms.community import kernighan_lin_bisection


@dataclass(frozen=True)
class SplitParams:
    # Filters — match browse page
    min_score:     float = 0.0
    tissue_ids:    tuple = ()
    source_ids:    tuple = ()
    type_ids:      tuple = ()

    # Negative sampling
    neg_ratio:   float = 1.0
    seed:        int   = 78539105873


@dataclass
class SplitSummary:
    n_proteins:        int
    n_positive_total:  int
    n_negative_total:  int
    n_discarded_edges: int
    n_discarded_nodes: int
    splits: list  # [{"name": str, "n_proteins": int, "n_pos": int, "n_neg": int}, …]


def get_interaction_graph(params: SplitParams) -> nx.Graph:
    """
    Build an undirected NetworkX graph of positive interactions matching
    the user's filters. Nodes are Protein primary keys (ints). Edge
    attributes: 'score'.
    """
    from hippie_website.models import Interaction

    qs = Interaction.objects.all()

    if params.min_score > 0:
        qs = qs.above_score(params.min_score)

    if params.tissue_ids:
        qs = qs.in_tissues(list(params.tissue_ids))

    if params.type_ids:
        qs = qs.of_types(list(params.type_ids))

    if params.source_ids:
        qs = qs.filter(sources__id__in=list(params.source_ids)).distinct()

    edge_tuples = qs.values_list("protein_1_id", "protein_2_id", "score") \
                    .iterator(chunk_size=10_000)

    G = nx.Graph()
    G.add_weighted_edges_from(edge_tuples, weight="score")
    return G


def test_partition_respects_disjoint_node_sets():
    params = SplitParams(seed=1)
    p = EdgePartition.__new__(EdgePartition)   # skip __init__
    p.params = params
    p.interaction_graph = nx.barbell_graph(10, 0)   # two cliques joined by an edge
    p.non_interaction_graph = nx.Graph()
    p.node_sets = []
    p.selected_sets = []
    p.discarded_edges = []
    p.discarded_nodes = set()

    p.generate_splits()
    a, b, c = p.node_sets
    assert a.isdisjoint(b) and b.isdisjoint(c) and a.isdisjoint(c), "Overlapping nodes!"


class EdgePartition():
    def __init__(self, params: SplitParams):
        self.params = params
        self.interaction_graph = get_interaction_graph(params)
        self.non_interaction_graph = None  # get_non_interaction_graph() NOTE: method will be implemented
        self.node_sets = []
        self.discarded_edges = []
        self.discarded_nodes = []
        self.selected_sets = []

    def single_split(self, graph):
        A, B = kernighan_lin_bisection(
            graph, seed=self.params.seed,
        )
        return A, B

    def generate_splits(self):
        node_set1, node_set_remaining = self.single_split(self.interaction_graph)
        node_set2, node_set3 = self.single_split(self.interaction_graph.subgraph(node_set_remaining))

        self.node_sets = [node_set1, node_set2, node_set3]

    @staticmethod
    def _get_random_node(node_idx, cumulative_deg, max_degree_sum):
        idx = np.random.randint(0, max_degree_sum)
        for i, val in enumerate(cumulative_deg):
            if val >= idx:
                return node_idx[i]
        raise ValueError("Degree selected is larger than maximum!")

    def get_random_balanced_negative_complement(self, current_node_set):
        c_subgraph = self.interaction_graph.subgraph(current_node_set)
        degrees = c_subgraph.degree()
        node_idx, obs_degrees = zip(*degrees)
        cumulative_deg = np.cumsum(obs_degrees)
        n_edges = c_subgraph.number_of_edges()
        n_selected_edges = 0

        positive_edge_tuples = [sorted((u, v)) for u, v in c_subgraph.edges()]
        selected_edges = []
        tries_until_failure = 1000
        tries = 0

        while n_selected_edges < n_edges:
            tries += 1
            random_edge = sorted((self._get_random_node(node_idx, cumulative_deg, int(cumulative_deg[-1])) for _ in range(2)))  # sorted as internal ids are always A > B
            if random_edge[0] != random_edge[1] and random_edge not in selected_edges and random_edge not in positive_edge_tuples:
                selected_edges.append(random_edge)
                n_selected_edges += 1
                tries = 0
            if tries > tries_until_failure:
                raise ValueError(f"No random edge selected in {tries_until_failure}, aborting.")

        g_negative = nx.Graph()
        g_negative.add_edges_from(selected_edges)

        return c_subgraph, g_negative

    def drop_exclusive_nodes(self):
        assert len(self.selected_sets) != 0, "Can't remove exclusive nodes for missing graphs!"
        dropped_nodes = set()
        for i, (pos_G, neg_G) in enumerate(self.selected_sets):
            while True:
                assert pos_G.number_of_edges() != 0 and neg_G.number_of_edges() != 0, "Removed all edges while balancing"
                nodes_to_drop  = {node for node, degree in dict(pos_G.degree).items() if degree == 0}
                nodes_to_drop |= {node for node, degree in dict(neg_G.degree).items() if degree == 0}

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
        self.discarded_edges = set(self.interaction_graph.edges()) - set(selected_subgraphs.edges())


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
    split_stats = []
    for i, (pos_G, neg_G) in enumerate(ep.selected_sets):
        name = SPLIT_NAMES[i]
        with open(work_dir / f"{name}_pos.csv", "w") as f:
            f.write("protein_1_id,protein_2_id,score\n")
            for u, v, data in pos_G.edges(data=True):
                score = data.get("score", data.get("weight", ""))
                f.write(f"{u},{v},{score}\n")
        with open(work_dir / f"{name}_neg.csv", "w") as f:
            f.write("protein_1_id,protein_2_id\n")
            for u, v in neg_G.edges():
                f.write(f"{u},{v}\n")
        split_stats.append({
            "name": name,
            "n_proteins": pos_G.number_of_nodes(),
            "n_pos": pos_G.number_of_edges(),
            "n_neg": neg_G.number_of_edges(),
        })

    summary = SplitSummary(
        n_proteins=ep.interaction_graph.number_of_nodes(),
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
