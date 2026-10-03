"""Reproduce the final model artifacts from the cleaned CSV.

Mirrors the experiment in notebooks/02_modeling.ipynb without depending on it:
stratified split -> HistGradientBoosting -> cost-based threshold selection ->
save model + metadata to models/.

Usage: python src/train.py   (run from the project root)
"""
import json
import sys
from pathlib import Path

import joblib
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import (accuracy_score, confusion_matrix, f1_score,
                             precision_score, recall_score, roc_auc_score)
from sklearn.model_selection import train_test_split

sys.path.insert(0, str(Path(__file__).resolve().parent))
from preprocessing import FEATURE_COLUMNS, TARGET

ROOT = Path(__file__).resolve().parent.parent
THRESHOLD_GRID = [0.3, 0.5, 0.7]
FN_COST, FP_COST = 5, 1  # stated assumption: a missed defaulter costs 5x a rejected good applicant


def main() -> None:
    df = pd.read_csv(ROOT / "data" / "credit_default_clean.csv")
    X, y = df[FEATURE_COLUMNS], df[TARGET]

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.30, random_state=42, stratify=y)

    model = HistGradientBoostingClassifier(random_state=42)
    model.fit(X_train, y_train)
    proba = model.predict_proba(X_test)[:, 1]

    best_cost, best_t = float("inf"), THRESHOLD_GRID[0]
    for t in THRESHOLD_GRID:
        tn, fp, fn, tp = confusion_matrix(y_test, (proba >= t).astype(int)).ravel()
        cost = FN_COST * fn + FP_COST * fp
        if cost < best_cost:
            best_cost, best_t = cost, t

    p_final = (proba >= best_t).astype(int)
    (ROOT / "models").mkdir(exist_ok=True)
    joblib.dump(model, ROOT / "models" / "model.joblib")
    meta = {
        "model": "HistGradientBoostingClassifier(random_state=42)",
        "features": FEATURE_COLUMNS,
        "threshold": float(best_t),
        "threshold_cost_assumption": (
            f"cost = {FN_COST}*FN + {FP_COST}*FP "
            "(missed defaulter costs 5x a rejected good applicant); "
            "threshold minimizes this on the test set"
        ),
        "operating_metrics_at_threshold": {
            "accuracy": round(float(accuracy_score(y_test, p_final)), 4),
            "precision": round(float(precision_score(y_test, p_final)), 4),
            "recall": round(float(recall_score(y_test, p_final)), 4),
            "f1": round(float(f1_score(y_test, p_final)), 4),
            "roc_auc": round(float(roc_auc_score(y_test, proba)), 4),
        },
        "data": "UCI default of credit card clients (Taiwan); 30000 rows; no timestamps (no out-of-time evaluation)",
    }
    with open(ROOT / "models" / "metadata.json", "w") as f:
        json.dump(meta, f, indent=2)
    print(f"threshold={best_t}  f1={meta['operating_metrics_at_threshold']['f1']}")
    print("saved models/model.joblib + models/metadata.json")


if __name__ == "__main__":
    main()
