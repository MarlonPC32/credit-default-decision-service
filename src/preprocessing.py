"""Shared preprocessing for the credit-default decision service.

Single source of truth for how a raw applicant record becomes a model input.
Used by: notebooks, src/train.py, src/app.py, tests/test_service.py.

Cleaning decisions (documented):
- EDUCATION codes 0, 5, 6 (345 records, 1.2%) are undefined in the data
  dictionary -> mapped to 4 ("others"). Known: 1 graduate school,
  2 university, 3 high school, 4 others.
- MARRIAGE code 0 (54 records, 0.2%) is undefined -> mapped to 3 ("others").
  Known: 1 married, 2 single, 3 others.
- SEX 1 = male, 2 = female; kept as-is.
- PAY_0..PAY_6 kept numeric: -2 no consumption, -1 paid duly, 0 revolving
  credit in use, 1..8 months of payment delay. (The dataset has no PAY_1
  column; PAY_0 is the most recent month. Kept as published.)
- No missing values in the source file.
"""
from __future__ import annotations

FEATURE_COLUMNS = [
    "LIMIT_BAL", "SEX", "EDUCATION", "MARRIAGE", "AGE",
    "PAY_0", "PAY_2", "PAY_3", "PAY_4", "PAY_5", "PAY_6",
    "BILL_AMT1", "BILL_AMT2", "BILL_AMT3", "BILL_AMT4", "BILL_AMT5", "BILL_AMT6",
    "PAY_AMT1", "PAY_AMT2", "PAY_AMT3", "PAY_AMT4", "PAY_AMT5", "PAY_AMT6",
]
TARGET = "default"

_EDUCATION_MAP = {0: 4, 5: 4, 6: 4}
_MARRIAGE_MAP = {0: 3}


def remap_codes(record: dict) -> dict:
    """Apply code remapping to a single raw record (dict of feature -> value)."""
    rec = dict(record)
    if rec.get("EDUCATION") in _EDUCATION_MAP:
        rec["EDUCATION"] = _EDUCATION_MAP[rec["EDUCATION"]]
    if rec.get("MARRIAGE") in _MARRIAGE_MAP:
        rec["MARRIAGE"] = _MARRIAGE_MAP[rec["MARRIAGE"]]
    return rec


def row_to_vector(record: dict) -> list:
    """Raw applicant record -> model input vector in FEATURE_COLUMNS order."""
    rec = remap_codes(record)
    return [rec[col] for col in FEATURE_COLUMNS]


def clean_frame(df):
    """Raw xls DataFrame -> cleaned DataFrame with `default` target column."""
    df = df.copy()
    df = df.rename(columns={"default payment next month": TARGET})
    df = df.drop(columns=["ID"])
    df["EDUCATION"] = df["EDUCATION"].replace(_EDUCATION_MAP)
    df["MARRIAGE"] = df["MARRIAGE"].replace(_MARRIAGE_MAP)
    return df
