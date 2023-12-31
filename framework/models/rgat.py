import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from sklearn.metrics import roc_auc_score, average_precision_score

from typing import Optional

import torch
import torch.nn.functional as F
from torch import Tensor
from torch.nn import Parameter, ReLU
from torch_scatter import scatter_add
from torch_sparse import SparseTensor

from torch_geometric.nn.conv import MessagePassing
from torch_geometric.nn.dense.linear import Linear
from torch_geometric.nn.inits import glorot, ones, zeros
from torch_geometric.typing import Adj, OptTensor, Size
from torch_geometric.utils import softmax


# Source: torch_geometric
class RGATConv(MessagePassing):
    _alpha: OptTensor

    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        num_relations: int,
        num_bases: Optional[int] = None,
        num_blocks: Optional[int] = None,
        mod: Optional[str] = None,
        attention_mechanism: str = "across-relation",
        attention_mode: str = "additive-self-attention",
        heads: int = 1,
        dim: int = 1,
        concat: bool = True,
        negative_slope: float = 0.2,
        dropout: float = 0.0,
        edge_dim: Optional[int] = None,
        bias: bool = True,
        **kwargs,
    ):
        kwargs.setdefault('aggr', 'add')
        super().__init__(node_dim=0, **kwargs)

        self.heads = heads
        self.negative_slope = negative_slope
        self.dropout = dropout
        self.mod = mod
        self.activation = ReLU()
        self.concat = concat
        self.attention_mode = attention_mode
        self.attention_mechanism = attention_mechanism
        self.dim = dim
        self.edge_dim = edge_dim

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.num_relations = num_relations
        self.num_bases = num_bases
        self.num_blocks = num_blocks

        mod_types = ['additive', 'scaled', 'f-additive', 'f-scaled']

        if (self.attention_mechanism != "within-relation"
                and self.attention_mechanism != "across-relation"):
            raise ValueError('attention mechanism must either be '
                             '"within-relation" or "across-relation"')

        if (self.attention_mode != "additive-self-attention"
                and self.attention_mode != "multiplicative-self-attention"):
            raise ValueError('attention mode must either be '
                             '"additive-self-attention" or '
                             '"multiplicative-self-attention"')

        if self.attention_mode == "additive-self-attention" and self.dim > 1:
            raise ValueError('"additive-self-attention" mode cannot be '
                             'applied when value of d is greater than 1. '
                             'Use "multiplicative-self-attention" instead.')

        if self.dropout > 0.0 and self.mod in mod_types:
            raise ValueError('mod must be None with dropout value greater '
                             'than 0 in order to sample attention '
                             'coefficients stochastically')

        if num_bases is not None and num_blocks is not None:
            raise ValueError('Can not apply both basis-decomposition and '
                             'block-diagonal-decomposition at the same time.')

        # The learnable parameters to compute both attention logits and
        # attention coefficients:
        self.q = Parameter(
            torch.Tensor(self.heads * self.out_channels,
                         self.heads * self.dim))
        self.k = Parameter(
            torch.Tensor(self.heads * self.out_channels,
                         self.heads * self.dim))

        if bias and concat:
            self.bias = Parameter(
                torch.Tensor(self.heads * self.dim * self.out_channels))
        elif bias and not concat:
            self.bias = Parameter(torch.Tensor(self.dim * self.out_channels))
        else:
            self.register_parameter('bias', None)

        if edge_dim is not None:
            self.lin_edge = Linear(self.edge_dim,
                                   self.heads * self.out_channels, bias=False,
                                   weight_initializer='glorot')
            self.e = Parameter(
                torch.Tensor(self.heads * self.out_channels,
                             self.heads * self.dim))
        else:
            self.lin_edge = None
            self.register_parameter('e', None)

        if num_bases is not None:
            self.att = Parameter(
                torch.Tensor(self.num_relations, self.num_bases))
            self.basis = Parameter(
                torch.Tensor(self.num_bases, self.in_channels,
                             self.heads * self.out_channels))
        elif num_blocks is not None:
            assert (
                self.in_channels % self.num_blocks == 0
                and (self.heads * self.out_channels) % self.num_blocks == 0), (
                    "both 'in_channels' and 'heads * out_channels' must be "
                    "multiple of 'num_blocks' used")
            self.weight = Parameter(
                torch.Tensor(self.num_relations, self.num_blocks,
                             self.in_channels // self.num_blocks,
                             (self.heads * self.out_channels) //
                             self.num_blocks))
        else:
            self.weight = Parameter(
                torch.Tensor(self.num_relations, self.in_channels,
                             self.heads * self.out_channels))

        self.w = Parameter(torch.ones(self.out_channels))
        self.l1 = Parameter(torch.Tensor(1, self.out_channels))
        self.b1 = Parameter(torch.Tensor(1, self.out_channels))
        self.l2 = Parameter(torch.Tensor(self.out_channels, self.out_channels))
        self.b2 = Parameter(torch.Tensor(1, self.out_channels))

        self._alpha = None

        self.reset_parameters()

    def reset_parameters(self):
        if self.num_bases is not None:
            glorot(self.basis)
            glorot(self.att)
        else:
            glorot(self.weight)
        glorot(self.q)
        glorot(self.k)
        zeros(self.bias)
        ones(self.l1)
        zeros(self.b1)
        torch.full(self.l2.size(), 1 / self.out_channels)
        zeros(self.b2)
        if self.lin_edge is not None:
            glorot(self.lin_edge)
            glorot(self.e)

    def forward(self, x: Tensor, edge_index: Adj, edge_type: OptTensor = None,
                edge_attr: OptTensor = None, size: Size = None,
                return_attention_weights=None):
        # propagate_type: (x: Tensor, edge_type: OptTensor, edge_attr: OptTensor)  # noqa
        out = self.propagate(edge_index=edge_index, edge_type=edge_type, x=x,
                             size=size, edge_attr=edge_attr)

        alpha = self._alpha
        assert alpha is not None
        self._alpha = None

        if isinstance(return_attention_weights, bool):
            if isinstance(edge_index, Tensor):
                return out, (edge_index, alpha)
            elif isinstance(edge_index, SparseTensor):
                return out, edge_index.set_value(alpha, layout='coo')
        else:
            return out


    def message(self, x_i: Tensor, x_j: Tensor, edge_type: Tensor,
                edge_attr: OptTensor, index: Tensor, ptr: OptTensor,
                size_i: Optional[int]) -> Tensor:

        if self.num_bases is not None:  # Basis-decomposition =================
            w = torch.matmul(self.att, self.basis.view(self.num_bases, -1))
            w = w.view(self.num_relations, self.in_channels,
                       self.heads * self.out_channels)
        if self.num_blocks is not None:  # Block-diagonal-decomposition =======
            if (x_i.dtype == torch.long and x_j.dtype == torch.long
                    and self.num_blocks is not None):
                raise ValueError('Block-diagonal decomposition not supported '
                                 'for non-continuous input features.')
            w = self.weight
            x_i = x_i.view(-1, 1, w.size(1), w.size(2))
            x_j = x_j.view(-1, 1, w.size(1), w.size(2))
            w = torch.index_select(w, 0, edge_type)
            outi = torch.einsum('abcd,acde->ace', x_i, w)
            outi = outi.contiguous().view(-1, self.heads * self.out_channels)
            outj = torch.einsum('abcd,acde->ace', x_j, w)
            outj = outj.contiguous().view(-1, self.heads * self.out_channels)
        else:  # No regularization/Basis-decomposition ========================
            if self.num_bases is None:
                w = self.weight
            w = torch.index_select(w, 0, edge_type)
            outi = torch.bmm(x_i.unsqueeze(1), w).squeeze(-2)
            outj = torch.bmm(x_j.unsqueeze(1), w).squeeze(-2)

        qi = torch.matmul(outi, self.q)
        kj = torch.matmul(outj, self.k)

        alpha_edge, alpha = 0, torch.tensor([0])
        if edge_attr is not None:
            if edge_attr.dim() == 1:
                edge_attr = edge_attr.view(-1, 1)
            assert self.lin_edge is not None, (
                "Please set 'edge_dim = edge_attr.size(-1)' while calling the "
                "RGATConv layer")
            edge_attributes = self.lin_edge(edge_attr).view(
                -1, self.heads * self.out_channels)
            if edge_attributes.size(0) != edge_attr.size(0):
                edge_attributes = torch.index_select(edge_attributes, 0,
                                                     edge_type)
            alpha_edge = torch.matmul(edge_attributes, self.e)

        if self.attention_mode == "additive-self-attention":
            if edge_attr is not None:
                alpha = torch.add(qi, kj) + alpha_edge
            else:
                alpha = torch.add(qi, kj)
            alpha = F.leaky_relu(alpha, self.negative_slope)
        elif self.attention_mode == "multiplicative-self-attention":
            if edge_attr is not None:
                alpha = (qi * kj) * alpha_edge
            else:
                alpha = qi * kj

        if self.attention_mechanism == "within-relation":
            across_out = torch.zeros_like(alpha)
            for r in range(self.num_relations):
                mask = edge_type == r
                across_out[mask] = softmax(alpha[mask], index[mask])
            alpha = across_out
        elif self.attention_mechanism == "across-relation":
            alpha = softmax(alpha, index, ptr, size_i)

        self._alpha = alpha

        if self.mod == "additive":
            if self.attention_mode == "additive-self-attention":
                ones = torch.ones_like(alpha)
                h = (outj.view(-1, self.heads, self.out_channels) *
                     ones.view(-1, self.heads, 1))
                h = torch.mul(self.w, h)

                return (outj.view(-1, self.heads, self.out_channels) *
                        alpha.view(-1, self.heads, 1) + h)
            elif self.attention_mode == "multiplicative-self-attention":
                ones = torch.ones_like(alpha)
                h = (outj.view(-1, self.heads, 1, self.out_channels) *
                     ones.view(-1, self.heads, self.dim, 1))
                h = torch.mul(self.w, h)

                return (outj.view(-1, self.heads, 1, self.out_channels) *
                        alpha.view(-1, self.heads, self.dim, 1) + h)

        elif self.mod == "scaled":
            if self.attention_mode == "additive-self-attention":
                ones = alpha.new_ones(index.size())
                degree = scatter_add(ones, index,
                                     dim_size=size_i)[index].unsqueeze(-1)
                degree = torch.matmul(degree, self.l1) + self.b1
                degree = self.activation(degree)
                degree = torch.matmul(degree, self.l2) + self.b2

                return torch.mul(
                    outj.view(-1, self.heads, self.out_channels) *
                    alpha.view(-1, self.heads, 1),
                    degree.view(-1, 1, self.out_channels))
            elif self.attention_mode == "multiplicative-self-attention":
                ones = alpha.new_ones(index.size())
                degree = scatter_add(ones, index,
                                     dim_size=size_i)[index].unsqueeze(-1)
                degree = torch.matmul(degree, self.l1) + self.b1
                degree = self.activation(degree)
                degree = torch.matmul(degree, self.l2) + self.b2

                return torch.mul(
                    outj.view(-1, self.heads, 1, self.out_channels) *
                    alpha.view(-1, self.heads, self.dim, 1),
                    degree.view(-1, 1, 1, self.out_channels))

        elif self.mod == "f-additive":
            alpha = torch.where(alpha > 0, alpha + 1, alpha)

        elif self.mod == "f-scaled":
            ones = alpha.new_ones(index.size())
            degree = scatter_add(ones, index,
                                 dim_size=size_i)[index].unsqueeze(-1)
            alpha = alpha * degree

        elif self.training and self.dropout > 0:
            alpha = F.dropout(alpha, p=self.dropout, training=True)

        else:
            alpha = alpha  # original

        if self.attention_mode == "additive-self-attention":
            return alpha.view(-1, self.heads, 1) * outj.view(
                -1, self.heads, self.out_channels)
        else:
            return (alpha.view(-1, self.heads, self.dim, 1) *
                    outj.view(-1, self.heads, 1, self.out_channels))

    def update(self, aggr_out: Tensor) -> Tensor:
        if self.attention_mode == "additive-self-attention":
            if self.concat is True:
                aggr_out = aggr_out.view(-1, self.heads * self.out_channels)
            else:
                aggr_out = aggr_out.mean(dim=1)

            if self.bias is not None:
                aggr_out = aggr_out + self.bias

            return aggr_out
        else:
            if self.concat is True:
                aggr_out = aggr_out.view(
                    -1, self.heads * self.dim * self.out_channels)
            else:
                aggr_out = aggr_out.mean(dim=1)
                aggr_out = aggr_out.view(-1, self.dim * self.out_channels)

            if self.bias is not None:
                aggr_out = aggr_out + self.bias

            return aggr_out

    def __repr__(self) -> str:
        return '{}({}, {}, heads={})'.format(self.__class__.__name__,
                                             self.in_channels,
                                             self.out_channels, self.heads)

