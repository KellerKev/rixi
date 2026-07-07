# finetune-qlora — QLoRA fine-tune on a remote GPU

> **The problem:** fine-tuning needs a GPU your laptop doesn't have, and your training env
> (CUDA, bitsandbytes, the exact torch build) is a nightmare to reproduce on a rented box.
> **Why it's hard normally:** build a CUDA Docker image, push it to a registry, pull it on the
> box, pray the versions match. **How RIXI does it:** `rixi run --task finetune` ships your code
> *and* the resolved `.pixi/` env to the GPU box and streams the training logs back — the env
> that trained is the env you tested.

A complete, self-contained QLoRA supervised fine-tune ([`train.py`](train.py)) sized for a
single 24 GB GPU. It loads a small base model in 4-bit, attaches LoRA adapters, trains on a
tiny instruction dataset, and saves the adapter — the canonical "remote GPU job" for RIXI.

## Run it on a GPU box

1. **Provision a GPU server** (or point at an existing one). The bundled helper creates a
   Scaleway L4 (24 GB) and installs the rixi server:

   ```bash
   ./provision-scaleway-gpu.sh                 # loopback-secure; prints an SSH-tunnel command
   # …or expose it authenticated; see the script header for options
   ```

2. **Ship this directory and run the `finetune` task** with the SDK or CLI:

   ```bash
   pip install rixi
   rixi run --server http://127.0.0.1:9000 --task finetune ./examples/finetune-qlora
   ```

   or from Python / a notebook:

   ```python
   from rixi import Client
   for line in Client("http://127.0.0.1:9000").stream(
           "examples/finetune-qlora", task="finetune"):
       print(line, end="")
   ```

The server resolves the Pixi env on the box (PyTorch GPU, transformers, peft, bitsandbytes),
runs the task on the GPU, and streams training logs back to you. The saved adapter lands in
`adapter-out/` on the server.

## Knobs

| Env var | Default | Meaning |
|---|---|---|
| `MODEL` | `TinyLlama/TinyLlama-1.1B-Chat-v1.0` | base model |
| `DATASET` | `yahma/alpaca-cleaned` | HF dataset (instruction/input/output) |
| `MAX_STEPS` | `40` | training steps (raise for real runs) |
| `OUTPUT_DIR` | `adapter-out` | where the LoRA adapter is written |

The demo defaults finish in minutes; scale the model, dataset slice, and steps up for real
fine-tunes. A 24 GB GPU comfortably fits a 7B base model in 4-bit.
