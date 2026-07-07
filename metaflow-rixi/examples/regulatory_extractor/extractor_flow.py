"""Regulatory extractor — a real ML flow whose training step runs on a rixi box by changing ONE line.

From a product's PUBLIC listing text, predict its CONFIDENTIAL regulatory profile
(is_medical_device · device_class · requires_prescription). The training step carries `@rixi`, so it
runs on a rixi box instead of your laptop — `@rixi` is a Metaflow compute backend, like
`@batch`/`@kubernetes`. The trained model travels back as a normal Metaflow artifact through the
shared S3 datastore, so the local `end` step reads what the remote step produced.

  start (local): build the catalog, split train/holdout
  train (@rixi): fit the extractor on the box; return the model + a base-vs-trained A/B
  end   (local): the model is a local artifact (it came back via S3) — report the A/B + a card,
                 and run a live prediction on a benign-looking listing

The model is a plain scikit-learn pipeline, so the flow is CPU-only and finishes in seconds; the
same structure scales to a GPU fine-tune (see the README's upgrade path).

Run (see README.md for the datastore/creds env; local dry-run = a local rixi server + MinIO):
  python extractor_flow.py --datastore=s3 --datastore-root=s3://metaflow/ run
"""
import os

# The box runs the step with a bare environment (no $USER); give Metaflow a stable identity so it
# doesn't error with "could not determine your user name". setdefault → orchestrator + box agree.
os.environ.setdefault("METAFLOW_USER", "workshop")

from metaflow import FlowSpec, step, rixi, card, current
from metaflow.cards import Markdown, Table

import catalog
import train_lib

# Point the @rixi step at any rixi box. Default: a local server (the zero-setup dry-run). For an
# auth'd/encrypted box, a token + AES key flow in from env (accept a base64 string or a key file).
RIXI_STEP_SERVER = os.environ.get("RIXI_STEP_SERVER", "http://127.0.0.1:9000")
RIXI_TOKEN = os.environ.get("RIXI_BEARER_TOKEN") or None
_aes = os.environ.get("RIXI_AES_KEY_B64") or ""
if not _aes and os.environ.get("RIXI_AES_KEY") and os.path.exists(os.environ["RIXI_AES_KEY"]):
    _aes = open(os.environ["RIXI_AES_KEY"]).read().strip()
RIXI_AES = _aes or None


class RegulatoryExtractorFlow(FlowSpec):
    @step
    def start(self):
        # LOCAL: build the catalog (public listing text + confidential labels) and split it.
        records = catalog.build_catalog()
        self.train_records, self.holdout_records = train_lib.split(records, holdout_frac=0.2)
        print("catalog: %d products → %d train / %d holdout"
              % (len(records), len(self.train_records), len(self.holdout_records)))
        self.next(self.train)

    @rixi(server=RIXI_STEP_SERVER, token=RIXI_TOKEN, aes_key=RIXI_AES)   # ← the one line: train on the box
    @step
    def train(self):
        # ON THE RIXI BOX: fit the extractor; the model + A/B travel back via the S3 datastore.
        self.model, self.ab = train_lib.train_and_evaluate(self.train_records, self.holdout_records)
        print("trained on the box: fields correct base %d/%d → trained %d/%d"
              % (self.ab["base_correct"], self.ab["total_fields"],
                 self.ab["trained_correct"], self.ab["total_fields"]))
        self.next(self.end)

    @card(type="blank")
    @step
    def end(self):
        # BACK ON THE LAPTOP: the model is a local artifact (it came through Object Storage). Report
        # the A/B, then run a live prediction on a benign-LOOKING listing to show what it learned.
        ab = self.ab
        print("A/B (held-out): base %d/%d → trained %d/%d fields correct"
              % (ab["base_correct"], ab["total_fields"], ab["trained_correct"], ab["total_fields"]))
        for f, s in ab["per_field"].items():
            print("  %-24s base %.0f%% → trained %.0f%%" % (f, 100 * s["base_acc"], 100 * s["trained_acc"]))

        benign = "Aura Rhythm Watch — Single-lead ECG continuous cardiac rhythm monitoring. Rated 4.7 stars."
        pred = train_lib.predict(self.model, benign)
        print("\nlive: %r\n  → %s" % (benign, train_lib.fmt(pred)))

        current.card.append(Markdown("# Regulatory extractor — trained on a rixi box, model returned via S3"))
        current.card.append(Markdown(
            "Predict a product's **regulatory profile** from its **public listing text**. "
            "A/B on held-out listings: **base %d/%d → trained %d/%d** fields correct."
            % (ab["base_correct"], ab["total_fields"], ab["trained_correct"], ab["total_fields"])))
        current.card.append(Table(
            [[f, "%.0f%%" % (100 * s["base_acc"]), "%.0f%%" % (100 * s["trained_acc"])]
             for f, s in ab["per_field"].items()],
            headers=["regulatory field", "base (majority)", "trained"]))
        current.card.append(Markdown("### Held-out examples (gold = confidential label)"))
        current.card.append(Table(
            [[e["product_name"], train_lib.fmt(e["gold"]), train_lib.fmt(e["base"]), train_lib.fmt(e["trained"])]
             for e in ab["examples"]],
            headers=["product", "gold", "base", "trained"]))


if __name__ == "__main__":
    RegulatoryExtractorFlow()
