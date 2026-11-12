"""Entraînement de l'adapter du domaine : SFT LoRA (TRL SFTTrainer + PEFT), puis GRPO à récompense d'exécution.

Usage (sur une machine GPU, après « pip install 'qraft-forge[train]' ») :

    python -m qraft_forge.train --data forge/ --base-model Qwen/Qwen2.5-Coder-7B-Instruct --out adapters/ventes \\
        --db duckdb:///replica/ventes.duckdb --semantic examples/ventes.semantic.yaml --stage all

Matériel indicatif (à calibrer au spike de la phase 0) :
- SFT LoRA d'un modèle de 7-8 milliards de paramètres en bf16 : un GPU de 80 Go (A100/H100), ou un GPU
  de 24 Go avec un modèle de 3 milliards ou une quantification 4 bits (QLoRA, non câblée ici).
  Compter quelques heures par époque pour 20 000 à 100 000 paires (prompts longs : le résumé du domaine
  est dans le prompt système, d'où --max-length élevé).
- GRPO : la génération domine le coût (num_generations complétions par prompt). Prévoir au moins un
  GPU de 80 Go pour un 7B ; --use-vllm accélère nettement la génération (vLLM à installer à part).
- La récompense exécute chaque intention produite sur la base : utiliser une réplique ou un échantillon,
  jamais la production. Les résultats sont mis en cache par empreinte d'intention.

Ce script n'est pas exécuté par les tests (pas de GPU) ; les imports lourds sont différés et protégés.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any


def _require_stack() -> dict[str, Any]:
    """Importe la pile d'entraînement, ou arrête avec un message clair."""
    try:
        import datasets
        import peft
        import torch
        import transformers
        import trl
    except ImportError as error:
        raise SystemExit(f"pile d'entraînement absente ({error.name}) : pip install 'qraft-forge[train]'") from error
    return {"datasets": datasets, "peft": peft, "torch": torch, "transformers": transformers, "trl": trl}


def _load_splits(stack: dict[str, Any], data_dir: Path) -> Any:
    files = {"train": str(data_dir / "train.jsonl")}
    if (data_dir / "validation.jsonl").exists() and (data_dir / "validation.jsonl").stat().st_size:
        files["validation"] = str(data_dir / "validation.jsonl")
    return stack["datasets"].load_dataset("json", data_files=files)


def _lora(stack: dict[str, Any], args: argparse.Namespace) -> Any:
    return stack["peft"].LoraConfig(r=args.lora_r, lora_alpha=args.lora_alpha, lora_dropout=args.lora_dropout,
                                    target_modules="all-linear", task_type="CAUSAL_LM")


def _base_model(stack: dict[str, Any], name: str) -> tuple[Any, Any]:
    transformers, torch = stack["transformers"], stack["torch"]
    tokenizer = transformers.AutoTokenizer.from_pretrained(name)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    dtype = torch.bfloat16 if torch.cuda.is_available() and torch.cuda.is_bf16_supported() else torch.float32
    model = transformers.AutoModelForCausalLM.from_pretrained(name, torch_dtype=dtype)
    return model, tokenizer


def run_sft(args: argparse.Namespace) -> Path:
    """SFT LoRA : prompt = [system, user], complétion = [assistant] ; la perte ne porte que sur la complétion."""
    stack = _require_stack()
    trl = stack["trl"]
    splits = _load_splits(stack, args.data)

    def to_prompt_completion(row: dict[str, Any]) -> dict[str, Any]:
        return {"prompt": row["messages"][:-1], "completion": row["messages"][-1:]}

    splits = splits.map(to_prompt_completion, remove_columns=splits["train"].column_names)
    model, tokenizer = _base_model(stack, args.base_model)
    output = args.out / "sft"
    config = trl.SFTConfig(
        output_dir=str(output),
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.lr,
        lr_scheduler_type="cosine",
        warmup_ratio=0.03,
        max_length=args.max_length,
        gradient_checkpointing=True,
        bf16=stack["torch"].cuda.is_available(),
        logging_steps=10,
        eval_strategy="epoch" if "validation" in splits else "no",
        save_strategy="epoch",
        report_to=args.report_to,
        seed=args.seed,
    )
    trainer = trl.SFTTrainer(
        model=model,
        args=config,
        train_dataset=splits["train"],
        eval_dataset=splits.get("validation"),
        processing_class=tokenizer,
        peft_config=_lora(stack, args),
    )
    trainer.train()
    trainer.save_model(str(output))  # adapter LoRA seul (quelques dizaines de Mo)
    tokenizer.save_pretrained(str(output))
    return output


