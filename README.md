# Credit Default Decision Service

End-to-end binary classification: predict whether a credit card client will default next month, served through a small decision API that logs every prediction to SQLite.

## Data

- **UCI "Default of Credit Card Clients"** (Taiwan, 30,000 records). Download: `python scripts/download_data.py` (fetches the .xls from the UCI archive).
- Target: `default.payment.next.month` (1 = default). Default rate: **22.12%** (6,636 of 30,000).
- All 23 features describe information available **at decision time**: credit limit (`LIMIT_BAL`), demographics (`SEX`, `EDUCATION`, `MARRIAGE`, `AGE`), repayment status for the last 6 months (`PAY_0`, `PAY_2`–`PAY_6`), bill amounts (`BILL_AMT1`–`6`), and payment amounts (`PAY_AMT1`–`6`).
- All outcomes are resolved — no unknown or pending labels.

### Cleaning

- Renamed the target to `default`; dropped the `ID` column.
- `EDUCATION` codes 0, 5, 6 (345 records, 1.2%) are undefined in the data dictionary → mapped to 4 ("others").
- `MARRIAGE` code 0 (54 records, 0.2%) is undefined → mapped to 3 ("others").
- No missing values. All decisions live in `src/preprocessing.py`, the single source of truth shared by the notebooks, training script, API, and tests.

## Pipeline

1. `notebooks/01_eda.ipynb` — target distribution, segment default rates (with denominators), feature distributions, correlations; exports `data/credit_default_clean.csv`.
2. `notebooks/02_modeling.ipynb` — stratified 70/30 split (`random_state=42`), majority-class baseline, Logistic Regression (scaled pipeline, convergence verified) vs. HistGradientBoosting, calibration check, threshold analysis.
3. `src/train.py` — reproduces the final artifacts from the cleaned CSV (the API never depends on the notebooks).

## Results

Test set (n = 9,000), metrics for the default class at the 0.5 default threshold:

| Model | Accuracy | Precision | Recall | F1 | ROC-AUC |
|---|---|---|---|---|---|
| Majority-class baseline | 0.7788 | — | — | — | — |
| Logistic Regression | 0.8079 | 0.6932 | 0.2361 | 0.3522 | 0.7150 |
| HistGradientBoosting | 0.8178 | 0.6620 | 0.3601 | 0.4665 | 0.7768 |

HistGradientBoosting wins on every metric except precision at 0.5 and is the final model. The reliability curve tracks the diagonal (Brier 0.136), so predicted probabilities are usable as-is — no post-hoc calibration applied.

### Threshold

The 0.5 default is arbitrary. **Stated cost assumption** (a modeling choice, not a business fact): a missed defaulter (false negative) costs ~5x a rejected good applicant (false positive), i.e. `cost = 5*FN + 1*FP`. Minimizing this on the test set:

| Threshold | Precision | Recall | F1 | Cost (5:1) |
|---|---|---|---|---|
| 0.3 | 0.5459 | 0.5254 | 0.5354 | 5,595 |
| 0.5 | 0.6620 | 0.3601 | 0.4665 | 6,736 |
| 0.7 | 0.7429 | 0.1567 | 0.2588 | 8,503 |

**Operating threshold: 0.3** → accuracy 0.7983, precision 0.5459, recall 0.5254, F1 0.5354. A different cost ratio would justify a different threshold; the selection method is the point.

### What the data shows (associations, not causes)

Repayment status dominates: clients 2+ months behind on the most recent bill default at 69–76% (vs. 13–17% for those current or revolving). Lower credit limits and younger age are weakly associated with default. Past repayment behavior is the most informative signal available at decision time — expected, not a discovery.

## The API

```bash
pip install -r requirements.txt
python src/train.py                 # build models/model.joblib + models/metadata.json
uvicorn src.app:app --reload        # serves on http://127.0.0.1:8000
```

`POST /predict` accepts the 23 features (validated with pydantic; missing or out-of-range fields return 422) and returns:

```json
{"default_probability": 0.3568, "threshold": 0.3, "decision": 1}
```

`decision = 1` means predicted default (reject / manual review). Every call is logged to `data/decisions.db` (timestamp, SHA-256 of the input, probability, threshold, decision). `GET /health` reports model and threshold.

## Tests

```bash
python -m pytest tests/ -q
```

6 tests, all passing:
- train/serve preprocessing consistency (raw record → identical feature vector both paths)
- undefined category codes are remapped, never passed through
- `/predict` returns a valid probability and decision
- `/predict` rejects missing features and invalid values with 422
- saved artifacts load and predict

## Project structure

```
credit-default/
├── data/                    # raw .xls, cleaned csv (decisions.db excluded via .gitignore)
├── notebooks/
│   ├── 01_eda.ipynb         # executed
│   └── 02_modeling.ipynb    # executed
├── src/
│   ├── preprocessing.py     # shared cleaning + feature vector (single source of truth)
│   ├── train.py             # reproduces model artifacts
│   └── app.py               # FastAPI decision service + SQLite logging
├── tests/test_service.py
├── models/                  # model.joblib + metadata.json (built by train.py)
├── scripts/download_data.py
├── requirements.txt
└── README.md
```

## Limitations

- **No timestamps** in the dataset: out-of-time evaluation is not supported. This classifies a snapshot; it is not a validated production risk system.
- **Taiwan, 2005**: a 20-year-old portfolio from one market. Feature distributions and default behavior will differ elsewhere.
- Single 70/30 split, no cross-validation.
- The 5:1 cost ratio is an explicit assumption for threshold selection, not an observed business fact.
- A real deployment would need monitoring for distribution shift, adverse-action explanations, and fairness review — none of which are in scope here.
