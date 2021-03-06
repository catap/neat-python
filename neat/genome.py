from operator import mul
from functools import reduce

from neat.config import ConfigParameter, write_pretty_params
from neat.genes import DefaultConnectionGene, DefaultNodeGene
from neat.six_util import iteritems, iterkeys

from neat.activations import ActivationFunctionSet
from neat.graphs import creates_cycle

from random import choice, random, shuffle


def product(x):
    return reduce(mul, x, 1.0)


class DefaultGenomeConfig(object):
    __params = [ConfigParameter('num_inputs', int),
                ConfigParameter('num_outputs', int),
                ConfigParameter('num_hidden', int),
                ConfigParameter('feed_forward', bool),
                ConfigParameter('compatibility_disjoint_coefficient', float),
                ConfigParameter('compatibility_weight_coefficient', float),
                ConfigParameter('conn_add_prob', float),
                ConfigParameter('conn_delete_prob', float),
                ConfigParameter('node_add_prob', float),
                ConfigParameter('node_delete_prob', float)]

    allowed_connectivity = ['unconnected', 'fs_neat', 'full', 'partial']
    aggregation_function_defs = {'sum': sum, 'max': max, 'min': min, 'product':product}

    def __init__(self, params):
        # Create full set of available activation functions.
        self.activation_defs = ActivationFunctionSet()
        self.activation_options = params.get('activation_options', 'sigmoid').strip().split()
        self.aggregation_options = params.get('aggregation_options', 'sum').strip().split()

        # Gather configuration data from the gene classes.
        self.__params += DefaultNodeGene.get_config_params()
        self.__params += DefaultConnectionGene.get_config_params()

        # Use the configuration data to interpret the supplied parameters.
        for p in self.__params:
            setattr(self, p.name, p.interpret(params))

        # By convention, input pins have negative keys, and the output
        # pins have keys 0,1,...
        self.input_keys = [-i - 1 for i in range(self.num_inputs)]
        self.output_keys = [i for i in range(self.num_outputs)]

        self.connection_fraction = None

        # Verify that initial connection type is valid.
        self.initial_connection = params.get('initial_connection', 'unconnected')
        if 'partial' in self.initial_connection:
            c, p = self.initial_connection.split()
            self.initial_connection = c
            self.connection_fraction = float(p)
            if not (0 <= self.connection_fraction <= 1):
                raise Exception("'partial' connection value must be between 0.0 and 1.0, inclusive.")

        assert self.initial_connection in self.allowed_connectivity

    def add_activation(self, name, func):
        self.activation_defs.add(name, func)

    def save(self, f):
        f.write('initial_connection      = {0}\n'.format(self.initial_connection))
        # Verify that initial connection type is valid.
        if 'partial' in self.initial_connection:
            c, p = self.initial_connection.split()
            self.initial_connection = c
            self.connection_fraction = float(p)
            if not (0 <= self.connection_fraction <= 1):
                raise Exception("'partial' connection value must be between 0.0 and 1.0, inclusive.")

        assert self.initial_connection in self.allowed_connectivity

        write_pretty_params(f, self, self.__params)


