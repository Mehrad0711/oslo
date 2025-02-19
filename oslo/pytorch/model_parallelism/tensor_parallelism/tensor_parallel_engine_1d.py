import copy
from typing import List

import torch
import torch.distributed as dist
from torch.nn import Embedding, Module

from oslo.pytorch.model_parallelism.utils.distributed import (
    ColumnParallelLinear,
    RowParallelLinear,
    VocabParallelEmbedding,
)
from oslo.pytorch.model_parallelism.utils.mappings import (
    TensorParallelMapping,
    update_module_arguments,
)


class TensorParallelEngine1D(object):
    def __init__(self, model, mpu, tp_mapping):
        self.model = model
        self.mpu = mpu
        self.tp_mapping = TensorParallelMapping(tp_mapping)

    def _update_mp_arguments(self):
        for module in self.model.modules():
            for elem in self.tp_mapping.update_attrs(self.model):
                if hasattr(module, elem.name):
                    world_size = self.mpu.get_tensor_parallel_world_size()
                    reduced_arg = getattr(module, elem.name) // world_size
                    setattr(module, elem.name, reduced_arg)

    @staticmethod
    def _deconstruct_combined_qkv(tensor, world_size):
        ranks = (len(tensor) + world_size - 1) // world_size
        tensor = [tensor[i * world_size : (i + 1) * world_size] for i in range(ranks)]
        tensor = list(map(lambda x: torch.cat([*x], dim=-1), zip(*tensor)))
        return tensor

    def _slice(
        self,
        module,
        reversed,
        fusion_degree,
        slice_bias,
        dim,
    ):
        dim = dim if not reversed else abs(dim - 1)
        world_size = self.mpu.get_tensor_parallel_world_size()
        gpu_index = self.mpu.get_tensor_parallel_rank()

        update_module_arguments(
            module=module,
            mpu=self.mpu,
            reversed=reversed,
            fusion_degree=fusion_degree,
            orig_module=copy.deepcopy(module.__class__),
            gather_output=False,
        )

        if hasattr(module, "weight") and module.weight is not None:
            if module.weight.dim() >= 1:
                weight = module.weight.chunk(fusion_degree * world_size, dim=dim)
                if fusion_degree > 1:
                    weight = self._deconstruct_combined_qkv(weight, world_size)
                module.weight.data = weight[gpu_index].contiguous()

            setattr(module.weight, "tp_rank", gpu_index)
            update_module_arguments(
                module=module,
                in_features=module.weight.size()[0],
                out_features=module.weight.size()[1],
            )

        if hasattr(module, "bias") and module.bias is not None:
            if slice_bias is True and module.bias.dim() >= 1:

                bias = module.bias.chunk(fusion_degree * world_size, dim=0)
                if fusion_degree > 1:
                    bias = self._deconstruct_combined_qkv(bias, world_size)
                module.bias.data = bias[gpu_index].contiguous()

            setattr(module.bias, "tp_rank", gpu_index)

        return module

    def _column_slice(
        self,
        module: Module,
        fusion_degree: int,
        reversed: bool,
    ) -> Module:
        return self._slice(
            module=module,
            reversed=reversed,
            fusion_degree=fusion_degree,
            slice_bias=True,
            dim=0,
        )

    def _row_slice(
        self,
        module: Module,
        fusion_degree: int,
        reversed: bool,
    ) -> List[Module]:
        return self._slice(
            module=module,
            reversed=reversed,
            fusion_degree=fusion_degree,
            slice_bias=False,
            dim=1,
        )

    def _parallelize_embedding(self):
        assert hasattr(
            self.model, "get_input_embeddings"
        ), "model must have method named ``get_input_embeddings()``."

        embedding = self.model.get_input_embeddings()
        gpu_index = self.mpu.get_tensor_parallel_rank()
        chunked_weights = torch.chunk(
            embedding.weight, chunks=self.mpu.get_tensor_parallel_world_size(), dim=0
        )
        chunked_weight = chunked_weights[gpu_index]
        embedding.weight.data = chunked_weight
        setattr(embedding.weight, "tp_rank", gpu_index)

        update_module_arguments(
            module=embedding,
            mpu=self.mpu,
            orig_module=copy.deepcopy(embedding.__class__),
        )

        embedding.__class__ = VocabParallelEmbedding

        for name, module in self.model.named_modules():
            if (
                hasattr(module, "weight")
                and module.weight is embedding.weight
                and not isinstance(module, Embedding)
                and not isinstance(module, VocabParallelEmbedding)
            ):
                update_module_arguments(
                    module=module,
                    mpu=self.mpu,
                    reversed=self.tp_mapping.is_reversed_param(self.model, name),
                    fusion_degree=1,
                    orig_module=copy.deepcopy(module.__class__),
                    gather_output=True,
                )

                module.__class__ = ColumnParallelLinear

    def _parallelize_modules(self):
        for param_name, module in self.model.named_modules():
            if self.tp_mapping.is_column_parallel(self.model, param_name):
                self._column_slice(
                    module=module,
                    reversed=self.tp_mapping.is_reversed_param(self.model, param_name),
                    fusion_degree=self.tp_mapping.get_combined_qkv_degree(
                        self.model, param_name, module
                    ),
                )
                module.__class__ = ColumnParallelLinear

            elif self.tp_mapping.is_row_parallel(self.model, param_name):
                self._row_slice(
                    module=module,
                    reversed=self.tp_mapping.is_reversed_param(self.model, param_name),
                    fusion_degree=1,
                )
                module.__class__ = RowParallelLinear

    def _postprocess(self):
        gpu_index = self.mpu.get_tensor_parallel_rank()

        for param in self.model.parameters():
            if not param.is_cuda:
                if torch.is_tensor(param):
                    setattr(param, "tp_rank", gpu_index)

        for param in self.model.buffers():
            if not param.is_cuda:
                if torch.is_tensor(param):
                    setattr(param, "tp_rank", gpu_index)

    def parallelize(self):
        self._update_mp_arguments()
        self._parallelize_embedding()
        self._parallelize_modules()
        self._postprocess()
        update_module_arguments(self.model, mpu=self.mpu)


