import collections

import numpy as np
import six
import tensorflow as tf

from deepchem.data import NumpyDataset
from deepchem.feat.graph_features import ConvMolFeaturizer
from deepchem.feat.mol_graphs import ConvMol
from deepchem.metrics import to_one_hot
from deepchem.models.tensorgraph.graph_layers import WeaveGather, \
    DTNNEmbedding, DTNNStep, DTNNGather, DAGLayer, \
    DAGGather, DTNNExtract, MessagePassing, SetGather
from deepchem.models.tensorgraph.graph_layers import WeaveLayerFactory
from deepchem.models.tensorgraph.layers import Layer, Dense, SoftMax, Reshape, \
    SoftMaxCrossEntropy, GraphConv, BatchNorm, Exp, ReduceMean, ReduceSum, \
    GraphPool, GraphGather, WeightedError, Dropout, BatchNorm, Stack, Flatten, GraphCNN, GraphCNNPool
from deepchem.models.tensorgraph.layers import L2Loss, Label, Weights, Feature
from deepchem.models.tensorgraph.tensor_graph import TensorGraph
from deepchem.trans import undo_transforms


class TrimGraphOutput(Layer):
  """Trim the output to the correct number of samples.

  GraphGather always outputs fixed size batches.  This layer trims the output
  to the number of samples that were in the actual input tensors.
  """

  def __init__(self, in_layers, **kwargs):
    super(TrimGraphOutput, self).__init__(in_layers, **kwargs)
    try:
      s = list(self.in_layers[0].shape)
      s[0] = None
      self._shape = tuple(s)
    except:
      pass

  def create_tensor(self, in_layers=None, set_tensors=True, **kwargs):
    inputs = self._get_input_tensors(in_layers)
    n_samples = tf.shape(inputs[1])[0]
    out_tensor = inputs[0][0:n_samples]
    if set_tensors:
      self.out_tensor = out_tensor
    return out_tensor


