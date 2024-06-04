import os
import warnings
import zipfile
from pathlib import Path
from typing import Dict, Union

import gradio as gr
import modules.scripts as scripts
import modules.shared as shared
from compile_ldm import SD21CompileCtx, compile_ldm_unet
from compile_sgm import compile_sgm_unet
from compile_vae import VaeCompileCtx
from modules import script_callbacks
from modules.processing import process_images
from modules.sd_models import select_checkpoint
from modules.ui_common import create_refresh_button
from onediff_hijack import do_hijack as onediff_do_hijack
from onediff_lora import HijackLoraActivate
from oneflow import __version__ as oneflow_version

from onediff import __version__ as onediff_version
from onediff.optimization.quant_optimizer import (
    quantize_model,
    varify_can_use_quantization,
)
from onediff.utils import logger, parse_boolean_from_env

def on_ui_settings():
    section = ("onediff", "OneDiff")
    shared.opts.add_option(
        "onediff_compiler_caches_path",
        shared.OptionInfo(
            str(Path(__file__).parent.parent / "compiler_caches"),
            "Directory for onediff compiler caches",
            section=section,
        ),
    )


script_callbacks.on_ui_settings(on_ui_settings)
onediff_do_hijack()

from ui_utils import (
    all_compiler_caches_path,
    get_all_compiler_caches,
    hints_message,
    refresh_all_compiler_caches,
)

"""oneflow_compiled UNetModel"""
compiled_unet = None
is_unet_quantized = False
compiled_ckpt_name = None


def generate_graph_path(ckpt_name: str, model_name: str) -> str:
    base_output_dir = shared.opts.outdir_samples or shared.opts.outdir_txt2img_samples
    save_ckpt_graphs_path = os.path.join(base_output_dir, "graphs", ckpt_name)
    os.makedirs(save_ckpt_graphs_path, exist_ok=True)

    file_name = f"{model_name}_graph_{onediff_version}_oneflow_{oneflow_version}"

    graph_file_path = os.path.join(save_ckpt_graphs_path, file_name)

    return graph_file_path


def get_calibrate_info(filename: str) -> Union[None, Dict]:
    calibration_path = Path(select_checkpoint().filename).parent / filename
    if not calibration_path.exists():
        return None

    logger.info(f"Got calibrate info at {str(calibration_path)}")
    calibrate_info = {}
    with open(calibration_path, "r") as f:
        for line in f.readlines():
            line = line.strip()
            items = line.split(" ")
            calibrate_info[items[0]] = [
                float(items[1]),
                int(items[2]),
                [float(x) for x in items[3].split(",")],
            ]
    return calibrate_info


def compile_unet(
    unet_model, quantization=False, *, options=None,
):
    from ldm.modules.diffusionmodules.openaimodel import UNetModel as UNetModelLDM
    from sgm.modules.diffusionmodules.openaimodel import UNetModel as UNetModelSGM

    if isinstance(unet_model, UNetModelLDM):
        compiled_unet = compile_ldm_unet(unet_model, options=options)
    elif isinstance(unet_model, UNetModelSGM):
        compiled_unet = compile_sgm_unet(unet_model, options=options)
    else:
        warnings.warn(
            f"Unsupported model type: {type(unet_model)} for compilation , skip",
            RuntimeWarning,
        )
        compiled_unet = unet_model
    # In OneDiff Community, quantization can be True when called by api
    if quantization and varify_can_use_quantization():
        calibrate_info = get_calibrate_info(
            f"{Path(select_checkpoint().filename).stem}_sd_calibrate_info.txt"
        )
        compiled_unet = quantize_model(
            compiled_unet, inplace=False, calibrate_info=calibrate_info
        )
    return compiled_unet


class UnetCompileCtx(object):
    """The unet model is stored in a global variable.
    The global variables need to be replaced with compiled_unet before process_images is run,
    and then the original model restored so that subsequent reasoning with onediff disabled meets expectations.
    """

    def __enter__(self):
        self._original_model = shared.sd_model.model.diffusion_model
        global compiled_unet
        shared.sd_model.model.diffusion_model = compiled_unet

    def __exit__(self, exc_type, exc_val, exc_tb):
        shared.sd_model.model.diffusion_model = self._original_model
        return False


