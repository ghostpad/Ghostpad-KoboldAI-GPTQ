from __future__ import annotations

import time
import torch
import requests
import numpy as np
from typing import List, Optional, Union

import utils
from logger import logger

from modeling.inference_model import (
    GenerationResult,
    GenerationSettings,
    InferenceModel,
    ModelCapabilities,
)

model_backend_name = "Horde"

class HordeException(Exception):
    """To be used for errors on server side of the Horde."""


class model_backend(InferenceModel):
    def __init__(self) -> None:
        super().__init__()
        self.url = "https://horde.koboldai.net"
        self.key = "0000000000"
        self.models = self.get_cluster_models()
        

        # Do not allow API to be served over the API
        self.capabilties = ModelCapabilities(api_host=False)

    def is_valid(self, model_name, model_path, menu_path):
        logger.debug("Horde Models: {}".format(self.models))
        return model_name == "CLUSTER" or model_name in [x['value'] for x in self.models]
    
    def get_requested_parameters(self, model_name, model_path, menu_path):
        requested_parameters = []
        requested_parameters.extend([{
                                        "uitype": "text",
                                        "unit": "text",
                                        "label": "URL",
                                        "id": "url",
                                        "default": self.url,
                                        "tooltip": "URL to the horde.",
                                        "menu_path": "",
                                        "check": {"value": "", 'check': "!="},
                                        "refresh_model_inputs": True,
                                        "extra_classes": ""
                                    },
                                    {
                                        "uitype": "text",
                                        "unit": "text",
                                        "label": "Key",
                                        "id": "key",
                                        "default": self.key,
                                        "check": {"value": "", 'check': "!="},
                                        "tooltip": "User Key to use when connecting to Horde (0000000000 is anonymous).",
                                        "menu_path": "",
                                        "refresh_model_inputs": True,
                                        "extra_classes": ""
                                    },
                                    {
                                        "uitype": "dropdown",
                                        "unit": "text",
                                        "label": "Model",
                                        "id": "model",
                                        "default": model_name,
                                        "check": {"value": "", 'check': "!="},
                                        "tooltip": "Which model to use when running OpenAI/GooseAI.",
                                        "menu_path": "",
                                        "refresh_model_inputs": False,
                                        "extra_classes": "",
                                        'children': self.models,

                                    }])
        return requested_parameters
        
    def set_input_parameters(self, parameters):
        self.key = parameters['key'].strip()
        self.model = parameters['model']
        self.url = parameters['url']
        
    def get_cluster_models(self):
        # Get list of models from public cluster
        logger.info("<purple>Retrieving engine list...</purple>")
        try:
            req = requests.get(f"{self.url}/api/v2/status/models?type=text")
        except:
            logger.init_err("KAI Horde Models", status="Failed")
            logger.error("Provided KoboldAI Horde URL unreachable")
            emit('from_server', {'cmd': 'errmsg', 'data': "Provided KoboldAI Horde URL unreachable"})
            return
        if not req.ok:
            # Something went wrong, print the message and quit since we can't initialize an engine
            logger.init_err("KAI Horde Models", status="Failed")
            logger.error(req.json())
            emit('from_server', {'cmd': 'errmsg', 'data': req.json()}, room="UI_1")
            return

        engines = req.json()
        try:
            engines = [{"text": en["name"], "value": en["name"]} for en in engines]
        except:
            logger.error(engines)
            raise
        logger.debug(engines)
        
        online_model = ""

        logger.init_ok("KAI Horde Models", status="OK")

        return engines

    def _load(self, save_model: bool, initial_load: bool) -> None:
        self.tokenizer = self._get_tokenizer(
            self.model
            #if len(self.model) > 0
            #else "gpt2",
        )

    def _raw_generate(
        self,
        prompt_tokens: Union[List[int], torch.Tensor],
        max_new: int,
        gen_settings: GenerationSettings,
        single_line: bool = False,
        batch_count: int = 1,
        seed: Optional[int] = None,
        **kwargs,
    ) -> GenerationResult:
        if seed is not None:
            logger.warning(
                "Seed is unsupported on the APIInferenceModel. Seed will be ignored."
            )

        decoded_prompt = utils.decodenewlines(self.tokenizer.decode(prompt_tokens))

        # Store context in memory to use it for comparison with generated content
        utils.koboldai_vars.lastctx = decoded_prompt

        # Build request JSON data
        reqdata = {
            "max_length": max_new,
            "max_context_length": utils.koboldai_vars.max_length,
            "rep_pen": gen_settings.rep_pen,
            "rep_pen_slope": gen_settings.rep_pen_slope,
            "rep_pen_range": gen_settings.rep_pen_range,
            "temperature": gen_settings.temp,
            "top_p": gen_settings.top_p,
            "top_k": int(gen_settings.top_k),
            "top_a": gen_settings.top_a,
            "tfs": gen_settings.tfs,
            "typical": gen_settings.typical,
            "n": batch_count,
        }

        cluster_metadata = {
            "prompt": decoded_prompt,
            "params": reqdata,
            "models": [x for x in utils.koboldai_vars.cluster_requested_models if x],
            "trusted_workers": False,
        }

        client_agent = "KoboldAI:2.0.0:koboldai.org"
        cluster_headers = {
            "apikey": self.key,
            "Client-Agent": client_agent,
        }

        try:
            # Create request
            req = requests.post(
                f"{self.url}/api/v2/generate/text/async",
                json=cluster_metadata,
                headers=cluster_headers,
            )
        except requests.exceptions.ConnectionError:
            errmsg = f"Horde unavailable. Please try again later"
            logger.error(errmsg)
            raise HordeException(errmsg)

        if req.status_code == 503:
            errmsg = f"KoboldAI API Error: No available KoboldAI servers found in Horde to fulfil this request using the selected models or other properties."
            logger.error(errmsg)
            raise HordeException(errmsg)
        elif not req.ok:
            errmsg = f"KoboldAI API Error: Failed to get a standard reply from the Horde. Please check the console."
            logger.error(req.url)
            logger.error(errmsg)
            logger.error(req.text)
            raise HordeException(errmsg)

        try:
            req_status = req.json()
        except requests.exceptions.JSONDecodeError:
            errmsg = f"Unexpected message received from the Horde: '{req.text}'"
            logger.error(errmsg)
            raise HordeException(errmsg)

        request_id = req_status["id"]
        logger.debug("Horde Request ID: {}".format(request_id))

        # We've sent the request and got the ID back, now we need to watch it to see when it finishes
        finished = False

        cluster_agent_headers = {"Client-Agent": client_agent}

        while not finished:
            try:
                req = requests.get(
                    f"{self.url}/api/v2/generate/text/status/{request_id}",
                    headers=cluster_agent_headers,
                )
            except requests.exceptions.ConnectionError:
                errmsg = f"Horde unavailable. Please try again later"
                logger.error(errmsg)
                raise HordeException(errmsg)

            if not req.ok:
                errmsg = f"KoboldAI API Error: Failed to get a standard reply from the Horde. Please check the console."
                logger.error(req.text)
                raise HordeException(errmsg)

            try:
                req_status = req.json()
            except requests.exceptions.JSONDecodeError:
                errmsg = (
                    f"Unexpected message received from the KoboldAI Horde: '{req.text}'"
                )
                logger.error(errmsg)
                raise HordeException(errmsg)

            if "done" not in req_status:
                errmsg = f"Unexpected response received from the KoboldAI Horde: '{req_status}'"
                logger.error(errmsg)
                raise HordeException(errmsg)

            finished = req_status["done"]
            utils.koboldai_vars.horde_wait_time = req_status["wait_time"]
            utils.koboldai_vars.horde_queue_position = req_status["queue_position"]
            utils.koboldai_vars.horde_queue_size = req_status["waiting"]

            if not finished:
                logger.debug(req_status)
                time.sleep(1)

        logger.debug("Last Horde Status Message: {}".format(req_status))

        if req_status["faulted"]:
            raise HordeException("Horde Text generation faulted! Please try again.")

        generations = req_status["generations"]
        gen_servers = [(cgen["worker_name"], cgen["worker_id"]) for cgen in generations]
        logger.info(f"Generations by: {gen_servers}")

        return GenerationResult(
            model=self,
            out_batches=np.array(
                [self.tokenizer.encode(cgen["text"]) for cgen in generations]
            ),
            prompt=prompt_tokens,
            is_whole_generation=True,
            single_line=single_line,
        )
