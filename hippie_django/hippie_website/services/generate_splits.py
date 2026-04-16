import networkx as nx
import numpy as np
seed = 78539105873

class EdgePartition():
    def __init__(self):
        self.interaction_graph = get_interaction_graph() # NOTE: method will be implemented
        self.non_interaction_graph = get_non_interaction_graph() # NOTE: method will be implemented
        self.node_sets = []
        self.discarded_edges = []
        self.discarded_nodes = []
        self.selected_sets = []
        
    @staticmethod  
    def single_split(graph):
        A, B = nx.kernighan_lin_bisection(
            graph, seed=seed,
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

        positive_edge_tuples = [sorted((u, v)) for u,v in c_subgraph.edges()]
        selected_edges = []
        tries_until_failure = 1000
        tries = 0

        while n_selected_edges < n_edges:
            tries += 1
            random_edge = sorted((self._get_random_node(node_idx, cumulative_deg, int(cumulative_deg[-1])) for _ in range(2))) # sorted as internal ids are always A > B
            if random_edge[0] != random_edge[1] and random_edge not in selected_edges and random_edge not in positive_edge_tuples:
                selected_edges.append(random_edge)
                n_selected_edges += 1
                tries = 0
            if tries > tries_until_failure:
                raise ValueError(f"No random edge selected in {tries_until_failure}, aborting.")
        
        g_negative = nx.Graph()
        g_negative.add_edges_from(selected_edges)
        
        return c_subgraph, g_negative
    
    def generate_balanced_complement_negative_splits(self):
        self.generate_splits()
        
        for nodes_split in self.node_sets:
            self.selected_sets.append(
                self.get_random_balanced_negative_complement(nodes_split)
            )
        self.drop_exclusive_nodes()
        self.set_discarded_edges()
        
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
        
    