class WeaveModel(TensorGraph):

  def __init__(self,
               n_tasks,
               n_atom_feat=75,
               n_pair_feat=14,
               n_hidden=50,
               n_graph_feat=128,
               mode="classification",
               **kwargs):
    """
            Parameters
            ----------
            n_tasks: int
              Number of tasks
            n_atom_feat: int, optional
              Number of features per atom.
            n_pair_feat: int, optional
              Number of features per pair of atoms.
            n_hidden: int, optional
              Number of units(convolution depths) in corresponding hidden layer
            n_graph_feat: int, optional
              Number of output features for each molecule(graph)
            mode: str
              Either "classification" or "regression" for type of model.
            """
    self.n_tasks = n_tasks
    self.n_atom_feat = n_atom_feat
    self.n_pair_feat = n_pair_feat
    self.n_hidden = n_hidden
    self.n_graph_feat = n_graph_feat
    self.mode = mode
    super(WeaveModel, self).__init__(**kwargs)
    self.build_graph()

  def build_graph(self):
    """Building graph structures:
                Features => WeaveLayer => WeaveLayer => Dense => WeaveGather => Classification or Regression
                """
    self.atom_features = Feature(shape=(None, self.n_atom_feat))
    self.pair_features = Feature(shape=(None, self.n_pair_feat))
    self.pair_split = Feature(shape=(None,), dtype=tf.int32)
    self.atom_split = Feature(shape=(None,), dtype=tf.int32)
    self.atom_to_pair = Feature(shape=(None, 2), dtype=tf.int32)
    weave_layer1A, weave_layer1P = WeaveLayerFactory(
        n_atom_input_feat=self.n_atom_feat,
        n_pair_input_feat=self.n_pair_feat,
        n_atom_output_feat=self.n_hidden,
        n_pair_output_feat=self.n_hidden,
        in_layers=[
            self.atom_features, self.pair_features, self.pair_split,
            self.atom_to_pair
        ])
    weave_layer2A, weave_layer2P = WeaveLayerFactory(
        n_atom_input_feat=self.n_hidden,
        n_pair_input_feat=self.n_hidden,
        n_atom_output_feat=self.n_hidden,
        n_pair_output_feat=self.n_hidden,
        update_pair=False,
        in_layers=[
            weave_layer1A, weave_layer1P, self.pair_split, self.atom_to_pair
        ])
    dense1 = Dense(
        out_channels=self.n_graph_feat,
        activation_fn=tf.nn.tanh,
        in_layers=weave_layer2A)
    batch_norm1 = BatchNorm(epsilon=1e-5, in_layers=[dense1])
    weave_gather = WeaveGather(
        self.batch_size,
        n_input=self.n_graph_feat,
        gaussian_expand=True,
        in_layers=[batch_norm1, self.atom_split])

    costs = []
    self.labels_fd = []
    for task in range(self.n_tasks):
      if self.mode == "classification":
        classification = Dense(
            out_channels=2, activation_fn=None, in_layers=[weave_gather])
        softmax = SoftMax(in_layers=[classification])
        self.add_output(softmax)

        label = Label(shape=(None, 2))
        self.labels_fd.append(label)
        cost = SoftMaxCrossEntropy(in_layers=[label, classification])
        costs.append(cost)
      if self.mode == "regression":
        regression = Dense(
            out_channels=1, activation_fn=None, in_layers=[weave_gather])
        self.add_output(regression)

        label = Label(shape=(None, 1))
        self.labels_fd.append(label)
        cost = L2Loss(in_layers=[label, regression])
        costs.append(cost)
    if self.mode == "classification":
      all_cost = Stack(in_layers=costs, axis=1)
    elif self.mode == "regression":
      all_cost = Stack(in_layers=costs, axis=1)
    self.weights = Weights(shape=(None, self.n_tasks))
    loss = WeightedError(in_layers=[all_cost, self.weights])
    self.set_loss(loss)

  def default_generator(self,
                        dataset,
                        epochs=1,
                        predict=False,
                        deterministic=True,
                        pad_batches=True):
    """TensorGraph style implementation """
    for epoch in range(epochs):
      if not predict:
        print('Starting epoch %i' % epoch)
      for (X_b, y_b, w_b, ids_b) in dataset.iterbatches(
          batch_size=self.batch_size,
          deterministic=deterministic,
          pad_batches=pad_batches):

        feed_dict = dict()
        if y_b is not None:
          for index, label in enumerate(self.labels_fd):
            if self.mode == "classification":
              feed_dict[label] = to_one_hot(y_b[:, index])
            if self.mode == "regression":
              feed_dict[label] = y_b[:, index:index + 1]
        if w_b is not None:
          feed_dict[self.weights] = w_b

        atom_feat = []
        pair_feat = []
        atom_split = []
        atom_to_pair = []
        pair_split = []
        start = 0
        for im, mol in enumerate(X_b):
          n_atoms = mol.get_num_atoms()
          # number of atoms in each molecule
          atom_split.extend([im] * n_atoms)
          # index of pair features
          C0, C1 = np.meshgrid(np.arange(n_atoms), np.arange(n_atoms))
          atom_to_pair.append(
              np.transpose(
                  np.array([C1.flatten() + start,
                            C0.flatten() + start])))
          # number of pairs for each atom
          pair_split.extend(C1.flatten() + start)
          start = start + n_atoms

          # atom features
          atom_feat.append(mol.get_atom_features())
          # pair features
          pair_feat.append(
              np.reshape(mol.get_pair_features(),
                         (n_atoms * n_atoms, self.n_pair_feat)))

        feed_dict[self.atom_features] = np.concatenate(atom_feat, axis=0)
        feed_dict[self.pair_features] = np.concatenate(pair_feat, axis=0)
        feed_dict[self.pair_split] = np.array(pair_split)
        feed_dict[self.atom_split] = np.array(atom_split)
        feed_dict[self.atom_to_pair] = np.concatenate(atom_to_pair, axis=0)
        yield feed_dict

  def predict_on_generator(self, generator, transformers=[], outputs=None):
    out = super(WeaveModel, self).predict_on_generator(
        generator, transformers=[], outputs=outputs)
    if outputs is None:
      outputs = self.outputs
    if len(outputs) > 1:
      out = np.stack(out, axis=1)

    out = undo_transforms(out, transformers)
    return out


