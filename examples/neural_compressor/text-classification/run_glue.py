#!/usr/bin/env python
# coding=utf-8
#  Copyright 2021 The HuggingFace Team. All rights reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

""" Finetuning the library models for sequence classification on GLUE."""
# You can also adapt this script on your own text classification task. Pointers for this are left as comments.

import logging
import os
import random
import sys
from dataclasses import dataclass, field
from typing import Optional

import datasets
import numpy as np
import transformers
from datasets import load_dataset, load_metric
from transformers import (
    AutoConfig,
    AutoModelForSequenceClassification,
    AutoTokenizer,
    DataCollatorWithPadding,
    EvalPrediction,
    HfArgumentParser,
    PretrainedConfig,
    TrainingArguments,
    default_data_collator,
    set_seed,
)
from transformers.trainer_utils import get_last_checkpoint
from transformers.utils import check_min_version
from transformers.utils.versions import require_version

from optimum.intel.neural_compressor import (
    IncDistillationConfig,
    IncDistiller,
    IncOptimizer,
    IncPruner,
    IncPruningConfig,
    IncQuantizationConfig,
    IncQuantizationMode,
    IncQuantizer,
    IncTrainer,
)
from optimum.intel.neural_compressor.quantization import IncQuantizedModelForSequenceClassification


os.environ["CUDA_VISIBLE_DEVICES"] = ""


# Will error if the minimal version of Transformers is not installed. Remove at your own risks.
check_min_version("4.15.0")

require_version("datasets>=1.8.0", "To fix: pip install -r examples/text-classification/requirements.txt")

task_to_keys = {
    "cola": ("sentence", None),
    "mnli": ("premise", "hypothesis"),
    "mrpc": ("sentence1", "sentence2"),
    "qnli": ("question", "sentence"),
    "qqp": ("question1", "question2"),
    "rte": ("sentence1", "sentence2"),
    "sst2": ("sentence", None),
    "stsb": ("sentence1", "sentence2"),
    "wnli": ("sentence1", "sentence2"),
}

logger = logging.getLogger(__name__)


@dataclass
class DataTrainingArguments:
    """
    Arguments pertaining to what data we are going to input our model for training and eval.

    Using `HfArgumentParser` we can turn this class
    into argparse arguments to be able to specify them on
    the command line.
    """

    task_name: Optional[str] = field(
        default=None,
        metadata={"help": "The name of the task to train on: " + ", ".join(task_to_keys.keys())},
    )
    dataset_name: Optional[str] = field(
        default=None, metadata={"help": "The name of the dataset to use (via the datasets library)."}
    )
    dataset_config_name: Optional[str] = field(
        default=None, metadata={"help": "The configuration name of the dataset to use (via the datasets library)."}
    )
    max_seq_length: int = field(
        default=128,
        metadata={
            "help": "The maximum total input sequence length after tokenization. Sequences longer "
            "than this will be truncated, sequences shorter will be padded."
        },
    )
    overwrite_cache: bool = field(
        default=False, metadata={"help": "Overwrite the cached preprocessed datasets or not."}
    )
    pad_to_max_length: bool = field(
        default=True,
        metadata={
            "help": "Whether to pad all samples to `max_seq_length`. "
            "If False, will pad the samples dynamically when batching to the maximum length in the batch."
        },
    )
    max_train_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": "For debugging purposes or quicker training, truncate the number of training examples to this "
            "value if set."
        },
    )
    max_eval_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": "For debugging purposes or quicker training, truncate the number of evaluation examples to this "
            "value if set."
        },
    )
    max_predict_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": "For debugging purposes or quicker training, truncate the number of prediction examples to this "
            "value if set."
        },
    )
    train_file: Optional[str] = field(
        default=None, metadata={"help": "A csv or a json file containing the training data."}
    )
    validation_file: Optional[str] = field(
        default=None, metadata={"help": "A csv or a json file containing the validation data."}
    )

    def __post_init__(self):
        if self.task_name is not None:
            self.task_name = self.task_name.lower()
            if self.task_name not in task_to_keys.keys():
                raise ValueError("Unknown task, you should pick one in " + ",".join(task_to_keys.keys()))
        elif self.dataset_name is not None:
            pass
        elif self.train_file is None or self.validation_file is None:
            raise ValueError("Need either a GLUE task, a training/validation file or a dataset name.")
        else:
            train_extension = self.train_file.split(".")[-1]
            assert train_extension in ["csv", "json"], "`train_file` should be a csv or a json file."
            validation_extension = self.validation_file.split(".")[-1]
            assert (
                validation_extension == train_extension
            ), "`validation_file` should have the same extension (csv or json) as `train_file`."


