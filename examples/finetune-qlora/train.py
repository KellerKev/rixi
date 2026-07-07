"""Minimal QLoRA fine-tune, sized to run on a single 24 GB GPU (e.g. Scaleway L4-1-24G).

This is a complete, self-contained SFT loop: load a small base model in 4-bit, attach LoRA
adapters, train on a tiny instruction dataset, and save the adapter. It is deliberately
small so a full run finishes in minutes and demonstrates the RIXI GPU workflow end-to-end;
scale MODEL, dataset, and STEPS up for real work.

Run it on a rixi GPU server:

    rixi run --server https://gpu-box:9000 --task finetune ./examples/finetune-qlora

Environment knobs: MODEL, DATASET, MAX_STEPS, OUTPUT_DIR.
"""
import os

import torch
from datasets import load_dataset
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    Trainer,
    TrainingArguments,
)

MODEL = os.environ.get("MODEL", "TinyLlama/TinyLlama-1.1B-Chat-v1.0")
DATASET = os.environ.get("DATASET", "yahma/alpaca-cleaned")
MAX_STEPS = int(os.environ.get("MAX_STEPS", "40"))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "adapter-out")


def main() -> None:
    if not torch.cuda.is_available():
        raise SystemExit(
            "No CUDA GPU visible. Ship this task to a GPU-backed rixi server "
            "(see examples/finetune-qlora/README.md).")
    print(f"GPU: {torch.cuda.get_device_name(0)} | base model: {MODEL}")

    tokenizer = AutoTokenizer.from_pretrained(MODEL)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    quant = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )
    model = AutoModelForCausalLM.from_pretrained(
        MODEL, quantization_config=quant, device_map="auto")
    model = prepare_model_for_kbit_training(model)
    model = get_peft_model(model, LoraConfig(
        r=16, lora_alpha=32, lora_dropout=0.05, bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
    ))
    model.print_trainable_parameters()

    # A small slice keeps the demo fast; drop the split slice for a full run.
    data = load_dataset(DATASET, split="train[:512]")

    def format_and_tokenize(row):
        instr, inp, out = row.get("instruction", ""), row.get("input", ""), row.get("output", "")
        prompt = f"### Instruction:\n{instr}\n"
        if inp:
            prompt += f"### Input:\n{inp}\n"
        text = prompt + f"### Response:\n{out}{tokenizer.eos_token}"
        enc = tokenizer(text, truncation=True, max_length=512, padding="max_length")
        enc["labels"] = enc["input_ids"].copy()
        return enc

    tokenized = data.map(format_and_tokenize, remove_columns=data.column_names)

    trainer = Trainer(
        model=model,
        args=TrainingArguments(
            output_dir=OUTPUT_DIR,
            per_device_train_batch_size=1,
            gradient_accumulation_steps=8,
            max_steps=MAX_STEPS,
            learning_rate=2e-4,
            bf16=True,
            logging_steps=5,
            save_strategy="no",
            report_to=[],
        ),
        train_dataset=tokenized,
    )
    trainer.train()
    model.save_pretrained(OUTPUT_DIR)
    tokenizer.save_pretrained(OUTPUT_DIR)
    print(f"✅ QLoRA adapter saved to {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