class DTNNModel(TensorGraph):

  def __init__(self,
               n_tasks,
               n_embedding=30,
               n_hidden=100,
               n_distance=100,
               distance_min=-1,
               distance_max=18,
               output_activation=True,
               mode="regression",
               **kwargs):
    """
            Parameters
            ----------
            n_tasks: int
              Number of tasks
            n_embedding: int, optional
              Number of features per atom.
            n_hidden: int, optional
              Number of features for each molecule after DTNNStep
            n_distance: int, optional
              granularity of distance matrix
              step size will be (distance_max-distance_min)/n_distance
            distance_min: float, optional
              minimum distance of atom pairs, default = -1 Angstorm
            distance_max: float, optional
              maximum distance of atom pairs, default = 18 Angstorm
            mode: str
              Either "classification" or "regression" for type of model.
            """
    self.n_tasks = n_tasks
    self.n_embedding = n_embedding
    self.n_hidden = n_hidden
    self.n_distance = n_distance
    self.distance_min = distance_min
    self.distance_max = distance_max
    self.step_size = (distance_max - distance_min) / n_distance
    self.steps = np.array(
        [distance_min + i * self.step_size for i in range(n_distance)])
    self.steps = np.expand_dims(self.steps, 0)
    self.output_activation = output_activation
    self.mode = mode
    super(DTNNModel, self).__init__(**kwargs)
    assert self.mode == "regression"
    self.build_graph()

  def build_graph(self):
    """Building graph structures:
            Features => DTNNEmbedding => DTNNStep => DTNNStep => DTNNGather => Regression
            """
    self.atom_number = Feature(shape=(None,), dtype=tf.int32)
    self.distance = Feature(shape=(None, self.n_distance))
    self.atom_membership = Feature(shape=(None,), dtype=tf.int32)
    self.distance_membership_i = Feature(shape=(None,), dtype=tf.int32)
    self.distance_membership_j = Feature(shape=(None,), dtype=tf.int32)

    dtnn_embedding = DTNNEmbedding(
        n_embedding=self.n_embedding, in_layers=[self.atom_number])
    dtnn_layer1 = DTNNStep(
        n_embedding=self.n_embedding,
        n_distance=self.n_distance,
        in_layers=[
            dtnn_embedding, self.distance, self.distance_membership_i,
            self.distance_membership_j
        ])
    dtnn_layer2 = DTNNStep(
        n_embedding=self.n_embedding,
        n_distance=self.n_distance,
        in_layers=[
            dtnn_layer1, self.distance, self.distance_membership_i,
            self.distance_membership_j
        ])
    dtnn_gather = DTNNGather(
        n_embedding=self.n_embedding,
        layer_sizes=[self.n_hidden],
        n_outputs=self.n_tasks,
        output_activation=self.output_activation,
        in_layers=[dtnn_layer2, self.atom_membership])

    costs = []
    self.labels_fd = []
    for task in range(self.n_tasks):
      regression = DTNNExtract(task, in_layers=[dtnn_gather])
      self.add_output(regression)
      label = Label(shape=(None, 1))
      self.labels_fd.append(label)
      cost = L2Loss(in_layers=[label, regression])
      costs.append(cost)

    all_cost = Stack(in_layers=costs, axis=1)
    self.weights = Weights(shape=(None, self.n_tasks))
    loss = WeightedError(in_layers=[all_cost, self.weights])
    self.set_loss(loss)

  def default_generator(self,
                        dataset,
                        epochs=1,
                        predict=False,
                        deterministic=True,
                        pad_batches=True):
    """TensorGraph style implementation"""
    for epoch in range(epochs):
      if not predict:
        print('Starting epoch %i' % epoch)
      for (X_b, y_b, w_b, ids_b) in dataset.iterbatches(
          batch_size=self.batch_size,
          deterministic=deterministic,
          pad_batches=pad_batches):

        feed_dict = dict()
        if y_b is not None:
          for index, label in enumerate(self.labels_fd):
            feed_dict[label] = y_b[:, index:index + 1]
        if w_b is not None:
          feed_dict[self.weights] = w_b
        distance = []
        atom_membership = []
        distance_membership_i = []
        distance_membership_j = []
        num_atoms = list(map(sum, X_b.astype(bool)[:, :, 0]))
        atom_number = [
            np.round(
                np.power(2 * np.diag(X_b[i, :num_atoms[i], :num_atoms[i]]),
                         1 / 2.4)).astype(int) for i in range(len(num_atoms))
        ]
        start = 0
        for im, molecule in enumerate(atom_number):
          distance_matrix = np.outer(
              molecule, molecule) / X_b[im, :num_atoms[im], :num_atoms[im]]
          np.fill_diagonal(distance_matrix, -100)
          distance.append(np.expand_dims(distance_matrix.flatten(), 1))
          atom_membership.append([im] * num_atoms[im])
          membership = np.array([np.arange(num_atoms[im])] * num_atoms[im])
          membership_i = membership.flatten(order='F')
          membership_j = membership.flatten()
          distance_membership_i.append(membership_i + start)
          distance_membership_j.append(membership_j + start)
          start = start + num_atoms[im]
        feed_dict[self.atom_number] = np.concatenate(atom_number)
        distance = np.concatenate(distance, 0)
        feed_dict[self.distance] = np.exp(-np.square(distance - self.steps) /
                                          (2 * self.step_size**2))
        feed_dict[self.distance_membership_i] = np.concatenate(
            distance_membership_i)
        feed_dict[self.distance_membership_j] = np.concatenate(
            distance_membership_j)
        feed_dict[self.atom_membership] = np.concatenate(atom_membership)

        yield feed_dict

  def predict(self, dataset, transformers=[], outputs=None):
    if outputs is None:
      outputs = self.outputs
    if transformers != [] and not isinstance(outputs, collections.Sequence):
      raise ValueError(
          "DTNN does not support single tensor output with transformers")
    retval = super(DTNNModel, self).predict(dataset, outputs=outputs)
    if not isinstance(outputs, collections.Sequence):
      return retval
    retval = np.concatenate(retval, axis=-1)
    return undo_transforms(retval, transformers)


