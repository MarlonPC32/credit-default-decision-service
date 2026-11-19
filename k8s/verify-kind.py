"""Exercise the deployed gateway and SQLite audit log using only the standard library."""

import hashlib
import json
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


# Synthetic customer used in the application tests and local runbook.
CUSTOMER = {
    "LIMIT_BAL": 20000, "SEX": 2, "EDUCATION": 2, "MARRIAGE": 1, "AGE": 35,
    "PAY_0": 0, "PAY_2": -1, "PAY_3": -1, "PAY_4": 0, "PAY_5": 0, "PAY_6": -1,
    "BILL_AMT1": 3913, "BILL_AMT2": 3102, "BILL_AMT3": 689, "BILL_AMT4": 0,
    "BILL_AMT5": 0, "BILL_AMT6": 0, "PAY_AMT1": 0, "PAY_AMT2": 689,
    "PAY_AMT3": 0, "PAY_AMT4": 0, "PAY_AMT5": 0, "PAY_AMT6": 0,
}
EXPECTED_PREDICTION = {"default_probability": 0.3174, "threshold": 0.15, "decision": 1}
AUDIT_QUERY = """
import json
import os
import sqlite3
from pathlib import Path

db = Path(os.environ["DECISIONS_DB"])
assert os.getuid() == 10001, os.getuid()
assert os.access(db.parent, os.W_OK) and os.access(db, os.W_OK), db
with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as connection:
    rows = connection.execute(
        "SELECT id, ts, input_hash, probability, threshold, decision FROM decisions ORDER BY id"
    ).fetchall()
print(json.dumps(rows))
"""


def main(context):
    kubectl = ["kubectl", "--context", context, "-n", "credit-default"]

    def kube(*args, stdin=None):
        return subprocess.check_output([*kubectl, *args], text=True, input=stdin).strip()

    def audit_rows():
        return json.loads(kube("exec", "-i", "deployment/fastapi", "--", "python", "-", stdin=AUDIT_QUERY))

    phase = kube("get", "pvc", "decisions-data", "-o", "jsonpath={.status.phase}")
    assert phase == "Bound", phase
    assert kube("exec", "deployment/gateway", "--", "id", "-u") == "10001"
    for service, caller in (("fastapi", "gateway"), ("gateway", "fastapi")):
        spec = json.loads(kube("get", "service", service, "-o", "json"))["spec"]
        assert spec["type"] == "ClusterIP", spec
        dns = kube("exec", f"deployment/{caller}", "--", "getent", "ahostsv4", service)
        assert spec["clusterIP"] in {line.split()[0] for line in dns.splitlines()}, dns
    assert audit_rows() == [], "The fresh, writable PVC must have an empty audit log"
    print("Both ClusterIP Services resolve from peer Pods. The PVC is Bound and writable as UID 10001.")

    with tempfile.TemporaryFile(mode="w+t") as log:
        forward = subprocess.Popen(
            [*kubectl, "port-forward", "--address", "127.0.0.1", "service/gateway", ":8080"],
            stdout=log, stderr=subprocess.STDOUT, text=True,
        )
        try:
            for _ in range(100):
                log.seek(0)
                lines = log.read().splitlines()
                for line in lines:
                    if line.startswith("Forwarding from 127.0.0.1:"):
                        port = line.split()[2].rsplit(":", 1)[1]
                        break
                else:
                    if forward.poll() is not None:
                        raise RuntimeError("kubectl port-forward exited")
                    time.sleep(0.2)
                    continue
                break
            else:
                raise RuntimeError("Timed out waiting for kubectl port-forward")

            opener = build_opener(ProxyHandler({}))

            def call(path, payload=None):
                data = json.dumps(payload).encode() if payload is not None else None
                request = Request(
                    f"http://127.0.0.1:{port}{path}", data=data,
                    headers={"Content-Type": "application/json"} if data is not None else {},
                )
                try:
                    with opener.open(request, timeout=10) as response:
                        return response.status, json.load(response)
                except HTTPError as error:
                    with error:
                        return error.code, json.load(error)
                except URLError as error:
                    raise RuntimeError(f"Gateway request failed for {path}: {error}") from error

            status, health = call("/health")
            assert status == 200 and health["status"] == "ok" and health["threshold"] == 0.15, health
            status, invalid = call("/predict", {})
            assert status == 422 and len(invalid["detail"]) == 23, invalid
            assert audit_rows() == [], "Validation failure wrote an audit row"
            status, prediction = call("/predict", CUSTOMER)
            assert status == 200 and prediction == EXPECTED_PREDICTION, prediction
            rows = audit_rows()
            assert len(rows) == 1, rows
            _, _, input_hash, probability, threshold, decision = rows[0]
            assert input_hash == hashlib.sha256(json.dumps(CUSTOMER, sort_keys=True).encode()).hexdigest()
            assert (round(probability, 4), threshold, decision) == (0.3174, 0.15, 1)
            print("Gateway returned 200 for health and prediction, and 422 for invalid input.")
            print("Invalid input wrote no rows. One valid prediction wrote exactly one matching audit row.")
        finally:
            if forward.poll() is None:
                forward.terminate()
                forward.wait(timeout=10)
            log.seek(0)
            print(log.read(), end="")

    old_uid = kube("get", "pod", "-l", "app=fastapi", "-o", "jsonpath={.items[0].metadata.uid}")
    kube("rollout", "restart", "deployment/fastapi")
    print(kube("rollout", "status", "deployment/fastapi", "--timeout=180s"))
    new_uid = kube("get", "pod", "-l", "app=fastapi", "-o", "jsonpath={.items[0].metadata.uid}")
    assert old_uid and new_uid and old_uid != new_uid, (old_uid, new_uid)
    assert audit_rows() == rows, "Audit row changed or disappeared after FastAPI Pod replacement"
    print("A new FastAPI Pod read the same audit row from the PVC.")


if __name__ == "__main__":
    main(sys.argv[1])
