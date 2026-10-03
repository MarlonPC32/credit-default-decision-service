"""Tests for the credit-default risk scoring service.

Run from the project root:
    python -m pytest tests/ -q

Unit tests need no downloads. The full-data integration test runs only when
data/default_of_credit_card_clients.xls is present (otherwise it skips).
"""
import os
import sqlite3
from pathlib import Path

import joblib
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src import preprocessing
from src.app import THRESHOLD, app
from src.preprocessing import FEATURE_COLUMNS, TARGET, clean_frame, row_to_vector

ROOT = Path(__file__).resolve().parent.parent
RAW_XLS = ROOT / "data" / "default_of_credit_card_clients.xls"

client = TestClient(app)

VALID_CUSTOMER = {
    "LIMIT_BAL": 20000, "SEX": 2, "EDUCATION": 2, "MARRIAGE": 1, "AGE": 35,
    "PAY_0": 0, "PAY_2": -1, "PAY_3": -1, "PAY_4": 0, "PAY_5": 0, "PAY_6": -1,
    "BILL_AMT1": 3913, "BILL_AMT2": 3102, "BILL_AMT3": 689, "BILL_AMT4": 0,
    "BILL_AMT5": 0, "BILL_AMT6": 0,
    "PAY_AMT1": 0, "PAY_AMT2": 689, "PAY_AMT3": 0, "PAY_AMT4": 0,
    "PAY_AMT5": 0, "PAY_AMT6": 0,
}


@pytest.fixture(autouse=True)
def _isolated_decision_db(monkeypatch, tmp_path):
    """Tests never touch the real decisions.db: point logging at a temp file."""
    monkeypatch.setenv("DECISIONS_DB", str(tmp_path / "test_decisions.db"))


def _synthetic_raw_frame() -> pd.DataFrame:
    """Tiny inline stand-in for the raw xls schema: ID + 23 features + target."""
    cols = ["ID"] + FEATURE_COLUMNS + ["default payment next month"]
    rows = [
        [1, 20000, 2, 2, 1, 24, 2, 2, -1, -1, -2, -2,
         3913, 3102, 689, 0, 0, 0, 0, 689, 0, 0, 0, 0, 1],
        [2, 120000, 2, 5, 2, 26, -1, 2, 0, 0, 0, 2,   # EDUCATION=5 is undefined
         2682, 1725, 2682, 3272, 3455, 3261, 0, 1000, 1000, 1000, 0, 2000, 1],
        [3, 90000, 1, 2, 0, 34, 0, 0, 0, 0, 0, 0,     # MARRIAGE=0 is undefined
         29239, 14027, 13559, 14331, 14948, 15549, 1518, 1500, 1000, 1000, 1000, 5000, 0],
    ]
    return pd.DataFrame(rows, columns=cols)


def test_preprocessing_train_serve_consistency():
    """A raw record preprocessed for serving must equal the training-time vector."""
    raw = _synthetic_raw_frame()
    cleaned = clean_frame(raw)

    for i in range(len(raw)):
        train_vector = [cleaned.iloc[i][c] for c in FEATURE_COLUMNS]
        raw_record = {c: raw.iloc[i][c] for c in FEATURE_COLUMNS}
        assert row_to_vector(raw_record) == train_vector

    # undefined codes remapped identically on both paths
    assert cleaned.loc[1, "EDUCATION"] == 4
    assert cleaned.loc[2, "MARRIAGE"] == 3
    assert "ID" not in cleaned.columns
    assert TARGET in cleaned.columns


def test_preprocessing_remaps_unknown_codes():
    """Undefined EDUCATION/MARRIAGE codes are remapped, never passed through."""
    rec = dict(VALID_CUSTOMER, EDUCATION=5, MARRIAGE=0)
    vec = row_to_vector(rec)
    assert vec[FEATURE_COLUMNS.index("EDUCATION")] == 4
    assert vec[FEATURE_COLUMNS.index("MARRIAGE")] == 3


@pytest.mark.skipif(not RAW_XLS.exists(),
                    reason="requires downloaded data/default_of_credit_card_clients.xls")
def test_integration_full_data_consistency():
    """Same consistency check against the real xls (integration, needs download)."""
    raw = pd.read_excel(RAW_XLS, header=1)
    cleaned = clean_frame(raw)
    train_row = cleaned.iloc[0]
    train_vector = [train_row[c] for c in FEATURE_COLUMNS]
    raw_record = {c: raw.iloc[0][c] for c in FEATURE_COLUMNS}
    assert row_to_vector(raw_record) == train_vector


def test_predict_returns_probability_and_decision():
    r = client.post("/predict", json=VALID_CUSTOMER)
    assert r.status_code == 200
    body = r.json()
    assert 0.0 <= body["default_probability"] <= 1.0
    assert body["decision"] in (0, 1)
    assert body["threshold"] == THRESHOLD


def test_predict_logs_decision_to_temp_db():
    db = Path(os.environ["DECISIONS_DB"])
    assert not db.exists()
    r = client.post("/predict", json=VALID_CUSTOMER)
    assert r.status_code == 200
    with sqlite3.connect(db) as con:
        rows = con.execute(
            "SELECT probability, threshold, decision FROM decisions").fetchall()
    assert len(rows) == 1
    assert rows[0][1] == THRESHOLD
    assert rows[0][2] == r.json()["decision"]


def test_predict_rejects_missing_features():
    bad = dict(VALID_CUSTOMER)
    del bad["PAY_0"]
    r = client.post("/predict", json=bad)
    assert r.status_code == 422


def test_predict_rejects_invalid_values():
    bad = dict(VALID_CUSTOMER, SEX=5)  # out of range
    r = client.post("/predict", json=bad)
    assert r.status_code == 422
    bad2 = dict(VALID_CUSTOMER, AGE="thirty")  # wrong type
    r2 = client.post("/predict", json=bad2)
    assert r2.status_code == 422


def test_artifacts_load_and_predict():
    model = joblib.load(ROOT / "models" / "model.joblib")
    vec = row_to_vector(VALID_CUSTOMER)
    proba = model.predict_proba([vec])[0]
    assert len(proba) == 2
    assert 0.0 <= proba[1] <= 1.0


def test_preprocessing_module_is_package_importable():
    """src must behave as a package: no sys.path hacks needed by consumers."""
    assert preprocessing.__name__ == "src.preprocessing"
