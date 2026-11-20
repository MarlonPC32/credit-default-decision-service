"""Check the optional Compose stack after it reports healthy."""

import json
import subprocess
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

opener = build_opener(ProxyHandler({}))


def request(url, data=None):
    http_request = Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with opener.open(http_request, timeout=5) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        with error:
            return error.code, json.load(error)


def query(expression, base="http://127.0.0.1:9090"):
    params = urlencode({"query": expression})
    status, body = request(f"{base}/api/v1/query?{params}")
    assert status == 200 and body["status"] == "success", body
    return {
        sample["metric"]["job"]: float(sample["value"][1])
        for sample in body["data"]["result"]
    }


def main():
    status, dashboard = request("http://127.0.0.1:3000/api/dashboards/uid/credit-service")
    assert status == 200 and len(dashboard["dashboard"]["panels"]) == 5, dashboard

    marker = "synthetic-private-marker"
    path = f"http://127.0.0.1:8080/predict?token={marker}"
    status, _ = request(path, json.dumps({"LIMIT_BAL": marker}).encode())
    assert status == 422, status

    jobs = {"fastapi", "gateway"}
    for _ in range(15):
        up = query('up{job=~"fastapi|gateway"}')
        counts = query('credit_http_requests_total{route="/predict",method="POST",status="422"}')
        latency = query('credit_http_request_duration_seconds_count{route="/predict",method="POST"}')
        if up == dict.fromkeys(jobs, 1) and counts == latency == dict.fromkeys(jobs, 1):
            break
        time.sleep(2)
    else:
        raise AssertionError(f"Expected one scraped request per service: {up=}, {counts=}, {latency=}")
    grafana_proxy = "http://127.0.0.1:3000/api/datasources/proxy/uid/credit-prometheus"
    assert query('up{job=~"fastapi|gateway"}', base=grafana_proxy) == dict.fromkeys(jobs, 1)

    compose = ["docker", "compose", "-f", "compose.yaml", "-f", "compose.observability.yaml"]
    for service in jobs:
        output = subprocess.check_output(
            [*compose, "logs", "--no-color", "--no-log-prefix", service], text=True,
        )
        assert marker not in output, f"{service} logged private request data"
        logs = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
        fields = [log.get("fields", log) for log in logs]
        assert any(
            entry.get("event") == "http_request" and entry.get("route") == "/predict"
            and entry.get("status") == 422 and entry.get("duration_seconds", -1) >= 0
            for entry in fields
        ), f"{service} is missing the structured HTTP log"
    print("Grafana provisioned the dashboard. Prometheus scraped both services and their request counts and latency.")
    print("Both services recorded the 422 in structured logs without logging the synthetic private marker.")


if __name__ == "__main__":
    main()
