# Copyright 2021 TUNiB Inc.
import math

import torch
import torch.nn.functional as F
from torch import nn


class VocabParallelEmbedding(nn.Embedding):
    """Embedding parallelized in the vocabulary dimension."""

    def _get_partitioned_vocab_range(self, world_size, rank):
        indices = [
            math.ceil(self.num_embeddings / world_size) for _ in range(world_size)
        ]

        indices[-1] = self.num_embeddings - sum(indices[:-1])
        indices.insert(0, 0)
        # 50257 => [0, 12565, 12565, 12565, 12652]

        for i in range(1, len(indices)):
            # cumulative summation
            # [0, 12565, 25130, 37595, 50257]
            indices[i] += indices[i - 1]

        return indices[rank], indices[rank + 1]

    def forward(self, inputs):
        world_size = self.mpu.get_tensor_parallel_world_size()

        if world_size > 1:
            indices = self._get_partitioned_vocab_range(
                world_size,
                rank=self.mpu.get_tensor_parallel_rank(),
            )

            vocab_start_index, vocab_end_index = indices
            input_mask = (inputs < vocab_start_index) | (inputs >= vocab_end_index)
            masked_input = inputs.clone() - vocab_start_index
            masked_input[input_mask] = 0
        else:
            masked_input = inputs

        output_parallel = F.embedding(
            masked_input,
            self.weight,
            self.padding_idx,
            self.max_norm,
            self.norm_type,
            self.scale_grad_by_freq,
            self.sparse,
        )

        if world_size > 1:
            output_parallel[input_mask, :] = 0.0

        out = self.mpu.reduce(output_parallel)
        return out


class ColumnParallelLinear(nn.Linear):
    """Linear layer with column parallelism."""

    def forward(self, inputs):
        inputs = self.mpu.broadcast(inputs)

        if self.reversed:
            outputs = torch.matmul(inputs, self.weight)
        else:
            outputs = torch.matmul(inputs, self.weight.t())

        if self.gather_output:
            outputs = self.mpu.gather(outputs).clone()

        if self.bias is not None:
            outputs += self.bias

        return outputs


class RowParallelLinear(nn.Linear):
    """Linear layer with row parallelism."""

    def forward(self, inputs):
        if self.reversed:
            outputs = torch.matmul(inputs, self.weight)
        else:
            outputs = torch.matmul(inputs, self.weight.t())

        outputs = self.mpu.reduce(outputs).clone()

        if self.bias is not None:
            outputs += self.bias

        return outputs


def allocate(mpu, parameter):
    tp_rank, pp_rank, dp_rank = 0, 0, 0

    if hasattr(parameter, "tp_rank"):
        tp_rank = parameter.tp_rank
    if hasattr(parameter, "pp_rank"):
        tp_rank = parameter.pp_rank
    if hasattr(parameter, "dp_rank"):
        tp_rank = parameter.dp_rank

    device = mpu.rank2device(tp_rank, pp_rank, dp_rank)
    parameter.data = parameter.to(f"cuda:{device}")


def deallocate(parameter):
    parameter.data = parameter.cpu()
