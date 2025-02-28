# coding=utf-8
# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""GPT2 style dataset."""

import os
import time
import token

import numpy as np
import torch
from data_utils.tokenization_enc_dec import EncDecTokenizer

from utils import print_rank_0
import mpu
from data.indexed_dataset import make_dataset as make_indexed_dataset
from data.indexed_dataset import MMapIndexedDataset


def get_train_valid_test_split_(splits_string, size):
    """ Get dataset splits from comma or '/' separated string list."""

    splits = []
    if splits_string.find(',') != -1:
        splits = [float(s) for s in splits_string.split(',')]
    elif splits_string.find('/') != -1:
        splits = [float(s) for s in splits_string.split('/')]
    else:
        splits = [float(splits_string)]
    while len(splits) < 3:
        splits.append(0.)
    splits = splits[:3]
    splits_sum = sum(splits)
    assert splits_sum > 0.0
    splits = [split / splits_sum for split in splits]
    splits_index = [0]
    for index, split in enumerate(splits):
        splits_index.append(splits_index[index] +
                            int(round(split * float(size))))
    diff = splits_index[-1] - size
    for index in range(1, len(splits_index)):
        splits_index[index] -= diff
    assert len(splits_index) == 4
    assert splits_index[-1] == size
    return splits_index


def build_train_valid_test_datasets(tokenizer, data_prefix, data_impl, splits_string,
                                    train_valid_test_num_samples,
                                    enc_seq_length, dec_seq_length, seed, skip_warmup):
    """Build train, valid, and test datasets."""

    context_data_prefix = data_prefix + "_context"
    target_data_prefix = data_prefix + "_target" 
    target_offset_data_prefix = data_prefix + "_target_offset"


    # Indexed dataset.
    context_indexed_dataset = get_indexed_dataset_(context_data_prefix,
                                                   data_impl,
                                                   skip_warmup)
    target_indexed_dataset = get_indexed_dataset_(target_data_prefix,
                                                  data_impl,
                                                  skip_warmup)
    target_offset_dataset = get_indexed_dataset_(target_offset_data_prefix,
                                                    data_impl,
                                                    skip_warmup)

    total_num_of_documents = context_indexed_dataset.sizes.shape[0]
    splits = get_train_valid_test_split_(splits_string, total_num_of_documents)

    # Print stats about the splits.
    print_rank_0(' > dataset split:')

    def print_split_stats(name, index):
        print_rank_0('    {}:'.format(name))
        print_rank_0('     document indices in [{}, {}) total of {} '
                     'documents'.format(splits[index], splits[index + 1],
                                        splits[index + 1] - splits[index]))
    print_split_stats('train', 0)
    print_split_stats('validation', 1)
    print_split_stats('test', 2)

    def build_dataset(index, name):
        dataset = None
        if splits[index + 1] > splits[index]:
            documents = np.arange(start=splits[index], stop=splits[index + 1],
                                  step=1, dtype=np.int32)
            dataset = EncDecDataset(tokenizer, name, data_prefix,
                                    documents, context_indexed_dataset, target_indexed_dataset, target_offset_dataset,
                                    train_valid_test_num_samples[index],
                                    enc_seq_length, dec_seq_length, seed)
        return dataset

    train_dataset = build_dataset(0, 'train')
    valid_dataset = build_dataset(1, 'valid')
    test_dataset = build_dataset(2, 'test')

    return (train_dataset, valid_dataset, test_dataset)


def get_indexed_dataset_(data_prefix, data_impl, skip_warmup):
    """Build indexed dataset."""
    print_rank_0(' > building dataset index ...')

    start_time = time.time()
    indexed_dataset = make_indexed_dataset(data_prefix,
                                           data_impl,
                                           skip_warmup)
    print_rank_0(' > finished creating indexed dataset in {:4f} '
                 'seconds'.format(time.time() - start_time))
    print_rank_0('    number of documents: {}'.format(
        indexed_dataset.sizes.shape[0]))

    return indexed_dataset


