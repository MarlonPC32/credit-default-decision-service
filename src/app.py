"""Credit-default decision service.

POST /predict  -> default probability + decision at the configured threshold.
GET  /health   -> service + artifact status.

Every decision is logged to SQLite (data/decisions.db): timestamp, SHA-256 of
the canonical input, probability, threshold, decision.
"""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import joblib
import pandas as pd
from fastapi import FastAPI
from pydantic import BaseModel, Field

from preprocessing import FEATURE_COLUMNS, row_to_vector

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "decisions.db"

app = FastAPI(title="Credit Default Decision Service")

_model = joblib.load(ROOT / "models" / "model.joblib")
_metadata = json.loads((ROOT / "models" / "metadata.json").read_text())
THRESHOLD: float = _metadata["threshold"]


class CreditApplication(BaseModel):
    LIMIT_BAL: int = Field(ge=0, le=10_000_000)
    SEX: int = Field(ge=1, le=2)
    EDUCATION: int = Field(ge=0, le=6)
    MARRIAGE: int = Field(ge=0, le=3)
    AGE: int = Field(ge=18, le=100)
    PAY_0: int = Field(ge=-2, le=8)
    PAY_2: int = Field(ge=-2, le=8)
    PAY_3: int = Field(ge=-2, le=8)
    PAY_4: int = Field(ge=-2, le=8)
    PAY_5: int = Field(ge=-2, le=8)
    PAY_6: int = Field(ge=-2, le=8)
    BILL_AMT1: int = Field(ge=-10_000_000, le=10_000_000)
    BILL_AMT2: int = Field(ge=-10_000_000, le=10_000_000)
    BILL_AMT3: int = Field(ge=-10_000_000, le=10_000_000)
    BILL_AMT4: int = Field(ge=-10_000_000, le=10_000_000)
    BILL_AMT5: int = Field(ge=-10_000_000, le=10_000_000)
    BILL_AMT6: int = Field(ge=-10_000_000, le=10_000_000)
    PAY_AMT1: int = Field(ge=0, le=10_000_000)
    PAY_AMT2: int = Field(ge=0, le=10_000_000)
    PAY_AMT3: int = Field(ge=0, le=10_000_000)
    PAY_AMT4: int = Field(ge=0, le=10_000_000)
    PAY_AMT5: int = Field(ge=0, le=10_000_000)
    PAY_AMT6: int = Field(ge=0, le=10_000_000)


def _init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS decisions (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   ts TEXT NOT NULL,
                   input_hash TEXT NOT NULL,
                   probability REAL NOT NULL,
                   threshold REAL NOT NULL,
                   decision INTEGER NOT NULL
               )"""
        )


@app.on_event("startup")
def _startup() -> None:
    _init_db()


def _log_decision(input_hash: str, probability: float, decision: int) -> None:
    _init_db()
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO decisions (ts, input_hash, probability, threshold, decision)"
            " VALUES (?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), input_hash,
             probability, THRESHOLD, decision),
        )


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model": _metadata["model"], "threshold": THRESHOLD}


@app.post("/predict")
def predict(application: CreditApplication) -> dict:
    raw = application.model_dump()
    vector = row_to_vector(raw)  # same preprocessing as training
    X = pd.DataFrame([vector], columns=FEATURE_COLUMNS)
    probability = float(_model.predict_proba(X)[0, 1])
    decision = int(probability >= THRESHOLD)
    input_hash = hashlib.sha256(
        json.dumps(raw, sort_keys=True).encode()).hexdigest()
    _log_decision(input_hash, probability, decision)
    return {
        "default_probability": round(probability, 4),
        "threshold": THRESHOLD,
        "decision": decision,  # 1 = predicted default (reject / review), 0 = predicted no default
    }
