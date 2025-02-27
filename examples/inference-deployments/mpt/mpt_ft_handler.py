# Copyright 2022 MosaicML Examples authors
# SPDX-License-Identifier: Apache-2.0

import argparse
import configparser
import copy
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import boto3
import botocore
import torch
import torch.distributed as dist
from huggingface_hub import snapshot_download
from torch.nn.utils.rnn import pad_sequence
from transformers import AutoTokenizer

from FasterTransformer.examples.pytorch.gpt.utils import comm  # isort: skip # yapf: disable # type: ignore
from FasterTransformer.examples.pytorch.gpt.utils.parallel_gpt import ParallelGPT  # isort: skip # yapf: disable # type: ignore
from scripts.inference.convert_hf_mpt_to_ft import convert_mpt_to_ft  # isort: skip # yapf: disable # type: ignore

LOCAL_CHECKPOINT_DIR = '/tmp/mpt'
LOCAL_MODEL_PATH = os.path.join(LOCAL_CHECKPOINT_DIR, 'local_model')


def download_convert(s3_path: Optional[str] = None,
                     hf_path: Optional[str] = None,
                     gcp_path: Optional[str] = None,
                     gpus: int = 1,
                     force_conversion: bool = False):
    """Download model and convert to FasterTransformer format.

    Args:
        s3_path (str): Path for model location in an s3 bucket.
        hf_path (str): Name of the model as on HF hub (e.g., mosaicml/mpt-7b-instruct) or local folder name containing
            the model (e.g., mpt-7b-instruct)
        gcp_path (str): Path for model location in a gcp bucket.
        gpus (int): Number of gpus to use for inference (Default: 1)
        force_conversion (bool): Force conversion to FT even if some features may not work as expected in FT (Default: False)
    """
    if not s3_path and not gcp_path and not hf_path:
        raise RuntimeError(
            'Either s3_path, gcp_path, or hf_path must be provided to download_convert'
        )
    model_name_or_path: str = ''

    # If s3_path or gcp_path is provided, initialize the s3 client for download
    s3 = None
    download_from_path = None
    if s3_path:
        # s3 creds need to already be present as env vars
        s3 = boto3.client('s3')
        download_from_path = s3_path
    if gcp_path:
        s3 = boto3.client(
            's3',
            region_name='auto',
            endpoint_url='https://storage.googleapis.com',
            aws_access_key_id=os.environ['GCS_KEY'],
            aws_secret_access_key=os.environ['GCS_SECRET'],
        )
        download_from_path = gcp_path

    # If either s3_path or gcp_path is provided, download files
    if s3:
        model_name_or_path = LOCAL_MODEL_PATH

        # Download model files
        if os.path.exists(LOCAL_MODEL_PATH):
            print(
                f'[+] Path {LOCAL_MODEL_PATH} already exists, skipping download'
            )
        else:
            Path(LOCAL_MODEL_PATH).mkdir(parents=True, exist_ok=True)

            print(f'Downloading model from path: {download_from_path}')

            parsed_path = urlparse(download_from_path)
            prefix = parsed_path.path.lstrip('/')  # type: ignore

            objs = s3.list_objects_v2(
                Bucket=parsed_path.netloc,
                Prefix=prefix,
            )
            downloaded_file_set = set(os.listdir(LOCAL_MODEL_PATH))
            for obj in objs['Contents']:
                file_key = obj['Key']
                try:
                    file_name = os.path.basename(file_key)
                    if not file_name or file_name.startswith('.'):
                        # Ignore hidden files
                        continue
                    if file_name not in downloaded_file_set:
                        print(
                            f'Downloading {os.path.join(LOCAL_MODEL_PATH, file_name)}...'
                        )
                        s3.download_file(Bucket=parsed_path.netloc,
                                         Key=file_key,
                                         Filename=os.path.join(
                                             LOCAL_MODEL_PATH, file_name))
                except botocore.exceptions.ClientError as e:
                    print(
                        f'Error downloading file with key: {file_key} with error: {e}'
                    )
    elif hf_path:
        print(f'Downloading HF model with name: {hf_path}')
        model_name_or_path = hf_path
        snapshot_download(repo_id=hf_path)

    # This is the format the the conversion script saves the converted checkpoint in
    local_ft_model_path = os.path.join(LOCAL_CHECKPOINT_DIR, f'{gpus}-gpu')
    ckpt_config_path = os.path.join(local_ft_model_path, 'config.ini')

    # Convert model to FT format
    # If FT checkpoint doesn't exist, create it.
    if not os.path.isfile(ckpt_config_path):
        print('Converting model to FT format')
        # Datatype of weights in the HF checkpoint
        weight_data_type = 'fp32'
        convert_mpt_to_ft(model_name_or_path, LOCAL_CHECKPOINT_DIR, gpus,
                          weight_data_type, force_conversion)
        if not os.path.isfile(ckpt_config_path):
            raise RuntimeError('Failed to create FT checkpoint')
    else:
        print(f'Reusing existing FT checkpoint at {local_ft_model_path}')