class EncDecDataset(torch.utils.data.Dataset):

    def __init__(self, tokenizer: EncDecTokenizer, name, data_prefix, documents, context_indexed_dataset: MMapIndexedDataset,
                target_indexed_dataset: MMapIndexedDataset, target_offset_dataset: MMapIndexedDataset, num_samples, enc_seq_length, dec_seq_length, seed):

        self.name = name
        self.context_indexed_dataset = context_indexed_dataset
        self.target_indexed_dataset = target_indexed_dataset
        self.target_offset_dataset = target_offset_dataset
        self.tokenizer = tokenizer
        self.enc_seq_length = enc_seq_length
        self.dec_seq_length = dec_seq_length

        # Checks
        assert np.min(documents) >= 0
        assert np.max(documents) < context_indexed_dataset.sizes.shape[0]

        # Build index mappings.
        self.doc_idx, self.sample_idx, self.shuffle_idx = _build_index_mappings(
            self.name, data_prefix, documents, self.context_indexed_dataset.sizes,
            num_samples, enc_seq_length - 1, seed)
            # NOTE: enc_seq_length - 1: This function is originally designed for autoregressive models, so the output length is actually input length +1

    def __len__(self):
        # -1 is due to data structure used to retieve the index:
        #    sample i --> [sample_idx[i], sample_idx[i+1])
        return self.sample_idx.shape[0] - 1

    def __getitem__(self, idx):
        # Get the shuffled index.
        # NOTE: We do not get shuffle idx because the documents are already shuffled
        idx = self.shuffle_idx[idx]
            
        # Start and end documents and offsets.
        doc_index_f = self.sample_idx[idx][0]
        doc_index_l = self.sample_idx[idx + 1][0]
        offset_f = self.sample_idx[idx][1]
        offset_l = self.sample_idx[idx + 1][1]
        # If we are within the same document, just extract the chunk.
        
        targets_list = []
        
        if doc_index_f == doc_index_l:
            contexts = self.context_indexed_dataset.get(self.doc_idx[doc_index_f],
                                              offset=offset_f,
                                              length=offset_l - offset_f + 1)
            contexts = [int(x) for x in contexts]
            ctx_eod_mask = [0 for _ in range(len(contexts))]
            target_offset = self.target_offset_dataset.get(self.doc_idx[doc_index_f])
            sentinel_count = 0
            for i in range(len(contexts)):
                token_id = contexts[i]
                assert token_id != self.tokenizer.eod_id
                if token_id >= self.tokenizer.vocab_size:
                    x = token_id - self.tokenizer.vocab_size
                    # get sentinel id
                    sentinel_id = self.tokenizer.get_sentinel_id(sentinel_count)
                    # get target
                    target = self.target_indexed_dataset.get(self.doc_idx[doc_index_f], offset=target_offset[2 * x], length=target_offset[2 * x + 1])
                    target = [int(x) for x in target]

                    # mark the eod pos in context
                    if self.tokenizer.eod_id in target:
                        ctx_eod_mask[i] = 1

                    assert target[0] == token_id, (target[0], token_id)
                    contexts[i] = sentinel_id
                    target[0] = sentinel_id

                    targets_list.append(target)

                    sentinel_count += 1

        else:
            # Otherwise, get the rest of the initial document.
            contexts_list = [self.context_indexed_dataset.get(self.doc_idx[doc_index_f], offset=offset_f)]
            target_offset_list = [self.target_offset_dataset.get(self.doc_idx[doc_index_f])]
            # Loop over all in between documents and add the entire document.
            for i in range(doc_index_f + 1, doc_index_l):
                contexts_list.append(self.context_indexed_dataset.get(self.doc_idx[i]))
                target_offset_list.append(self.target_offset_dataset.get(self.doc_idx[i]))
            # And finally add the relevant portion of last document.
            contexts_list.append(self.context_indexed_dataset.get(self.doc_idx[doc_index_l], length=offset_l + 1))
            target_offset_list.append(self.target_offset_dataset.get(self.doc_idx[doc_index_l]))
            
            contexts_list = [[int(x) for x in tmp_contexts] for tmp_contexts in contexts_list]

            sentinel_count = 0
            ctx_eod_mask_list = []
            for (k, tmp_contexts), tmp_target_offset in zip(enumerate(contexts_list), target_offset_list):
                tmp_ctx_eod_mask = [0 for _ in tmp_contexts]
                for i in range(len(tmp_contexts)):
                    token_id = tmp_contexts[i]
                    assert token_id != self.tokenizer.eod_id
                    if token_id >= self.tokenizer.vocab_size:
                        x = token_id - self.tokenizer.vocab_size
                        # get sentinel id
                        sentinel_id = self.tokenizer.get_sentinel_id(sentinel_count)
                        # get target
                        target = self.target_indexed_dataset.get(self.doc_idx[doc_index_f + k], offset=tmp_target_offset[2 * x], length=tmp_target_offset[2 * x + 1])
                        target = [int(x) for x in target]

                        # mark the eod pos in context
                        if self.tokenizer.eod_id in target:
                            tmp_ctx_eod_mask[i] = 1
                        
                        assert target[0] == token_id, (target[0], token_id)
                        tmp_contexts[i] = sentinel_id
                        target[0] = sentinel_id

                        targets_list.append(target)

                        sentinel_count += 1

                ctx_eod_mask_list.append(tmp_ctx_eod_mask)
            
            contexts = [x for y in contexts_list for x in y]
            ctx_eod_mask = [x for y in ctx_eod_mask_list for x in y]

        targets = [x for y in targets_list for x in y]
        targets = [1] + targets

        # targets = []
        # sentinel_count = 0
        # ctx_eod_mask = [0 for _ in range(len(contexts))]
        # for i in range(len(contexts)):
        #     token_id = contexts[i]
        #     assert token_id != self.tokenizer.eod_id
        #     if token_id >= self.tokenizer.vocab_size:
        #         # get sentinel id
        #         sentinel_id = self.tokenizer.get_sentinel_id(sentinel_count)
        #         # get the targets
        #         target = self.target_indexed_dataset.get(self.target_offset)
        #         target = [int(x) for x in target]

        #         # mark the eod pos in context
        #         if self.tokenizer.eod_id in target:
        #             ctx_eod_mask[i] = 1
        #         assert int(token_id) == int(target[0]), "{}, {}".format(token_id, target[0])
        #         # replace with local sentinel ids
        #         contexts[i] = sentinel_id
        #         target[0] = sentinel_id
        #         targets.extend(target)
        #         sentinel_count += 1

        targets.append(self.tokenizer.get_sentinel_id(sentinel_count))

        # if torch.distributed.get_rank() == 0:
        #     print("context", self.tokenizer.decode(contexts))
        #     print("target", self.tokenizer.decode(targets))

        assert len(contexts) == self.enc_seq_length, "contexts length({}) must equal enc_seq_length({})".format(len(contexts), self.enc_seq_length)
        if len(targets) > self.dec_seq_length + 1:
            print("targets length({}) maybe too long, cut to dec_seq_length + 1({})".format(len(targets), self.dec_seq_length + 1))
            targets = targets[:self.dec_seq_length+1]

        labels = targets[1:]
        targets = targets[:-1]

        assert len(targets) <= self.dec_seq_length, "target length: {}, length constrain: {}".format(len(targets), self.dec_seq_length)

        targets = targets + [self.tokenizer.pad_id] * (self.dec_seq_length - len(targets))
        labels = labels + [self.tokenizer.pad_id] * (self.dec_seq_length - len(labels))

        return {
            "contexts": np.array(contexts), 
            "targets": np.array(targets), 
            "labels": np.array(labels),
            "ctx_eod_mask": np.array(ctx_eod_mask)
        }


