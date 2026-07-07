"""Synthetic product catalog — the single source of truth for the regulatory-extractor sample.

Each product has PUBLIC listing text (what a shopper sees) and a CONFIDENTIAL regulatory profile
(``is_medical_device``, ``device_class`` (EU MDR I/IIa/IIb/III), ``requires_prescription``). The
sample demonstrates that a model can recover the regulated profile from the public listing text
alone, including for products whose listing reads perfectly benign (a "Wearable Tech" heart strap
that is really a Class IIa cardiac device).

The catalog is generated deterministically in-process — there is no external data source, so the
sample runs anywhere with nothing to seed.
"""
from __future__ import annotations

import random

FIELDS = ("is_medical_device", "device_class", "requires_prescription")

# category → regulatory truth + the listing vocabulary that (functionally) gives it away.
#   labels          = (is_medical_device, device_class, requires_prescription)
#   nouns           = product-name nouns
#   signal          = functional phrases that carry the regulatory signal (medical vs consumer)
#   fluff           = benign marketing tone (deliberately the SAME across medical/consumer, so the
#                     model must learn from the functional signal, not the sales adjectives)
_CATEGORIES = {
    "audio":          {"labels": (False, "none", False),
                       "nouns": ["Earbuds", "Headphones", "Speaker", "Soundbar"],
                       "signal": ["wireless bluetooth audio", "active noise cancelling", "deep bass drivers",
                                  "40-hour playback", "studio sound"]},
    "fitness_band":   {"labels": (False, "none", False),
                       "nouns": ["Fitness Band", "Activity Tracker", "Smart Band", "Step Tracker"],
                       "signal": ["daily step counter", "sleep tracking", "calorie estimate",
                                  "workout reminders", "move goals"]},
    "kitchen":        {"labels": (False, "none", False),
                       "nouns": ["Blender", "Kettle", "Toaster", "Coffee Maker"],
                       "signal": ["stainless steel kitchen appliance", "1200-watt motor",
                                  "dishwasher safe", "one-touch brewing", "keep-warm plate"]},
    "supplement":     {"labels": (False, "none", False),
                       "nouns": ["Multivitamin", "Omega-3 Capsules", "Vitamin D Drops", "Magnesium Tablets"],
                       "signal": ["dietary supplement", "daily wellness", "60 capsules",
                                  "supports immune health", "non-GMO"]},
    "ecg_wearable":   {"labels": (True, "IIa", False),
                       "nouns": ["Heart Monitor", "Cardio Strap", "Rhythm Watch", "ECG Wearable"],
                       "signal": ["single-lead ECG", "continuous cardiac rhythm monitoring",
                                  "atrial fibrillation detection", "heart arrhythmia alerts", "clinical-grade ECG sensor"]},
    "pulse_oximeter": {"labels": (True, "IIa", False),
                       "nouns": ["Pulse Oximeter", "SpO2 Monitor", "Finger Oximeter", "Oxygen Monitor"],
                       "signal": ["blood oxygen saturation SpO2", "pulse oximetry", "perfusion index",
                                  "medical oxygen readings", "fingertip sensor"]},
    "bp_monitor":     {"labels": (True, "IIa", False),
                       "nouns": ["Blood Pressure Monitor", "BP Cuff", "Arm Monitor", "Pressure Meter"],
                       "signal": ["upper-arm blood pressure", "systolic and diastolic readings",
                                  "hypertension tracking", "inflatable cuff", "clinically validated measurement"]},
    "thermometer":    {"labels": (True, "IIa", False),
                       "nouns": ["Thermometer", "Fever Scanner", "Ear Thermometer", "Forehead Thermometer"],
                       "signal": ["clinical body temperature", "fever measurement", "infrared medical thermometer",
                                  "accurate to 0.1C", "for infants and adults"]},
    "cgm":            {"labels": (True, "IIb", True),
                       "nouns": ["Glucose Monitor", "CGM Sensor", "Glucose Patch", "Diabetes Sensor"],
                       "signal": ["continuous glucose monitoring", "interstitial glucose sensor",
                                  "for diabetes management", "14-day wear sensor", "prescription required device"]},
    "bandage":        {"labels": (True, "I", False),
                       "nouns": ["Adhesive Bandages", "Wound Dressing", "Sterile Plasters", "Gauze Pads"],
                       "signal": ["sterile wound dressing", "adhesive first-aid bandage", "absorbent gauze",
                                  "for minor cuts and wounds", "breathable fabric"]},
    "surgical_tool":  {"labels": (True, "IIa", False),
                       "nouns": ["Surgical Scissors", "Forceps", "Scalpel Set", "Suture Kit"],
                       "signal": ["reusable surgical instrument", "surgical-grade stainless steel",
                                  "autoclave sterilizable", "for clinical procedures", "precision surgical tool"]},
    "implant":        {"labels": (True, "III", True),
                       "nouns": ["Hip Implant", "Cardiac Stent", "Bone Screw", "Spinal Cage"],
                       "signal": ["implantable orthopedic device", "sterile surgical implant",
                                  "titanium prosthesis", "for permanent implantation", "prescription surgical device"]},
}

