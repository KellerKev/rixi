# data-pipeline — run a data/ETL job near your data

> **The problem:** the dataset (and the RAM to process it) live on a server, not your laptop —
> and RIXI isn't only for ML. **Why it's hard normally:** copy the data down, or SSH in and
> hand-install the data stack on the box. **How RIXI does it:** ship the job — code and its exact
> Polars/PyArrow environment — to a bigger box or one sitting next to the data, and stream the
> result back. Nothing installed locally.

A small, self-contained ETL ([`pipeline.py`](pipeline.py)): generate a dataset → aggregate it with
Polars → write Parquet + a printed summary. Swap the `extract()` step for your real source (a CSV
path, an object-store URL, a SQL query).

## Run it

Locally:

```bash
pixi run pipeline
```

On a remote RIXI server (ships this dir + its env, runs there, streams back):

```bash
rixi run --server http://127.0.0.1:9000 --task pipeline .
```

## Knobs

| Env var | Default | Meaning |
|---|---|---|
| `ROWS` | `100000` | rows of synthetic data to generate |
| `OUTPUT` | `out.parquet` | where the aggregated Parquet is written |