class TensorDeparallelEngine1D(object):
    def __init__(self, model, mpu, tp_mapping):
        self.model = model
        self.mpu = mpu
        self.tp_mapping = TensorParallelMapping(tp_mapping)

    def _update_mp_arguments(self):
        for module in self.model.modules():
            for elem in self.tp_mapping.update_attrs(self.model):
                if hasattr(module, elem.name):
                    world_size = self.mpu.get_tensor_parallel_world_size()
                    reduced_arg = getattr(module, elem.name) * world_size
                    setattr(module, elem.name, reduced_arg)

    @torch.no_grad()
    def _reconstruct_combined_qkv(self, tensors, fusion_degree, dim):
        result_tensors = {i: [] for i in range(fusion_degree)}
        for tensor in tensors:
            chunks = tensor.chunk(fusion_degree, dim=dim)
            for i, chunk in enumerate(chunks):
                result_tensors[i].append(chunk)
        for key, val in result_tensors.items():
            result_tensors[key] = torch.cat(val, dim=dim)
        return torch.cat(list(result_tensors.values()), dim=dim)

    def _merge(
        self,
        module,
        reversed,
        fusion_degree,
        merge_bias,
        dim,
    ):
        dim = dim if not reversed else abs(dim - 1)
        world_size = self.mpu.get_tensor_parallel_world_size()
        update_module_arguments(module=module, mpu=None)

        if hasattr(module, "weight") and module.weight is not None:
            if module.weight.dim() >= 1:
                if not module.weight.is_contiguous():
                    module.weight.data = module.weight.contiguous()
                if not module.weight.is_cuda:
                    module.weight.data = module.weight.cuda()

                tensor_list = [
                    torch.zeros_like(module.weight) for _ in range(world_size)
                ]

                dist.all_gather(
                    tensor_list,
                    module.weight,
                    group=self.mpu.get_tensor_parallel_group(),
                )

                if fusion_degree > 1:
                    output = self._reconstruct_combined_qkv(
                        tensor_list, fusion_degree, dim
                    )
                else:
                    output = torch.cat(tensor_list, dim=dim)

                module.weight.data = output
                update_module_arguments(
                    module=module,
                    in_features=module.weight.size()[0],
                    out_features=module.weight.size()[1],
                    nx=module.weight.size()[0],
                    nf=module.weight.size()[1],
                )

        if hasattr(module, "bias") and module.bias is not None:
            if merge_bias is True and module.bias.dim() >= 1:
                if not module.bias.is_contiguous():
                    module.bias.data = module.bias.contiguous()
                if not module.bias.is_cuda:
                    module.bias.data = module.bias.cuda()

                tensor_list = [torch.zeros_like(module.bias) for _ in range(world_size)]

                dist.all_gather(
                    tensor_list,
                    module.bias,
                    group=self.mpu.get_tensor_parallel_group(),
                )

                if fusion_degree > 1:
                    output = self._reconstruct_combined_qkv(
                        tensor_list, fusion_degree, 0
                    )
                else:
                    output = torch.cat(tensor_list, dim=0)

                module.bias.data = output

        return module

    def _column_merge(
        self,
        module: Module,
        fusion_degree: int,
        reversed: bool,
    ) -> Module:

        merged_module = self._merge(
            module=module,
            reversed=reversed,
            fusion_degree=fusion_degree,
            merge_bias=True,
            dim=0,
        )

        if hasattr(module, "orig_module"):
            module.__class__ = module.orig_module

        return merged_module

    def _row_merge(
        self,
        module: Module,
        fusion_degree: int,
        reversed: bool,
    ) -> List[Module]:
        merged_module = self._merge(
            module=module,
            reversed=reversed,
            fusion_degree=fusion_degree,
            merge_bias=False,
            dim=1,
        )

        if hasattr(module, "orig_module"):
            module.__class__ = module.orig_module

        return merged_module

    def _deparallelize_embedding(self):
        embedding = self.model.get_input_embeddings()
        if not embedding.weight.is_cuda:
            embedding.weight.data = embedding.weight.cuda()

        gathered_weight = self.mpu._gather(embedding.weight, dim=0)
        embedding.weight.data = gathered_weight

        update_module_arguments(
            module=embedding,
            mpu=None,
            num_embeddings=embedding.weight.size()[0],
        )

        if hasattr(embedding, "orig_module"):
            embedding.__class__ = embedding.orig_module

        for module in self.model.modules():
            if (
                hasattr(module, "weight")
                and module.weight is embedding.weight
                and not isinstance(module, Embedding)
                and not isinstance(module, VocabParallelEmbedding)
            ):
                update_module_arguments(
                    module=module,
                    mpu=None,
                    out_features=embedding.num_embeddings,
                )

                if hasattr(module, "orig_module"):
                    module.__class__ = module.orig_module

                module.weight.data = module.weight

    def _deparallelize_modules(self):
        for param_name, module in self.model.named_modules():
            if self.tp_mapping.is_column_parallel(self.model, param_name):
                self._column_merge(
                    module=module,
                    fusion_degree=module.fusion_degree,
                    reversed=module.reversed,
                )
            elif self.tp_mapping.is_row_parallel(self.model, param_name):
                self._row_merge(
                    module=module,
                    fusion_degree=module.fusion_degree,
                    reversed=module.reversed,
                )

    def deparallelize(self):
        self._update_mp_arguments()
        self._deparallelize_embedding()
        self._deparallelize_modules()
        update_module_arguments(self.model, mpu=None)
