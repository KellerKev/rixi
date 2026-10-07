# showcase-gpu-lifecycle — the whole loop, end to end ⭐

> **The problem:** going from "I have a model idea" to "it's serving behind an API" means renting
> a GPU, reproducing your environment on it, training, standing up a server, *and* remembering to
> shut it all down before the bill runs away. Five tools, five chances to get it wrong.
> **How RIXI does it:** one lifecycle — provision → fine-tune → serve → tear down — with the same
> `rixi` primitive at every step.

This is the marquee walkthrough. It chains the other examples into one complete workflow:

1. **Provision** a GPU box (or bring your own).
2. **Fine-tune** on it — [`finetune-qlora`](../finetune-qlora/) ships its exact env and QLoRA-trains
   on the GPU, logs streaming back to you.
3. **Serve** the result behind an **OpenAI-compatible API** — the [inference-server](../../inference-server/)
   runs as a keep-alive task, the [proxy](../../proxy/) fronts it.
4. **Tear down** the box so billing stops.

## Prerequisites

- `pip install rixi`
- A reachable RIXI server on a GPU box. The quickest path:
  ```bash
  ../../provision-scaleway-gpu.sh          # creates a Scaleway L4 (24 GB), prints an SSH-tunnel command
  # open the tunnel it prints, then:
  export RIXI_SERVER=http://127.0.0.1:9000 # the tunnelled port
  ```
  (Any reachable rixi server works — set `RIXI_SERVER` to it however you got it.)

  This walkthrough uses `provision-scaleway-gpu.sh` rather than `rixi up` because its serving step
  goes through the full client and the proxy, which don't yet speak the per-request tokens and
  encrypted request bodies that `rixi up` boxes require. To **train only**, `rixi up` is the quicker
  path — see [`finetune-qlora`](../finetune-qlora/).

## Run it

```bash
RIXI_SERVER=$RIXI_SERVER ./run.sh
# add PROVISIONED=1 to also destroy the box at the end:
RIXI_SERVER=$RIXI_SERVER PROVISIONED=1 ./run.sh
```

`run.sh` automates the cleanly-scriptable steps (preflight → fine-tune → optional teardown) and
prints the exact commands for the two interactive steps (the keep-alive serve task + the foreground
proxy, which need a live Task ID). Walk through those two, then:

```bash
# call your freshly fine-tuned model like any OpenAI endpoint
curl http://localhost:8002/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-3.5-turbo","messages":[{"role":"user","content":"hello"}]}'
```

## Tear down

```bash
../../provision-scaleway-gpu.sh --destroy      # deletes the instance + its IP/volumes; stops billing
```

That's the whole value in one loop: a GPU you didn't have, running your exact code and environment,
serving your fine-tuned model behind a standard API — and gone the moment you're done paying for it.
