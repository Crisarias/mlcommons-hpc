# Copyright 2023 NVIDIA CORPORATION
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

import argparse
import gc
import hashlib
import io
import os
import time
from pathlib import Path
from typing import List, Union

import numpy as np
import pandas as pd
import torch
from mlperf_common.frameworks.pyt import PyTCommunicationHandler
from mlperf_common.logging import MLLoggerWrapper
from mlperf_logging import mllog

from openfold.checkpoint_utils import (
    resume_from_latest_checkpoint,
    save_checkpoint_from_training,
)
from openfold.config import AlphaFoldConfig
from openfold.dataloaders import (
    InitialTrainingDataloaderPQ,
    InitialTrainingDataloaderPT,
    ValidationDataloader,
)
from openfold.datasets import InitialTrainingDataset, ValidationDataset
from openfold.distributed import dist_gather_val_metrics, dist_reduce_losses_avg
from openfold.helpers import get_seed_from_string, get_timestamp_string, map_dict_values
from openfold.log_utils import save_logs
from openfold.loss import AlphaFoldLoss
from openfold.lr_scheduler import OpenFoldBenchmarkLRScheduler
from openfold.model.alphafold import AlphaFold
from openfold.numpy_utils import NUMPY_SEED_MODULUS
from openfold.samplers import InitialTrainingSampler, ValidationSampler
from openfold.swa import AlphaFoldSWA
from openfold.torch_utils import disable_tf32, enable_tf32, map_tensor_tree
from openfold.validation_metrics import compute_validation_metrics


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--training_dirpath",
        type=Path,
        required=True,
        help="Path to training output directory.",
    )
    parser.add_argument(
        "--pdb_mmcif_chains_filepath",
        type=Path,
        required=True,
        help="Path to mmcif chains CSV file generated by data preprocessing.",
    )
    parser.add_argument(
        "--pdb_mmcif_dicts_dirpath",
        type=Path,
        required=True,
        help="Path to mmcif dicts directory generated by data preprocessing.",
    )
    parser.add_argument(
        "--pdb_obsolete_filepath",
        type=Path,
        required=True,
        help="Path to `obsolete.dat` file.",
    )
    parser.add_argument(
        "--pdb_alignments_dirpath",
        type=Path,
        required=True,
        help="Path to PDB alignments directory generated by data preprocessing.",
    )
    parser.add_argument(
        "--train_max_pdb_release_date",
        type=str,
        default="2021-09-16",
        help="Max PDB release date for training.",
    )
    parser.add_argument(
        "--val_min_cameo_submission_date",
        type=str,
        default="2021-09-17",
        help="Min submission date for CAMEO validation.",
    )
    parser.add_argument(
        "--val_max_cameo_submission_date",
        type=str,
        default="2021-12-11",
        help="Max submission date for CAMEO validation.",
    )
    parser.add_argument(
        "--val_max_sequence_length",
        type=int,
        default=700,
        help="Max sequence length for filtering CAMEO validation set.",
    )
    parser.add_argument(
        "--target_avg_lddt_ca_value",
        type=float,
        default=0.8,
        help="Target avg lDDT-Ca value required to stop training.",
    )
    parser.add_argument(
        "--initialize_parameters_from",
        type=Path,
        default=None,
        help="""Optional path to `.pt` checkpoint file
        used for parameter initialization.""",
    )
    parser.add_argument(
        "--precision",
        choices=["fp32", "tf32", "bf16", "fp16", "amp"],
        default="tf32",
        help="Numerical precision.",
    )
    parser.add_argument(
        "--seed",
        type=str,
        default="1234567890",
        help="Global seed for pseudorandom number generators.",
    )
    parser.add_argument(
        "--num_train_iters",
        type=int,
        default=2000,
        help="Number of training iterations.",
    )
    parser.add_argument(
        "--log_every_iters",
        type=int,
        default=-1,
        help="""Save logs every given iteration.
        A non-positive value disables the log saving.""",
    )
    parser.add_argument(
        "--checkpoint_every_iters",
        type=int,
        default=0,
        help="""Save checkpoints every given iteration.
        A non-positive value disables the checkpoint saving.""",
    )
    parser.add_argument(
        "--keep_last_checkpoints",
        type=int,
        default=0,
        help="How many last checkpoints to keep.",
    )
    parser.add_argument(
        "--val_every_iters",
        type=int,
        default=40,
        help="Compute validation every given iteration.",
    )
    parser.add_argument(
        "--keep_best_checkpoints",
        type=int,
        default=0,
        help="How many best checkpoints to keep.",
    )
    parser.add_argument(
        "--keep_val_checkpoints",
        action="store_true",
        help="Whether to keep all validation checkpoints.",
    )
    parser.add_argument(
        "--local_batch_size",
        type=int,
        default=1,
        help="Local batch size.",
    )
    parser.add_argument(
        "--base_lr",
        type=float,
        default=1e-3,
        help="Base learning rate value.",
    )
    parser.add_argument(
        "--warmup_lr_init",
        type=float,
        default=1e-5,
        help="Warm-up initial learning rate value.",
    )
    parser.add_argument(
        "--warmup_lr_iters",
        type=int,
        default=0,
        help="Num iterations for learning rate warm-up.",
    )
    parser.add_argument(
        "--gradient_accumulation_iters",
        type=int,
        default=1,
        help="""Gradient accumulation iters.
        The default value of 1 means no accumulation.
        When set to > 1, other _iters and _length args must be scaled accordingly.""",
    )
    parser.add_argument(
        "--initial_training_dataloader_type",
        choices=["InitialTrainingDataloaderPT", "InitialTrainingDataloaderPQ"],
        default="InitialTrainingDataloaderPT",
        help="""Initial training dataloader type.
        InitialTrainingDataloaderPT - standard PyTorch DataLoader with deterministic
        sample order.
        InitialTrainingDataloaderPQ - custom dataloader with non-blocking priority queue
        based on PyTorch multiprocessing. Ensures higher throughput at the cost of
        non-deterministic sample order. This dataloader does not wait for time-consuming
        samples, which results in biased sample order where 'faster' samples may appear
        before 'slow' ones more frequently than in deterministic sample order.""",
    )
    parser.add_argument(
        "--num_train_dataloader_workers",
        type=int,
        default=14,
        help="Num workers (subprocesses) for each instance of training dataloader.",
    )
    parser.add_argument(
        "--num_val_dataloader_workers",
        type=int,
        default=2,
        help="Num workers (subprocesses) for each instance of validation dataloader.",
    )
    parser.add_argument(
        "--filter_by_alignments",
        action="store_true",
        help="Whether to filter out mmcif chains with no alignments.",
    )
    parser.add_argument(
        "--use_only_pdb_chain_ids",
        type=str,
        nargs="*",
        default=None,
        help="""Optional list of pdb chain ids
        for intersection with train and val datasets.""",
    )
    parser.add_argument(
        "--save_process_logs",
        action="store_true",
        help="Whether to save logs from each process.",
    )
    parser.add_argument(
        "--mlperf_benchmark_type",
        choices=["TimeToTrain", "Throughput"],
        default="TimeToTrain",
        help="MLPerf benchmark type.",
    )
    parser.add_argument(
        "--distributed",
        action="store_true",
        help="Whether to enable distributed training.",
    )
    args = parser.parse_args()
    # saving checkpoints must coincide with validation:
    if args.checkpoint_every_iters > 0:
        assert args.val_every_iters % args.checkpoint_every_iters == 0
    # saving logs must coincide with validation and checkpoints:
    if args.log_every_iters > 0:
        assert args.val_every_iters % args.log_every_iters == 0
        if args.checkpoint_every_iters > 0:
            assert args.checkpoint_every_iters % args.log_every_iters == 0
    # everything must be divisble by gradient accumulation length:
    assert args.gradient_accumulation_iters >= 1
    assert args.num_train_iters % args.gradient_accumulation_iters == 0
    assert args.val_every_iters % args.gradient_accumulation_iters == 0
    assert args.checkpoint_every_iters % args.gradient_accumulation_iters == 0
    assert args.log_every_iters % args.gradient_accumulation_iters == 0
    assert args.warmup_lr_iters % args.gradient_accumulation_iters == 0
    return args