class RGAT(nn.Module):
    def __init__(self, args, num_nodes, num_edge_type, **kwargs):
        super().__init__()
        self.args = args
        self.num_edge_type = num_edge_type

        # Encoder: RGAT
        self.node_emb = nn.Embedding(num_nodes, args.in_dim)
        if num_edge_type > 20:
            self.conv1 = RGATConv(args.in_dim, args.hidden_dim, num_edge_type * 2, num_blocks=4)
            self.conv2 = RGATConv(args.hidden_dim, args.out_dim, num_edge_type * 2, num_blocks=4)
        else:
            self.conv1 = RGATConv(args.in_dim, args.hidden_dim, num_edge_type * 2)
            self.conv2 = RGATConv(args.hidden_dim, args.out_dim, num_edge_type * 2)
        self.relu = nn.ReLU()
        
        # Decoder: DistMult
        self.W = nn.Parameter(torch.Tensor(num_edge_type, args.out_dim))
        nn.init.xavier_uniform_(self.W, gain=nn.init.calculate_gain('relu'))
    
    def forward(self, x, edge, edge_type, return_all_emb=False):
        x = self.node_emb(x)
        x1 = self.conv1(x, edge, edge_type)
        x = self.relu(x1)
        x2 = self.conv2(x, edge, edge_type)
        
        if return_all_emb:
            return x1, x2

        return x2

    def decode(self, z, edge_index, edge_type):
        h = z[edge_index[0]]
        t = z[edge_index[1]]
        r = self.W[edge_type]
        
        logits = torch.sum(h * r * t, dim=1)
        
        return logits