@dataclass
class ModelArguments:
    """
    Arguments pertaining to which model/config/tokenizer we are going to fine-tune from.
    """

    model_name_or_path: str = field(
        metadata={"help": "Path to pretrained model or model identifier from huggingface.co/models"}
    )
    config_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained config name or path if not the same as model_name"}
    )
    tokenizer_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained tokenizer name or path if not the same as model_name"}
    )
    cache_dir: Optional[str] = field(
        default=None,
        metadata={"help": "Where do you want to store the pretrained models downloaded from huggingface.co"},
    )
    use_fast_tokenizer: bool = field(
        default=True,
        metadata={"help": "Whether to use one of the fast tokenizer (backed by the tokenizers library) or not."},
    )
    model_revision: str = field(
        default="main",
        metadata={"help": "The specific model version to use (can be a branch name, tag name or commit id)."},
    )
    use_auth_token: bool = field(
        default=False,
        metadata={
            "help": "Will use the token generated when running `transformers-cli login` (necessary to use this script "
            "with private models)."
        },
    )


@dataclass
class OptimizationArguments:
    """
    Arguments pertaining to what type of optimization we are going to apply on the model.
    """

    apply_quantization: bool = field(
        default=False,
        metadata={"help": "Whether or not to apply quantization."},
    )
    quantization_approach: Optional[str] = field(
        default=None,
        metadata={"help": "Quantization approach. Supported approach are static, dynamic and aware_training."},
    )
    apply_pruning: bool = field(
        default=False,
        metadata={"help": "Whether or not to apply pruning."},
    )
    target_sparsity: Optional[float] = field(
        default=None,
        metadata={"help": "Targeted sparsity when pruning the model."},
    )
    apply_distillation: bool = field(
        default=False,
        metadata={"help": "Whether or not to apply distillation."},
    )
    teacher_model_name_or_path: str = field(
        default=False, metadata={"help": "Path to pretrained model or model identifier from huggingface.co/models"}
    )
    quantization_config: Optional[str] = field(
        default=None,
        metadata={
            "help": "Path to the directory containing the YAML configuration file used to control the quantization and"
            "tuning behavior."
        },
    )
    pruning_config: Optional[str] = field(
        default=None,
        metadata={
            "help": "Path to the directory containing the YAML configuration file used to control the pruning behavior."
        },
    )
    distillation_config: Optional[str] = field(
        default=None,
        metadata={
            "help": "Path to the directory containing the YAML configuration file used to control the distillation"
            "behavior."
        },
    )
    metric: Optional[str] = field(
        default=None,
        metadata={"help": "Metric used for the tuning strategy."},
    )
    tolerance_criterion: Optional[float] = field(
        default=None,
        metadata={"help": "Performance tolerance when optimizing the model."},
    )
    verify_loading: bool = field(
        default=False,
        metadata={"help": "Whether or not to verify the loading of the quantized model."},
    )