def create_alphafold_module(
    alphafold_config: AlphaFoldConfig,
    device: torch.device,
    seed: int,
) -> AlphaFold:
    numpy_random_state = np.random.get_state()
    torch_rng_state = torch.get_rng_state()
    torch_cuda_rng_state = torch.cuda.get_rng_state(device=device)
    np.random.seed(seed % NUMPY_SEED_MODULUS)
    torch.manual_seed(seed)
    alphafold = AlphaFold(config=alphafold_config)
    alphafold.to(device=device)
    torch.cuda.set_rng_state(torch_cuda_rng_state, device=device)
    torch.set_rng_state(torch_rng_state)
    np.random.set_state(numpy_random_state)
    return alphafold


def initialize_parameters_from_checkpoint(
    alphafold: AlphaFold,
    optimizer: torch.optim.Optimizer,
    checkpoint_filepath: Path,
    device: torch.device,
    verbose: bool,
) -> str:
    init_checkpoint_bytes = checkpoint_filepath.read_bytes()
    init_checkpoint_md5_hash = hashlib.md5(init_checkpoint_bytes).hexdigest()

    init_checkpoint = torch.load(io.BytesIO(init_checkpoint_bytes), map_location=device)
    is_resumable_checkpoint = bool(
        "alphafold_state_dict" in init_checkpoint
        and "optimizer_state_dict" in init_checkpoint
    )
    if is_resumable_checkpoint:
        init_alphafold_state_dict = init_checkpoint["alphafold_state_dict"]
        init_optimizer_state_dict = init_checkpoint["optimizer_state_dict"]
    else:
        init_alphafold_state_dict = init_checkpoint
        init_optimizer_state_dict = None

    # Initialize alphafold module:
    if verbose:
        print(f"Initializing parameters from {repr(checkpoint_filepath)}...")
    alphafold.load_state_dict(init_alphafold_state_dict, strict=True)
    if verbose:
        print(f"Parameters initialized from {repr(checkpoint_filepath)} successfully!")

    # Initialize optimizer state:
    if init_optimizer_state_dict is not None:
        if verbose:
            print(f"Initializing optimizer from {repr(checkpoint_filepath)}...")
        optimizer.load_state_dict(init_optimizer_state_dict)
        if verbose:
            print(
                f"Optimizer initialized from {repr(checkpoint_filepath)} successfully!"
            )

    return init_checkpoint_md5_hash


