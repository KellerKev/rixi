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

1. **Get a GPU box.** `rixi up` creates a Scaleway L4 (24 GB), installs the rixi server, and
   saves a connection profile — one command, with your Scaleway key in the environment:

   ```bash
   pip install rixi
   export SCW_SECRET_KEY=… SCW_ACCESS_KEY=…     # or pass --scw-secret-key / --scw-access-key
   rixi up --provider scaleway --size sample-gpu --name gpu
   ```

   Already have a rixi server on a GPU? Skip this step and add `--server <url>` below.

2. **Ship this directory and run the `finetune` task** — the profile supplies the address, a
   fresh token per request, and the encryption key:

   ```bash
   rixi run --task finetune ./examples/finetune-qlora
   ```

   or from Python / a notebook:

   ```python
   from rixi.cloud import ProfileStore
   client = ProfileStore().resolve("gpu").client()     # or Client("http://…") for your own server
   for line in client.stream("examples/finetune-qlora", task="finetune"):
       print(line, end="")
   ```

3. **Destroy the box** when you're done, so billing stops. The adapter is written to
   `adapter-out/` on the box, so use it (for example, serve it) before you destroy the box:

   ```bash
   rixi down gpu
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
