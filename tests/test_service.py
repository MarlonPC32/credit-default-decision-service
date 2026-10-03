"""Tests for the credit-default decision service.

Run from the project root:  python -m pytest tests/ -q
"""
import sys
from pathlib import Path

import joblib
import pandas as pd
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from preprocessing import FEATURE_COLUMNS, TARGET, clean_frame, row_to_vector  # noqa: E402
from app import app  # noqa: E402

client = TestClient(app)

VALID_APPLICATION = {
    "LIMIT_BAL": 20000, "SEX": 2, "EDUCATION": 2, "MARRIAGE": 1, "AGE": 35,
    "PAY_0": 0, "PAY_2": -1, "PAY_3": -1, "PAY_4": 0, "PAY_5": 0, "PAY_6": -1,
    "BILL_AMT1": 3913, "BILL_AMT2": 3102, "BILL_AMT3": 689, "BILL_AMT4": 0,
    "BILL_AMT5": 0, "BILL_AMT6": 0,
    "PAY_AMT1": 0, "PAY_AMT2": 689, "PAY_AMT3": 0, "PAY_AMT4": 0,
    "PAY_AMT5": 0, "PAY_AMT6": 0,
}


def test_preprocessing_train_serve_consistency():
    """A raw record preprocessed for serving must equal the training-time vector."""
    raw = pd.read_excel(ROOT / "data" / "default_of_credit_card_clients.xls", header=1)
    cleaned = clean_frame(raw)
    train_row = cleaned.iloc[0]
    train_vector = [train_row[c] for c in FEATURE_COLUMNS]

    raw_record = {c: raw.iloc[0][c] for c in FEATURE_COLUMNS}
    serve_vector = row_to_vector(raw_record)

    assert serve_vector == train_vector


def test_preprocessing_remaps_unknown_codes():
    """Undefined EDUCATION/MARRIAGE codes are remapped, never passed through."""
    rec = dict(VALID_APPLICATION, EDUCATION=5, MARRIAGE=0)
    vec = row_to_vector(rec)
    assert vec[FEATURE_COLUMNS.index("EDUCATION")] == 4
    assert vec[FEATURE_COLUMNS.index("MARRIAGE")] == 3


def test_predict_returns_probability_and_decision():
    r = client.post("/predict", json=VALID_APPLICATION)
    assert r.status_code == 200
    body = r.json()
    assert 0.0 <= body["default_probability"] <= 1.0
    assert body["decision"] in (0, 1)
    assert body["threshold"] == 0.3


def test_predict_rejects_missing_features():
    bad = dict(VALID_APPLICATION)
    del bad["PAY_0"]
    r = client.post("/predict", json=bad)
    assert r.status_code == 422


def test_predict_rejects_invalid_values():
    bad = dict(VALID_APPLICATION, SEX=5)  # out of range
    r = client.post("/predict", json=bad)
    assert r.status_code == 422
    bad2 = dict(VALID_APPLICATION, AGE="thirty")  # wrong type
    r2 = client.post("/predict", json=bad2)
    assert r2.status_code == 422


def test_artifacts_load_and_predict():
    model = joblib.load(ROOT / "models" / "model.joblib")
    vec = row_to_vector(VALID_APPLICATION)
    proba = model.predict_proba([vec])[0]
    assert len(proba) == 2
    assert 0.0 <= proba[1] <= 1.0
