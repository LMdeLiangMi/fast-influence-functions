# Copyright (c) 2020, salesforce.com, inc.
# All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# For full license text, see the LICENSE file in the repo root or https://opensource.org/licenses/BSD-3-Clause

import sys
import torch
import transformers
import numpy as np
from contexttimer import Timer
from typing import List, Dict, Any
from transformers import GlueDataset
from transformers import TrainingArguments
from transformers import default_data_collator

from influence_utils import parallel
from influence_utils import faiss_utils
from influence_utils import nn_influence_utils
from influence_utils.nn_influence_utils import compute_s_test
from experiments import constants
from experiments import misc_utils
from experiments import remote_utils
from experiments.data_utils import (
    glue_output_modes,
    glue_compute_metrics)


def one_experiment(
        model: torch.nn.Module,
        train_dataset: GlueDataset,
        test_inputs: Dict[str, torch.Tensor],
        batch_size: int,
        random: bool,
        n_gpu: int,
        device: torch.device,
        damp: float,
        scale: float,
        num_samples: int,
) -> List[torch.Tensor]:

    params_filter = [
        n for n, p in model.named_parameters()
        if not p.requires_grad]

    weight_decay_ignores = [
        "bias",
        "LayerNorm.weight"] + [
        n for n, p in model.named_parameters()
        if not p.requires_grad]

    # Make sure each dataloader is re-initialized
    batch_train_data_loader = misc_utils.get_dataloader(
        dataset=train_dataset,
        batch_size=batch_size,
        random=random)

    s_test = compute_s_test(
        n_gpu=n_gpu,
        device=device,
        model=model,
        test_inputs=test_inputs,
        train_data_loaders=[batch_train_data_loader],
        params_filter=params_filter,
        weight_decay=constants.WEIGHT_DECAY,
        weight_decay_ignores=weight_decay_ignores,
        damp=damp,
        scale=scale,
        num_samples=num_samples)

    return [X.cpu() for X in s_test]


def main(
    mode: str,
    num_examples_to_test: int = 5,
    num_repetitions: int = 4,
) -> List[Dict[str, Any]]:

    if mode not in ["only-correct", "only-incorrect"]:
        raise ValueError(f"Unrecognized mode {mode}")

    task_tokenizer, task_model = misc_utils.create_tokenizer_and_model(
        constants.MNLI_MODEL_PATH)
    train_dataset, eval_dataset = misc_utils.create_datasets(
        task_name="mnli",
        tokenizer=task_tokenizer)
    eval_instance_data_loader = misc_utils.get_dataloader(
        dataset=eval_dataset,
        batch_size=1,
        random=False)

    output_mode = glue_output_modes["mnli"]

    def build_compute_metrics_fn(task_name: str):
        def compute_metrics_fn(p):
            if output_mode == "classification":
                preds = np.argmax(p.predictions, axis=1)
            elif output_mode == "regression":
                preds = np.squeeze(p.predictions)
            return glue_compute_metrics(task_name, preds, p.label_ids)

        return compute_metrics_fn

    # Most of these arguments are placeholders
    # and are not really used at all, so ignore
    # the exact values of these.
    trainer = transformers.Trainer(
        model=task_model,
        args=TrainingArguments(
            output_dir="./tmp-output",
            per_device_train_batch_size=128,
            per_device_eval_batch_size=128,
            learning_rate=5e-5,
            logging_steps=100),
        data_collator=default_data_collator,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        compute_metrics=build_compute_metrics_fn("mnli"),
    )

    task_model.cuda()
    num_examples_tested = 0
    output_collections = []
    for test_index, test_inputs in enumerate(eval_instance_data_loader):
        if num_examples_tested >= num_examples_to_test:
            break

        # Skip when we only want cases of correction prediction but the
        # prediction is incorrect, or vice versa
        prediction_is_correct = misc_utils.is_prediction_correct(
            trainer=trainer,
            model=task_model,
            inputs=test_inputs)

        if mode == "only-correct" and prediction_is_correct is False:
            continue

        if mode == "only-incorrect" and prediction_is_correct is True:
            continue

        for k, v in test_inputs.items():
            if isinstance(v, torch.Tensor):
                test_inputs[k] = v.to(torch.device("cuda"))

        # with batch-size 128, 1500 iterations is enough
        for num_samples in range(700, 1300 + 1, 100):  # 7 choices
            for batch_size in [1, 2, 4, 8, 16, 32, 64, 128]:  # 8 choices
                for repetition in range(num_repetitions):
                    print(f"Running #{test_index} "
                          f"N={num_samples} "
                          f"B={batch_size} "
                          f"R={repetition} takes ...", end=" ")
                    with Timer() as timer:
                        s_test = one_experiment(
                            model=task_model,
                            train_dataset=train_dataset,
                            test_inputs=test_inputs,
                            batch_size=batch_size,
                            random=True,
                            n_gpu=1,
                            device=torch.device("cuda"),
                            damp=constants.DEFAULT_INFLUENCE_HPARAMS["mnli"]["mnli"]["damp"],
                            scale=constants.DEFAULT_INFLUENCE_HPARAMS["mnli"]["mnli"]["scale"],
                            num_samples=num_samples)
                        time_elapsed = timer.elapsed
                        print(f"{time_elapsed:.2f} seconds")

                    outputs = {
                        "test_index": test_index,
                        "num_samples": num_samples,
                        "batch_size": batch_size,
                        "repetition": repetition,
                        "s_test": s_test,
                        "time_elapsed": time_elapsed,
                        "correct": prediction_is_correct,
                    }
                    output_collections.append(outputs)
                    remote_utils.save_and_mirror_scp_to_remote(
                        object_to_save=outputs,
                        file_name=f"stest.{mode}.{num_examples_to_test}."
                                  f"{test_index}.{num_samples}."
                                  f"{batch_size}.{repetition}.pth")

        num_examples_tested += 1

    return output_collections
