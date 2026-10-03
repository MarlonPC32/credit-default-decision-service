# Credit Default Decision Service

End-to-end binary classification: predict whether an **existing** credit card customer will default next month, served through a small risk-scoring API that logs every scored customer to SQLite.

This is risk scoring for **review of current customers** — not an approval screen for new applicants. `decision = 1` means "flag for review", never "reject".

## Data

- **UCI "Default of Credit Card Clients"** (Taiwan, 30,000 records). Download: `python scripts/download_data.py` (fetches the .xls from the UCI archive).
- Target: `default.payment.next.month` (1 = default). Default rate: **22.12%** (6,636 of 30,000).
- All 23 features describe information available **at decision time** for existing customers: credit limit (`LIMIT_BAL`), demographics (`SEX`, `EDUCATION`, `MARRIAGE`, `AGE`), repayment status for the last 6 months (`PAY_0`, `PAY_2`–`PAY_6`), bill amounts (`BILL_AMT1`–`6`), and payment amounts (`PAY_AMT1`–`6`).
- All outcomes are resolved — no unknown or pending labels.

### Cleaning

- Renamed the target to `default`; dropped the `ID` column.
- `EDUCATION` codes 0, 5, 6 (345 records, 1.2%) are undefined in the data dictionary → mapped to 4 ("others").
- `MARRIAGE` code 0 (54 records, 0.2%) is undefined → mapped to 3 ("others").
- No missing values. All decisions live in `src/preprocessing.py`, the single source of truth shared by the notebooks, training script, API, and tests.

## Pipeline

1. `notebooks/01_eda.ipynb` — target distribution, segment default rates (with denominators), feature distributions, correlations; exports `data/credit_default_clean.csv`.
2. `notebooks/02_modeling.ipynb` — stratified 60/20/20 train/validation/test split (`random_state=42`); model and threshold selected on **validation** only, then frozen and evaluated **once** on the held-out test set. Includes a calibration evaluation with per-bin sample sizes.
3. `src/train.py` — reproduces the final artifacts from the cleaned CSV (the API never depends on the notebooks).

## Results

Model comparison on the validation set (0.5 threshold):

| Model | Accuracy | ROC-AUC |
|---|---|---|
| Logistic Regression (scaled pipeline) | 0.8028 | 0.7030 |
| HistGradientBoosting | 0.8135 | 0.7719 |

HistGradientBoosting wins and is the final model. Frozen model + frozen threshold, evaluated once on the test set (n = 6,000):

| Metric | Value |
|---|---|
| Accuracy | 0.6328 |
| Precision | 0.3532 |
| Recall | 0.7943 |
| F1 | 0.4890 |
| ROC-AUC | 0.7858 |
| Majority-class baseline accuracy | 0.7788 |

Note: accuracy at the operating point (0.63) is *below* the majority baseline (0.78). That is the direct consequence of the cost assumption below — the 5:1 ratio prioritizes catching defaulters (recall 0.79) over avoiding false alarms (precision 0.35). The model still separates the classes (ROC-AUC 0.79); the threshold is optimal for the stated cost, not for accuracy.

### Calibration evaluation

Reliability curve on the validation set with per-bin sample sizes (see notebook for the full table). HistGradientBoosting tracks the diagonal reasonably through the populated bins (Brier 0.1381); the top bins are thinly populated (e.g. 55 customers in [0.8, 0.9), 0 in [0.9, 1.0)), so high-probability estimates rest on little data. No post-hoc calibration was applied — and reliability outside this sample is not established.

### Threshold

The 0.5 default is arbitrary. **Illustrative cost assumption** (a modeling choice, not a business fact): a missed defaulter (false negative) costs ~5x a false alarm on a good customer (false positive), i.e. `cost = 5*FN + 1*FP`. Minimized over a 0.10–0.90 grid (0.05 steps) on the **validation** set:

| Threshold | LogReg cost | HGB cost |
|---|---|---|
| 0.10 | 4,433 | 3,596 |
| 0.15 | 4,304 | **3,426** |
| 0.20 | 4,049 | 3,505 |
| 0.25 | 3,909 | 3,638 |
| 0.30 | 4,213 | 3,837 |
| 0.50 | 5,299 | 4,675 |
| 0.70 | 6,346 | 5,575 |
| 0.90 | 6,615 | 6,635 |

(Full 17-point table in the notebook.) The cost curve is U-shaped with an interior minimum: HGB at **0.15** (cost 3,426), beating LogReg's best (0.25, cost 3,909). **Operating threshold: 0.15**, frozen before the single test evaluation. A different cost ratio would justify a different threshold; the selection method is the point. The 0.05 grid is coarse — a finer search could shift the optimum slightly.

### What the data shows (associations, not causes)

Repayment status dominates: customers 2+ months behind on the most recent bill default at 69–76% (vs. 13–17% for those current or revolving). Lower credit limits and younger age are weakly associated with default. Past repayment behavior is the most informative signal available at decision time — expected, not a discovery.

## The API

```bash
pip install -r requirements.txt
python -m src.train                # build models/model.joblib + models/metadata.json
uvicorn src.app:app --reload       # serves on http://127.0.0.1:8000
```

`POST /predict` accepts the 23 features (validated with pydantic; missing or out-of-range fields return 422) and returns:

```json
{"default_probability": 0.3568, "threshold": 0.15, "decision": 1}
```

`decision = 1` means predicted default → **flag for review**. Every call is logged to `data/decisions.db` (timestamp, SHA-256 of the input, probability, threshold, decision; override the location with the `DECISIONS_DB` environment variable). `GET /health` reports model and threshold.

## Tests

```bash
python -m pytest tests/ -q
```

Unit tests require **no download**. 9 tests:

- train/serve preprocessing consistency on a synthetic fixture (raw record → identical feature vector both paths; undefined codes remapped)
- integration test against the real `.xls` — **skipped** automatically when the file is absent (run `python scripts/download_data.py` to enable it)
- `/predict` returns a valid probability and decision
- `/predict` logs the decision to a **temporary** SQLite database (never the real `decisions.db`)
- `/predict` rejects missing features and invalid values with 422
- saved artifacts load and predict
- `src` imports work as a package (no `sys.path` hacks)

## Project structure

```
credit-default/
├── data/                    # raw .xls, cleaned csv (decisions.db excluded via .gitignore)
├── notebooks/
│   ├── 01_eda.ipynb         # executed
│   └── 02_modeling.ipynb    # executed
├── src/
│   ├── preprocessing.py     # shared cleaning + feature vector (single source of truth)
│   ├── train.py             # reproduces model artifacts (run: python -m src.train)
│   └── app.py               # FastAPI risk-scoring service + SQLite logging
├── tests/test_service.py
├── models/                  # model.joblib + metadata.json (built by train.py)
├── scripts/download_data.py
├── requirements.txt
└── README.md
```

## Limitations

- **Existing customers, not applicants**: this scores customers with six months of history. It is not validated as an approval policy for new applicants, and `decision = 1` is a review flag, not a rejection.
- **No timestamps** in the dataset: out-of-time evaluation is not supported. This classifies a snapshot; it is not a validated production risk system.
- **Taiwan, 2005**: a 20-year-old portfolio from one market. Feature distributions and default behavior will differ elsewhere.
- Single split, no cross-validation; threshold grid at 0.05 steps is coarse.
- The 5:1 cost ratio is an explicit illustrative assumption, not an observed business fact.
- Probability reliability outside this sample is not established.
- A real deployment would need monitoring for distribution shift, adverse-action explanations, and fairness review — none of which are in scope here.
