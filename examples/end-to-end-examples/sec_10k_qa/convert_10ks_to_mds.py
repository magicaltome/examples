# Copyright 2022 MosaicML Examples authors
# SPDX-License-Identifier: Apache-2.0

# Copyright 2022 MosaicML LLM Foundry authors
# SPDX-License-Identifier: Apache-2.0

import os
import tempfile
from argparse import ArgumentParser, Namespace
from typing import Dict, Iterable, Optional

import datasets
from composer.utils import maybe_create_object_store_from_uri, parse_uri
from llmfoundry.data import ConcatTokensDataset
from streaming import MDSWriter
from torch.utils.data import DataLoader, get_worker_info
from tqdm import tqdm
from transformers import AutoTokenizer


def parse_args() -> Namespace:
    """Parse commandline arguments."""
    parser = ArgumentParser(
        description=
        'Convert dataset into MDS format, optionally concatenating and tokenizing'
    )
    parser.add_argument('--max_workers', type=int, default=64)
    parser.add_argument('--remote', type=str, required=False, default=None)
    parser.add_argument('--out_root', type=str, required=True)
    parser.add_argument('--in_root', type=str, required=True)
    parser.add_argument('--dataset_subset',
                        type=str,
                        required=False,
                        default='small_full')
    parser.add_argument('--compression', type=str, default='zstd')

    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument(
        '--concat_tokens',
        type=int,
        help='Convert text to tokens and concatenate up to this many tokens')

    parser.add_argument('--tokenizer', type=str, required=False, default=None)
    parser.add_argument('--bos_text', type=str, required=False, default=None)
    parser.add_argument('--eos_text', type=str, required=False, default=None)
    parser.add_argument('--no_wrap', default=False, action='store_true')

    parsed = parser.parse_args()

    if os.path.isdir(parsed.out_root) and len(
            set(os.listdir(parsed.out_root)).intersection(set(
                parsed.splits))) > 0:
        raise ValueError(
            f'--out_root={parsed.out_root} contains {os.listdir(parsed.out_root)} which cannot overlap with the requested splits {parsed.splits}.'
        )

    # Make sure we have needed concat options
    if (parsed.concat_tokens is not None and
            isinstance(parsed.concat_tokens, int) and parsed.tokenizer is None):
        parser.error(
            'When setting --concat_tokens, you must specify a --tokenizer')

    # now that we have validated them, change BOS/EOS to strings
    if parsed.bos_text is None:
        parsed.bos_text = ''
    if parsed.eos_text is None:
        parsed.eos_text = ''
    return parsed


def build_dataloader(dataset, batch_size) -> DataLoader:
    return DataLoader(
        dataset=dataset,
        sampler=None,
        batch_size=batch_size,
        num_workers=8,
        prefetch_factor=2,
    )


def generate_samples(
        loader: DataLoader,
        truncate_num_samples: Optional[int] = None
) -> Iterable[Dict[str, bytes]]:
    """Generator over samples of a dataloader.

    Args:
       loader (DataLoader): A dataloader emitting batches like {key: [sample0_bytes, sample1_bytes, sample2_bytes, ...]}
       truncate_num_samples (Optional[int]): An optional # of samples to stop at.

    Yields:
        Sample dicts.
    """
    n_samples = 0
    for batch in loader:
        keys = list(batch.keys())
        current_bs = len(batch[keys[0]])
        for idx in range(current_bs):
            if truncate_num_samples is not None and n_samples == truncate_num_samples:
                return
            n_samples += 1
            yield {k: v[idx] for k, v in batch.items()}


class DownloadingIterable:

    def __init__(self, identifiers, input_folder_prefix, output_folder,
                 object_store):
        self.identifiers = [
            identifier.split('|||') for identifier in identifiers
        ]
        self.object_store = object_store
        self.input_folder_prefix = input_folder_prefix
        self.output_folder = output_folder

    def __iter__(self):
        worker_info = get_worker_info()
        worker_id = worker_info.id if worker_info else 0
        num_workers = worker_info.num_workers if worker_info else 1
        identifiers_shard = self.identifiers[worker_id::num_workers]
        print(f'Worker {worker_id} processing {len(identifiers_shard)} files')
        for (_, ticker, report_date) in identifiers_shard:
            os.makedirs(os.path.join(self.output_folder, ticker), exist_ok=True)

            year = report_date.split('-')[0]
            self.object_store.download_object(
                os.path.join(self.input_folder_prefix, ticker,
                             f'sec_{year}_txt.txt'),
                os.path.join(self.output_folder, ticker, f'sec_{year}.txt'),
            )

            with open(
                    os.path.join(self.output_folder, ticker,
                                 f'sec_{year}.txt')) as _txt_file:
                txt = _txt_file.read()

            yield {'text': txt}


def main(
    tokenizer_name: str,
    output_folder: str,
    input_folder: str,
    dataset_subset: str,
    concat_tokens: int,
    eos_text: str,
    bos_text: str,
    no_wrap: bool,
    max_workers: int,
    compression: str,
) -> None:
    """Main: create C4/pile streaming dataset.

    Args:
        args (Namespace): Commandline arguments.
    """
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_name)
    # we will enforce length, so suppress warnings about sequences too long for the model
    tokenizer.model_max_length = int(1e30)
    columns = {'tokens': 'bytes'}

    object_store = maybe_create_object_store_from_uri(input_folder)
    _, _, folder_prefix = parse_uri(input_folder)

    for split in ['validation', 'test', 'train']:
        print(f'Processing {split}')
        with tempfile.TemporaryDirectory() as tmp_dir:
            sub_prefix = os.path.join(folder_prefix, split)
            sec_filing_data = datasets.load_dataset(
                'JanosAudran/financial-reports-sec',
                dataset_subset,
                num_proc=os.cpu_count() // 2,
                split=split)

            def joined_identifier(example):
                example[
                    'joined_identifier'] = f"{example['docID']}|||{example['tickers'][0]}|||{example['reportDate']}"
                return example

            sec_filing_data = sec_filing_data.map(joined_identifier,
                                                  num_proc=os.cpu_count() // 2)
            unique_identifiers = sec_filing_data.unique('joined_identifier')

            print(f'Processing {len(unique_identifiers)} documents')

            downloading_iter = DownloadingIterable(unique_identifiers,
                                                   sub_prefix,
                                                   os.path.join(tmp_dir, split),
                                                   object_store)

            dataset = ConcatTokensDataset(
                hf_dataset=downloading_iter,
                max_length=concat_tokens,
                tokenizer=tokenizer,
                eos_text=eos_text,
                bos_text=bos_text,
                no_wrap=no_wrap,
            )
            # Get samples

            loader = build_dataloader(dataset=dataset, batch_size=512)
            samples = generate_samples(loader)

            # Write samples
            print(f'Converting to MDS format...')
            with MDSWriter(out=os.path.join(output_folder, split),
                           max_workers=max_workers,
                           progress_bar=False,
                           columns=columns,
                           compression=compression) as out:
                for sample in tqdm(samples):
                    out.write(sample)


if __name__ == '__main__':
    args = parse_args()
    main(
        tokenizer_name=args.tokenizer,
        output_folder=args.out_root,
        input_folder=args.in_root,
        dataset_subset=args.dataset_subset,
        concat_tokens=args.concat_tokens,
        eos_text=args.eos_text,
        bos_text=args.bos_text,
        no_wrap=args.no_wrap,
        max_workers=args.max_workers,
        compression=args.compression,
    )