class DAGModel(TensorGraph):

  def __init__(self,
               n_tasks,
               max_atoms=50,
               n_atom_feat=75,
               n_graph_feat=30,
               n_outputs=30,
               layer_sizes=[100],
               layer_sizes_gather=[100],
               dropout=None,
               mode="classification",
               **kwargs):
    """
            Parameters
            ----------
            n_tasks: int
              Number of tasks.
            max_atoms: int, optional
              Maximum number of atoms in a molecule, should be defined based on dataset.
            n_atom_feat: int, optional
              Number of features per atom.
            n_graph_feat: int, optional
              Number of features for atom in the graph.
            n_outputs: int, optional
              Number of features for each molecule.
            layer_sizes: list of int, optional
              List of hidden layer size(s) in the propagation step:
              length of this list represents the number of hidden layers,
              and each element is the width of corresponding hidden layer.
            layer_sizes_gather: list of int, optional
              List of hidden layer size(s) in the gather step.
            dropout: None or float, optional
              Dropout probability, applied after each propagation step and gather step.
            mode: str, optional
              Either "classification" or "regression" for type of model.
            """
    self.n_tasks = n_tasks
    self.max_atoms = max_atoms
    self.n_atom_feat = n_atom_feat
    self.n_graph_feat = n_graph_feat
    self.n_outputs = n_outputs
    self.layer_sizes = layer_sizes
    self.layer_sizes_gather = layer_sizes_gather
    self.dropout = dropout
    self.mode = mode
    super(DAGModel, self).__init__(**kwargs)
    self.build_graph()

  def build_graph(self):
    """Building graph structures:
                Features => DAGLayer => DAGGather => Classification or Regression
                """
    self.atom_features = Feature(shape=(None, self.n_atom_feat))
    self.parents = Feature(
        shape=(None, self.max_atoms, self.max_atoms), dtype=tf.int32)
    self.calculation_orders = Feature(
        shape=(None, self.max_atoms), dtype=tf.int32)
    self.calculation_masks = Feature(
        shape=(None, self.max_atoms), dtype=tf.bool)
    self.membership = Feature(shape=(None,), dtype=tf.int32)
    self.n_atoms = Feature(shape=(), dtype=tf.int32)
    dag_layer1 = DAGLayer(
        n_graph_feat=self.n_graph_feat,
        n_atom_feat=self.n_atom_feat,
        max_atoms=self.max_atoms,
        layer_sizes=self.layer_sizes,
        dropout=self.dropout,
        batch_size=self.batch_size,
        in_layers=[
            self.atom_features, self.parents, self.calculation_orders,
            self.calculation_masks, self.n_atoms
        ])
    dag_gather = DAGGather(
        n_graph_feat=self.n_graph_feat,
        n_outputs=self.n_outputs,
        max_atoms=self.max_atoms,
        layer_sizes=self.layer_sizes_gather,
        dropout=self.dropout,
        in_layers=[dag_layer1, self.membership])

    costs = []
    self.labels_fd = []
    for task in range(self.n_tasks):
      if self.mode == "classification":
        classification = Dense(
            out_channels=2, activation_fn=None, in_layers=[dag_gather])
        softmax = SoftMax(in_layers=[classification])
        self.add_output(softmax)

        label = Label(shape=(None, 2))
        self.labels_fd.append(label)
        cost = SoftMaxCrossEntropy(in_layers=[label, classification])
        costs.append(cost)
      if self.mode == "regression":
        regression = Dense(
            out_channels=1, activation_fn=None, in_layers=[dag_gather])
        self.add_output(regression)

        label = Label(shape=(None, 1))
        self.labels_fd.append(label)
        cost = L2Loss(in_layers=[label, regression])
        costs.append(cost)
    if self.mode == "classification":
      all_cost = Stack(in_layers=costs, axis=1)
    elif self.mode == "regression":
      all_cost = Stack(in_layers=costs, axis=1)
    self.weights = Weights(shape=(None, self.n_tasks))
    loss = WeightedError(in_layers=[all_cost, self.weights])
    self.set_loss(loss)

  def default_generator(self,
                        dataset,
                        epochs=1,
                        predict=False,
                        deterministic=True,
                        pad_batches=True):
    """TensorGraph style implementation"""
    for epoch in range(epochs):
      if not predict:
        print('Starting epoch %i' % epoch)
      for (X_b, y_b, w_b, ids_b) in dataset.iterbatches(
          batch_size=self.batch_size,
          deterministic=deterministic,
          pad_batches=pad_batches):

        feed_dict = dict()
        if y_b is not None:
          for index, label in enumerate(self.labels_fd):
            if self.mode == "classification":
              feed_dict[label] = to_one_hot(y_b[:, index])
            if self.mode == "regression":
              feed_dict[label] = y_b[:, index:index + 1]
        if w_b is not None:
          feed_dict[self.weights] = w_b

        atoms_per_mol = [mol.get_num_atoms() for mol in X_b]
        n_atoms = sum(atoms_per_mol)
        start_index = [0] + list(np.cumsum(atoms_per_mol)[:-1])

        atoms_all = []
        # calculation orders for a batch of molecules
        parents_all = []
        calculation_orders = []
        calculation_masks = []
        membership = []
        for idm, mol in enumerate(X_b):
          # padding atom features vector of each molecule with 0
          atoms_all.append(mol.get_atom_features())
          parents = mol.parents
          parents_all.extend(parents)
          calculation_index = np.array(parents)[:, :, 0]
          mask = np.array(calculation_index - self.max_atoms, dtype=bool)
          calculation_orders.append(calculation_index + start_index[idm])
          calculation_masks.append(mask)
          membership.extend([idm] * atoms_per_mol[idm])

        feed_dict[self.atom_features] = np.concatenate(atoms_all, axis=0)
        feed_dict[self.parents] = np.stack(parents_all, axis=0)
        feed_dict[self.calculation_orders] = np.concatenate(
            calculation_orders, axis=0)
        feed_dict[self.calculation_masks] = np.concatenate(
            calculation_masks, axis=0)
        feed_dict[self.membership] = np.array(membership)
        feed_dict[self.n_atoms] = n_atoms
        yield feed_dict

  def predict_on_generator(self, generator, transformers=[], outputs=None):
    out = super(DAGModel, self).predict_on_generator(
        generator, transformers=[], outputs=outputs)
    if outputs is None:
      outputs = self.outputs
    if len(outputs) > 1:
      out = np.stack(out, axis=1)

    out = undo_transforms(out, transformers)
    return out