class DefaultGenome(object):
    """
    A genome for generalized neural networks.

    Terminology
        pin: Point at which the network is conceptually connected to the external world;
             pins are either input or output.
        node:
        connection:
        key: Identifier for an object, unique within the set of similar objects.

    Design assumptions and conventions.
        1. Each output pin is connected only to the output of its own unique
           neuron by an implicit connection with weight one. This connection
           is permanently enabled.
        2. The output pin's key is always the same as the key for its
           associated neuron.
        3. Output neurons can be modified but not deleted.
        4. The input values are applied to the input pins unmodified.
    """

    @classmethod
    def parse_config(cls, param_dict):
        return DefaultGenomeConfig(param_dict)

    @classmethod
    def write_config(cls, f, config):
        config.save(f)

    def __init__(self, key, config):
        """
        :param key: This genome's unique identifier.
        :param config: A neat.config.Config instance.
        """
        self.key = key

        # (gene_key, gene) pairs for gene sets.
        self.connections = {}
        self.nodes = {}

        # Fitness results.
        # TODO: This should probably be stored elsewhere.
        self.fitness = None
        self.cross_fitness = None

    def mutate(self, config):
        """ Mutates this genome. """

        # TODO: Make a configuration item to choose whether or not multiple
        # mutations can happen simultaneously.
        if random() < config.node_add_prob:
            self.mutate_add_node(config)

        if random() < config.node_delete_prob:
            self.mutate_delete_node(config)

        if random() < config.conn_add_prob:
            self.mutate_add_connection(config)

        if random() < config.conn_delete_prob:
            self.mutate_delete_connection()

        # Mutate connection genes.
        for cg in self.connections.values():
            cg.mutate(config)

        # Mutate node genes (bias, response, etc.).
        for ng in self.nodes.values():
            ng.mutate(config)

    def crossover(self, other, key, config):
        """ Crosses over parents' genomes and returns a child. """
        if self.fitness > other.fitness:
            parent1 = self
            parent2 = other
        else:
            parent1 = other
            parent2 = self

        # creates a new child
        child = DefaultGenome(key, config)
        child.inherit_genes(parent1, parent2)

        return child

    def inherit_genes(self, parent1, parent2):
        """ Applies the crossover operator. """
        assert (parent1.fitness >= parent2.fitness)

        # Inherit connection genes
        for key, cg1 in iteritems(parent1.connections):
            cg2 = parent2.connections.get(key)
            if cg2 is None:
                # Excess or disjoint gene: copy from the fittest parent.
                self.connections[key] = cg1.copy()
            else:
                # Homologous gene: combine genes from both parents.
                self.connections[key] = cg1.crossover(cg2)

        # Inherit node genes
        parent1_set = parent1.nodes
        parent2_set = parent2.nodes

        for key, ng1 in iteritems(parent1_set):
            ng2 = parent2_set.get(key)
            assert key not in self.nodes
            if ng2 is None:
                # Extra gene: copy from the fittest parent
                self.nodes[key] = ng1.copy()
            else:
                # Homologous gene: combine genes from both parents.
                self.nodes[key] = ng1.crossover(ng2)

    def get_new_node_key(self):
        new_id = 0
        while new_id in self.nodes:
            new_id += 1
        return new_id

    def mutate_add_node(self, config):
        if not self.connections:
            return None, None

        # Choose a random connection to split
        conn_to_split = choice(list(self.connections.values()))
        new_node_id = self.get_new_node_key()
        ng = self.create_node(config, new_node_id)
        self.nodes[new_node_id] = ng

        # Disable this connection and create two new connections joining its nodes via
        # the given node.  The new node+connections have roughly the same behavior as
        # the original connection (depending on the activation function of the new node).
        conn_to_split.enabled = False

        i, o = conn_to_split.key
        self.add_connection(config, i, new_node_id, 1.0, True)
        self.add_connection(config, new_node_id, o, conn_to_split.weight, True)

    def add_connection(self, config, input_key, output_key, weight, enabled):
        # TODO: Add validation of this connection addition.
        key = (input_key, output_key)
        connection = DefaultConnectionGene(key)
        connection.init_attributes(config)
        connection.weight = weight
        connection.enabled = enabled
        self.connections[key] = connection

    def mutate_add_connection(self, config):
        '''
        Attempt to add a new connection, the only restriction being that the output
        node cannot be one of the network input pins.
        '''
        possible_outputs = list(iterkeys(self.nodes))
        out_node = choice(possible_outputs)

        possible_inputs = possible_outputs + config.input_keys
        in_node = choice(possible_inputs)

        # Don't duplicate connections.
        key = (in_node, out_node)
        if key in self.connections:
            return

        # For feed-forward networks, avoid creating cycles.
        if config.feed_forward and creates_cycle(list(iterkeys(self.connections)), key):
            return

        cg = self.create_connection(config, in_node, out_node)
        self.connections[cg.key] = cg

    def mutate_delete_node(self, config):
        # Do nothing if there are no non-output nodes.
        available_nodes = [(k, v) for k, v in iteritems(self.nodes) if k not in config.output_keys]
        if not available_nodes:
            return -1

        del_key, del_node = choice(available_nodes)

        connections_to_delete = set()
        for k, v in iteritems(self.connections):
            if del_key in v.key:
                connections_to_delete.add(v.key)

        for key in connections_to_delete:
            del self.connections[key]

        del self.nodes[del_key]

        return del_key

    def mutate_delete_connection(self):
        if self.connections:
            key = choice(list(self.connections.keys()))
            del self.connections[key]

    def distance(self, other, config):
        """
        Returns the genetic distance between this genome and the other. This distance value
        is used to compute genome compatibility for speciation.
        """

        # Compute node gene distance component.
        node_distance = 0.0
        if self.nodes or other.nodes:
            disjoint_nodes = 0
            for k2 in iterkeys(other.nodes):
                if k2 not in self.nodes:
                    disjoint_nodes += 1

            for k1, n1 in iteritems(self.nodes):
                n2 = other.nodes.get(k1)
                if n2 is None:
                    disjoint_nodes += 1
                else:
                    # Homologous genes compute their own distance value.
                    node_distance += n1.distance(n2, config)

            max_nodes = max(len(self.nodes), len(other.nodes))
            node_distance = (node_distance + config.compatibility_disjoint_coefficient * disjoint_nodes) / max_nodes

        # Compute connection gene differences.
        connection_distance = 0.0
        if self.connections or other.connections:
            disjoint_connections = 0
            for k2 in iterkeys(other.connections):
                if k2 not in self.connections:
                    disjoint_connections += 1

            for k1, c1 in iteritems(self.connections):
                c2 = other.connections.get(k1)
                if c2 is None:
                    disjoint_connections += 1
                else:
                    # Homologous genes compute their own distance value.
                    connection_distance += c1.distance(c2, config)

            max_conn = max(len(self.connections), len(other.connections))
            connection_distance = (connection_distance + config.compatibility_disjoint_coefficient * disjoint_connections) / max_conn

        distance = node_distance + connection_distance
        return distance

    def size(self):
        '''Returns genome 'complexity', taken to be (number of nodes, number of enabled connections)'''
        num_enabled_connections = sum([1 for cg in self.connections.values() if cg.enabled is True])
        return len(self.nodes), num_enabled_connections

    def __str__(self):
        s = "Nodes:"
        for k, ng in iteritems(self.nodes):
            s += "\n\t{0} {1!s}".format(k, ng)
        s += "\nConnections:"
        connections = list(self.connections.values())
        connections.sort()
        for c in connections:
            s += "\n\t" + str(c)
        return s

    def add_hidden_nodes(self, config):
        for i in range(config.num_hidden):
            node_key = self.get_new_node_key()
            assert node_key not in self.nodes
            node = self.__class__.create_node(config, node_key)
            self.nodes[node_key] = node

    @classmethod
    def create(cls, config, key):
        g = cls.create_unconnected(config, key)

        # Add hidden nodes if requested.
        if config.num_hidden > 0:
            g.add_hidden_nodes(config)

        # Add connections based on initial connectivity type.
        if config.initial_connection == 'fs_neat':
            g.connect_fs_neat()
        elif config.initial_connection == 'full':
            g.connect_full(config)
        elif config.initial_connection == 'partial':
            g.connect_partial(config)

        return g

    @staticmethod
    def create_node(config, node_id):
        node = DefaultNodeGene(node_id)
        node.init_attributes(config)
        return node

    @staticmethod
    def create_connection(config, input_id, output_id):
        connection = DefaultConnectionGene((input_id, output_id))
        connection.init_attributes(config)
        return connection

    @classmethod
    def create_unconnected(cls, config, key):
        '''Create a genome for a network with no hidden nodes and no connections.'''
        c = cls(key, config)

        # Create node genes for the output pins.
        for node_key in config.output_keys:
            c.nodes[node_key] = cls.create_node(config, node_key)

        return c

    def connect_fs_neat(self, config):
        """ Randomly connect one input to all hidden and output nodes (FS-NEAT). """
        input_id = choice(config.input_keys)
        for output_id in list(self.hidden.keys()) + list(self.outputs.keys()):
            connection = self.create_connection(config, input_id, output_id)
            self.connections[connection.key] = connection

    def compute_full_connections(self, config):
        """ Compute connections for a fully-connected feed-forward genome (each input connected to all nodes). """
        connections = []
        for input_id in config.input_keys:
            for node_id in iterkeys(self.nodes):
                connections.append((input_id, node_id))

        return connections

    def connect_full(self, config):
        """ Create a fully-connected genome. """
        for input_id, output_id in self.compute_full_connections(config):
            connection = self.create_connection(config, input_id, output_id)
            self.connections[connection.key] = connection

    def connect_partial(self, config):
        assert 0 <= config.connection_fraction <= 1
        all_connections = self.compute_full_connections(config)
        shuffle(all_connections)
        num_to_add = int(round(len(all_connections) * config.connection_fraction))
        for input_id, output_id in all_connections[:num_to_add]:
            connection = self.create_connection(config, input_id, output_id)
            self.connections[connection.key] = connection