def _build_index_mappings(name, data_prefix, documents, sizes,
                          num_samples, seq_length, seed):
    """Build doc-idx, sample-idx, and shuffle-idx.
    doc-idx: is an array (ordered) of documents to be used in training.
    sample-idx: is the start document index and document offset for each
       training sample.
    shuffle-idx: maps the sample index into a random index into sample-idx.
    """
    # Number of tokens in each epoch and number of required epochs.
    tokens_per_epoch = _num_tokens(documents, sizes)
    num_epochs = _num_epochs(tokens_per_epoch, seq_length, num_samples)
    # rng state
    np_rng = np.random.RandomState(seed=seed)

    # Filename of the index mappings.
    _filename = data_prefix
    _filename += '_{}_indexmap'.format(name)
    _filename += '_{}ns'.format(num_samples)
    _filename += '_{}sl'.format(seq_length)
    _filename += '_{}s'.format(seed)
    doc_idx_filename = _filename + '_doc_idx.npy'
    sample_idx_filename = _filename + '_sample_idx.npy'
    shuffle_idx_filename = _filename + '_shuffle_idx.npy'

    # Build the indexed mapping if not exist.
    if torch.distributed.get_rank() % 8 == 0:
        if (not os.path.isfile(doc_idx_filename)) or \
           (not os.path.isfile(sample_idx_filename)) or \
           (not os.path.isfile(shuffle_idx_filename)):

            print_rank_0(' > WARNING: could not find index map files, building '
                         'the indices on rank 0 ...')
            # doc-idx.
            start_time = time.time()
            doc_idx = _build_doc_idx(documents, num_epochs, np_rng)
            np.save(doc_idx_filename, doc_idx, allow_pickle=True)
            print_rank_0(' > elasped time to build and save doc-idx mapping '
                         '(seconds): {:4f}'.format(time.time() - start_time))
            # sample-idx.
            start_time = time.time()
            # Use C++ implementation for speed.
            # First compile and then import.
            from data.dataset_utils import compile_helper
            compile_helper()
            from data import helpers
            assert doc_idx.dtype == np.int32
            assert sizes.dtype == np.int32
            sample_idx = helpers.build_sample_idx(sizes, doc_idx, seq_length,
                                                  num_epochs, tokens_per_epoch)
            # sample_idx = _build_sample_idx(sizes, doc_idx, seq_length,
            #                               num_epochs, tokens_per_epoch)
            np.save(sample_idx_filename, sample_idx, allow_pickle=True)
            print_rank_0(' > elasped time to build and save sample-idx mapping '
                         '(seconds): {:4f}'.format(time.time() - start_time))
            # shuffle-idx.
            start_time = time.time()
            # -1 is due to data structure used to retieve the index:
            #    sample i --> [sample_idx[i], sample_idx[i+1])
            shuffle_idx = _build_shuffle_idx(sample_idx.shape[0] - 1, np_rng)
            np.save(shuffle_idx_filename, shuffle_idx, allow_pickle=True)
            print_rank_0(' > elasped time to build and save shuffle-idx mapping'
                         ' (seconds): {:4f}'.format(time.time() - start_time))

    # This should be a barrier but nccl barrier assumes
    # device_index=rank which is not the case for model
    # parallel case
    counts = torch.cuda.LongTensor([1])
    torch.distributed.all_reduce(counts, group=mpu.get_data_parallel_group())
    assert counts[0].item() == torch.distributed.get_world_size(
        group=mpu.get_data_parallel_group())

    # Load mappings.
    start_time = time.time()
    print_rank_0(' > loading doc-idx mapping from {}'.format(
        doc_idx_filename))
    doc_idx = np.load(doc_idx_filename, allow_pickle=True, mmap_mode='r')
    print_rank_0(' > loading sample-idx mapping from {}'.format(
        sample_idx_filename))
    sample_idx = np.load(sample_idx_filename, allow_pickle=True, mmap_mode='r')
    print_rank_0(' > loading shuffle-idx mapping from {}'.format(
        shuffle_idx_filename))
    shuffle_idx = np.load(shuffle_idx_filename, allow_pickle=True, mmap_mode='r')
    print_rank_0('    loaded indexed file in {:3.3f} seconds'.format(
        time.time() - start_time))
    print_rank_0('    total number of samples: {}'.format(
        sample_idx.shape[0]))
    print_rank_0('    total number of epochs: {}'.format(num_epochs))

    return doc_idx, sample_idx, shuffle_idx