class GraphConvModel(TensorGraph):

  def __init__(self,
               n_tasks,
               graph_conv_layers=[64, 64],
               dense_layer_size=128,
               dropout=0.0,
               mode="classification",
               number_atom_features=75,
               n_classes=2,
               uncertainty=False,
               **kwargs):
    """
    Parameters
    ----------
    n_tasks: int
      Number of tasks
    graph_conv_layers: list of int
      Width of channels for the Graph Convolution Layers
    dense_layer_size: int
      Width of channels for Atom Level Dense Layer before GraphPool
    dropout: float
      Droupout dropout probability.  Dropout is applied after the per Atom Level Dense Layer
    mode: str
      Either "classification" or "regression"
    number_atom_features: int
        75 is the default number of atom features created, but
        this can vary if various options are passed to the
        function atom_features in graph_features
    n_classes: int
      the number of classes to predict (only used in classification mode)
    uncertainty: bool
      if True, include extra outputs and loss terms to enable the uncertainty
      in outputs to be predicted
    """
    if mode not in ['classification', 'regression']:
      raise ValueError("mode must be either 'classification' or 'regression'")
    self.n_tasks = n_tasks
    self.mode = mode
    self.dense_layer_size = dense_layer_size
    self.dropout = dropout
    self.graph_conv_layers = graph_conv_layers
    kwargs['use_queue'] = False
    self.number_atom_features = number_atom_features
    self.n_classes = n_classes
    self.uncertainty = uncertainty
    if uncertainty:
      if mode != "regression":
        raise ValueError("Uncertainty is only supported in regression mode")
      if dropout == 0.0:
        raise ValueError('Dropout must be included to predict uncertainty')
    super(GraphConvModel, self).__init__(**kwargs)
    self.build_graph()

  def build_graph(self):
    """
    Building graph structures:
    """
    self.atom_features = Feature(shape=(None, self.number_atom_features))
    self.degree_slice = Feature(shape=(None, 2), dtype=tf.int32)
    self.membership = Feature(shape=(None,), dtype=tf.int32)

    self.deg_adjs = []
    for i in range(0, 10 + 1):
      deg_adj = Feature(shape=(None, i + 1), dtype=tf.int32)
      self.deg_adjs.append(deg_adj)
    in_layer = self.atom_features
    for layer_size in self.graph_conv_layers:
      gc1_in = [in_layer, self.degree_slice, self.membership] + self.deg_adjs
      gc1 = GraphConv(layer_size, activation_fn=tf.nn.relu, in_layers=gc1_in)
      batch_norm1 = BatchNorm(in_layers=[gc1])
      if self.dropout > 0.0:
        batch_norm1 = Dropout(self.dropout, in_layers=batch_norm1)
      gp_in = [batch_norm1, self.degree_slice, self.membership] + self.deg_adjs
      in_layer = GraphPool(in_layers=gp_in)
    dense = Dense(
        out_channels=self.dense_layer_size,
        activation_fn=tf.nn.relu,
        in_layers=[in_layer])
    batch_norm3 = BatchNorm(in_layers=[dense])
    if self.dropout > 0.0:
      batch_norm3 = Dropout(self.dropout, in_layers=batch_norm3)
    readout = GraphGather(
        batch_size=self.batch_size,
        activation_fn=tf.nn.tanh,
        in_layers=[batch_norm3, self.degree_slice, self.membership] +
        self.deg_adjs)

    n_tasks = self.n_tasks
    if self.mode == 'classification':
      n_classes = self.n_classes
      labels = Label(shape=(None, n_tasks, n_classes))
      weights = Weights(shape=(None, n_tasks))
      logits = Reshape(
          shape=(None, n_tasks, n_classes),
          in_layers=[
              Dense(in_layers=readout, out_channels=n_tasks * n_classes)
          ])
      logits = TrimGraphOutput([logits, labels])
      output = SoftMax(logits)
      self.add_output(output)
      loss = SoftMaxCrossEntropy(in_layers=[labels, logits])
      weighted_loss = WeightedError(in_layers=[loss, weights])
      self.set_loss(weighted_loss)
    else:
      labels = Label(shape=(None, n_tasks))
      weights = Weights(shape=(None, n_tasks))
      output = Reshape(
          shape=(None, n_tasks),
          in_layers=[Dense(in_layers=readout, out_channels=n_tasks)])
      output = TrimGraphOutput([output, labels])
      self.add_output(output)
      if self.uncertainty:
        log_var = Reshape(
            shape=(None, n_tasks),
            in_layers=[Dense(in_layers=readout, out_channels=n_tasks)])
        log_var = TrimGraphOutput([log_var, labels])
        var = Exp(log_var)
        self.add_variance(var)
        diff = labels - output
        weighted_loss = weights * (diff * diff / var + log_var)
        weighted_loss = ReduceSum(ReduceMean(weighted_loss, axis=[1]))
      else:
        weighted_loss = ReduceSum(L2Loss(in_layers=[labels, output, weights]))
      self.set_loss(weighted_loss)

  def default_generator(self,
                        dataset,
                        epochs=1,
                        predict=False,
                        deterministic=True,
                        pad_batches=True):
    for epoch in range(epochs):
      for ind, (X_b, y_b, w_b, ids_b) in enumerate(
          dataset.iterbatches(
              self.batch_size,
              pad_batches=pad_batches,
              deterministic=deterministic)):
        d = {}
        if self.mode == 'classification':
          d[self.labels[0]] = to_one_hot(y_b.flatten(), self.n_classes).reshape(
              -1, self.n_tasks, self.n_classes)
        else:
          d[self.labels[0]] = y_b
        d[self.task_weights[0]] = w_b
        multiConvMol = ConvMol.agglomerate_mols(X_b)
        d[self.atom_features] = multiConvMol.get_atom_features()
        d[self.degree_slice] = multiConvMol.deg_slice
        d[self.membership] = multiConvMol.membership
        for i in range(1, len(multiConvMol.get_deg_adjacency_lists())):
          d[self.deg_adjs[i - 1]] = multiConvMol.get_deg_adjacency_lists()[i]
        yield d

  def predict_on_smiles(self, smiles, transformers=[], untransform=False):
    """Generates predictions on a numpy array of smile strings

            # Returns:
              y_: numpy ndarray of shape (n_samples, n_tasks)
            """
    max_index = len(smiles) - 1
    n_tasks = len(self.outputs)
    num_batches = (max_index // self.batch_size) + 1
    featurizer = ConvMolFeaturizer()

    y_ = []
    for i in range(num_batches):
      start = i * self.batch_size
      end = min((i + 1) * self.batch_size, max_index + 1)
      smiles_batch = smiles[start:end]
      y_.append(
          self.predict_on_smiles_batch(smiles_batch, featurizer, transformers))
    y_ = np.concatenate(y_, axis=0)[:max_index + 1]
    y_ = y_.reshape(-1, n_tasks)

    if untransform:
      y_ = undo_transforms(y_, transformers)

    return y_


class MPNNModel(TensorGraph):
  """ Message Passing Neural Network,
      default structures built according to https://arxiv.org/abs/1511.06391 """

  def __init__(self,
               n_tasks,
               n_atom_feat=70,
               n_pair_feat=8,
               n_hidden=100,
               T=5,
               M=10,
               mode="regression",
               **kwargs):
    """
            Parameters
            ----------
            n_tasks: int
              Number of tasks
            n_atom_feat: int, optional
              Number of features per atom.
            n_pair_feat: int, optional
              Number of features per pair of atoms.
            n_hidden: int, optional
              Number of units(convolution depths) in corresponding hidden layer
            n_graph_feat: int, optional
              Number of output features for each molecule(graph)

            """
    self.n_tasks = n_tasks
    self.n_atom_feat = n_atom_feat
    self.n_pair_feat = n_pair_feat
    self.n_hidden = n_hidden
    self.T = T
    self.M = M
    self.mode = mode
    super(MPNNModel, self).__init__(**kwargs)
    self.build_graph()

  def build_graph(self):
    # Build placeholders
    self.atom_features = Feature(shape=(None, self.n_atom_feat))
    self.pair_features = Feature(shape=(None, self.n_pair_feat))
    self.atom_split = Feature(shape=(None,), dtype=tf.int32)
    self.atom_to_pair = Feature(shape=(None, 2), dtype=tf.int32)

    message_passing = MessagePassing(
        self.T,
        message_fn='enn',
        update_fn='gru',
        n_hidden=self.n_hidden,
        in_layers=[self.atom_features, self.pair_features, self.atom_to_pair])

    atom_embeddings = Dense(self.n_hidden, in_layers=[message_passing])

    mol_embeddings = SetGather(
        self.M,
        self.batch_size,
        n_hidden=self.n_hidden,
        in_layers=[atom_embeddings, self.atom_split])

    dense1 = Dense(
        out_channels=2 * self.n_hidden,
        activation_fn=tf.nn.relu,
        in_layers=[mol_embeddings])
    costs = []
    self.labels_fd = []
    for task in range(self.n_tasks):
      if self.mode == "classification":
        classification = Dense(
            out_channels=2, activation_fn=None, in_layers=[dense1])
        softmax = SoftMax(in_layers=[classification])
        self.add_output(softmax)

        label = Label(shape=(None, 2))
        self.labels_fd.append(label)
        cost = SoftMaxCrossEntropy(in_layers=[label, classification])
        costs.append(cost)
      if self.mode == "regression":
        regression = Dense(
            out_channels=1, activation_fn=None, in_layers=[dense1])
        self.add_output(regression)

        label = Label(shape=(None, 1))
        self.labels_fd.append(label)
        cost = L2Loss(in_layers=[label, regression])
        costs.append(cost)
    if self.mode == "classification":
      all_cost = Stack(in_layers=costs, axis=1)
    elif self.mode == "regression":
      all_cost = Stack(in_layers=costs, axis=1)
    self.weights = Weights(shape=(None, self.n_tasks))
    loss = WeightedError(in_layers=[all_cost, self.weights])
    self.set_loss(loss)

  def default_generator(self,
                        dataset,
                        epochs=1,
                        predict=False,
                        deterministic=True,
                        pad_batches=True):
    """ Same generator as Weave models """
    for epoch in range(epochs):
      if not predict:
        print('Starting epoch %i' % epoch)
      for (X_b, y_b, w_b, ids_b) in dataset.iterbatches(
          batch_size=self.batch_size,
          deterministic=deterministic,
          pad_batches=pad_batches):

        feed_dict = dict()
        if y_b is not None:
          for index, label in enumerate(self.labels_fd):
            if self.mode == "classification":
              feed_dict[label] = to_one_hot(y_b[:, index])
            if self.mode == "regression":
              feed_dict[label] = y_b[:, index:index + 1]
        # w_b act as the indicator of unique samples in the batch
        if w_b is not None:
          feed_dict[self.weights] = w_b

        atom_feat = []
        pair_feat = []
        atom_split = []
        atom_to_pair = []
        pair_split = []
        start = 0
        for im, mol in enumerate(X_b):
          n_atoms = mol.get_num_atoms()
          # number of atoms in each molecule
          atom_split.extend([im] * n_atoms)
          # index of pair features
          C0, C1 = np.meshgrid(np.arange(n_atoms), np.arange(n_atoms))
          atom_to_pair.append(
              np.transpose(
                  np.array([C1.flatten() + start,
                            C0.flatten() + start])))
          # number of pairs for each atom
          pair_split.extend(C1.flatten() + start)
          start = start + n_atoms

          # atom features
          atom_feat.append(mol.get_atom_features())
          # pair features
          pair_feat.append(
              np.reshape(mol.get_pair_features(),
                         (n_atoms * n_atoms, self.n_pair_feat)))

        feed_dict[self.atom_features] = np.concatenate(atom_feat, axis=0)
        feed_dict[self.pair_features] = np.concatenate(pair_feat, axis=0)
        feed_dict[self.atom_split] = np.array(atom_split)
        feed_dict[self.atom_to_pair] = np.concatenate(atom_to_pair, axis=0)
        yield feed_dict

  def predict(self, dataset, transformers=[], batch_size=None):
    # MPNN only accept padded input
    generator = self.default_generator(dataset, predict=True, pad_batches=True)
    return self.predict_on_generator(generator, transformers)

  def predict_proba(self, dataset, transformers=[], batch_size=None):
    # MPNN only accept padded input
    generator = self.default_generator(dataset, predict=True, pad_batches=True)
    return self.predict_proba_on_generator(generator, transformers)

  def predict_proba_on_generator(self, generator, transformers=[]):
    """
            Returns:
              y_pred: numpy ndarray of shape (n_samples, n_classes*n_tasks)
            """
    if not self.built:
      self.build()
    with self._get_tf("Graph").as_default():
      out_tensors = [x.out_tensor for x in self.outputs]
      results = []
      for feed_dict in generator:
        # Extract number of unique samples in the batch from w_b
        n_valid_samples = len(np.nonzero(np.sum(feed_dict[self.weights], 1))[0])
        feed_dict = {
            self.layers[k.name].out_tensor: v
            for k, v in six.iteritems(feed_dict)
        }
        feed_dict[self._training_placeholder] = 0.0
        result = np.array(self.session.run(out_tensors, feed_dict=feed_dict))
        if len(result.shape) == 3:
          result = np.transpose(result, axes=[1, 0, 2])
        result = undo_transforms(result, transformers)
        # Only fetch the first set of unique samples
        results.append(result[:n_valid_samples])
      return np.concatenate(results, axis=0)

  def predict_on_generator(self, generator, transformers=[]):
    return self.predict_proba_on_generator(generator, transformers)


#################### Deprecation warnings for renamed TensorGraph models ####################

import warnings

TENSORGRAPH_DEPRECATION = "{} is deprecated and has been renamed to {} and will be removed in DeepChem 3.0."


class GraphConvTensorGraph(GraphConvModel):

  def __init__(self, *args, **kwargs):

    warnings.warn(
        TENSORGRAPH_DEPRECATION.format("GraphConvTensorGraph",
                                       "GraphConvModel"), FutureWarning)

    super(GraphConvTensorGraph, self).__init__(*args, **kwargs)


class WeaveTensorGraph(WeaveModel):

  def __init__(self, *args, **kwargs):

    warnings.warn(
        TENSORGRAPH_DEPRECATION.format("WeaveTensorGraph", "WeaveModel"),
        FutureWarning)

    super(WeaveModel, self).__init__(*args, **kwargs)


class DTNNTensorGraph(DTNNModel):

  def __init__(self, *args, **kwargs):

    warnings.warn(
        TENSORGRAPH_DEPRECATION.format("DTNNTensorGraph", "DTNNModel"),
        FutureWarning)

    super(DTNNModel, self).__init__(*args, **kwargs)


class DAGTensorGraph(DAGModel):

  def __init__(self, *args, **kwargs):

    warnings.warn(
        TENSORGRAPH_DEPRECATION.format("DAGTensorGraph", "DAGModel"),
        FutureWarning)

    super(DAGModel, self).__init__(*args, **kwargs)


class MPNNTensorGraph(MPNNModel):

  def __init__(self, *args, **kwargs):

    warnings.warn(
        TENSORGRAPH_DEPRECATION.format("MPNNTensorGraph", "MPNNModel"),
        FutureWarning)

    super(MPNNModel, self).__init__(*args, **kwargs)