def validation(
    alphafold: Union[AlphaFold, AlphaFoldSWA],
    validation_dataloader: ValidationDataloader,
    device: torch.device,
) -> List[dict]:
    alphafold.eval()
    val_metrics_list = []
    val_batch_iterator = iter(validation_dataloader)
    for _ in range(len(validation_dataloader)):
        perf = -time.perf_counter()
        val_batch = next(val_batch_iterator)
        assert len(val_batch["id"]) == 1
        id_tuple = val_batch["id"][0]
        with torch.no_grad():
            val_batch = map_tensor_tree(
                fn=lambda t: t.to(device=device),
                tree=val_batch,
            )
            val_outputs = alphafold(val_batch)
            val_batch = map_tensor_tree(fn=lambda t: t[..., -1], tree=val_batch)
            val_metrics = compute_validation_metrics(
                predicted_atom_positions=val_outputs["final_atom_positions"],
                target_atom_positions=val_batch["all_atom_positions"],
                atom_mask=val_batch["all_atom_mask"],
                metrics_names={"lddt_ca"},
            )
        perf += time.perf_counter()
        val_metrics = map_dict_values(fn=lambda t: t.item(), d=val_metrics)
        val_metrics["val_index"] = id_tuple[1]
        val_metrics["pdb_chain_id"] = id_tuple[3]
        val_metrics["duration"] = perf
        val_metrics_list.append(val_metrics)
    alphafold.train()
    return val_metrics_list