class Script(scripts.Script):
    current_type = None

    def title(self):
        return "onediff_diffusion_model"

    def ui(self, is_img2img):
        """this function should create gradio UI elements. See https://gradio.app/docs/#components
        The return value should be an array of all components that are used in processing.
        Values of those returned components will be passed to run() and process() functions.
        """
                
        with gr.Row():
            # TODO: set choices as Tuple[str, str] after the version of gradio specified webui upgrades
            compiler_cache = gr.Dropdown(
                label="Compiler caches (Beta)",
                choices=["None"] + get_all_compiler_caches(),
                value="None",
                elem_id="onediff_compiler_cache",
            )
            create_refresh_button(
                compiler_cache,
                refresh_all_compiler_caches,
                lambda: {"choices": ["None"] + get_all_compiler_caches()},
                "onediff_refresh_compiler_caches",
            )
            save_cache_name = gr.Textbox(label="Saved cache name (Beta)")
        with gr.Row():
            always_recompile = gr.components.Checkbox(
                label="always_recompile",
                visible=parse_boolean_from_env("ONEDIFF_DEBUG"),
            )
        gr.HTML(hints_message, elem_id="hintMessage", visible=not varify_can_use_quantization())
        is_quantized = gr.components.Checkbox(
            label="Model Quantization(int8) Speed Up",
            visible=varify_can_use_quantization(),
        )
        return [is_quantized, compiler_cache, save_cache_name, always_recompile]

    def show(self, is_img2img):
        return True

    def check_model_change(self, model):
        is_changed = False

        def get_model_type(model):
            return {
                "is_sdxl": model.is_sdxl,
                "is_sd2": model.is_sd2,
                "is_sd1": model.is_sd1,
                "is_ssd": model.is_ssd,
            }

        if self.current_type is None:
            is_changed = True
        else:
            for key, v in self.current_type.items():
                if v != getattr(model, key):
                    is_changed = True
                    break

        if is_changed is True:
            self.current_type = get_model_type(model)
        return is_changed

    def run(
        self,
        p,
        quantization=False,
        compiler_cache=None,
        saved_cache_name="",
        always_recompile=False,
    ):
        
        global compiled_unet, compiled_ckpt_name, is_unet_quantized
        current_checkpoint = shared.opts.sd_model_checkpoint
        original_diffusion_model = shared.sd_model.model.diffusion_model

        ckpt_changed = current_checkpoint != compiled_ckpt_name
        model_changed = self.check_model_change(shared.sd_model)
        quantization_changed = quantization != is_unet_quantized
        need_recompile = (
            (
                quantization and ckpt_changed
            )  # always recompile when switching ckpt with 'int8 speed model' enabled
            or model_changed  # always recompile when switching model to another structure
            or quantization_changed  # always recompile when switching model from non-quantized to quantized (and vice versa)
            or always_recompile
        )

        is_unet_quantized = quantization
        compiled_ckpt_name = current_checkpoint
        if need_recompile:
            compiled_unet = compile_unet(
                original_diffusion_model, quantization=quantization
            )

            # Due to the version of gradio compatible with sd-webui, the CompilerCache dropdown box always returns a string
            if compiler_cache not in [None, "None"]:
                compiler_cache_path = all_compiler_caches_path() + f"/{compiler_cache}"
                if not Path(compiler_cache_path).exists():
                    raise FileNotFoundError(
                        f"Cannot find cache {compiler_cache_path}, please make sure it exists"
                    )
                try:
                    compiled_unet.load_graph(compiler_cache_path, run_warmup=True)
                except zipfile.BadZipFile:
                    raise RuntimeError(
                        "Load cache failed. Please make sure that the --disable-safe-unpickle parameter is added when starting the webui"
                    )
                except Exception as e:
                    raise RuntimeError(
                        f"Load cache failed ({e}). Please make sure cache has the same sd version (or unet architure) with current checkpoint"
                    )

        else:
            logger.info(
                f"Model {current_checkpoint} has same sd type of graph type {self.current_type}, skip compile"
            )

        with UnetCompileCtx(), VaeCompileCtx(), SD21CompileCtx(), HijackLoraActivate():
            proc = process_images(p)

        if saved_cache_name != "":
            if not os.access(str(all_compiler_caches_path()), os.W_OK):
                raise PermissionError(
                    f"The directory {all_compiler_caches_path()} does not have write permissions, and compiler cache cannot be written to this directory. \
                                      Please change it in the settings to a directory with write permissions"
                )
            if not Path(all_compiler_caches_path()).exists():
                Path(all_compiler_caches_path()).mkdir()
            saved_cache_name = all_compiler_caches_path() + f"/{saved_cache_name}"
            if not Path(saved_cache_name).exists():
                compiled_unet.save_graph(saved_cache_name)

        return proc


