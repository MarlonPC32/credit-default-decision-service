# Rust gateway

The Rust gateway listens on `127.0.0.1:8080` and forwards `GET /health` and
`POST /predict` to FastAPI. FastAPI owns validation, scoring, and SQLite audit
logging. Rust forwards prediction request bytes and their content type, and
relays FastAPI's HTTP 200 and 422 prediction responses with their original bodies.

Prediction bodies larger than 64 KiB receive HTTP 413 before FastAPI is called.
The gateway returns HTTP 502 for connection failures or unexpected upstream
statuses, and HTTP 504 when the upstream response exceeds the five-second deadline.
The HTTP client disables retries and redirects. FastAPI may already have logged
a decision by the time a client receives a timeout.

## Run locally

From the repository root, start FastAPI with the project's Python environment active:

```sh
python -m uvicorn src.app:app --host 127.0.0.1 --port 8000 --no-access-log
```

The Python environment needs the dependencies in `requirements.txt`. If using the
environment prepared by Codex onboarding, activate it with
`source /workspace/.venvs/credit-default-decision-service/bin/activate`.

In a second terminal, from the repository root, use Rust 1.91.1 or newer:

```sh
cd rust-runtime
FASTAPI_BASE_URL=http://127.0.0.1:8000 cargo run --locked
```

In the current Codex cloud environment, enable the workspace toolchain before
running Cargo:

```sh
export CARGO_HOME=/workspace/.cache/credit-default-cargo
export RUSTUP_HOME=/workspace/.cache/credit-default-rustup
export PATH="$CARGO_HOME/bin:$PATH"
```

From a third terminal, run `curl -i http://127.0.0.1:8080/health`. Expect HTTP 200
with FastAPI's health JSON. Stop FastAPI and repeat the request to see HTTP 502.

With both services running, send an empty prediction to see FastAPI's HTTP 422
response and field-level validation details:

```sh
curl -i -H 'Content-Type: application/json' --data '{}' http://127.0.0.1:8080/predict
```

Run the gateway tests with `cargo test --locked` from `rust-runtime/`.
Run the Python suite with `python -m pytest tests/ -q` from the repository root.
