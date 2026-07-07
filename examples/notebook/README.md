# notebook — drive RIXI from Jupyter with the SDK

> **The problem:** you work in a notebook, but the compute (a GPU, a big box) lives elsewhere.
> **Why it's hard normally:** notebook magics that SSH out, or copy-pasting shell commands into
> cells. **How RIXI does it:** `from rixi import Client` — run and stream remote tasks straight
> from a cell, results in your notebook. The workflow the `rixi` PyPI package unlocks.

[`quickstart.ipynb`](quickstart.ipynb) walks through it: check health, `run()` a task and collect
the output, then `stream()` a long job (e.g. the QLoRA fine-tune) live in the notebook.

## Run it

```bash
pixi run lab           # opens JupyterLab on quickstart.ipynb
```

The notebook talks to a RIXI server at `http://127.0.0.1:9000` by default — start one locally
(`cd ../../server && pixi run python rixi_server.py --port 9000`) or point `Client(...)` at a
remote box (or an SSH tunnel to one).

> The `pixi.toml` installs `rixi` from this repo checkout (`path = "../.."`). After `pip install
> rixi` is published you can switch it to a plain `rixi = ">=0.2"`.