def _num_tokens(documents, sizes):
    """Total number of tokens in the dataset."""
    return np.sum(sizes[documents])


def _num_epochs(tokens_per_epoch, seq_length, num_samples):
    """Based on number of samples and sequence lenght, calculate how many
    epochs will be needed."""
    num_epochs = 0
    total_tokens = 0
    while True:
        num_epochs += 1
        total_tokens += tokens_per_epoch
        # -1 is because we need to retrieve seq_length + 1 token each time
        # but the last token will overlap with the first token of the next
        # sample except for the last sample.
        if ((total_tokens - 1) // seq_length) >= num_samples:
            return num_epochs


def _build_doc_idx(documents, num_epochs, np_rng):
    """Build an array with length = number-of-epochs * number-of-dcuments.
    Each index is mapped to a corresponding document."""
    doc_idx = np.mgrid[0:num_epochs, 0:len(documents)][1]
    doc_idx[:] = documents
    doc_idx = doc_idx.reshape(-1)
    doc_idx = doc_idx.astype(np.int32)
    np_rng.shuffle(doc_idx)
    return doc_idx


def _build_sample_idx(sizes, doc_idx, seq_length,
                      num_epochs, tokens_per_epoch):
    """Sample index mapping is a 2D array with sizes
    [number-of-samples + 1, 2] where [..., 0] contains
    the index into `doc_idx` and [..., 1] is the
    starting offset in that document."""

    # Total number of samples. For -1 see comments in `_num_epochs`.
    num_samples = (num_epochs * tokens_per_epoch - 1) // seq_length
    sample_idx = np.zeros([num_samples + 1, 2], dtype=np.int32)

    # Index into sample_idx.
    sample_index = 0
    # Index into doc_idx.
    doc_idx_index = 0
    # Begining offset for each document.
    doc_offset = 0
    # Start with first document and no offset.
    sample_idx[sample_index][0] = doc_idx_index
    sample_idx[sample_index][1] = doc_offset
    sample_index += 1
    while sample_index <= num_samples:
        # Start with a fresh sequence.
        remaining_seq_length = seq_length + 1
        while remaining_seq_length != 0:
            # Get the document length.
            doc_id = doc_idx[doc_idx_index]
            doc_length = sizes[doc_id] - doc_offset
            # And add it to the current sequence.
            remaining_seq_length -= doc_length
            # If we have more than a full sequence, adjust offset and set
            # remaining length to zero so we return from the while loop.
            # Note that -1 here is for the same reason we have -1 in
            # `_num_epochs` calculations.
            if remaining_seq_length <= 0:
                doc_offset += (remaining_seq_length + doc_length - 1)
                remaining_seq_length = 0
            else:
                # Otherwise, start from the begining of the next document.
                doc_idx_index += 1
                doc_offset = 0
        # Record the sequence.
        sample_idx[sample_index][0] = doc_idx_index
        sample_idx[sample_index][1] = doc_offset
        sample_index += 1

    return sample_idx


def _build_shuffle_idx(size, np_rng):
    """Build the range [0, size) and shuffle."""
    dtype_ = np.uint32
    if size >= (np.iinfo(np.uint32).max - 1):
        dtype_ = np.int64
    shuffle_idx = np.arange(start=0, stop=size, step=1, dtype=dtype_)
    np_rng.shuffle(shuffle_idx)
    return shuffle_idx
