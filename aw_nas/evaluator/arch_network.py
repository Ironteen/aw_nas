"""
Networks that take architectures as inputs.
"""

import abc

import numpy as np
import scipy.sparse as sp
import torch
from torch import nn
import torch.nn.functional as F

from aw_nas import utils
from aw_nas.base import Component
from aw_nas.utils import DenseGraphConvolution

__all__ = ["PointwiseComparator"]

class ArchNetwork(Component):
    REGISTRY = "arch_network"

    @abc.abstractmethod
    def save(self, path):
        pass

    @abc.abstractmethod
    def load(self, path):
        pass


class ArchEmbedder(Component, nn.Module):
    REGISTRY = "arch_embedder"

    def __init__(self, schedule_cfg):
        Component.__init__(self, schedule_cfg)
        nn.Module.__init__(self)

    def save(self, path):
        torch.save(self.state_dict(), path)

    def load(self, path):
        self.load_state_dict(torch.load(path))


class LSTMArchEmbedder(ArchEmbedder):
    NAME = "lstm"

    def __init__(self, search_space,
                 op_embedding_size=48,
                 node_embedding_size=48,
                 hidden_size=96,
                 dropout_ratio=0.,
                 num_layers=1,
                 schedule_cfg=None):
        super(LSTMArchEmbedder, self).__init__(schedule_cfg)
        self.search_space = search_space

        self.op_embedding_size = op_embedding_size
        self.node_embedding_size = node_embedding_size
        self.hidden_size = hidden_size
        self.num_layers = num_layers

        _n_node_input = self.search_space.num_init_nodes + self.search_space.num_steps - 1
        # unlike controller rl_network,
        # this module only support shared primitives for all cell groups
        self.op_emb = nn.Embedding(len(self.search_space.shared_primitives), self.op_embedding_size)
        self.node_emb = nn.Embedding(_n_node_input, self.node_embedding_size)

        self.rnn = nn.LSTM(input_size=self.op_embedding_size + self.node_embedding_size,
                           hidden_size=self.hidden_size, num_layers=self.num_layers,
                           batch_first=True, dropout=dropout_ratio)
        self._one_param = next(self.parameters())

        # calculate out dim
        self.out_dim = self.hidden_size

    def embed_and_transform_arch(self, archs):
        if isinstance(archs, (np.ndarray, list, tuple)):
            archs = np.array(archs)
            if archs.ndim == 3:
                archs = np.expand_dims(archs, 0)
            else:
                assert archs.ndim == 4
            archs = torch.tensor(archs).to(self._one_param.device)

        # embedding nodes
        # (batch_size, num_cell_groups, num_node_inputs * num_steps, node_embedding_size)
        node_embs = self.node_emb(archs[:, :, 0, :])
        # embedding ops
        op_embs = self.op_emb(archs[:, :, 1, :])
        # re-arrange
        cat_emb = torch.cat([node_embs, op_embs], dim=-1)

        return torch.reshape(cat_emb, [cat_emb.shape[0], -1, cat_emb.shape[-1]])

    def forward(self, archs):
        emb = self.embed_and_transform_arch(archs)
        # TODO: dropout on embedding?
        out, _ = self.rnn(emb)
        # normalize the output following NAO
        # FIXME: do not know why
        out = F.normalize(out, 2, dim=-1)

        # average across decisions (time steps)
        out = torch.mean(out, dim=1)
        # FIXME: normalzie again, why?
        out = F.normalize(out, 2, dim=-1)
        return out

# ---- GCNArchEmbedder ----
# try:
#     from pygcn.layers import GraphConvolution
# except ImportError as e:
#     from aw_nas.utils import logger as _logger
#     _logger.getChild("arch_network").warn(
#         ("Error importing module pygcn: {}\n"
#          "Should install the pygcn package for graph convolution").format(e))

def sparse_mx_to_torch_sparse_tensor(sparse_mx):
    """Convert a scipy sparse matrix to a torch sparse tensor."""
    sparse_mx = sparse_mx.tocoo().astype(np.float32)
    indices = torch.from_numpy(
        np.vstack((sparse_mx.row, sparse_mx.col)).astype(np.int64))
    values = torch.from_numpy(sparse_mx.data)
    shape = torch.Size(sparse_mx.shape)
    return torch.sparse.FloatTensor(indices, values, shape)