def training(args: argparse.Namespace) -> None:
    if args.distributed:
        torch.distributed.init_process_group(backend="nccl", init_method="env://")

    if torch.distributed.is_initialized():
        # Assuming distributed training:
        assert args.distributed is True
        # https://pytorch.org/docs/stable/elastic/run.html#environment-variables
        rank = int(os.environ["RANK"])
        world_size = int(os.environ["WORLD_SIZE"])
        local_world_size = int(os.environ["LOCAL_WORLD_SIZE"])
        local_rank = int(os.environ["LOCAL_RANK"])
        assert world_size % local_world_size == 0
        num_nodes = world_size // local_world_size
        main_rank = 0
        is_main_process = bool(rank == main_rank)
        process_name = f"dist_process_rank{rank}"
        device = torch.device(f"cuda:{local_rank}")
        global_batch_size = args.local_batch_size * world_size
        if is_main_process:
            print(f"initialized distributed training: WORLD_SIZE={world_size}")
    else:
        # Assuming single GPU training:
        print("single GPU training")
        assert args.distributed is False
        rank = None
        world_size = None
        local_world_size = None
        local_rank = None
        num_nodes = None
        main_rank = None
        is_main_process = True
        process_name = "single_process"
        device = torch.device("cuda:0")
        global_batch_size = args.local_batch_size

    # Create output directory:
    args.training_dirpath.mkdir(parents=True, exist_ok=True)

    # MLPerf logging setup:
    mllog_datestamp = os.environ.get("DATESTAMP", "yymmddHHMMSSfffffffff")
    mlperf_instance = "0"  # TODO: set this value correctly for "Throughput" benchmark
    if args.mlperf_benchmark_type == "TimeToTrain":
        mllog_suffix = os.environ.get("EXP_ID", "0")
    elif args.mlperf_benchmark_type == "Throughput":
        mllog_suffix = mlperf_instance
    else:
        raise ValueError(f"unknown {repr(args.mlperf_benchmark_type)}")
    mllog_filename = f"{mllog_datestamp}_{mllog_suffix}.log"
    mllog_filepath = args.training_dirpath / mllog_filename
    mllog.config(filename=str(mllog_filepath))
    mllogger = MLLoggerWrapper(PyTCommunicationHandler(), value=None)
    mllogger.start(key=mllogger.constants.INIT_START, sync=True)
    mllogger.event(key=mllogger.constants.CACHE_CLEAR, value=True)
    mllogger.mlperf_submission_log(benchmark="openfold", num_nodes=num_nodes)
    mllogger.event(key=mllogger.constants.SEED, value=args.seed)
    mllogger.event(key="number_of_ranks", value=world_size)
    mllogger.event(key="number_of_nodes", value=num_nodes)
    mllogger.event(key="accelerators_per_node", value=local_world_size)
    mllogger.event(key=mllogger.constants.GLOBAL_BATCH_SIZE, value=global_batch_size)
    mllogger.event(
        key=mllogger.constants.GRADIENT_ACCUMULATION_STEPS,
        value=args.gradient_accumulation_iters,
    )
    mllogger.event(key="target_avg_lddt_ca_value", value=args.target_avg_lddt_ca_value)
    mllogger.event(
        key="train_max_pdb_release_date", value=args.train_max_pdb_release_date
    )
    mllogger.event(
        key="val_min_cameo_submission_date", value=args.val_min_cameo_submission_date
    )
    mllogger.event(
        key="val_max_cameo_submission_date", value=args.val_max_cameo_submission_date
    )
    mllogger.event(key="val_max_sequence_length", value=args.val_max_sequence_length)
    mllogger.event(key="val_every_iters", value=args.val_every_iters)

    # Set device:
    torch.cuda.set_device(device=device)

    # Numerical precision settings:
    if args.precision == "fp32":
        disable_tf32()
    elif args.precision == "tf32":
        enable_tf32()
    elif args.precision in {"bf16", "fp16", "amp"}:
        raise NotImplementedError(f"precision={repr(args.precision)}")
    else:
        raise ValueError(f"unknown precision={repr(args.precision)}")
    mllogger.event(key="precision", value=args.precision)

    # Get alphafold config:
    alphafold_config = AlphaFoldConfig.from_preset(
        stage="initial_training",
        precision=args.precision,
    )

    # Create alphafold module:
    alphafold = create_alphafold_module(
        alphafold_config=alphafold_config,
        device=device,
        seed=get_seed_from_string(f"alphafold_init_{args.seed}"),
    )
    alphafold.train()
    mllogger.event(
        key="train_sequence_crop_size", value=alphafold_config.train_sequence_crop_size
    )
    mllogger.event(
        key="num_recycling_iters", value=alphafold_config.num_recycling_iters
    )
    mllogger.event(key="max_msa_clusters", value=alphafold_config.max_msa_clusters)
    mllogger.event(key="max_extra_msa", value=alphafold_config.max_extra_msa)
    mllogger.event(key="templates_enabled", value=alphafold_config.templates_enabled)
    mllogger.event(key="max_templates", value=alphafold_config.max_templates)

    # Create alphafold loss module:
    alphafold_loss = AlphaFoldLoss(config=alphafold_config.loss_config)
    mllogger.event(key="fape_loss_weight", value=alphafold_loss.fape_loss_config.weight)
    mllogger.event(
        key="fape_loss_backbone_weight",
        value=alphafold_loss.fape_loss_config.backbone_weight,
    )
    mllogger.event(
        key="fape_loss_sidechain_weight",
        value=alphafold_loss.fape_loss_config.sidechain_weight,
    )
    mllogger.event(
        key="supervised_chi_loss_weight",
        value=alphafold_loss.supervised_chi_loss_config.weight,
    )
    mllogger.event(
        key="distogram_loss_weight", value=alphafold_loss.distogram_loss_config.weight
    )
    mllogger.event(
        key="masked_msa_loss_weight", value=alphafold_loss.masked_msa_loss_config.weight
    )
    mllogger.event(
        key="plddt_loss_weight", value=alphafold_loss.plddt_loss_config.weight
    )

    # Create optimizer:
    optimizer = torch.optim.Adam(
        params=alphafold.parameters(),
        lr=args.base_lr,  # lr is controlled by lr_scheduler
        betas=(
            alphafold_config.optimizer_adam_beta_1,
            alphafold_config.optimizer_adam_beta_2,
        ),
        eps=alphafold_config.optimizer_adam_eps,
        weight_decay=alphafold_config.optimizer_adam_weight_decay,
        amsgrad=alphafold_config.optimizer_adam_amsgrad,
    )
    mllogger.event(key=mllogger.constants.OPT_NAME, value="Adam")
    mllogger.event(key=mllogger.constants.OPT_BASE_LR, value=args.base_lr)
    mllogger.event(
        key=mllogger.constants.OPT_ADAM_BETA_1,
        value=alphafold_config.optimizer_adam_beta_1,
    )
    mllogger.event(
        key=mllogger.constants.OPT_ADAM_BETA_2,
        value=alphafold_config.optimizer_adam_beta_2,
    )
    mllogger.event(
        key=mllogger.constants.OPT_ADAM_EPSILON,
        value=alphafold_config.optimizer_adam_eps,
    )
    mllogger.event(
        key=mllogger.constants.OPT_WEIGHT_DECAY,
        value=alphafold_config.optimizer_adam_weight_decay,
    )
    mllogger.event(key="opt_amsgrad", value=alphafold_config.optimizer_adam_amsgrad)
    mllogger.event(
        key="opt_gradient_clipping", value=alphafold_config.gradient_clipping
    )
    mllogger.event(
        key=mllogger.constants.OPT_GRADIENT_CLIP_NORM,
        value=alphafold_config.clip_grad_max_norm,
    )

    # Create learning rate scheduler:
    lr_scheduler = OpenFoldBenchmarkLRScheduler(
        base_lr=args.base_lr,
        warmup_lr_init=args.warmup_lr_init,
        warmup_lr_iters=args.warmup_lr_iters,
        optimizer=optimizer,
    )
    mllogger.event(key="opt_learning_rate_warmup_init", value=args.warmup_lr_init)
    mllogger.event(
        key=mllogger.constants.OPT_LR_WARMUP_STEPS, value=args.warmup_lr_iters
    )

    # Initialize parameters from checkpoint if provided:
    if args.initialize_parameters_from is not None:
        init_checkpoint_md5_hash = initialize_parameters_from_checkpoint(
            alphafold=alphafold,
            optimizer=optimizer,
            checkpoint_filepath=args.initialize_parameters_from,
            device=device,
            verbose=is_main_process,
        )
        mllogger.event(key="init_checkpoint_md5_hash", value=init_checkpoint_md5_hash)

    # Create optional SWA version of AlphaFold for evaluation and checkpoints:
    swa_alphafold = AlphaFoldSWA(
        alphafold=alphafold,
        enabled=alphafold_config.swa_enabled,
        decay_rate=alphafold_config.swa_decay_rate,
    )
    mllogger.event(key="swa_enabled", value=alphafold_config.swa_enabled)
    mllogger.event(key="swa_decay_rate", value=alphafold_config.swa_decay_rate)

    # Resume from latest checkpoint if it exists:
    num_prev_iters = resume_from_latest_checkpoint(
        alphafold=alphafold,
        optimizer=optimizer,
        swa_alphafold=swa_alphafold,
        training_dirpath=args.training_dirpath,
        device=device,
        verbose=is_main_process,
    )
    assert num_prev_iters % args.gradient_accumulation_iters == 0

    # Distributed wrapper:
    if args.distributed:
        alphafold = torch.nn.parallel.DistributedDataParallel(module=alphafold)

    # Log number of model parameters:
    mllogger.event(
        key="model_parameters_count",
        value=sum(p.numel() for p in alphafold.parameters()),
    )

    # Create logging-related objects:
    train_logs = []
    process_logs = []
    logs_dirpath = args.training_dirpath / "logs"
    train_logs_outpath = logs_dirpath / "training.log"
    process_logs_outpath = logs_dirpath / (process_name + ".log")
    val_logs_outpath = logs_dirpath / "validation.log"
    is_logging_enabled = bool(args.log_every_iters > 0)
    is_main_process_and_logging = bool(is_main_process and is_logging_enabled)

    # Start data staging:
    mllogger.event(key="staging_start")
    staging_perf = -time.perf_counter()

    # <data staging code here>

    # Finalize data staging:
    staging_perf += time.perf_counter()
    mllogger.event(
        key="staging_stop",
        sync=False,
        metadata={"staging_duration": staging_perf, "instance": mlperf_instance},
    )
    mllogger.event(
        key="tracked_stats",
        sync=False,
        value={"staging_duration": staging_perf},
        metadata={"step": 0, "instance": mlperf_instance},
    )

    # Start MLPerf time-to-train (TTT) measurement:
    mllogger.log_init_stop_run_start()

    # Create training dataset:
    initial_training_dataset = InitialTrainingDataset(
        pdb_mmcif_chains_filepath=args.pdb_mmcif_chains_filepath,
        pdb_mmcif_dicts_dirpath=args.pdb_mmcif_dicts_dirpath,
        pdb_obsolete_filepath=args.pdb_obsolete_filepath,
        pdb_alignments_dirpath=args.pdb_alignments_dirpath,
        max_pdb_release_date=args.train_max_pdb_release_date,
        alphafold_config=alphafold_config,
        filter_by_alignments=args.filter_by_alignments,
        use_only_pdb_chain_ids=args.use_only_pdb_chain_ids,
        name=f"initial_training_dataset_{process_name}",
    )
    mllogger.event(
        key=mllogger.constants.TRAIN_SAMPLES, value=len(initial_training_dataset)
    )

    # Create validation dataset:
    validation_dataset = ValidationDataset(
        pdb_mmcif_chains_filepath=args.pdb_mmcif_chains_filepath,
        pdb_mmcif_dicts_dirpath=args.pdb_mmcif_dicts_dirpath,
        pdb_obsolete_filepath=args.pdb_obsolete_filepath,
        pdb_alignments_dirpath=args.pdb_alignments_dirpath,
        min_cameo_submission_date=args.val_min_cameo_submission_date,
        max_cameo_submission_date=args.val_max_cameo_submission_date,
        max_sequence_length=args.val_max_sequence_length,
        alphafold_config=alphafold_config,
        filter_by_alignments=args.filter_by_alignments,
        use_only_pdb_chain_ids=args.use_only_pdb_chain_ids,
        name=f"validation_dataset_{process_name}",
    )
    mllogger.event(key=mllogger.constants.EVAL_SAMPLES, value=len(validation_dataset))

    # Create training sampler:
    initial_training_sampler = InitialTrainingSampler(
        dataset=initial_training_dataset,
        local_batch_size=args.local_batch_size,
        global_batch_size=global_batch_size,
        num_train_iters=args.num_train_iters,
        seed=get_seed_from_string(f"initial_training_sampler_{args.seed}"),
        is_distributed=args.distributed,
        rank=rank,
        world_size=world_size,
        num_prev_iters=num_prev_iters,
    )

    # Create validation sampler:
    validation_sampler = ValidationSampler(
        dataset=validation_dataset,
        is_distributed=args.distributed,
        rank=rank,
        world_size=world_size,
    )

    # Create training dataloader:
    if args.initial_training_dataloader_type == "InitialTrainingDataloaderPT":
        InitialTrainingDataloader = InitialTrainingDataloaderPT
    elif args.initial_training_dataloader_type == "InitialTrainingDataloaderPQ":
        InitialTrainingDataloader = InitialTrainingDataloaderPQ
    else:
        raise ValueError(
            "unknown initial_training_dataloader_type="
            f"{repr(args.initial_training_dataloader_type)}"
        )
    initial_training_dataloader = InitialTrainingDataloader(
        dataset=initial_training_dataset,
        sampler=initial_training_sampler,
        local_batch_size=args.local_batch_size,
        num_workers=args.num_train_dataloader_workers,
        seed=get_seed_from_string(f"initial_training_dataloader_{args.seed}"),
        uniform_recycling_iters=list(
            range(0, alphafold_config.num_recycling_iters + 1)
        ),
        gradient_accumulation_iters=args.gradient_accumulation_iters,
        num_prev_iters=num_prev_iters,
    )
    train_batch_iterator = iter(initial_training_dataloader)
    mllogger.event(
        key="initial_training_dataloader_type",
        value=args.initial_training_dataloader_type,
    )

    # Create validation dataloader:
    validation_dataloader = ValidationDataloader(
        dataset=validation_dataset,
        sampler=validation_sampler,
        num_workers=args.num_val_dataloader_workers,
    )

    # Training loop:
    first_iteration = num_prev_iters + 1
    for iteration in range(first_iteration, args.num_train_iters + 1):
        # Train-val cycle:
        train_val_cycle_i = (iteration - 1) // args.val_every_iters + 1
        is_train_val_cycle_start = bool((iteration - 1) % args.val_every_iters == 0)
        is_train_val_cycle_end = bool(iteration % args.val_every_iters == 0)
        train_val_cycle_size = global_batch_size * args.val_every_iters
        epoch_num = train_val_cycle_i * train_val_cycle_size

        # Start MLPerf training throughput measurement:
        if is_train_val_cycle_start:
            mllogger.start(
                key=mllogger.constants.EPOCH_START,
                sync=False,
                metadata={"epoch_num": epoch_num, "instance": mlperf_instance},
            )

        # Start training iteration perf measurement:
        perf_training = -time.perf_counter()

        # Deterministic forward pass during training (dropout etc.):
        torch.manual_seed(
            get_seed_from_string(f"forward_{args.seed}_{rank}_{iteration}")
        )

        # Next train batch:
        train_batch = next(train_batch_iterator)
        train_batch = map_tensor_tree(
            fn=lambda t: t.to(device=device),
            tree=train_batch,
        )
        num_recycling_iters = train_batch["aatype"].shape[-1] - 1

        # Forward pass:
        train_outputs = alphafold(train_batch)
        loss, losses = alphafold_loss(
            outputs=train_outputs,
            batch=map_tensor_tree(fn=lambda t: t[..., -1], tree=train_batch),
        )
        loss = loss / args.gradient_accumulation_iters

        # Backward pass:
        if (iteration - 1) % args.gradient_accumulation_iters == 0:
            optimizer.zero_grad()
        loss.backward()

        if iteration % args.gradient_accumulation_iters == 0:
            # Gradient clipping:
            if alphafold_config.gradient_clipping:
                torch.nn.utils.clip_grad_norm_(
                    parameters=alphafold.parameters(),
                    max_norm=alphafold_config.clip_grad_max_norm,
                )

            # LR scheduler update:
            lr_scheduler(iteration)

            # Optimizer step (weights/parameters update):
            optimizer.step()

            # SWA update:
            if swa_alphafold.enabled:
                swa_alphafold.update(alphafold)

        # Average losses from distributed training:
        if is_logging_enabled:
            if args.distributed:
                losses_avg = dist_reduce_losses_avg(
                    losses=losses,
                    is_main_process=is_main_process,
                    main_rank=main_rank,
                    device=device,
                    synchronize=False,
                )
            else:
                losses_avg = losses
            # Convert losses from Dict[str, torch.Tensor] to Dict[str, float]:
            losses = map_dict_values(fn=lambda t: t.item(), d=losses)
            if is_main_process:
                losses_avg = map_dict_values(fn=lambda t: t.item(), d=losses_avg)

        # Finalize training iteration perf measurement:
        perf_training += time.perf_counter()

        # Update process logs:
        if is_logging_enabled and args.save_process_logs:
            process_log = {
                "iteration": iteration,
                "sample_ids": list(map(list, train_batch["id"])),
                "num_recycling_iters": num_recycling_iters,
                "timestamp": get_timestamp_string(),
                **{f"losses.{k}": v for k, v in losses.items()},
                "duration": perf_training,
            }
            process_logs.append(process_log)

        # Update train logs:
        if is_main_process_and_logging:
            train_log = {
                "iteration": iteration,
                "global_batch_size": global_batch_size,
                "num_recycling_iters": num_recycling_iters,
                "timestamp": get_timestamp_string(),
                **{f"losses_avg.{k}": v for k, v in losses_avg.items()},
                "duration": perf_training,
            }
            train_logs.append(train_log)
            print(f"training {train_log}")

        # Save process and train logs:
        if is_logging_enabled and iteration % args.log_every_iters == 0:
            if args.save_process_logs:
                save_logs(process_logs, process_logs_outpath, append=True)
            process_logs.clear()
            if is_main_process:
                save_logs(train_logs, train_logs_outpath, append=True)
                train_logs.clear()

        # End MLPerf training throughput measurement:
        if is_train_val_cycle_end:
            mllogger.end(
                key=mllogger.constants.EPOCH_STOP,
                sync=False,
                metadata={"epoch_num": epoch_num, "instance": mlperf_instance},
            )

        # Validation (evaluation):
        is_validation = is_train_val_cycle_end
        if is_validation:
            # Start MLPerf evaluation measurement:
            mllogger.start(
                key=mllogger.constants.EVAL_START,
                sync=False,
                metadata={"epoch_num": epoch_num, "instance": mlperf_instance},
            )
            perf_validation = -time.perf_counter()
            if is_main_process_and_logging:
                print("validation...")
            del train_batch, train_outputs, loss
            # Execute validation (evaluation) loop:
            val_metrics_list = validation(
                alphafold=swa_alphafold if swa_alphafold.enabled else alphafold,
                validation_dataloader=validation_dataloader,
                device=device,
            )
            if args.distributed:
                # Collect per-sample validation metrics to main process:
                val_metrics_list = dist_gather_val_metrics(
                    val_metrics_list=val_metrics_list,
                    val_pdb_chain_ids=validation_dataset.pdb_chain_ids,
                    is_main_process=is_main_process,
                    main_rank=main_rank,
                    world_size=world_size,
                    device=device,
                    synchronize=True,
                )
            perf_validation += time.perf_counter()
            if is_main_process:
                # Compute aggregated validation metrics in main process:
                val_metrics_df = pd.DataFrame(val_metrics_list)
                val_avg_lddt_ca = float(val_metrics_df["lddt_ca"].mean())
                val_size = len(val_metrics_list)
                assert val_size == len(validation_dataset)
                val_throughput = val_size / perf_validation
                mllogger.event(
                    key="eval_accuracy",
                    value=val_avg_lddt_ca,
                    metadata={"epoch_num": epoch_num, "instance": mlperf_instance},
                )
            if is_main_process_and_logging:
                # Save validation logs:
                val_log = {
                    "iteration": iteration,
                    "avg_lddt_ca": val_avg_lddt_ca,
                    "timestamp": get_timestamp_string(),
                    "duration": perf_validation,
                    "size": val_size,
                    "throughput": val_throughput,
                }
                print(f"validation {val_log}")
                val_log["metrics_list"] = val_metrics_list
                save_logs([val_log], val_logs_outpath, append=True)
            # Check if validation reaches target accuracy:
            if is_main_process:
                if val_avg_lddt_ca >= args.target_avg_lddt_ca_value:
                    stop_training_flag = torch.ones(1, device=device)
                else:
                    stop_training_flag = torch.zeros(1, device=device)
            else:
                stop_training_flag = torch.zeros(1, device=device)
            if args.distributed:
                torch.distributed.broadcast(tensor=stop_training_flag, src=main_rank)
            # Preventively clear the cache created during validation:
            gc.collect()
            torch.cuda.empty_cache()
            # End MLPerf evaluation measurement:
            mllogger.end(
                key=mllogger.constants.EVAL_STOP,
                sync=False,
                metadata={"epoch_num": epoch_num, "instance": mlperf_instance},
            )

        # Save checkpoint:
        if (
            is_main_process
            and args.checkpoint_every_iters > 0
            and iteration % args.checkpoint_every_iters == 0
        ):
            save_checkpoint_from_training(
                alphafold=alphafold,
                optimizer=optimizer,
                swa_alphafold=swa_alphafold,
                iteration=iteration,
                training_dirpath=args.training_dirpath,
                keep_last_checkpoints=args.keep_last_checkpoints,
                keep_best_checkpoints=args.keep_best_checkpoints,
                keep_val_checkpoints=args.keep_val_checkpoints,
                is_validation=is_validation,
                val_avg_lddt_ca=val_avg_lddt_ca if is_validation else None,
            )

        # Stop training if reached target validation metric:
        if is_validation and stop_training_flag:
            break

    # Synchronize before return:
    if args.distributed:
        torch.distributed.barrier()

    # Log the end of the training loop:
    mllogger.log_run_stop(status=mllogger.constants.SUCCESS)


if __name__ == "__main__":
    try:
        training(parse_args())
    except KeyboardInterrupt:
        print("KeyboardInterrupt... exit(1)")
        exit(1)
