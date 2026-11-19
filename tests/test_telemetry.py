import io
import json
import logging

from prometheus_client.parser import text_string_to_metric_families
from fastapi.testclient import TestClient

from src.app import app
from src.telemetry import LOGGER


def read_samples(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    return {
        (sample.name, frozenset(sample.labels.items())): sample.value
        for family in text_string_to_metric_families(response.text)
        for sample in family.samples
    }


def test_validation_metrics_and_logs_exclude_customer_data(monkeypatch, tmp_path):
    db = tmp_path / "decisions.db"
    monkeypatch.setenv("DECISIONS_DB", str(db))
    client = TestClient(app)
    before = read_samples(client)
    stream = io.StringIO()
    recorder = logging.StreamHandler(stream)
    LOGGER.addHandler(recorder)
    try:
        invalid = client.post("/predict?token=private-query", json={"LIMIT_BAL": "private-amount"})
        assert invalid.status_code == 422
        assert client.get("/private-path").status_code == 404
    finally:
        LOGGER.removeHandler(recorder)

    after = read_samples(client)
    expected = (
        ("credit_http_requests_total", {"method": "POST", "route": "/predict", "status": "422"}),
        ("credit_http_request_duration_seconds_count", {"method": "POST", "route": "/predict"}),
        ("credit_http_requests_total", {"method": "GET", "route": "other", "status": "404"}),
    )
    for name, labels in expected:
        key = (name, frozenset(labels.items()))
        assert after[key] == before.get(key, 0) + 1
    assert read_samples(client) == after
    assert "private-" not in stream.getvalue()
    logs = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [(entry["route"], entry["status"]) for entry in logs] == [("/predict", 422), ("other", 404)]
    assert all(entry["service"] == "fastapi" and entry["duration_seconds"] >= 0 for entry in logs)
    assert not db.exists()