class GCNArchEmbedder(ArchEmbedder):
    NAME = "gcn"

    def __init__(self, search_space,
                 op_dim=48, op_hid=48, gcn_out_dims=[128, 128],
                 dropout=0.,
                 schedule_cfg=None):
        super(GCNArchEmbedder, self).__init__(schedule_cfg)

        self.search_space = search_space

        # configs
        self.op_dim = op_dim
        self.op_hid = op_hid
        self.gcn_out_dims = gcn_out_dims
        self.dropout = dropout
        self._num_init_nodes = self.search_space.num_init_nodes
        self._num_node_inputs = self.search_space.num_node_inputs
        self._num_steps = self.search_space.num_steps
        self._num_nodes = self._num_steps + self._num_init_nodes

        # the embedding of the first two nodes
        # self.init_node_emb = nn.ModuleList(
        #     [nn.Embedding(
        #         self.search_space.num_init_nodes, self._num_node_inputs * self.op_dim)
        #      for _ in self.search_space.num_cell_groups]
        # )
        # share init node embedding for all cell groups
        self.init_node_emb = nn.Embedding(
            self._num_init_nodes, self._num_node_inputs * self.op_dim)

        self.op_emb = nn.Embedding(len(search_space.shared_primitives), self.op_dim)
        # concat the embedding [op0, op1, ...] for each node
        self.x_hidden = nn.Linear(self._num_node_inputs * self.op_dim, self.op_hid)

        # init graph convolutions
        self.gcns = []
        in_dim = self.op_hid
        for dim in self.gcn_out_dims:
            self.gcns.append(DenseGraphConvolution(in_dim, dim))
            in_dim = dim
        self.gcns = nn.ModuleList(self.gcns)
        self.num_gcn_layers = len(self.gcns)

        self.out_dim = self.search_space.num_cell_groups * in_dim

        self._one_param = next(self.parameters())

    def get_adj_sparse(self, arch):
        return self._get_adj_sparse(arch, self._num_init_nodes,
                                    self._num_node_inputs, self._num_nodes)

    def get_adj_dense(self, arch):
        return self._get_adj_dense(arch, self._num_init_nodes,
                                   self._num_node_inputs, self._num_nodes)

    def _get_adj_sparse(self, arch, num_init_nodes, num_node_inputs, num_nodes): #pylint: disable=no-self-use
        """
        :param arch: previous_nodes, e.g. [1, 0, 0, 1, 2, 0, 4, 4],
            0, 1 is the previous init nodes
        :param num_node:
        :return:
        """
        f_nodes = np.array(arch)
        t_nodes = np.repeat(np.array(range(num_init_nodes, num_nodes)), num_node_inputs)
        adj = sp.coo_matrix((np.ones(f_nodes.shape[0]), (t_nodes, f_nodes)),
                            shape=(num_nodes, num_nodes), dtype=np.float32)
        adj = adj.multiply(adj > 0)
        # build symmetric adjacency matrix for undirected graph
        # adj = adj + adj.T.multiply(adj.T > adj) - adj.multiply(adj.T > adj)
        adj = sparse_mx_to_torch_sparse_tensor(adj)
        return adj

    def _get_adj_dense(self, arch, num_init_nodes, num_node_inputs, num_nodes): #pylint: disable=no-self-use
        """
        get dense adjecent matrix, could be batched
        :param arch: previous_nodes, e.g. [1, 0, 0, 1, 2, 0, 4, 4],
            0, 1 is the previous init nodes
        :param num_node:
        :return:
        """
        f_nodes = np.array(arch)
        _ndim = f_nodes.ndim
        if _ndim == 1:
            f_nodes = np.expand_dims(arch, 0)
        else:
            assert _ndim == 2
        batch_size = f_nodes.shape[0]
        t_nodes = np.tile(
            np.repeat(np.array(range(num_init_nodes, num_nodes)), num_node_inputs)[None, :],
            [batch_size, 1]
        )
        batch_inds = np.tile(np.arange(batch_size)[:, None], [1, t_nodes.shape[1]])
        indexes = np.stack((batch_inds, t_nodes, f_nodes))
        indexes = indexes.reshape([3, -1])
        indexes, edge_counts = np.unique(indexes, return_counts=True, axis=1)
        adj = torch.zeros(batch_size, num_nodes, num_nodes)
        adj[indexes] += torch.tensor(edge_counts, dtype=torch.float32)
        if _ndim == 1:
            adj = adj[0]
        return adj

    def embed_and_transform_arch(self, archs):
        if isinstance(archs, (np.ndarray, list, tuple)):
            archs = np.array(archs)
            if archs.ndim == 3:
                # one arch
                archs = np.expand_dims(archs, 0)
            else:
                assert archs.ndim == 4

        # get adjacent matrix
        # sparse
        # archs[:, :, 0, :]: (batch_size, num_cell_groups, num_node_inputs * num_steps)
        b_size, n_cg, _, n_edge = archs.shape
        adjs = self.get_adj_dense(archs[:, :, 0, :].reshape([-1, n_edge]))
        adjs = adjs.reshape([b_size, n_cg, adjs.shape[1], adjs.shape[2]]).to(self._one_param.device)
        # (batch_size, num_cell_groups, num_nodes, num_nodes)

        # embedding ops
        op_inds = torch.tensor(archs[:, :, 1, :]).to(self._one_param.device)
        op_embs = self.op_emb(op_inds)
        # (batch_size, num_cell_groups, num_node_inputs * num_steps, op_dim)

        shape = op_embs.shape
        # concat two input op embedding for each node, use reshape to replace split+cat
        # inter_node_embs = [t.unsqueeze(3) for t in torch.split(
        #     op_embs.reshape([
        #         shape[0], shape[1], self._num_steps,
        #         self._num_node_inputs, shape[3]]),
        #     1, dim=3)]
        # inter_node_embs = torch.cat(inter_node_embs, dim=-1)
        inter_node_embs = op_embs.reshape([
            shape[0], shape[1], self._num_steps, self._num_node_inputs * shape[3]])
        # (batch_size, num_cell_groups, num_steps, num_node_inputs * self.op_dim)

        # embedding of all nodes
        unsqueezed_init_emb = self.init_node_emb\
                                  .weight\
                                  .unsqueeze(0)\
                                  .unsqueeze(0)\
                                  .repeat([shape[0], shape[1], 1, 1])
        node_embs = torch.cat((unsqueezed_init_emb, inter_node_embs), dim=2)
        # (batch_size, num_cell_groups, num_nodes, num_node_inputs * self.op_dim)

        x = self.x_hidden(node_embs)
        # (batch_size, num_cell_groups, num_nodes, op_hid)
        return adjs, x

    def forward(self, archs):
        # adjs: (batch_size, num_cell_groups, num_nodes, num_nodes)
        # x: (batch_size, num_cell_groups, num_nodes, op_hid)
        adjs, x = self.embed_and_transform_arch(archs)
        y = x
        for i_layer, gcn in enumerate(self.gcns):
            y = gcn(y, adjs)
            if i_layer != self.num_gcn_layers - 1:
                y = F.relu(y)
            y = F.dropout(y, self.dropout, training=self.training)
        # y: (batch_size, num_cell_groups, num_nodes, gcn_out_dims[-1])
        y = y[:, :, 2:, :] # do not keep the init node embedding
        y = torch.mean(y, dim=2) # average across nodes (bs, nc, god)
        y = torch.reshape(y, [y.shape[0], -1]) # concat across cell groups, just reshape here
        return y