class MPTFTModelHandler:

    DEFAULT_GENERATE_KWARGS = {
        # Output sequence length to generate.
        'output_len': 256,
        # Beam width for beam search
        'beam_width': 1,
        # top k candidate number
        'top_k': 0,
        # top p probability threshold
        'top_p': 0.95,
        # temperature parameter
        'temperature': 0.8,
        # Penalty for repetitions
        'repetition_penalty': 1.0,
        # Presence penalty. Similar to repetition, but additive rather than multiplicative.
        'presence_penalty': 0.0,
        'beam_search_diversity_rate': 0.0,
        'len_penalty': 0.0,
        'bad_words_list': None,
        # A minimum number of tokens to generate.
        'min_length': 0,
        # if True, use different random seed for sentences in a batch.
        'random_seed': True
    }

    INPUT_KEY = 'input'
    PARAMETERS_KEY = 'parameters'

    def __init__(self,
                 model_name_or_path: str,
                 ft_lib_path: str,
                 inference_data_type: str = 'bf16',
                 int8_mode: int = 0,
                 gpus: int = 1):
        """Fastertransformer model handler for MPT foundation series.

        Args:
            model_name_or_path (str): Name of the model as on HF hub (e.g., mosaicml/mpt-7b-instruct) or local model name (e.g., mpt-7b-instruct)
            ft_lib_path (str): Path to the libth_transformer dynamic lib file(.e.g., build/lib/libth_transformer.so).
            inference_data_type (str): Data type to use for inference (Default: bf16)
            int8_mode (int): The level of quantization to perform. 0: No quantization. All computation in data_type,
                1: Quantize weights to int8, all compute occurs in fp16/bf16. Not supported when data_type is fp32
            gpus (int): Number of gpus to use for inference (Default: 1)
        """
        self.model_name_or_path = model_name_or_path

        self.tokenizer = AutoTokenizer.from_pretrained(self.model_name_or_path,
                                                       trust_remote_code=True)

        # Make sure the seed on all ranks is the same. This is important.
        # Multi-gpu generate calls will hang without this.
        torch.manual_seed(0)

        model_path = os.path.join(LOCAL_CHECKPOINT_DIR, f'{gpus}-gpu')
        ckpt_config_path = os.path.join(model_path, 'config.ini')

        ckpt_config = configparser.ConfigParser()
        ckpt_config.read(ckpt_config_path)

        # Disable this optimization.
        # https://github.com/NVIDIA/FasterTransformer/blob/main/docs/gpt_guide.md#advanced-features
        shared_contexts_ratio = 0.0

        if 'gpt' in ckpt_config.keys():
            head_num = ckpt_config.getint('gpt', 'head_num')
            size_per_head = ckpt_config.getint('gpt', 'size_per_head')
            vocab_size = ckpt_config.getint('gpt', 'vocab_size')
            start_id = ckpt_config.getint('gpt', 'start_id')
            end_id = ckpt_config.getint('gpt', 'end_id')
            layer_num = ckpt_config.getint('gpt', 'num_layer')
            max_seq_len = ckpt_config.getint('gpt', 'max_pos_seq_len')
            weights_data_type = ckpt_config.get('gpt', 'weight_data_type')
            tensor_para_size = ckpt_config.getint('gpt', 'tensor_para_size')
            pipeline_para_size = ckpt_config.getint('gpt',
                                                    'pipeline_para_size',
                                                    fallback=1)
            layernorm_eps = ckpt_config.getfloat('gpt',
                                                 'layernorm_eps',
                                                 fallback=1e-5)
            use_attention_linear_bias = ckpt_config.getboolean(
                'gpt', 'use_attention_linear_bias')
            has_positional_encoding = ckpt_config.getboolean(
                'gpt', 'has_positional_encoding')
        else:
            raise RuntimeError(
                'Unexpected config.ini for the FT checkpoint. Expected FT checkpoint to contain the `gpt` key.'
            )

        self.end_id = end_id

        if not comm.is_model_parallel_initailized():
            comm.initialize_model_parallel(tensor_para_size, pipeline_para_size)

        print('Initializing FasterTransformer')
        self.model = ParallelGPT(
            head_num,
            size_per_head,
            vocab_size,
            start_id,
            end_id,
            layer_num,
            max_seq_len,
            tensor_para_size,
            pipeline_para_size,
            lib_path=ft_lib_path,
            inference_data_type=inference_data_type,
            int8_mode=int8_mode,
            weights_data_type=weights_data_type,
            layernorm_eps=layernorm_eps,
            use_attention_linear_bias=use_attention_linear_bias,
            has_positional_encoding=has_positional_encoding,
            shared_contexts_ratio=shared_contexts_ratio)
        print(f'Loading FT checkpoint from {model_path}')
        if not self.model.load(ckpt_path=model_path):
            raise RuntimeError(
                'Could not load model from a FasterTransformer checkpoint')
        print('FT initialization complete')

        self.device = comm.get_device()

    def _parse_model_request(self, model_request: Dict) -> Tuple[str, Dict]:
        if self.INPUT_KEY not in model_request:
            raise RuntimeError(
                f'"{self.INPUT_KEY}" must be provided to generate call')

        generate_input = model_request[self.INPUT_KEY]

        # Set default generate kwargs
        generate_kwargs = copy.deepcopy(self.DEFAULT_GENERATE_KWARGS)
        # If request contains any additional kwargs, add them to generate_kwargs
        for k, v in model_request.get(self.PARAMETERS_KEY, {}).items():
            generate_kwargs[k] = v

        return generate_input, generate_kwargs

    def _convert_kwargs(self, generate_inputs: List[str],
                        generate_kwargs: Dict):
        """Converts generate_kwargs into required torch types."""
        batch_size = len(generate_inputs)

        # Allow 'max_length' to be an alias for 'output_len'. Makes it less
        # likely clients break when we swap in the FT handler.
        if 'max_length' in generate_kwargs:
            generate_kwargs['output_len'] = generate_kwargs['max_length']
            del generate_kwargs['max_length']

        # Integer args may be floats if the values are from a json payload.
        generate_kwargs['output_len'] = int(generate_kwargs['output_len'])
        generate_kwargs['top_k'] = int(generate_kwargs['top_k']) * torch.ones(
            batch_size, dtype=torch.int32)
        generate_kwargs['top_p'] *= torch.ones(batch_size, dtype=torch.float32)
        generate_kwargs['temperature'] *= torch.ones(batch_size,
                                                     dtype=torch.float32)
        repetition_penalty = generate_kwargs['repetition_penalty']
        generate_kwargs[
            'repetition_penalty'] = None if repetition_penalty == 1.0 else repetition_penalty * torch.ones(
                batch_size, dtype=torch.float32)
        presence_penalty = generate_kwargs['presence_penalty']
        generate_kwargs[
            'presence_penalty'] = None if presence_penalty == 0.0 else presence_penalty * torch.ones(
                batch_size, dtype=torch.float32)
        generate_kwargs['beam_search_diversity_rate'] *= torch.ones(
            batch_size, dtype=torch.float32)
        generate_kwargs['len_penalty'] *= torch.ones(size=[batch_size],
                                                     dtype=torch.float32)
        generate_kwargs['min_length'] = int(
            generate_kwargs['min_length']) * torch.ones(size=[batch_size],
                                                        dtype=torch.int32)
        if generate_kwargs['random_seed']:
            generate_kwargs['random_seed'] = torch.randint(0,
                                                           10000,
                                                           size=[batch_size],
                                                           dtype=torch.int64)

    def _parse_model_requests(
            self, model_requests: List[Dict]) -> Tuple[List[str], Dict]:
        """Splits requests into a flat list of inputs and merged kwargs."""
        generate_inputs = []
        generate_kwargs = {}
        for req in model_requests:
            generate_input, generate_kwarg = self._parse_model_request(req)
            generate_inputs += [generate_input]

            for k, v in generate_kwarg.items():
                if k in generate_kwargs and generate_kwargs[k] != v:
                    raise RuntimeError(
                        f'Request has conflicting values for kwarg {k}')
                generate_kwargs[k] = v

        return generate_inputs, generate_kwargs

    @torch.no_grad()
    def predict(self, model_requests: List[Dict]) -> List[str]:
        generate_inputs, generate_kwargs = self._parse_model_requests(
            model_requests)
        self._convert_kwargs(generate_inputs, generate_kwargs)

        start_ids = [
            torch.tensor(self.tokenizer.encode(c),
                         dtype=torch.int32,
                         device=self.device) for c in generate_inputs
        ]
        start_lengths = [len(ids) for ids in start_ids]
        start_ids = pad_sequence(start_ids,
                                 batch_first=True,
                                 padding_value=self.end_id)
        start_lengths = torch.IntTensor(start_lengths)
        tokens_batch = self.model(start_ids, start_lengths, **generate_kwargs)
        outputs = []
        for tokens in tokens_batch:
            for beam_id in range(generate_kwargs['beam_width']):
                # Do not exclude context input from the output
                # token = tokens[beam_id][start_lengths[i]:]
                token = tokens[beam_id]
                # stop at end_id; This is the same as eos_token_id
                token = token[token != self.end_id]
                output = self.tokenizer.decode(token, skip_special_tokens=True)
                outputs.append(output)
        return outputs

    def predict_stream(self, **model_requests: Dict):
        raise RuntimeError('Streaming is not supported with FasterTransformer!')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawTextHelpFormatter)

    parser.add_argument(
        '--ft_lib_path',
        type=str,
        required=True,
        help=
        'Path to the libth_transformer dynamic lib file(e.g., build/lib/libth_transformer.so.'
    )
    parser.add_argument(
        '--name_or_dir',
        '-i',
        type=str,
        help=
        'HF hub Model name (e.g., mosaicml/mpt-7b) or local dir path to load checkpoint from',
        required=True)
    parser.add_argument('--inference_data_type',
                        '--data_type',
                        type=str,
                        choices=['fp32', 'fp16', 'bf16'],
                        default='bf16')
    parser.add_argument(
        '--int8_mode',
        type=int,
        default=0,
        choices=[0, 1],
        help=
        'The level of quantization to perform. 0: No quantization. All computation in data_type. 1: Quantize weights to int8, all compute occurs in fp16/bf16. Not supported when data_type is fp32'
    )
    parser.add_argument('--gpus',
                        type=int,
                        default=1,
                        help='The number of gpus to use for inference.')

    parser.add_argument(
        '--force',
        action='store_true',
        help=
        'Force conversion to FT even if some features may not work as expected in FT'
    )

    args = parser.parse_args()

    s3_path = None
    hf_path = None
    if 's3' in args.name_or_dir:
        s3_path = args.name_or_dir
    else:
        hf_path = args.name_or_dir

    if not comm.is_model_parallel_initailized():
        # pipeline parallelism is 1 for now
        comm.initialize_model_parallel(tensor_para_size=args.gpus,
                                       pipeline_para_size=1)

    if comm.get_rank() == 0:
        download_convert(s3_path=s3_path,
                         hf_path=hf_path,
                         gpus=args.gpus,
                         force_conversion=args.force)
    if dist.is_initialized():
        dist.barrier()

    model_handle = MPTFTModelHandler(args.name_or_dir, args.ft_lib_path,
                                     args.inference_data_type, args.int8_mode,
                                     args.gpus)
    inputs = {'input': 'Who is the president of the USA?'}
    out = model_handle.predict([inputs])
    print(out[0])
