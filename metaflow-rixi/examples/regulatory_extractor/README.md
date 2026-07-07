# Regulatory extractor — a real ML step on a rixi box, by changing one line

From a product's **public listing text**, predict its **confidential regulatory profile**:

```
is_medical_device · device_class (EU MDR I/IIa/IIb/III) · requires_prescription
```

A listing that reads perfectly benign — a "Wearable Tech" heart strap — can in fact be a **Class IIa
cardiac** device, and the model recovers that regulated profile **from the public listing text
alone**. The model is a scikit-learn extractor that trains in seconds, so the sample runs anywhere
with **no GPU and no external data source**.

## Metaflow integration (the `@rixi` path)

A single decorator moves the training step off your laptop:

```python
class RegulatoryExtractorFlow(FlowSpec):
    @step
    def start(self):            # LOCAL: build the catalog, split train/holdout
        ...

    @rixi(server=RIXI_STEP_SERVER)   # ← the one line: this step runs on a rixi box
    @step
    def train(self):            # ON THE BOX: fit the extractor; model returns via S3
        self.model, self.ab = train_lib.train_and_evaluate(...)

    @step
    def end(self):              # LOCAL: reads self.model back from the datastore; A/B + card
        ...
```

`@rixi` is a Metaflow **compute backend**, like `@batch`/`@kubernetes`: the `train` step runs on a
box you point at (or one the [gateway](../../../gateway/) provisions on demand), while `start`/`end`
stay local. The trained model is a normal Metaflow **artifact** — it travels back through the shared
**S3 datastore**, so the local `end` step reads what the remote step produced. A training step moves
from your laptop to remote compute by adding a single decorator, on infrastructure you run yourself.

## Run it (local, free — a local rixi server + MinIO)

Same setup as [`../branching_flow`](../branching_flow) — an S3-compatible datastore and a rixi box:

```bash
# 1) an S3-compatible datastore (MinIO)
docker run -d -p 9100:9000 -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \
  minio/minio server /data
aws --endpoint-url http://127.0.0.1:9100 s3 mb s3://metaflow

# 2) a rixi box (a local rixi server is fine as the "box" for the dry-run)
cd ../../../server && pixi run python rixi_server.py --port 9000 &

# 3) install the extension + run the flow (the `train` step runs on the box)
pip install -e ../../../metaflow-rixi rixi
export METAFLOW_DEFAULT_DATASTORE=s3 METAFLOW_DATASTORE_SYSROOT_S3=s3://metaflow/
export METAFLOW_S3_ENDPOINT_URL=http://127.0.0.1:9100
export AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin AWS_DEFAULT_REGION=us-east-1
export RIXI_STEP_SERVER=http://127.0.0.1:9000
python extractor_flow.py --datastore=s3 --datastore-root=s3://metaflow/ run
```

Expected tail — the model learned the regulated labels from the listing text alone:

```
trained on the box: fields correct base 93/156 → trained 150/156
A/B (held-out): base 93/156 → trained 150/156 fields correct
  is_medical_device        base 62% → trained 96%
  device_class             base 35% → trained 94%
  requires_prescription    base 83% → trained 98%
live: 'Aura Rhythm Watch — Single-lead ECG continuous cardiac rhythm monitoring...'
  → medical · class IIa
```

(Exact numbers vary slightly with the split; the point is base-majority ≪ trained. The ~4% the
trained model misses are the deliberately benign-only listings — a "Smart Wearable" with no
functional signal in the text — which are genuinely underdetermined.)

## Put the step on a real / on-demand box

Point `@rixi` at a real server, or let the gateway provision one and tear it down:

```python
@rixi(server="https://gpu-box:9000", token="…", aes_key="…")   # a box you point at
@rixi(resource="hetzner-cpu", gateway="ws://gw:7100", secret="…")   # gateway provisions on demand
```

For an auth'd/encrypted box, set `RIXI_BEARER_TOKEN` and `RIXI_AES_KEY_B64` (or `RIXI_AES_KEY` =
a key file) in the environment — the flow reads them automatically.

## Files

| File | Role |
|---|---|
| `extractor_flow.py` | the Metaflow flow — `start` (local) → `train` (`@rixi`) → `end` (local, + a card) |
| `train_lib.py` | the model: TF-IDF → LogisticRegression per field, and a base-vs-trained A/B |
| `catalog.py` | the synthetic product catalog (public text + confidential labels) — no external data source |
| `pixi.toml` | the env the box resolves to run the step (scikit-learn + metaflow + boto3 + `@rixi`) |

## Scaling to a GPU fine-tune

This is the small, reproducible version. The same flow shape scales to a full fine-tune: replace
`train_lib` with a QLoRA (or other GPU) trainer, point `@rixi` at a **GPU** box — or
`@rixi(resource="scw-l4", …)` so the gateway provisions one — and return the trained **adapter** as
the artifact instead of a scikit-learn model. Nothing else in the flow changes: `start`/`end`, the
S3 artifact hand-off, and the base-vs-trained A/B are identical.