# ---- END: GCNArchEmbedder ----

class PointwiseComparator(ArchNetwork, nn.Module):
    """
    Compatible to NN regression-based predictor of architecture performance.
    """
    NAME = "pointwise_comparator"

    def __init__(self, search_space,
                 arch_embedder_type="lstm", arch_embedder_cfg=None,
                 mlp_hiddens=(200, 200, 200), mlp_dropout=0.1,
                 optimizer={
                     "type": "Adam",
                     "lr": 0.001
                 }, scheduler=None,
                 compare_loss_type="margin_linear",
                 compare_margin=0.01,
                 schedule_cfg=None):
        # [optional] arch reconstruction loss (arch_decoder_type/cfg)
        super(PointwiseComparator, self).__init__(schedule_cfg)
        nn.Module.__init__(self)

        # configs
        self.compare_loss_type = compare_loss_type
        self.compare_margin = compare_margin

        self.search_space = search_space
        ae_cls = ArchEmbedder.get_class_(arch_embedder_type)
        self.arch_embedder = ae_cls(self.search_space, **(arch_embedder_cfg or {}))

        dim = self.embedding_dim = self.arch_embedder.out_dim
        # construct MLP from embedding to score
        self.mlp = []
        for hidden_size in mlp_hiddens:
            self.mlp.append(nn.Sequential(
                nn.Linear(dim, hidden_size),
                nn.ReLU(inplace=False),
                nn.Dropout(p=mlp_dropout)))
            dim = hidden_size
        self.mlp.append(nn.Linear(dim, 1))
        self.mlp = nn.Sequential(*self.mlp)

        # init optimizer and scheduler
        self.optimizer = utils.init_optimizer(self.parameters(), optimizer)
        self.scheduler = utils.init_scheduler(self.optimizer, scheduler)
        self._one_param = next(self.parameters())

    def predict(self, arch):
        score = torch.sigmoid(self.mlp(self.arch_embedder(arch))).squeeze()
        return score

    def update_predict_rollouts(self, rollouts, labels):
        archs = [r.arch for r in rollouts]
        return self.update_predict(archs, labels)

    def update_predict_list(self, predict_lst):
        # use MSE regression loss to step
        archs = [item[0] for item in predict_lst]
        labels = [item[1] for item in predict_lst]
        return self.update_predict(archs, labels)

    def update_predict(self, archs, labels):
        scores = torch.sigmoid(self.mlp(self.arch_embedder(archs)))
        mse_loss = F.mse_loss(
            scores.squeeze(),
            torch.tensor(labels).to(self._one_param.device))
        mse_loss.backward()
        self.optimizer.step()
        return mse_loss.item()

    def compare(self, arch_1, arch_2):
        # pointwise score and comparen
        s_1 = self.mlp(self.arch_embedder(arch_1)).squeeze()
        s_2 = self.mlp(self.arch_embedder(arch_2)).squeeze()
        return torch.sigmoid(s_2 - s_1)

    def update_compare_rollouts(self, compare_rollouts, better_labels):
        arch_1, arch_2 = zip(*[(r.rollout_1.arch, r.rollout_2.arch) for r in compare_rollouts])
        return self.update_compare(arch_1, arch_2, better_labels)

    def update_compare_list(self, compare_lst):
        # use binary classification loss to step
        arch_1, arch_2, better_labels = zip(*compare_lst)
        return self.update_compare(arch_1, arch_2, better_labels)

    def update_compare(self, arch_1, arch_2, better_labels):
        if self.compare_loss_type == "binary_cross_entropy":
            # compare_score = self.compare(arch_1, arch_2)
            s_1 = self.mlp(self.arch_embedder(arch_1)).squeeze()
            s_2 = self.mlp(self.arch_embedder(arch_2)).squeeze()
            compare_score = torch.sigmoid(s_2 - s_1)
            pair_loss = F.binary_cross_entropy(
                compare_score,
                torch.tensor(better_labels).to(self._one_param.device))
        elif self.compare_loss_type == "margin_linear":
            # in range (0, 1) to make the `compare_margin` meaningful
            s_1 = self.predict(arch_1)
            s_2 = self.predict(arch_2)
            better_pm = 2 * torch.tensor(np.array(better_labels, dtype=np.float32))\
                                 .to(self._one_param.device) - 1
            zero_ = torch.tensor(0., dtype=torch.float32, device=self._one_param.device)
            pair_loss = torch.mean(torch.max(zero_, self.compare_margin - better_pm * (s_2 - s_1)))
        pair_loss.backward()
        self.optimizer.step()
        # return pair_loss.item(), s_1, s_2
        return pair_loss.item()

    def save(self, path):
        self.arch_embedder.save("{}-embedder".format(path))
        torch.save(self.mlp, "{}-mlp".format(path))

    def load(self, path):
        self.arch_embedder.load("{}-embedder".format(path))
        self.mlp = torch.load("{}-mlp".format(path))

    def on_epoch_start(self, epoch):
        super(PointwiseComparator, self).on_epoch_start(epoch)
        if self.scheduler is not None:
            self.scheduler.step(epoch - 1)
            self.logger.info("Epoch %3d: lr: %.5f", epoch, self.scheduler.get_lr()[0])