def main():
    # See all possible arguments in src/transformers/training_args.py
    # or by passing the --help flag to this script.
    # We now keep distinct sets of args, for a cleaner separation of concerns.

    parser = HfArgumentParser((ModelArguments, DataTrainingArguments, TrainingArguments, OptimizationArguments))
    if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
        # If we pass only one argument to the script and it's the path to a json file,
        # let's parse it to get our arguments.
        model_args, data_args, training_args, optim_args = parser.parse_json_file(
            json_file=os.path.abspath(sys.argv[1])
        )
    else:
        model_args, data_args, training_args, optim_args = parser.parse_args_into_dataclasses()

    # Setup logging
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    log_level = training_args.get_process_log_level()
    logger.setLevel(log_level)
    datasets.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.enable_default_handler()
    transformers.utils.logging.enable_explicit_format()

    # Log on each process the small summary:
    logger.warning(
        f"Process rank: {training_args.local_rank}, device: {training_args.device}, n_gpu: {training_args.n_gpu}"
        + f"distributed training: {bool(training_args.local_rank != -1)}, 16-bits training: {training_args.fp16}"
    )
    logger.info(f"Training/evaluation parameters {training_args}")

    # Detecting last checkpoint.
    last_checkpoint = None
    if os.path.isdir(training_args.output_dir) and training_args.do_train and not training_args.overwrite_output_dir:
        last_checkpoint = get_last_checkpoint(training_args.output_dir)
        if last_checkpoint is None and len(os.listdir(training_args.output_dir)) > 0:
            raise ValueError(
                f"Output directory ({training_args.output_dir}) already exists and is not empty. "
                "Use --overwrite_output_dir to overcome."
            )
        elif last_checkpoint is not None and training_args.resume_from_checkpoint is None:
            logger.info(
                f"Checkpoint detected, resuming training at {last_checkpoint}. To avoid this behavior, change "
                "the `--output_dir` or add `--overwrite_output_dir` to train from scratch."
            )

    # Set seed before initializing model.
    set_seed(training_args.seed)

    # Get the datasets: you can either provide your own CSV/JSON training and evaluation files (see below)
    # or specify a GLUE benchmark task (the dataset will be downloaded automatically from the datasets Hub).
    #
    # For CSV/JSON files, this script will use as labels the column called 'label' and as pair of sentences the
    # sentences in columns called 'sentence1' and 'sentence2' if such column exists or the first two columns not named
    # label if at least two columns are provided.
    #
    # If the CSVs/JSONs contain only one non-label column, the script does single sentence classification on this
    # single column. You can easily tweak this behavior (see below)
    #
    # In distributed training, the load_dataset function guarantee that only one local process can concurrently
    # download the dataset.
    if data_args.task_name is not None:
        # Downloading and loading a dataset from the hub.
        raw_datasets = load_dataset("glue", data_args.task_name, cache_dir=model_args.cache_dir)
    elif data_args.dataset_name is not None:
        # Downloading and loading a dataset from the hub.
        raw_datasets = load_dataset(
            data_args.dataset_name, data_args.dataset_config_name, cache_dir=model_args.cache_dir
        )
    else:
        # Loading a dataset from your local files.
        # CSV/JSON training and evaluation files are needed.
        data_files = {"train": data_args.train_file, "validation": data_args.validation_file}

        for key in data_files.keys():
            logger.info(f"load a local file for {key}: {data_files[key]}")

        if data_args.train_file.endswith(".csv"):
            # Loading a dataset from local csv files
            raw_datasets = load_dataset("csv", data_files=data_files, cache_dir=model_args.cache_dir)
        else:
            # Loading a dataset from local json files
            raw_datasets = load_dataset("json", data_files=data_files, cache_dir=model_args.cache_dir)
    # See more about loading any type of standard or custom dataset at
    # https://huggingface.co/docs/datasets/loading_datasets.html.

    # Labels
    if data_args.task_name is not None:
        is_regression = data_args.task_name == "stsb"
        if not is_regression:
            label_list = raw_datasets["train"].features["label"].names
            num_labels = len(label_list)
        else:
            num_labels = 1
    else:
        # Trying to have good defaults here, don't hesitate to tweak to your needs.
        is_regression = raw_datasets["train"].features["label"].dtype in ["float32", "float64"]
        if is_regression:
            num_labels = 1
        else:
            # A useful fast method:
            # https://huggingface.co/docs/datasets/package_reference/main_classes.html#datasets.Dataset.unique
            label_list = raw_datasets["train"].unique("label")
            label_list.sort()  # Let's sort it for determinism
            num_labels = len(label_list)

    # Load pretrained model and tokenizer
    #
    # In distributed training, the .from_pretrained methods guarantee that only one local process can concurrently
    # download model & vocab.
    config = AutoConfig.from_pretrained(
        model_args.config_name if model_args.config_name else model_args.model_name_or_path,
        num_labels=num_labels,
        finetuning_task=data_args.task_name,
        cache_dir=model_args.cache_dir,
        revision=model_args.model_revision,
        use_auth_token=True if model_args.use_auth_token else None,
    )
    tokenizer = AutoTokenizer.from_pretrained(
        model_args.tokenizer_name if model_args.tokenizer_name else model_args.model_name_or_path,
        cache_dir=model_args.cache_dir,
        use_fast=model_args.use_fast_tokenizer,
        revision=model_args.model_revision,
        use_auth_token=True if model_args.use_auth_token else None,
    )
    model = AutoModelForSequenceClassification.from_pretrained(
        model_args.model_name_or_path,
        from_tf=bool(".ckpt" in model_args.model_name_or_path),
        config=config,
        cache_dir=model_args.cache_dir,
        revision=model_args.model_revision,
        use_auth_token=True if model_args.use_auth_token else None,
    )

    # Preprocessing the raw_datasets
    if data_args.task_name is not None:
        sentence1_key, sentence2_key = task_to_keys[data_args.task_name]
    else:
        # Again, we try to have some nice defaults but don't hesitate to tweak to your use case.
        non_label_column_names = [name for name in raw_datasets["train"].column_names if name != "label"]
        if "sentence1" in non_label_column_names and "sentence2" in non_label_column_names:
            sentence1_key, sentence2_key = "sentence1", "sentence2"
        else:
            if len(non_label_column_names) >= 2:
                sentence1_key, sentence2_key = non_label_column_names[:2]
            else:
                sentence1_key, sentence2_key = non_label_column_names[0], None

    # Padding strategy
    if data_args.pad_to_max_length:
        padding = "max_length"
    else:
        # We will pad later, dynamically at batch creation, to the max sequence length in each batch
        padding = False

    # Some models have set the order of the labels to use, so let's make sure we do use it.
    label_to_id = None
    if (
        model.config.label2id != PretrainedConfig(num_labels=num_labels).label2id
        and data_args.task_name is not None
        and not is_regression
    ):
        # Some have all caps in their config, some don't.
        label_name_to_id = {k.lower(): v for k, v in model.config.label2id.items()}
        if list(sorted(label_name_to_id.keys())) == list(sorted(label_list)):
            label_to_id = {i: int(label_name_to_id[label_list[i]]) for i in range(num_labels)}
        else:
            logger.warning(
                "Your model seems to have been trained with labels, but they don't match the dataset: ",
                f"model labels: {list(sorted(label_name_to_id.keys()))}, dataset labels: {list(sorted(label_list))}."
                "\nIgnoring the model labels as a result.",
            )
    elif data_args.task_name is None and not is_regression:
        label_to_id = {v: i for i, v in enumerate(label_list)}

    if label_to_id is not None:
        model.config.label2id = label_to_id
        model.config.id2label = {id: label for label, id in config.label2id.items()}

    if data_args.max_seq_length > tokenizer.model_max_length:
        logger.warning(
            f"The max_seq_length passed ({data_args.max_seq_length}) is larger than the maximum length for the"
            f"model ({tokenizer.model_max_length}). Using max_seq_length={tokenizer.model_max_length}."
        )
    max_seq_length = min(data_args.max_seq_length, tokenizer.model_max_length)

    def preprocess_function(examples):
        # Tokenize the texts
        args = (
            (examples[sentence1_key],) if sentence2_key is None else (examples[sentence1_key], examples[sentence2_key])
        )
        result = tokenizer(*args, padding=padding, max_length=max_seq_length, truncation=True)

        # Map labels to IDs (not necessary for GLUE tasks)
        if label_to_id is not None and "label" in examples:
            result["label"] = [(label_to_id[l] if l != -1 else -1) for l in examples["label"]]
        return result

    with training_args.main_process_first(desc="dataset map pre-processing"):
        raw_datasets = raw_datasets.map(
            preprocess_function, batched=True, load_from_cache_file=not data_args.overwrite_cache
        )
    if training_args.do_train:
        if "train" not in raw_datasets:
            raise ValueError("--do_train requires a train dataset")
        train_dataset = raw_datasets["train"]
        if data_args.max_train_samples is not None:
            train_dataset = train_dataset.select(range(data_args.max_train_samples))

    if training_args.do_eval:
        if "validation" not in raw_datasets and "validation_matched" not in raw_datasets:
            raise ValueError("--do_eval requires a validation dataset")
        eval_dataset = raw_datasets["validation_matched" if data_args.task_name == "mnli" else "validation"]
        if data_args.max_eval_samples is not None:
            eval_dataset = eval_dataset.select(range(data_args.max_eval_samples))

    # Log a few random samples from the training set:
    if training_args.do_train:
        for index in random.sample(range(len(train_dataset)), 3):
            logger.info(f"Sample {index} of the training set: {train_dataset[index]}.")

    # Get the metric function
    if data_args.task_name is not None:
        metric = load_metric("glue", data_args.task_name)
    else:
        metric = load_metric("accuracy")

    # You can define your custom compute_metrics function. It takes an `EvalPrediction` object (a namedtuple with a
    # predictions and label_ids field) and has to return a dictionary string to float.
    def compute_metrics(p: EvalPrediction):
        preds = p.predictions[0] if isinstance(p.predictions, tuple) else p.predictions
        preds = np.squeeze(preds) if is_regression else np.argmax(preds, axis=1)
        if data_args.task_name is not None:
            result = metric.compute(predictions=preds, references=p.label_ids)
            if len(result) > 1:
                result["combined_score"] = np.mean(list(result.values())).item()
            return result
        elif is_regression:
            return {"mse": ((preds - p.label_ids) ** 2).mean().item()}
        else:
            return {"accuracy": (preds == p.label_ids).astype(np.float32).mean().item()}

    # Data collator will default to DataCollatorWithPadding, so we change it if we already did the padding.
    if data_args.pad_to_max_length:
        data_collator = default_data_collator
    elif training_args.fp16:
        data_collator = DataCollatorWithPadding(tokenizer, pad_to_multiple_of=8)
    else:
        data_collator = None

    # Initialize our Trainer
    trainer = IncTrainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset if training_args.do_train else None,
        eval_dataset=eval_dataset if training_args.do_eval else None,
        compute_metrics=compute_metrics,
        tokenizer=tokenizer,
        data_collator=data_collator,
    )

    resume_from_checkpoint = training_args.resume_from_checkpoint
    metric_name = optim_args.metric
    if metric_name is None:
        metric_name = "eval_" + (
            "pearson"
            if data_args.task_name == "stsb"
            else "matthews_correlation"
            if data_args.task_name == "cola"
            else "accuracy"
        )

    def take_eval_steps(model, trainer, metric_name, save_metrics=False):
        trainer.model = model
        metrics = trainer.evaluate()
        if save_metrics:
            trainer.save_metrics("eval", metrics)
        logger.info("{}: {}".format(metric_name, metrics.get(metric_name)))
        logger.info("Throughput: {} samples/sec".format(metrics.get("eval_samples_per_second")))
        return metrics[metric_name]

    def eval_func(model):
        return take_eval_steps(model, trainer, metric_name)

    def take_train_steps(model, trainer, resume_from_checkpoint, last_checkpoint):
        trainer.model_wrapped = model
        trainer.model = model
        checkpoint = None
        if resume_from_checkpoint is not None:
            checkpoint = resume_from_checkpoint
        elif last_checkpoint is not None:
            checkpoint = last_checkpoint
        train_result = trainer.train(agent, resume_from_checkpoint=checkpoint)
        metrics = train_result.metrics
        trainer.save_model()  # Saves the tokenizer too for easy upload
        trainer.log_metrics("train", metrics)
        trainer.save_metrics("train", metrics)
        trainer.save_state()
        return trainer.model

    def train_func(model):
        return take_train_steps(model, trainer, resume_from_checkpoint, last_checkpoint)

    quantizer = None
    pruner = None
    distiller = None

    if not optim_args.apply_quantization and not optim_args.apply_pruning and not optim_args.apply_distillation:
        raise ValueError("No optimization activated.")

    result_baseline_model = take_eval_steps(model, trainer, metric_name)

    default_config = os.path.join(os.path.abspath(os.path.join(__file__, os.path.pardir, os.path.pardir)), "config")

    if optim_args.apply_quantization:

        if not training_args.do_eval:
            raise ValueError("do_eval must be set to True for quantization.")

        q8_config = IncQuantizationConfig.from_pretrained(
            optim_args.quantization_config if optim_args.quantization_config is not None else default_config,
            config_file_name="quantization.yml",
            cache_dir=model_args.cache_dir,
        )

        # Set metric tolerance if specified
        if optim_args.tolerance_criterion is not None:
            q8_config.set_tolerance(optim_args.tolerance_criterion)

        # Set quantization approach if specified
        if optim_args.quantization_approach is not None:
            supported_approach = {"static", "dynamic", "aware_training"}
            if optim_args.quantization_approach not in supported_approach:
                raise ValueError(
                    "Unknown quantization approach. Supported approach are " + ", ".join(supported_approach)
                )
            quant_approach = getattr(IncQuantizationMode, optim_args.quantization_approach.upper()).value
            q8_config.set_config("quantization.approach", quant_approach)

        quant_approach = IncQuantizationMode(q8_config.get_config("quantization.approach"))
        # torch FX used for post-training quantization and quantization aware training
        # dynamic quantization will be added when torch FX is more mature
        if quant_approach != IncQuantizationMode.DYNAMIC:
            if not training_args.do_train:
                raise ValueError("do_train must be set to True for static and aware training quantization.")

            q8_config.set_config("model.framework", "pytorch_fx")

        calib_dataloader = trainer.get_train_dataloader() if quant_approach == IncQuantizationMode.STATIC else None
        quantizer = IncQuantizer(
            q8_config, eval_func=eval_func, train_func=train_func, calib_dataloader=calib_dataloader
        )

    if optim_args.apply_pruning:

        if not training_args.do_train:
            raise ValueError("do_train must be set to True for pruning.")

        pruning_config = IncPruningConfig.from_pretrained(
            optim_args.pruning_config if optim_args.pruning_config is not None else default_config,
            config_file_name="prune.yml",
            cache_dir=model_args.cache_dir,
        )

        # Set targeted sparsity if specified
        if optim_args.target_sparsity is not None:
            pruning_config.set_config(
                "pruning.approach.weight_compression.target_sparsity", optim_args.target_sparsity
            )

        pruning_start_epoch = pruning_config.get_config("pruning.approach.weight_compression.start_epoch")
        pruning_end_epoch = pruning_config.get_config("pruning.approach.weight_compression.end_epoch")

        if pruning_start_epoch > training_args.num_train_epochs - 1:
            logger.warning(
                f"Pruning end epoch {pruning_start_epoch} is higher than the total number of training epoch "
                f"{training_args.num_train_epochs}. No pruning will be applied."
            )

        if pruning_end_epoch > training_args.num_train_epochs - 1:
            logger.warning(
                f"Pruning end epoch {pruning_end_epoch} is higher than the total number of training epoch "
                f"{training_args.num_train_epochs}. The target sparsity will not be reached."
            )

        # Creation Pruning object used for IncTrainer training loop
        pruner = IncPruner(pruning_config, eval_func=eval_func, train_func=train_func)

    if optim_args.apply_distillation:

        if optim_args.teacher_model_name_or_path is None:
            raise ValueError("A teacher model is needed to apply distillation.")

        if not training_args.do_train:
            raise ValueError("do_train must be set to True for distillation.")

        teacher_config = AutoConfig.from_pretrained(
            optim_args.teacher_model_name_or_path,
            num_labels=num_labels,
            finetuning_task=data_args.task_name,
        )
        teacher_tokenizer = AutoTokenizer.from_pretrained(
            optim_args.teacher_model_name_or_path,
            use_fast=model_args.use_fast_tokenizer,
        )
        teacher_model = AutoModelForSequenceClassification.from_pretrained(
            optim_args.teacher_model_name_or_path,
            from_tf=bool(".ckpt" in model_args.model_name_or_path),
            config=teacher_config,
        )

        teacher_model.to(training_args.device)

        if teacher_tokenizer.vocab != tokenizer.vocab:
            raise ValueError("Teacher model and student model should have same tokenizer.")

        distillation_config = IncDistillationConfig.from_pretrained(
            optim_args.distillation_config if optim_args.distillation_config is not None else default_config,
            config_file_name="distillation.yml",
            cache_dir=model_args.cache_dir,
        )

        # Creation Distillation object used for IncTrainer training loop
        distiller = IncDistiller(
            teacher_model=teacher_model, config=distillation_config, eval_func=eval_func, train_func=train_func
        )

    optimizer = IncOptimizer(
        model,
        quantizer=quantizer,
        pruner=pruner,
        distiller=distiller,
        one_shot_optimization=True,
        eval_func=eval_func,
        train_func=train_func,
    )

    agent = optimizer.get_agent()
    optimized_model = optimizer.fit()
    result_optimized_model = take_eval_steps(optimized_model, trainer, metric_name, save_metrics=True)

    # Save the resulting model and its corresponding configuration in the given directory
    optimizer.save_pretrained(training_args.output_dir)
    # Compute the model's sparsity
    sparsity = optimizer.get_sparsity()

    logger.info(
        f"Optimized model with {metric_name} of {result_optimized_model} and sparsity of {round(sparsity, 2)}% "
        f"saved to: {training_args.output_dir}. Original model had an {metric_name} of {result_baseline_model}."
    )

    if optim_args.apply_quantization and optim_args.verify_loading:

        # Load the model obtained after Intel Neural Compressor quantization
        loaded_model = IncQuantizedModelForSequenceClassification.from_pretrained(training_args.output_dir)
        loaded_model.eval()
        result_loaded_model = take_eval_steps(loaded_model, trainer, metric_name)

        if result_loaded_model != result_optimized_model:
            logger.error("The quantized model was not successfully loaded.")
        else:
            logger.info(f"The quantized model was successfully loaded.")


def _mp_fn(index):
    # For xla_spawn (TPUs)
    main()


if __name__ == "__main__":
    main()