from pathlib import Path
import tempfile

from django.test import TestCase

from .services.generate_splits import EdgePartition, SplitParams, generate_splits
from .tests import make_interaction, make_protein


class ScratchExperiment(TestCase):
    def test_experiment(self):
        # 9 disjoint pairs (18 nodes). 9 is odd, so splitting into two exactly
        # balanced 9/9 halves (Kernighan-Lin's invariant for even N) forces at
        # least one pair to be broken across the halves, by parity -- this is
        # guaranteed regardless of seed. That gives node_set1 a real isolated
        # node with 4 *other* intact edges (8 real nodes) alongside it, so
        # negative sampling has plenty of room among the real nodes alone.
        k = 9
        proteins = [make_protein(f"P{i}", accession=f"ACC{i}") for i in range(2 * k)]
        for i in range(k):
            make_interaction(proteins[2 * i], proteins[2 * i + 1])

        for seed in range(1, 21):
            params = SplitParams(seed=seed)
            ep = EdgePartition(params)
            ep.generate_splits()
            diag = []
            safe = True
            for ns in ep.node_sets:
                sub = ep.interaction_graph.subgraph(ns)
                zero = [node for node, d in sub.degree() if d == 0]
                real_nodes = [n for n in ns if n not in zero]
                real_sub = ep.interaction_graph.subgraph(real_nodes)
                n_real = len(real_nodes)
                real_edges = real_sub.number_of_edges()
                real_non_edges = n_real * (n_real - 1) // 2 - real_edges
                diag.append((len(ns), sub.number_of_edges(), len(zero), real_non_edges))
                if zero and real_non_edges < sub.number_of_edges() * 2:
                    safe = False
            has_zero = any(d[2] > 0 for d in diag)
            if not has_zero or not safe:
                continue
            print(f"seed={seed} diag(size,edges,n_zero,real_non_edges)={diag}")
            with tempfile.TemporaryDirectory() as td:
                try:
                    summary = generate_splits(params, Path(td), lambda step, frac: None)
                except (ValueError, AssertionError) as e:
                    print(f"seed={seed} -> FAILED: {e}")
                    continue
            pre_prune_total = ep.interaction_graph.number_of_nodes()
            post_prune_total = sum(s["n_proteins"] for s in summary.splits)
            print(
                f"seed={seed} SUCCESS summary.n_proteins={summary.n_proteins} "
                f"pre_prune_total={pre_prune_total} post_prune_total={post_prune_total} "
                f"n_discarded_nodes={summary.n_discarded_nodes} "
                f"splits_n_proteins={[s['n_proteins'] for s in summary.splits]}"
            )
