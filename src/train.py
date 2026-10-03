"""Reproduce the final model artifacts from the cleaned CSV.

Mirrors the experiment in notebooks/02_modeling.ipynb without depending on it:

1. Stratified 60/20/20 train/validation/test split (random_state=42).
2. Model selection on VALIDATION: each candidate's threshold is tuned on
   validation (grid 0.10-0.90, step 0.05, minimizing
   cost = 5*FN + 1*FP); the candidate with the lowest validation cost wins.
3. Threshold is frozen at the validation-selected value; the frozen model is
   evaluated ONCE on the held-out test set. The test set is never used for
   any selection decision.
4. Save model + metadata to models/.

Cost assumption (illustrative, not a business fact): a missed defaulter
(false negative) costs 5x a false alarm on a good customer (false positive).

Usage (from the project root):
    python -m src.train
"""
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, confusion_matrix, f1_score,
                             precision_score, recall_score, roc_auc_score)
from sklearn.model_selection import train_test_split
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .preprocessing import FEATURE_COLUMNS, TARGET

ROOT = Path(__file__).resolve().parent.parent
THRESHOLD_GRID = [round(float(t), 2) for t in np.arange(0.10, 0.91, 0.05)]
FN_COST, FP_COST = 5, 1  # illustrative assumption, see module docstring


def cost_at(y_true, proba, threshold: float) -> int:
    tn, fp, fn, tp = confusion_matrix(y_true, (proba >= threshold).astype(int)).ravel()
    return FN_COST * fn + FP_COST * fp


def best_threshold(y_true, proba) -> float:
    """Lowest threshold minimizing cost; ties resolve to the lower threshold."""
    return min(THRESHOLD_GRID, key=lambda t: cost_at(y_true, proba, t))


def main() -> None:
    df = pd.read_csv(ROOT / "data" / "credit_default_clean.csv")
    X, y = df[FEATURE_COLUMNS], df[TARGET]

    X_train, X_rest, y_train, y_rest = train_test_split(
        X, y, test_size=0.40, random_state=42, stratify=y)
    X_val, X_test, y_val, y_test = train_test_split(
        X_rest, y_rest, test_size=0.50, random_state=42, stratify=y_rest)
    print(f"train={len(y_train)} val={len(y_val)} test={len(y_test)} "
          f"(default rates {y_train.mean():.3f}/{y_val.mean():.3f}/{y_test.mean():.3f})")

    candidates = {
        "logreg": make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000)),
        "hgb": HistGradientBoostingClassifier(random_state=42),
    }

    # --- model + threshold selection on VALIDATION only ---
    val_results = {}
    for name, model in candidates.items():
        model.fit(X_train, y_train)
        p_val = model.predict_proba(X_val)[:, 1]
        t = best_threshold(y_val, p_val)
        val_results[name] = (model, t, cost_at(y_val, p_val, t))
        print(f"validation: {name:>6} best threshold {t:.2f} (cost {val_results[name][2]:,})")

    selected = min(val_results, key=lambda n: val_results[n][2])
    model, threshold, val_cost = val_results[selected]
    print(f"selected on validation: {selected} @ threshold {threshold:.2f}")

    # --- freeze model + threshold; evaluate ONCE on the held-out test set ---
    proba_test = model.predict_proba(X_test)[:, 1]
    p_test = (proba_test >= threshold).astype(int)

    baseline = DummyClassifier(strategy="most_frequent").fit(X_train, y_train)
    base_acc = accuracy_score(y_test, baseline.predict(X_test))

    test_metrics = {
        "accuracy": round(float(accuracy_score(y_test, p_test)), 4),
        "precision": round(float(precision_score(y_test, p_test)), 4),
        "recall": round(float(recall_score(y_test, p_test)), 4),
        "f1": round(float(f1_score(y_test, p_test)), 4),
        "roc_auc": round(float(roc_auc_score(y_test, proba_test)), 4),
        "baseline_accuracy": round(float(base_acc), 4),
    }

    (ROOT / "models").mkdir(exist_ok=True)
    joblib.dump(model, ROOT / "models" / "model.joblib")
    meta = {
        "model": f"{type(model).__name__}(selected on validation, "
                 f"candidates: {sorted(candidates)})",
        "features": FEATURE_COLUMNS,
        "split": "stratified 60/20/20 train/validation/test, random_state=42",
        "threshold": float(threshold),
        "threshold_selection": (
            f"grid 0.10-0.90 step 0.05, cost = {FN_COST}*FN + {FP_COST}*FP, "
            f"minimized on the VALIDATION set only (selected cost {val_cost:,}); "
            "test set used once for final evaluation"
        ),
        "threshold_cost_assumption": (
            f"cost = {FN_COST}*FN + {FP_COST}*FP "
            "(illustrative assumption: a missed defaulter costs 5x a false alarm "
            "on a good customer); not an observed business fact"
        ),
        "test_metrics_at_frozen_threshold": test_metrics,
        "data": ("UCI default of credit card clients (Taiwan); 30000 rows; "
                 "existing card customers with 6 months of history; no timestamps "
                 "(no out-of-time evaluation)"),
    }
    with open(ROOT / "models" / "metadata.json", "w") as f:
        json.dump(meta, f, indent=2)
    print(f"frozen threshold={threshold:.2f}  test f1={test_metrics['f1']}")
    print("saved models/model.joblib + models/metadata.json")


if __name__ == "__main__":
    main()
