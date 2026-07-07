"""A small, self-contained ETL job: generate → transform → write.

Demonstrates using RIXI for data work, not just ML: ship this directory to a rixi server
(near your data, or just on a bigger machine) and run the `pipeline` task. It synthesizes a
sales dataset, aggregates it with Polars, and writes Parquet + a printed summary — swap the
`extract()` step for your real source (a CSV path, an object-store URL, a SQL query).

    rixi run --server http://127.0.0.1:9000 --task pipeline ./examples/data-pipeline
"""
import os

import polars as pl

OUTPUT = os.environ.get("OUTPUT", "out.parquet")
N = int(os.environ.get("ROWS", "100000"))


def extract() -> pl.DataFrame:
    # Deterministic synthetic data so the demo needs no external source.
    regions = ["EMEA", "AMER", "APAC"]
    idx = pl.int_range(0, N, eager=True)
    return pl.DataFrame({
        "order_id": idx,
        "region": idx.map_elements(lambda i: regions[i % 3], return_dtype=pl.Utf8),
        "units": (idx % 7 + 1),
        "unit_price": (idx % 50 + 10).cast(pl.Float64),
    })


def transform(df: pl.DataFrame) -> pl.DataFrame:
    return (
        df.with_columns((pl.col("units") * pl.col("unit_price")).alias("revenue"))
          .group_by("region")
          .agg([
              pl.len().alias("orders"),
              pl.col("units").sum().alias("total_units"),
              pl.col("revenue").sum().round(2).alias("total_revenue"),
          ])
          .sort("total_revenue", descending=True)
    )


def main() -> None:
    df = extract()
    print(f"extracted {df.height:,} rows")
    summary = transform(df)
    summary.write_parquet(OUTPUT)
    print(f"✅ wrote {OUTPUT}")
    print(summary)


if __name__ == "__main__":
    main()
