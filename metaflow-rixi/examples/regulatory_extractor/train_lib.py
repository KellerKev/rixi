"""The regulatory extractor: from a product's PUBLIC listing text, predict its CONFIDENTIAL
regulatory profile. Plain scikit-learn (TF-IDF → a small classifier per field) — real ML that
trains in seconds on a CPU, so the sample runs on ANY rixi box (no GPU required).

`train_and_evaluate` returns the trained model (a plain dict Metaflow pickles into the S3 datastore)
plus a base-vs-trained A/B, so the flow can show the model actually learned.
"""
from __future__ import annotations

from sklearn.dummy import DummyClassifier
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression

from catalog import FIELDS


def split(records: list[dict], holdout_frac: float = 0.2, seed: int = 0):
    """Deterministic train/holdout split (no external deps; stable across the local↔box boundary)."""
    rows = sorted(records, key=lambda r: r["product_id"])
    import random
    random.Random(seed).shuffle(rows)
    n_hold = max(1, int(len(rows) * holdout_frac))
    return rows[n_hold:], rows[:n_hold]


def _labels(records, field):
    return [str(r[field]) for r in records]


def train_and_evaluate(train_records: list[dict], holdout_records: list[dict]) -> tuple[dict, dict]:
    """Fit a TF-IDF + LogisticRegression per regulatory field; compare against a majority-class
    baseline on the held-out set. Returns (model, ab)."""
    train_text = [r["listing_text"] for r in train_records]
    hold_text = [r["listing_text"] for r in holdout_records]

    vec = TfidfVectorizer(ngram_range=(1, 2), min_df=1, sublinear_tf=True)
    xtr = vec.fit_transform(train_text)
    xho = vec.transform(hold_text)

    clf: dict = {}
    base: dict = {}
    per_field: dict = {}
    for field in FIELDS:
        ytr = _labels(train_records, field)
        yho = _labels(holdout_records, field)
        model = LogisticRegression(max_iter=1000, C=4.0).fit(xtr, ytr)
        dummy = DummyClassifier(strategy="most_frequent").fit(xtr, ytr)
        clf[field] = model
        base[field] = dummy
        trained_pred = list(model.predict(xho))
        base_pred = list(dummy.predict(xho))
        per_field[field] = {
            "base_acc": round(_acc(base_pred, yho), 3),
            "trained_acc": round(_acc(trained_pred, yho), 3),
        }

    # A few example rows for the card: product → gold / base / trained across all fields.
    examples = []
    base_correct = trained_correct = 0
    for i, r in enumerate(holdout_records):
        gold = {f: str(r[f]) for f in FIELDS}
        bpred = {f: str(base[f].predict(xho[i])[0]) for f in FIELDS}
        tpred = {f: str(clf[f].predict(xho[i])[0]) for f in FIELDS}
        base_correct += sum(bpred[f] == gold[f] for f in FIELDS)
        trained_correct += sum(tpred[f] == gold[f] for f in FIELDS)
        if i < 8:
            examples.append({"product_name": r["product_name"], "gold": gold, "base": bpred, "trained": tpred})

    total = len(holdout_records) * len(FIELDS)
    ab = {
        "per_field": per_field,
        "examples": examples,
        "base_correct": base_correct,
        "trained_correct": trained_correct,
        "total_fields": total,
        "n_holdout": len(holdout_records),
    }
    model_obj = {"vectorizer": vec, "classifiers": clf, "fields": list(FIELDS)}
    return model_obj, ab


def predict(model: dict, listing_text: str) -> dict:
    """Predict the regulatory profile for one listing (used by the flow's `end` demo + the README)."""
    x = model["vectorizer"].transform([listing_text])
    out = {}
    for field, est in model["classifiers"].items():
        v = est.predict(x)[0]
        out[field] = v == "True" if v in ("True", "False") else v
    return out


def _acc(pred, gold) -> float:
    return sum(p == g for p, g in zip(pred, gold)) / max(1, len(gold))


def fmt(profile: dict) -> str:
    """Compact one-line render of a regulatory profile for tables/logs."""
    med = "medical" if str(profile.get("is_medical_device")) == "True" else "consumer"
    rx = " · Rx" if str(profile.get("requires_prescription")) == "True" else ""
    return "%s · class %s%s" % (med, profile.get("device_class"), rx)
