#!/usr/bin/env python
#  type: ignore


# coding=utf-8
# Copyright 2023 Authors of "A Watermark for Large Language Models"
# available at https://arxiv.org/abs/2301.10226
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

from __future__ import annotations

import collections
from functools import lru_cache
from itertools import chain, tee
from math import sqrt

import scipy.stats
import torch
from tokenizers import Tokenizer
from transformers import AutoTokenizer, LogitsProcessor

from .alternative_prf_schemes import prf_lookup, seeding_scheme_lookup
from .normalizers import normalization_strategy_lookup


class WatermarkBase:
    def __init__(
        self,
        vocab: list[int] = None,
        gamma: float = 0.25,
        delta: float = 2.0,
        seeding_scheme: str = "selfhash",  # simple default, find more schemes in alternative_prf_schemes.py
        select_green_tokens: bool = True,  # should always be the default if not running in legacy mode
    ):
        # patch now that None could now maybe be passed as seeding_scheme
        if seeding_scheme is None:
            seeding_scheme = "selfhash"

        # Vocabulary setup
        self.vocab = vocab
        self.vocab_size = len(vocab)

        # Watermark behavior:
        self.gamma = gamma
        self.delta = delta
        self.rng = None
        self._initialize_seeding_scheme(seeding_scheme)
        # Legacy behavior:
        self.select_green_tokens = select_green_tokens

    def _initialize_seeding_scheme(self, seeding_scheme: str) -> None:
        """Initialize all internal settings of the seeding strategy from a colloquial, "public" name for the scheme."""
        self.prf_type, self.context_width, self.self_salt, self.hash_key = seeding_scheme_lookup(
            seeding_scheme
        )

    def _seed_rng(self, input_ids: torch.LongTensor) -> None:
        """Seed RNG from local context. Not batched, because the generators we use (like cuda.random) are not batched."""
        # Need to have enough context for seed generation
        if input_ids.shape[-1] < self.context_width:
            raise ValueError(
                f"seeding_scheme requires at least a {self.context_width} token prefix to seed the"
                " RNG."
            )

        prf_key = prf_lookup[self.prf_type](
            input_ids[-self.context_width :], salt_key=self.hash_key
        )
        # enable for long, interesting streams of pseudorandom numbers: print(prf_key)
        self.rng.manual_seed(prf_key % (2**64 - 1))  # safeguard against overflow from long

    def _get_greenlist_ids(self, input_ids: torch.LongTensor) -> torch.LongTensor:
        """Seed rng based on local context width and use this information to generate ids on the green list."""
        self._seed_rng(input_ids)

        greenlist_size = int(self.vocab_size * self.gamma)
        vocab_permutation = torch.randperm(
            self.vocab_size, device=input_ids.device, generator=self.rng
        )
        if self.select_green_tokens:  # directly
            greenlist_ids = vocab_permutation[:greenlist_size]  # new
        else:  # select green via red
            greenlist_ids = vocab_permutation[
                (self.vocab_size - greenlist_size) :
            ]  # legacy behavior

        return greenlist_ids