def run_grpo(args: argparse.Namespace, adapter: Path) -> Path:
    """GRPO : on poursuit l'entraînement du même adapter avec la récompense d'exécution."""
    stack = _require_stack()
    trl, peft = stack["trl"], stack["peft"]
    if not args.db:
        raise SystemExit("--db est requis pour GRPO : la récompense exécute les intentions produites")

    from qraft.engine import build_graph
    from qraft.execute import Source

    from .reward import ExecutionReward

    source = Source(args.db)
    graph = build_graph(source, semantic=args.semantic)
    reward = ExecutionReward(graph, source)

    splits = _load_splits(stack, args.data)

    def to_grpo(row: dict[str, Any]) -> dict[str, Any]:
        # TRL transmet les colonnes supplémentaires à la récompense : « reference » porte l'intention attendue.
        return {"prompt": row["messages"][:-1], "reference": row["messages"][-1]["content"]}

    train = splits["train"].map(to_grpo, remove_columns=splits["train"].column_names)
    base, tokenizer = _base_model(stack, args.base_model)
    model = peft.PeftModel.from_pretrained(base, str(adapter), is_trainable=True)
    output = args.out / "grpo"
    config = trl.GRPOConfig(
        output_dir=str(output),
        learning_rate=args.grpo_lr,
        per_device_train_batch_size=args.grpo_batch_size,  # doit être un multiple de num_generations (par appareil)
        gradient_accumulation_steps=args.grad_accum,
        num_generations=args.num_generations,
        max_prompt_length=args.max_length,
        max_completion_length=args.max_completion_length,
        max_steps=args.grpo_steps,
        beta=args.beta,
        temperature=1.0,
        gradient_checkpointing=True,
        bf16=stack["torch"].cuda.is_available(),
        use_vllm=args.use_vllm,
        logging_steps=10,
        save_steps=max(50, args.grpo_steps // 5),
        report_to=args.report_to,
        seed=args.seed,
    )
    trainer = trl.GRPOTrainer(model=model, reward_funcs=[reward], args=config, train_dataset=train,
                              processing_class=tokenizer)
    trainer.train()
    trainer.save_model(str(output))
    tokenizer.save_pretrained(str(output))
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m qraft_forge.train",
                                     description="Entraîne l'adapter LoRA d'un domaine : SFT puis GRPO.")
    parser.add_argument("--data", type=Path, required=True, help="dossier produit par « qraft-forge generate »")
    parser.add_argument("--base-model", required=True, help="identifiant Hugging Face du modèle de base")
    parser.add_argument("--out", type=Path, required=True, help="dossier des adapters (sft/ et grpo/)")
    parser.add_argument("--stage", choices=("sft", "grpo", "all"), default="all")
    parser.add_argument("--adapter", type=Path, help="adapter SFT existant (pour --stage grpo)")
    parser.add_argument("--db", help="URL de la base (réplique) pour la récompense GRPO")
    parser.add_argument("--semantic", type=Path, help="couche sémantique YAML du domaine")
    parser.add_argument("--epochs", type=float, default=2.0)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--grad-accum", type=int, default=4)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--max-length", type=int, default=4096, help="le prompt système contient le résumé du domaine")
    parser.add_argument("--lora-r", type=int, default=16)
    parser.add_argument("--lora-alpha", type=int, default=32)
    parser.add_argument("--lora-dropout", type=float, default=0.05)
    parser.add_argument("--grpo-steps", type=int, default=500)
    parser.add_argument("--grpo-lr", type=float, default=5e-6)
    parser.add_argument("--grpo-batch-size", type=int, default=8)
    parser.add_argument("--num-generations", type=int, default=8)
    parser.add_argument("--max-completion-length", type=int, default=768)
    parser.add_argument("--beta", type=float, default=0.0, help="poids KL (0 : pas de modèle de référence en mémoire)")
    parser.add_argument("--use-vllm", action="store_true", help="génération GRPO par vLLM")
    parser.add_argument("--report-to", default="none", help="none | mlflow | tensorboard")
    parser.add_argument("--seed", type=int, default=42)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    adapter = args.adapter
    if args.stage in ("sft", "all"):
        adapter = run_sft(args)
        print(f"Adapter SFT : {adapter}")
    if args.stage in ("grpo", "all"):
        if adapter is None:
            raise SystemExit("--adapter est requis pour --stage grpo")
        print(f"Adapter GRPO : {run_grpo(args, adapter)}")


if __name__ == "__main__":
    main()
