import torch
import oneflow as flow
from oneflow.framework.args_tree import ArgsTree
from onediff.utils import logger

from .utils.hash_utils import generate_input_structure_key


def input_output_processor(func):
    def process_input(*args, **kwargs):
        def input_fn(value):
            if isinstance(value, torch.Tensor):
                # TODO: https://github.com/siliconflow/sd-team/issues/109
                return flow.utils.tensor.from_torch(value.contiguous())
            else:
                return value

        args_tree = ArgsTree((args, kwargs), False, tensor_type=torch.Tensor)

        input_structure_key = generate_input_structure_key(args_tree)
        out = args_tree.map_leaf(input_fn)
        mapped_args = out[0]
        mapped_kwargs = out[1]
        return mapped_args, mapped_kwargs, input_structure_key

    def process_output(output):
        def output_fn(value):
            if isinstance(value, flow.Tensor):
                return flow.utils.tensor.to_torch(value)
            else:
                return value

        out_tree = ArgsTree((output, None), False)
        out = out_tree.map_leaf(output_fn)
        return out[0]

    def wrapper(self: "OneflowDeployableModule", *args, **kwargs):
        mapped_args, mapped_kwargs, input_structure_key = process_input(*args, **kwargs)
        if (
            self._deployable_module_options.use_graph
            and self._deployable_module_enable_dynamic
            and self._deployable_module_dpl_graph is not None
            and self._deployable_module_input_structure_key != input_structure_key
        ):
            logger.warning(
                "Input structure key has changed. Resetting the deployable module graph."
            )
            self._deployable_module_dpl_graph = None
            self._load_graph_first_run = True
            self._deployable_module_input_structure_key = None

        output = func(self, *mapped_args, **mapped_kwargs)
        return process_output(output)

    return wrapper