class WatermarkLogitsProcessor(WatermarkBase, LogitsProcessor):
    """LogitsProcessor modifying model output scores in a pipe. Can be used in any HF pipeline to modify scores to fit the watermark,
    but can also be used as a standalone tool inserted for any model producing scores inbetween model outputs and next token sampler.
    """

    def __init__(
        self,
        *args,
        store_spike_ents: bool = False,
        device: torch.device = None,
        tokenizer: AutoTokenizer = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self.store_spike_ents = store_spike_ents
        self.spike_entropies = None
        if self.store_spike_ents:
            self._init_spike_entropies()

        # NOTE used to be lazy initialized when inputs are given to co-locate but we will just assume it's all on the same device and put it here
        self.rng = torch.Generator(device=device)
        self.tokenizer = tokenizer

    def _init_spike_entropies(self):
        alpha = torch.exp(torch.tensor(self.delta)).item()
        gamma = self.gamma

        self.z_value = ((1 - gamma) * (alpha - 1)) / (1 - gamma + (alpha * gamma))
        self.expected_gl_coef = (gamma * alpha) / (1 - gamma + (alpha * gamma))

        # catch for overflow when bias is "infinite"
        if alpha == torch.inf:
            self.z_value = 1.0
            self.expected_gl_coef = 1.0

    def _get_spike_entropies(self):
        spike_ents = [[] for _ in range(len(self.spike_entropies))]
        for b_idx, ent_tensor_list in enumerate(self.spike_entropies):
            for ent_tensor in ent_tensor_list:
                spike_ents[b_idx].append(ent_tensor.item())
        return spike_ents

    def _get_and_clear_stored_spike_ents(self):
        spike_ents = self._get_spike_entropies()
        self.spike_entropies = None
        return spike_ents

    def _compute_spike_entropy(self, scores):
        # precomputed z value in init
        probs = scores.softmax(dim=-1)
        denoms = 1 + (self.z_value * probs)
        renormed_probs = probs / denoms
        sum_renormed_probs = renormed_probs.sum()
        return sum_renormed_probs

    def _calc_greenlist_mask(self, scores: torch.Tensor, greenlist_token_ids) -> torch.BoolTensor:
        # Cannot lose loop, greenlists might have different lengths
        green_tokens_mask = torch.zeros_like(scores, dtype=torch.bool)
        for b_idx, greenlist in enumerate(greenlist_token_ids):
            if len(greenlist) > 0:
                green_tokens_mask[b_idx][greenlist] = True
        return green_tokens_mask

    def _bias_greenlist_logits(
        self, scores: torch.Tensor, greenlist_mask: torch.Tensor, greenlist_bias: float
    ) -> torch.Tensor:
        scores[greenlist_mask] = scores[greenlist_mask] + greenlist_bias
        return scores

    def _score_rejection_sampling(
        self, input_ids: torch.LongTensor, scores: torch.Tensor, tail_rule="fixed_compute"
    ) -> list[int]:
        """Generate greenlist based on current candidate next token. Reject and move on if necessary. Method not batched."""
        sorted_scores, greedy_predictions = scores.sort(dim=-1, descending=True)

        final_greenlist = []
        for idx, prediction_candidate in enumerate(greedy_predictions):
            greenlist_ids = self._get_greenlist_ids(
                torch.cat([input_ids, prediction_candidate[None]], dim=0)
            )  # add candidate to prefix
            if prediction_candidate in greenlist_ids:  # test for consistency
                final_greenlist.append(prediction_candidate)

            # What follows below are optional early-stopping rules for efficiency
            if tail_rule == "fixed_score":
                if sorted_scores[0] - sorted_scores[idx + 1] > self.delta:
                    break
            elif tail_rule == "fixed_list_length":
                if len(final_greenlist) == 10:
                    break
            elif tail_rule.startswith("fixed_compute"):
                pieces = tail_rule.split("_")
                if len(pieces) == 3:
                    limit = int(pieces[2])
                else:
                    limit = 40

                if idx == limit:
                    break
            else:
                pass  # do not break early
        return torch.as_tensor(final_greenlist, device=input_ids.device)

    def __call__(self, input_ids: torch.LongTensor, scores: torch.Tensor) -> torch.Tensor:
        """Call with previous context as input_ids, and scores for next token."""

        # this is lazy to allow us to co-locate on the watermarked model's device
        # self.rng = torch.Generator(device=input_ids.device) if self.rng is None else self.rng
        # was lazy but just give device at init

        # NOTE, it would be nice to get rid of this batch loop, but currently,
        # the seed and partition operations are not tensor/vectorized, thus
        # each sequence in the batch needs to be treated separately.
        list_of_greenlist_ids = [None for _ in input_ids]  # Greenlists could differ in length
        # probably for self_salt only because 25% you're in your own but maybe not?
        for b_idx, input_seq in enumerate(input_ids):
            if self.self_salt:
                greenlist_ids = self._score_rejection_sampling(input_seq, scores[b_idx])
            else:
                greenlist_ids = self._get_greenlist_ids(input_seq)
            list_of_greenlist_ids[b_idx] = greenlist_ids

            # logic for computing and storing spike entropies for analysis
            if self.store_spike_ents:
                if self.spike_entropies is None:
                    self.spike_entropies = [[] for _ in range(input_ids.shape[0])]
                self.spike_entropies[b_idx].append(self._compute_spike_entropy(scores[b_idx]))

        green_tokens_mask = self._calc_greenlist_mask(
            scores=scores, greenlist_token_ids=list_of_greenlist_ids
        )
        scores = self._bias_greenlist_logits(
            scores=scores, greenlist_mask=green_tokens_mask, greenlist_bias=self.delta
        )

        return scores


class HardWatermarkLogitsProcessor(WatermarkLogitsProcessor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def _bias_greenlist_logits(
        self, scores: torch.Tensor, greenlist_mask: torch.Tensor, greenlist_bias: float
    ) -> torch.Tensor:
        scores[~greenlist_mask] = 0.0
        return scores


class WatermarkDetector(WatermarkBase):
    """This is the detector for all watermarks imprinted with WatermarkLogitsProcessor.

    The detector needs to be given the exact same settings that were given during text generation  to replicate the watermark
    greenlist generation and so detect the watermark.
    This includes the correct device that was used during text generation, the correct tokenizer, the correct
    seeding_scheme name, and parameters (delta, gamma).

    Optional arguments are
    * normalizers ["unicode", "homoglyphs", "truecase"] -> These can mitigate modifications to generated text that could trip the watermark
    * ignore_repeated_ngrams -> This option changes the detection rules to count every unique ngram only once. (Where n is the size of the context)
    * z_threshold -> Changing this threshold will change the sensitivity of the detector.
    """

    def __init__(
        self,
        *args,
        device: torch.device = None,
        tokenizer: Tokenizer = None,
        z_threshold: float = 4.0,
        normalizers: list[str] = ["unicode"],  # or also: ["unicode", "homoglyphs", "truecase"]
        ignore_repeated_ngrams: bool = True,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        # also configure the metrics returned/preprocessing options
        assert device, "Must pass device"
        assert tokenizer, "Need an instance of the generating tokenizer to perform detection"

        self.tokenizer = tokenizer
        self.device = device
        self.z_threshold = z_threshold
        self.rng = torch.Generator(device=self.device)

        self.normalizers = []
        for normalization_strategy in normalizers:
            self.normalizers.append(normalization_strategy_lookup(normalization_strategy))
        self.ignore_repeated_ngrams = ignore_repeated_ngrams

    def dummy_detect(  # noqa: C901
        self,
        return_prediction: bool = True,
        return_scores: bool = True,
        z_threshold: float = None,
        return_num_tokens_scored: bool = True,
        return_num_green_tokens: bool = True,
        return_green_fraction: bool = True,
        return_token_mask: bool = False,
        return_all_window_scores: bool = False,
        return_z_score: bool = True,
        return_z_at_T: bool = True,
        return_p_value: bool = True,
    ):
        # HF-style output dictionary
        score_dict = dict()
        if return_num_tokens_scored:
            score_dict.update(dict(num_tokens_scored=float("nan")))
        if return_num_green_tokens:
            score_dict.update(dict(num_green_tokens=float("nan")))
        if return_green_fraction:
            score_dict.update(dict(green_fraction=float("nan")))
        if return_z_score:
            score_dict.update(dict(z_score=float("nan")))
        if return_p_value:
            z_score = score_dict.get("z_score")
            if z_score is None:
                z_score = float("nan")
            score_dict.update(dict(p_value=float("nan")))
        if return_token_mask:
            score_dict.update(dict(green_token_mask=[]))
        if return_all_window_scores:
            score_dict.update(dict(window_list=[]))
        if return_z_at_T:
            score_dict.update(dict(z_score_at_T=torch.tensor([])))

        output_dict = {}
        if return_scores:
            output_dict.update(score_dict)
        # if passed return_prediction then perform the hypothesis test and return the outcome
        if return_prediction:
            z_threshold = z_threshold if z_threshold else self.z_threshold
            assert (
                z_threshold is not None
            ), "Need a threshold in order to decide outcome of detection test"
            output_dict["prediction"] = False

        return output_dict

    def _compute_z_score(self, observed_count, T):
        # count refers to number of green tokens, T is total number of tokens
        expected_count = self.gamma
        numer = observed_count - expected_count * T
        denom = sqrt(T * expected_count * (1 - expected_count))
        z = numer / denom
        return z

    def _compute_p_value(self, z):
        p_value = scipy.stats.norm.sf(z)
        return p_value

    @lru_cache(maxsize=2**32)
    def _get_ngram_score_cached(self, prefix: tuple[int], target: int):
        """Expensive re-seeding and sampling is cached."""
        # Handle with care, should ideally reset on __getattribute__ access to self.prf_type, self.context_width, self.self_salt, self.hash_key
        greenlist_ids = self._get_greenlist_ids(torch.as_tensor(prefix, device=self.device))
        return True if target in greenlist_ids else False

    def _score_ngrams_in_passage(self, input_ids: torch.Tensor):
        """Core function to gather all ngrams in the input and compute their watermark."""
        if len(input_ids) - self.context_width < 1:
            raise ValueError(
                f"Must have at least {1} token to score after the first"
                f" min_prefix_len={self.context_width} tokens required by the seeding scheme."
            )

        # Compute scores for all ngrams contexts in the passage:
        token_ngram_generator = ngrams(
            input_ids.cpu().tolist(), self.context_width + 1 - self.self_salt
        )
        frequencies_table = collections.Counter(token_ngram_generator)
        ngram_to_watermark_lookup = {}
        for idx, ngram_example in enumerate(frequencies_table.keys()):
            prefix = ngram_example if self.self_salt else ngram_example[:-1]
            target = ngram_example[-1]
            ngram_to_watermark_lookup[ngram_example] = self._get_ngram_score_cached(prefix, target)

        return ngram_to_watermark_lookup, frequencies_table

    def _get_green_at_T_booleans(self, input_ids, ngram_to_watermark_lookup) -> tuple[torch.Tensor]:
        """Generate binary list of green vs. red per token, a separate list that ignores repeated ngrams, and a list of offsets to
        convert between both representations:
        green_token_mask = green_token_mask_unique[offsets] except for all locations where otherwise a repeat would be counted
        """
        green_token_mask, green_token_mask_unique, offsets, rev_offsets = [], [], [], []
        used_ngrams = {}
        unique_ngram_idx = 0
        ngram_examples = ngrams(input_ids.cpu().tolist(), self.context_width + 1 - self.self_salt)

        for idx, ngram_example in enumerate(ngram_examples):
            green_token_mask.append(ngram_to_watermark_lookup[ngram_example])
            if self.ignore_repeated_ngrams:
                if ngram_example in used_ngrams:
                    pass
                else:
                    used_ngrams[ngram_example] = True
                    unique_ngram_idx += 1
                    green_token_mask_unique.append(ngram_to_watermark_lookup[ngram_example])
                    rev_offsets.append(idx)
            else:
                green_token_mask_unique.append(ngram_to_watermark_lookup[ngram_example])
                unique_ngram_idx += 1
            offsets.append(
                unique_ngram_idx - 1
            )  # aligned with full mask, "at this token what was the last in the unique list? (so only firs appearances)"
        return (
            torch.tensor(green_token_mask),
            torch.tensor(green_token_mask_unique),
            torch.tensor(offsets),
            torch.tensor(rev_offsets),
        )

    def _score_sequence(  # noqa: C901
        self,
        input_ids: torch.Tensor,
        return_num_tokens_scored: bool = True,
        return_num_green_tokens: bool = True,
        return_green_fraction: bool = True,
        return_token_mask: bool = True,
        return_z_score: bool = True,
        return_z_at_T: bool = True,
        return_p_value: bool = True,
    ):
        ngram_to_watermark_lookup, frequencies_table = self._score_ngrams_in_passage(input_ids)
        green_token_mask, green_mask_unique, offsets, rev_offsets = self._get_green_at_T_booleans(
            input_ids, ngram_to_watermark_lookup
        )

        # we want the token mask to match with input_ids
        actual_token_mask = torch.full(input_ids.shape, -1)  # -1 not used, 0 bad, 1 good
        assert (
            len(input_ids) == len(offsets) + self.context_width - self.self_salt
        )  # important assert to know if they are aligned

        # Count up scores over all ngrams
        if self.ignore_repeated_ngrams:
            # Method that only counts a green/red hit once per unique ngram.
            # New num total tokens scored (T) becomes the number unique ngrams.
            # We iterate over all unqiue token ngrams in the input, computing the greenlist
            # induced by the context in each, and then checking whether the last
            # token falls in that greenlist.
            num_tokens_scored = len(frequencies_table.keys())
            green_token_count = sum(ngram_to_watermark_lookup.values())
            actual_token_mask[rev_offsets + self.context_width - int(self.self_salt)] = (
                green_mask_unique.to(int)
            )
        else:
            num_tokens_scored = sum(frequencies_table.values())
            assert num_tokens_scored == len(input_ids) - self.context_width + self.self_salt
            green_token_count = sum(
                freq * outcome
                for freq, outcome in zip(
                    frequencies_table.values(), ngram_to_watermark_lookup.values()
                )
            )
            actual_token_mask[self.context_width - self.self_salt :] = green_token_mask.to(int)

        assert green_token_count == green_mask_unique.sum()

        # HF-style output dictionary
        score_dict = dict()
        if return_num_tokens_scored:
            score_dict.update(dict(num_tokens_scored=num_tokens_scored))
        if return_num_green_tokens:
            score_dict.update(dict(num_green_tokens=green_token_count))
        if return_green_fraction:
            score_dict.update(dict(green_fraction=(green_token_count / num_tokens_scored)))
        if return_z_score:
            score_dict.update(
                dict(z_score=self._compute_z_score(green_token_count, num_tokens_scored))
            )
        if return_p_value:
            z_score = score_dict.get("z_score")
            if z_score is None:
                z_score = self._compute_z_score(green_token_count, num_tokens_scored)
            score_dict.update(dict(p_value=self._compute_p_value(z_score)))
        if return_token_mask:
            score_dict.update(dict(token_mask=actual_token_mask.tolist()))
        if return_z_at_T:
            # Score z_at_T separately:
            sizes = torch.arange(1, len(green_mask_unique) + 1)
            seq_z_score_enum = torch.cumsum(green_mask_unique, dim=0) - self.gamma * sizes
            seq_z_score_denom = torch.sqrt(sizes * self.gamma * (1 - self.gamma))
            z_score_at_effective_T = seq_z_score_enum / seq_z_score_denom
            z_score_at_T = z_score_at_effective_T[offsets]  # return zscore to original text!!!
            assert torch.isclose(z_score_at_T[-1], torch.tensor(z_score))
            if not self.ignore_repeated_ngrams:
                pass
                # print('Warning: z@T is very likely wrong as it was never implemented for ignorenonuniquengrams=False')
            score_dict.update(dict(z_score_at_T=z_score_at_T))

        return score_dict

    def _score_windows_impl_batched(
        self,
        input_ids: torch.Tensor,
        window_size: str,
        window_stride: int = 1,
    ):
        # Implementation details:
        # 1) --ignore_repeated_ngrams is applied globally, and windowing is then applied over the reduced binary vector
        #      this is only one way of doing it, another would be to ignore bigrams within each window (maybe harder to parallelize that)
        # 2) These windows on the binary vector of green/red hits, independent of context_width, in contrast to Kezhi's first implementation
        # 3) z-scores from this implementation cannot be directly converted to p-values, and should only be used as labels for a
        #    ROC chart that calibrates to a chosen FPR. Due, to windowing, the multiple hypotheses will increase scores across the board#
        #    naive_count_correction=True is a partial remedy to this

        ngram_to_watermark_lookup, frequencies_table = self._score_ngrams_in_passage(input_ids)
        green_mask, green_mask_unique, offsets, rev_offsets = self._get_green_at_T_booleans(
            input_ids, ngram_to_watermark_lookup
        )
        len_full_context = len(green_mask_unique)

        partial_sum_id_table = torch.cumsum(green_mask_unique, dim=0)

        if window_size == "max":
            # could start later, small window sizes cannot generate enough power
            # more principled: solve (T * Spike_Entropy - g * T) / sqrt(T * g * (1 - g)) = z_thresh for T
            sizes = range(1, len_full_context)
        else:
            sizes = [int(x) for x in window_size.split(",") if len(x) > 0]

        z_score_max_per_window = torch.zeros(len(sizes))
        cumulative_eff_z_score = torch.zeros(len_full_context)
        s = window_stride

        window_fits = False
        for idx, size in enumerate(sizes):
            if size <= len_full_context:
                # Compute hits within window for all positions in parallel:
                window_score = torch.zeros(len_full_context - size + 1, dtype=torch.long)
                # Include 0-th window
                window_score[0] = partial_sum_id_table[size - 1]
                # All other windows from the 1st:
                window_score[1:] = partial_sum_id_table[size::s] - partial_sum_id_table[:-size:s]

                # Now compute batched z_scores
                batched_z_score_enum = window_score - self.gamma * size
                z_score_denom = sqrt(size * self.gamma * (1 - self.gamma))
                batched_z_score = batched_z_score_enum / z_score_denom

                # And find the maximal hit
                maximal_z_score = batched_z_score.max()
                z_score_max_per_window[idx] = maximal_z_score

                z_score_at_effective_T = torch.cummax(batched_z_score, dim=0)[0]
                cumulative_eff_z_score[size::s] = torch.maximum(
                    cumulative_eff_z_score[size::s], z_score_at_effective_T[:-1]
                )
                window_fits = True  # successful computation for any window in sizes

        if not window_fits:
            raise ValueError(
                f"Could not find a fitting window with window sizes {window_size} for (effective)"
                f" context length {len_full_context}."
            )

        # Compute optimal window size and z-score
        cumulative_z_score = cumulative_eff_z_score[offsets]  # return to original text
        optimal_z, optimal_window_size_idx = z_score_max_per_window.max(
            dim=0
        )  # optimal over window sizes
        optimal_window_size = sizes[optimal_window_size_idx]
        return (
            optimal_z,  # best z over all given sizes
            optimal_window_size,  # size for which that was achieved
            z_score_max_per_window,  # [IGNORED] full map sizeidx (not size!)
            cumulative_z_score,  # maps length of text -> best zscore at that length (assuming stride=1)
            green_mask,  # full green mask
        )

    def _score_sequence_window(
        self,
        input_ids: torch.Tensor,
        return_num_tokens_scored: bool = True,
        return_num_green_tokens: bool = True,
        return_green_fraction: bool = True,
        return_token_mask: bool = True,
        return_z_score: bool = True,
        return_z_at_T: bool = True,
        return_p_value: bool = True,
        window_size: str = None,
        window_stride: int = 1,
    ):
        (
            optimal_z,
            optimal_window_size,
            _,
            z_score_at_T,
            green_mask,
        ) = self._score_windows_impl_batched(input_ids, window_size, window_stride)

        # HF-style output dictionary
        score_dict = dict()
        if return_num_tokens_scored:
            score_dict.update(dict(num_tokens_scored=optimal_window_size))  # size of window

        # Undo z -> green count?
        denom = sqrt(optimal_window_size * self.gamma * (1 - self.gamma))
        green_token_count = int(optimal_z * denom + self.gamma * optimal_window_size)
        green_fraction = green_token_count / optimal_window_size

        if return_num_green_tokens:
            score_dict.update(dict(num_green_tokens=green_token_count))  # in the best window
        if return_green_fraction:
            score_dict.update(dict(green_fraction=green_fraction))  # in the best window
        if return_z_score:
            score_dict.update(dict(z_score=optimal_z))  # of that window
        if return_z_at_T:
            score_dict.update(dict(z_score_at_T=z_score_at_T))  # best z at len (assuming stride=1)
        if return_p_value:
            z_score = score_dict.get("z_score", optimal_z)
            score_dict.update(dict(p_value=self._compute_p_value(z_score)))

        # Return per-token results for mask. This is still the same, just scored by windows
        # best would be to mark the actually counted tokens differently
        if return_token_mask:
            raise NotImplementedError("Token mask not implemented for windowed scoring")
            score_dict.update(dict(token_mask=green_mask.tolist()))  # FULL

        return score_dict

    def detect(  # noqa: C901
        self,
        text: str = None,
        tokenized_text: list[int] = None,  # not needed if text is there
        window_size: str = None,  # set to anything but None to use WinMax
        window_stride: int = None,  # WinMax param: defaults inside to 1, but can be changed
        return_prediction: bool = True,
        return_scores: bool = True,
        z_threshold: float = None,  # can override the one given in the init
        convert_to_float: bool = False,
        **kwargs,
    ) -> dict:
        """Scores a given string of text and returns a dictionary of results."""
        assert tokenized_text is None, "Generally we expect to pass raw text to the detector?"

        assert (text is not None) ^ (
            tokenized_text is not None
        ), "Must pass either the raw or tokenized string"
        if return_prediction:
            kwargs["return_p_value"] = (
                True  # to return the "confidence":=1-p of positive detections
            )

        # run optional normalizers on text
        for normalizer in self.normalizers:
            text = normalizer(text)
        if len(self.normalizers) > 0:
            print(f"Text after normalization:\n\n{text}\n")

        if tokenized_text is None:
            assert self.tokenizer is not None, (
                "Watermark detection on raw string ",
                "requires an instance of the tokenizer ",
                "that was used at generation time.",
            )
            batchenc_obj = self.tokenizer(
                text, return_tensors="pt", return_offsets_mapping=True, add_special_tokens=False
            )
            tokenized_text = batchenc_obj["input_ids"][0].to(self.device)
            offset_mapping = batchenc_obj["offset_mapping"][0].to(self.device)
            if tokenized_text[0] == self.tokenizer.bos_token_id:
                tokenized_text = tokenized_text[1:]
                offset_mapping = offset_mapping[1:]
                print("Removed BOS token (should not happen though?)")
        else:
            # try to remove the bos_tok at beginning if it's there
            if (self.tokenizer is not None) and (tokenized_text[0] == self.tokenizer.bos_token_id):
                tokenized_text = tokenized_text[1:]

        # call score method
        output_dict = {}

        if window_size is not None:
            # assert window_size <= len(tokenized_text) cannot assert for all new types
            score_dict = self._score_sequence_window(
                tokenized_text,
                window_size=window_size,
                window_stride=window_stride,
                **kwargs,
            )
            output_dict.update(score_dict)
        else:
            score_dict = self._score_sequence(tokenized_text, **kwargs)
        if return_scores:
            output_dict.update(score_dict)
        # if passed return_prediction then perform the hypothesis test and return the outcome
        if return_prediction:
            z_threshold = z_threshold if z_threshold else self.z_threshold
            assert (
                z_threshold is not None
            ), "Need a threshold in order to decide outcome of detection test"
            output_dict["prediction"] = score_dict["z_score"] > z_threshold
            if output_dict["prediction"]:
                output_dict["confidence"] = 1 - score_dict["p_value"]

        # if there is token_mask we also need to return the corresponding spans for each token
        # this is needed to be able to color the tokens in the text
        if "token_mask" in output_dict:
            output_dict["offset_mapping"] = offset_mapping.tolist()

        # convert any numerical values to float if requested
        if convert_to_float:
            for key, value in output_dict.items():
                if isinstance(value, int):
                    output_dict[key] = float(value)

        return output_dict


##########################################################################
# Ngram iteration from nltk, extracted to remove the dependency
# Natural Language Toolkit: Utility functions
#
# Copyright (C) 2001-2023 NLTK Project
# Author: Steven Bird <stevenbird1@gmail.com>
#         Eric Kafe <kafe.eric@gmail.com> (acyclic closures)
# URL: <https://www.nltk.org/>
# For license information, see https://github.com/nltk/nltk/blob/develop/LICENSE.txt
##########################################################################


def ngrams(sequence, n, pad_left=False, pad_right=False, pad_symbol=None):
    sequence = iter(sequence)
    if pad_left:
        sequence = chain((pad_symbol,) * (n - 1), sequence)
    if pad_right:
        sequence = chain(sequence, (pad_symbol,) * (n - 1))
    iterables = tee(sequence, n)

    for i, sub_iterable in enumerate(iterables):  # For each window,
        for _ in range(i):  # iterate through every order of ngrams
            next(sub_iterable, None)  # generate the ngrams within the window.
    return zip(*iterables)  # Unpack and flattens the iterables.


class VariableContextWatermarkLogitsProcessor(WatermarkLogitsProcessor):
    def __init__(
        self,
        *args,
        min_context_width=1,
        max_context_width=4,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.min_context_width = min_context_width
        self.max_context_width = max_context_width

    def _compute_context_width(self, input_ids):
        """Compute the pseudorandom context width based on input_ids."""
        # Use a PRF to generate context width between min and max
        prf_key = prf_lookup[self.prf_type](input_ids, salt_key=self.hash_key)
        context_range = self.max_context_width - self.min_context_width + 1
        context_width = (prf_key % context_range) + self.min_context_width
        # Ensure context_width does not exceed length of input_ids
        context_width = min(context_width, input_ids.shape[-1])
        # print(f"Generated context width: {context_width}")
        return context_width

    def _seed_rng(self, input_ids: torch.LongTensor) -> None:
        """Seed RNG from variable context width."""
        context_width = self._compute_context_width(input_ids)
        if input_ids.shape[-1] < context_width:
            raise ValueError(f"Not enough tokens to seed RNG with context width {context_width}.")
        # Use the context_width to seed the RNG
        prf_key = prf_lookup[self.prf_type](input_ids[-context_width:], salt_key=self.hash_key)
        self.rng.manual_seed(prf_key % (2**64 - 1))

    def _get_greenlist_ids(self, input_ids: torch.LongTensor) -> torch.LongTensor:
        """Generate greenlist IDs using variable context width."""
        self._seed_rng(input_ids)
        greenlist_size = int(self.vocab_size * self.gamma)
        vocab_permutation = torch.randperm(
            self.vocab_size, device=input_ids.device, generator=self.rng
        )
        if self.select_green_tokens:
            greenlist_ids = vocab_permutation[:greenlist_size]
        else:
            greenlist_ids = vocab_permutation[-greenlist_size:]
        return greenlist_ids


class VariableContextWatermarkDetector(WatermarkDetector):
    def __init__(
        self,
        *args,
        min_context_width=1,
        max_context_width=4,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.min_context_width = min_context_width
        self.max_context_width = max_context_width

    def _compute_context_width(self, input_ids):
        """Compute the pseudorandom context width based on input_ids."""
        # Use a PRF to generate context width between min and max
        prf_key = prf_lookup[self.prf_type](input_ids, salt_key=self.hash_key)
        context_range = self.max_context_width - self.min_context_width + 1
        context_width = (prf_key % context_range) + self.min_context_width
        # Ensure context_width does not exceed length of input_ids
        context_width = min(context_width, input_ids.shape[-1])
        print(f"Detected context width: {context_width}")
        return context_width

    def _seed_rng(self, input_ids: torch.LongTensor) -> None:
        """Seed RNG from variable context width."""
        context_width = self._compute_context_width(input_ids)
        if input_ids.shape[-1] < context_width:
            raise ValueError(f"Not enough tokens to seed RNG with context width {context_width}.")
        # Use the context_width to seed the RNG
        prf_key = prf_lookup[self.prf_type](input_ids[-context_width:], salt_key=self.hash_key)
        self.rng.manual_seed(prf_key % (2**64 - 1))

    def _get_greenlist_ids(self, input_ids: torch.LongTensor) -> torch.LongTensor:
        """Generate greenlist IDs using variable context width."""
        self._seed_rng(input_ids)
        greenlist_size = int(self.vocab_size * self.gamma)
        vocab_permutation = torch.randperm(
            self.vocab_size, device=input_ids.device, generator=self.rng
        )
        if self.select_green_tokens:
            greenlist_ids = vocab_permutation[:greenlist_size]
        else:
            greenlist_ids = vocab_permutation[-greenlist_size:]
        return greenlist_ids

    @lru_cache(maxsize=2**32)
    def _get_ngram_score_cached(self, context: tuple[int], target: int):
        """Cached method to compute whether the target token is in the greenlist for the given context."""
        greenlist_ids = self._get_greenlist_ids(torch.as_tensor(context, device=self.device))
        return True if target in greenlist_ids else False

    def _score_ngrams_in_passage(self, input_ids: torch.Tensor):
        """Score all ngrams in the passage using variable context widths."""
        ngram_to_watermark_lookup = {}
        frequencies_table = collections.Counter()
        input_ids_list = input_ids.cpu().tolist()

        for position in range(1, len(input_ids_list)):
            prefix = input_ids_list[:position]
            context_width = self._compute_context_width(torch.tensor(prefix, device=self.device))
            context_width = min(context_width, position)  # Cannot be greater than position

            if context_width == 0:
                continue  # Cannot compute watermark with context width 0

            context_start = position - context_width
            context = tuple(input_ids_list[context_start:position])
            target = input_ids_list[position]

            # Create ngram key including context width to handle variable context widths
            ngram_key = (context_width, context, target)

            frequencies_table[ngram_key] += 1

            if ngram_key not in ngram_to_watermark_lookup:
                ngram_to_watermark_lookup[ngram_key] = self._get_ngram_score_cached(context, target)

        return ngram_to_watermark_lookup, frequencies_table

    def _get_green_at_T_booleans(self, input_ids, ngram_to_watermark_lookup):
        """Generate binary lists indicating green tokens and handle repeated ngrams."""
        green_token_mask = []
        green_token_mask_unique = []
        offsets = []
        rev_offsets = []
        used_ngrams = {}
        unique_ngram_idx = 0
        positions = []

        input_ids_list = input_ids.cpu().tolist()

        for position in range(1, len(input_ids_list)):
            prefix = input_ids_list[:position]
            context_width = self._compute_context_width(torch.tensor(prefix, device=self.device))
            context_width = min(context_width, position)

            if context_width == 0:
                continue  # Cannot compute watermark with context width 0

            context_start = position - context_width
            context = tuple(input_ids_list[context_start:position])
            target = input_ids_list[position]

            ngram_key = (context_width, context, target)
            positions.append(position)
            green = ngram_to_watermark_lookup[ngram_key]
            green_token_mask.append(green)

            if self.ignore_repeated_ngrams:
                if ngram_key in used_ngrams:
                    pass
                else:
                    used_ngrams[ngram_key] = True
                    unique_ngram_idx += 1
                    green_token_mask_unique.append(green)
                    rev_offsets.append(len(green_token_mask) - 1)
            else:
                green_token_mask_unique.append(green)
                unique_ngram_idx += 1

            offsets.append(unique_ngram_idx - 1)

        return (
            torch.tensor(green_token_mask),
            torch.tensor(green_token_mask_unique),
            torch.tensor(offsets),
            torch.tensor(rev_offsets),
            positions,
        )

    def _score_sequence(
        self,
        input_ids: torch.Tensor,
        return_num_tokens_scored: bool = True,
        return_num_green_tokens: bool = True,
        return_green_fraction: bool = True,
        return_token_mask: bool = True,
        return_z_score: bool = True,
        return_z_at_T: bool = True,
        return_p_value: bool = True,
    ):
        ngram_to_watermark_lookup, frequencies_table = self._score_ngrams_in_passage(input_ids)
        (
            green_token_mask,
            green_mask_unique,
            offsets,
            rev_offsets,
            positions,
        ) = self._get_green_at_T_booleans(input_ids, ngram_to_watermark_lookup)

        actual_token_mask = torch.full(input_ids.shape, -1, dtype=int)  # Initialize with -1

        positions_tensor = torch.tensor(positions)
        if self.ignore_repeated_ngrams:
            # Mark positions corresponding to unique ngrams
            actual_token_mask[positions_tensor[rev_offsets]] = torch.tensor(
                green_mask_unique, dtype=int
            )
            num_tokens_scored = len(green_mask_unique)
            green_token_count = green_mask_unique.sum().item()
        else:
            actual_token_mask[positions_tensor] = torch.tensor(green_token_mask, dtype=int)
            num_tokens_scored = len(green_token_mask)
            green_token_count = sum(
                freq * outcome
                for freq, outcome in zip(
                    frequencies_table.values(), ngram_to_watermark_lookup.values()
                )
            )

        # Handle cases where no tokens were scored
        if num_tokens_scored == 0:
            z_score = 0.0
            p_value = 1.0
            green_fraction = 0.0
        else:
            green_fraction = green_token_count / num_tokens_scored
            z_score = self._compute_z_score(green_token_count, num_tokens_scored)
            p_value = self._compute_p_value(z_score)

        # Prepare the output dictionary
        score_dict = {}
        if return_num_tokens_scored:
            score_dict["num_tokens_scored"] = num_tokens_scored
        if return_num_green_tokens:
            score_dict["num_green_tokens"] = green_token_count
        if return_green_fraction:
            score_dict["green_fraction"] = green_fraction
        if return_z_score:
            score_dict["z_score"] = z_score
        if return_p_value:
            score_dict["p_value"] = p_value
        if return_token_mask:
            score_dict["token_mask"] = actual_token_mask.tolist()
        if return_z_at_T:
            if num_tokens_scored == 0:
                score_dict["z_score_at_T"] = []
            else:
                sizes = torch.arange(1, len(green_mask_unique) + 1)
                seq_z_score_enum = torch.cumsum(green_mask_unique, dim=0) - self.gamma * sizes
                seq_z_score_denom = torch.sqrt(sizes * self.gamma * (1 - self.gamma))
                z_score_at_effective_T = seq_z_score_enum / seq_z_score_denom
                z_score_at_T = z_score_at_effective_T[offsets]

                score_dict["z_score_at_T"] = z_score_at_T.tolist()

        return score_dict

    def _compute_z_score(self, observed_count, T):
        if T == 0:
            return 0.0
        expected_count = self.gamma * T
        numer = observed_count - expected_count
        denom = sqrt(T * self.gamma * (1 - self.gamma))
        if denom == 0:
            return 0.0
        z = numer / denom
        return z


# class VariableContextWatermarkDetector(WatermarkDetector):
#     def __init__(
#         self,
#         *args,
#         min_context_width=1,
#         max_context_width=4,
#         **kwargs,
#     ):
#         super().__init__(*args, **kwargs)
#         self.min_context_width = min_context_width
#         self.max_context_width = max_context_width

#     def _compute_context_width(self, input_ids):
#         """Compute the pseudorandom context width based on input_ids."""
#         prf_key = prf_lookup[self.prf_type](input_ids, salt_key=self.hash_key)
#         context_range = self.max_context_width - self.min_context_width + 1
#         context_width = (prf_key % context_range) + self.min_context_width
#         context_width = min(context_width, input_ids.shape[-1])
#         return context_width

#     def _score_sequence(
#         self,
#         input_ids: torch.Tensor,
#         return_num_tokens_scored: bool = True,
#         return_num_green_tokens: bool = True,
#         return_green_fraction: bool = True,
#         return_token_mask: bool = True,
#         return_z_score: bool = True,
#         return_z_at_T: bool = True,
#         return_p_value: bool = True,
#     ):
#         """Score the input sequence using variable context widths."""
#         green_token_mask = []
#         num_tokens_scored = 0
#         green_token_count = 0

#         # Start from min_context_width since we need at least that many tokens
#         for i in range(self.min_context_width, len(input_ids)):
#             # Compute context width for current position
#             input_ids_slice = input_ids[:i]
#             context_width = self._compute_context_width(input_ids_slice)
#             # Ensure context width does not exceed current position
#             context_width = min(context_width, i)
#             # Get the context and target token
#             context = input_ids_slice[-context_width:]
#             target_token = input_ids[i]
#             # Seed RNG and generate greenlist
#             prf_key = prf_lookup[self.prf_type](context, salt_key=self.hash_key)
#             self.rng.manual_seed(prf_key % (2**64 - 1))
#             greenlist_size = int(self.vocab_size * self.gamma)
#             vocab_permutation = torch.randperm(
#                 self.vocab_size, device=input_ids.device, generator=self.rng
#             )
#             if self.select_green_tokens:
#                 greenlist_ids = vocab_permutation[:greenlist_size]
#             else:
#                 greenlist_ids = vocab_permutation[-greenlist_size:]
#             # Check if the target token is in the greenlist
#             is_green = target_token in greenlist_ids
#             green_token_mask.append(is_green)
#             num_tokens_scored += 1
#             if is_green:
#                 green_token_count += 1

#         # Convert green_token_mask to tensor
#         green_token_mask = torch.tensor(green_token_mask, device=input_ids.device)

#         # Compute statistical metrics
#         green_fraction = green_token_count / num_tokens_scored if num_tokens_scored > 0 else 0.0
#         z_score = (
#             self._compute_z_score(green_token_count, num_tokens_scored)
#             if num_tokens_scored > 0
#             else 0.0
#         )

#         # Build the output dictionary
#         score_dict = {}
#         if return_num_tokens_scored:
#             score_dict["num_tokens_scored"] = num_tokens_scored
#         if return_num_green_tokens:
#             score_dict["num_green_tokens"] = green_token_count
#         if return_green_fraction:
#             score_dict["green_fraction"] = green_fraction
#         if return_z_score:
#             score_dict["z_score"] = z_score
#         if return_p_value:
#             p_value = self._compute_p_value(z_score)
#             score_dict["p_value"] = p_value
#         if return_token_mask:
#             # Pad the beginning with -1 to align with input_ids
#             padding = [-1] * self.min_context_width
#             actual_token_mask = padding + green_token_mask.to(int).tolist()
#             score_dict["token_mask"] = actual_token_mask

#         return score_dict