_BRANDS = ["Aura", "Nordic", "V612", "Helix", "Cira", "PulsePro", "MediCore", "EverGood",
           "Lumo", "Kite", "Vantar", "Orbit", "Solva", "Meridian", "Kova", "Zenith"]
# Class-neutral product names for the deliberately-hard "benign-looking" listings.
_GENERIC_NOUNS = ["Smart Wearable", "Smart Device", "Wearable Tech", "Home Gadget", "Personal Monitor", "Smart Band"]
_TONE = ["Sleek design and all-day comfort.", "Ships free, 2-year warranty.", "Best-seller this season.",
         "Trusted by thousands of customers.", "Easy setup, works out of the box.",
         "Premium build, everyday price.", "Rated 4.7 stars.", "Order today, arrives tomorrow."]


def build_catalog(per_category: int = 22, seed: int = 7) -> list[dict]:
    """Return a deterministic list of products: public listing text + confidential regulatory labels."""
    rng = random.Random(seed)
    rows: list[dict] = []
    pid = 1000
    for cat, spec in _CATEGORIES.items():
        med, dclass, rx = spec["labels"]
        for _ in range(per_category):
            pid += 1
            brand = rng.choice(_BRANDS)
            noun = rng.choice(spec["nouns"])
            # Listing text = benign marketing tone + a couple of functional signal phrases. The
            # regulated categories share the SAME tone words as consumer ones, so the classifier
            # must learn from the functional signal, not the adjectives. ~14% of listings are
            # deliberately "benign-only": a CLASS-NEUTRAL name ("Smart Wearable") and pure marketing
            # fluff with no functional signal — the workshop's "looks benign, actually Class IIa"
            # case. These are genuinely hard (the text underdetermines the label), so the trained
            # model lands in a credible ~90s% rather than a suspiciously perfect 100%.
            if rng.random() < 0.14:
                gnoun = rng.choice(_GENERIC_NOUNS)
                listing = "%s %s — %s %s" % (brand, gnoun, rng.choice(_TONE), rng.choice(_TONE))
                name = "%s %s" % (brand, gnoun)
            else:
                sig = rng.sample(spec["signal"], k=min(2, len(spec["signal"])))
                listing = "%s %s — %s %s %s" % (brand, noun, sig[0].capitalize(),
                                                (sig[1] + ".") if len(sig) > 1 else "", rng.choice(_TONE))
                name = "%s %s" % (brand, noun)
            rows.append({
                "product_id": pid,
                "product_name": name,
                "listing_text": " ".join(listing.split()),
                "category": cat,
                "is_medical_device": bool(med),
                "device_class": dclass,
                "requires_prescription": bool(rx),
            })
    rng.shuffle(rows)
    return rows


if __name__ == "__main__":  # quick peek: python catalog.py
    cat = build_catalog()
    print("%d products across %d categories" % (len(cat), len(_CATEGORIES)))
    for r in cat[:5]:
        print(" ", r["listing_text"])
        print("     →", {f: r[f] for f in FIELDS})
