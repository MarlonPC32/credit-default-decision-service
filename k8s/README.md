# Local Kubernetes with Minikube

Use the [PowerShell instructions](#windows-docker-desktop-and-powershell) on
Windows. The first sections below use a POSIX shell.

## Verify Kubernetes in GitHub Actions

The **Kubernetes integration** workflow runs on pushes to
`feature/rust-runtime-gateway`. Open its run in the repository's Actions tab
to verify Kubernetes without installing any tools locally. It builds the
existing images, loads them into a temporary single-node kind cluster on an
Ubuntu runner, and applies these same manifests without an overlay. It checks
Pod readiness, Service DNS, PVC access, gateway HTTP responses, and the same
SQLite audit row after a FastAPI Pod replacement.

On failure, open the job output and the `kind-diagnostics` artifact for Pod
logs, events, and cluster diagnostics. The job deletes its cluster on success
or failure. Its SQLite data lives only inside that disposable cluster.

Run the commands from the repository root. Use Docker, Minikube, and
`kubectl` against a dedicated profile named `credit-default`. The commands below
use Kubernetes 1.34.0. On rootless Docker, Minikube's Docker driver also needs
cgroup v2 delegation. Check the
[driver requirements](https://minikube.sigs.k8s.io/docs/drivers/docker/) if
bootstrap fails.

In the managed Codex cloud, Minikube 1.37.0 reached node creation but Docker
rejected the node's `/lib/modules` bind mount with
`error while creating mount source path '/lib/modules': mkdir /lib/modules: permission denied`.
Use a machine with a supported Minikube Docker driver to run the deployment
and persistence checks. For a read-only home directory, point `MINIKUBE_HOME`
and `KUBECONFIG` at writable locations before running Minikube.

## Validate, start, and deploy

Validate all six resources offline with kubeconform before deploying:

```sh
kubeconform -strict -summary -kubernetes-version 1.34.0 \
  k8s/namespace.yaml k8s/fastapi.yaml k8s/gateway.yaml
```

Start the dedicated cluster and check for a default StorageClass:

```sh
minikube start -p credit-default --driver=docker --container-runtime=containerd \
  --kubernetes-version=v1.34.0
kubectl --context credit-default wait --for=condition=Ready node --all --timeout=120s
kubectl --context credit-default get storageclass
```

Build and load the existing Docker images. If the build network requires a
custom CA, use `--secret id=build_ca,src=/path/to/trusted-ca-bundle.pem`
with each Docker build. The bundle is used only for dependency downloads.

```sh
docker build -t credit-default-fastapi:m4 .
docker build -t credit-default-gateway:m4 ./rust-runtime
minikube -p credit-default image load credit-default-fastapi:m4
minikube -p credit-default image load credit-default-gateway:m4
```

The manifests use `imagePullPolicy: Never` so the node uses those imported tags.
Run strict server-side validation after creating the namespace, then deploy:

```sh
kubectl --context credit-default apply -f k8s/namespace.yaml
kubectl --context credit-default apply --dry-run=server --validate=strict \
  -f k8s/fastapi.yaml -f k8s/gateway.yaml
kubectl --context credit-default apply -f k8s/fastapi.yaml -f k8s/gateway.yaml
kubectl --context credit-default -n credit-default rollout status deploy/fastapi --timeout=180s
kubectl --context credit-default -n credit-default rollout status deploy/gateway --timeout=120s
kubectl --context credit-default -n credit-default get pods,svc,pvc
kubectl --context credit-default -n credit-default exec deploy/fastapi -- \
  sh -c 'test "$(id -u)" = 10001 && test -w "$(dirname "$DECISIONS_DB")" && test -w "$DECISIONS_DB"'
kubectl --context credit-default -n credit-default exec deploy/gateway -- \
  sh -c 'test "$(id -u)" = 10001'
```

The gateway resolves `fastapi` using Kubernetes DNS and calls its internal
Service on port `8000`. Both Services are ClusterIP. The Rust readiness probe
calls through to FastAPI, while its startup and liveness probes check only the
local listener. An upstream outage can make the gateway unready without causing
a gateway restart. FastAPI's startup probe allows time for model and database
initialization.

FastAPI mounts a `ReadWriteOnce` claim at `/var/lib/credit-default`. The
workload uses UID/GID `10001` and requests filesystem group `10001`. Actual
volume permissions depend on the StorageClass and provisioner, so verify them
with the command above. The single replica and `Recreate` strategy avoid
overlapping application writers on rollout. `ReadWriteOnce` limits mounting
to one node, and SQLite serializes writes. The local volume survives Pod
replacement but does not provide cross-node failover.

## Verify HTTP and persistence

In a dedicated terminal, forward only the gateway to the local machine:

```sh
kubectl --context credit-default -n credit-default port-forward \
  --address 127.0.0.1 svc/gateway 8080:8080
```

In another terminal, activate the project's Python environment. Run the
following from the repository root against a fresh test PVC, which starts with
zero rows:

```sh
set -e
audit_count() {
  kubectl --context credit-default -n credit-default exec deploy/fastapi -- \
    python -c 'import os, sqlite3; db = sqlite3.connect(os.environ["DECISIONS_DB"]); print(db.execute("SELECT count(*) FROM decisions").fetchone()[0])'
}

test "$(audit_count)" = 0
python - <<'PY'
import httpx

with httpx.Client(base_url="http://127.0.0.1:8080", trust_env=False) as client:
    health = client.get("/health")
    assert health.status_code == 200, health.text
    assert health.json()["threshold"] == 0.15
    invalid = client.post("/predict", json={})
    assert invalid.status_code == 422, invalid.text
    assert len(invalid.json()["detail"]) == 23
PY
test "$(audit_count)" = 0
python - <<'PY'
import httpx
from tests.test_service import VALID_CUSTOMER

with httpx.Client(base_url="http://127.0.0.1:8080", trust_env=False) as client:
    valid = client.post("/predict", json=VALID_CUSTOMER)
    assert valid.status_code == 200, valid.text
    assert valid.json() == {
        "default_probability": 0.3174, "threshold": 0.15, "decision": 1,
    }
PY
test "$(audit_count)" = 1
kubectl --context credit-default -n credit-default exec deploy/fastapi -- \
  python -c 'import os, sqlite3; db = sqlite3.connect(os.environ["DECISIONS_DB"]); (p, t, d), = db.execute("SELECT probability, threshold, decision FROM decisions"); assert (round(p, 4), t, d) == (0.3174, 0.15, 1)'
kubectl --context credit-default -n credit-default rollout restart deploy/fastapi
kubectl --context credit-default -n credit-default rollout status deploy/fastapi --timeout=180s
test "$(audit_count)" = 1
```

Run existing tests separately:

```sh
(cd rust-runtime && cargo fmt --check && cargo test --locked && cargo build --locked)
OMP_NUM_THREADS=2 python -m pytest tests/ -q -rs
```

## Cleanup

Stop the port-forward with Ctrl-C. To stop the dedicated profile and keep its
SQLite data, run `minikube stop -p credit-default`. To remove only the
workloads and Services while keeping the PVC and namespace:

```sh
kubectl --context credit-default -n credit-default delete deployment fastapi gateway
kubectl --context credit-default -n credit-default delete service fastapi gateway
```

For a disposable test cluster, `kubectl --context credit-default delete
namespace credit-default` deletes the claim and may delete its data. To remove
the dedicated cluster and its local data, run `minikube delete -p credit-default`.
Use these destructive commands only after confirming the data is disposable or
backed up.

## Windows (Docker Desktop and PowerShell)

Use Windows 10 or 11 with hardware virtualization enabled and WSL 2 installed.
In an administrator terminal, run `wsl --install` if WSL is missing, then
restart if prompted. In PowerShell, install Docker Desktop, Minikube, and
PowerShell 7 if needed:

```powershell
winget install --exact --id Docker.DockerDesktop
winget install --exact --id Kubernetes.minikube
winget install --exact --id Microsoft.PowerShell
```

Start Docker Desktop, select its WSL 2 engine and Linux container mode, and
wait for Docker to be ready. Open PowerShell 7.4 or later in the repository
root on `feature/rust-runtime-gateway`. The commands use Minikube's matching
`kubectl` through its wrapper. Docker Desktop's separate Kubernetes cluster
is unnecessary.

### 1. Start Minikube and load the images

```powershell
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

docker version
docker info --format '{{.OSType}}' # Expect linux
minikube version
minikube start -p credit-default --driver=docker --container-runtime=containerd --kubernetes-version=v1.34.0
minikube -p credit-default kubectl -- --context credit-default wait --for=condition=Ready node --all --timeout=120s
minikube -p credit-default kubectl -- --context credit-default get storageclass # Expect a default class

docker build -t credit-default-fastapi:m4 .
docker build -t credit-default-gateway:m4 ./rust-runtime
minikube -p credit-default image load credit-default-fastapi:m4
minikube -p credit-default image load credit-default-gateway:m4
minikube -p credit-default image ls
```

### 2. Validate and deploy

```powershell
minikube -p credit-default kubectl -- --context credit-default apply -f k8s/namespace.yaml
minikube -p credit-default kubectl -- --context credit-default apply --dry-run=server --validate=strict -f k8s/fastapi.yaml -f k8s/gateway.yaml
minikube -p credit-default kubectl -- --context credit-default apply -f k8s/fastapi.yaml -f k8s/gateway.yaml
minikube -p credit-default kubectl -- --context credit-default -n credit-default rollout status deploy/fastapi --timeout=180s
minikube -p credit-default kubectl -- --context credit-default -n credit-default rollout status deploy/gateway --timeout=120s
minikube -p credit-default kubectl -- --context credit-default -n credit-default get pods,svc,pvc
```

Expect both Pods to be ready, both Services to report `ClusterIP`, and the
`decisions-data` claim to report `Bound`. If FastAPI fails on startup, check
its logs and the PVC's events before altering permissions. A preexisting PVC
may contain data. Use a fresh test profile or stop the smoke test if the
initial audit count is nonzero.

### 3. Forward the gateway and exercise both endpoints

In a separate PowerShell terminal, leave the following command running:

```powershell
minikube -p credit-default kubectl -- --context credit-default -n credit-default port-forward --address 127.0.0.1 svc/gateway 8080:8080
```

In the original terminal, run the following. It uses PowerShell's HTTP client
and the Python interpreter inside the FastAPI container. It needs no local
Python installation.

```powershell
$auditScript = @'
import json
import os
import sqlite3
from pathlib import Path

database = Path(os.environ["DECISIONS_DB"])
assert os.getuid() == 10001
assert os.access(database.parent, os.W_OK) and os.access(database, os.W_OK)
with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
    rows = connection.execute(
        "SELECT probability, threshold, decision FROM decisions ORDER BY id"
    ).fetchall()
print(json.dumps({"count": len(rows), "rows": rows}))
'@

function Get-Audit {
    $auditScript | minikube -p credit-default kubectl -- --context credit-default -n credit-default exec -i deploy/fastapi -- python - | ConvertFrom-Json
}

$gatewayUid = minikube -p credit-default kubectl -- --context credit-default -n credit-default exec deploy/gateway -- id -u
if ($gatewayUid -ne '10001') { throw 'Gateway UID must be 10001' }
$before = Get-Audit
if ($before.count -ne 0) { throw 'Use an empty test database' }

$base = 'http://127.0.0.1:8080'
$health = Invoke-WebRequest -Uri "$base/health" -NoProxy -SkipHttpErrorCheck
$healthBody = $health.Content | ConvertFrom-Json
if ($health.StatusCode -ne 200 -or $healthBody.status -ne 'ok' -or $healthBody.threshold -ne 0.15) {
    throw 'Expected gateway health HTTP 200 with threshold 0.15'
}
$invalid = Invoke-WebRequest -Method Post -Uri "$base/predict" -ContentType 'application/json' -Body '{}' -NoProxy -SkipHttpErrorCheck
if ($invalid.StatusCode -ne 422 -or ($invalid.Content | ConvertFrom-Json).detail.Count -ne 23) {
    throw 'Expected HTTP 422 with 23 validation errors'
}
if ((Get-Audit).count -ne 0) { throw 'Invalid prediction wrote an audit row' }

$customer = @'
{
  "LIMIT_BAL": 20000, "SEX": 2, "EDUCATION": 2, "MARRIAGE": 1, "AGE": 35,
  "PAY_0": 0, "PAY_2": -1, "PAY_3": -1, "PAY_4": 0, "PAY_5": 0, "PAY_6": -1,
  "BILL_AMT1": 3913, "BILL_AMT2": 3102, "BILL_AMT3": 689, "BILL_AMT4": 0,
  "BILL_AMT5": 0, "BILL_AMT6": 0, "PAY_AMT1": 0, "PAY_AMT2": 689,
  "PAY_AMT3": 0, "PAY_AMT4": 0, "PAY_AMT5": 0, "PAY_AMT6": 0
}
'@
$valid = Invoke-WebRequest -Method Post -Uri "$base/predict" -ContentType 'application/json' -Body $customer -NoProxy -SkipHttpErrorCheck
$prediction = $valid.Content | ConvertFrom-Json
if ($valid.StatusCode -ne 200 -or $prediction.default_probability -ne 0.3174 -or $prediction.threshold -ne 0.15 -or $prediction.decision -ne 1) {
    throw "Unexpected prediction response: $($valid.Content)"
}
$audit = Get-Audit
if ($audit.count -ne 1 -or [Math]::Round($audit.rows[0][0], 4) -ne 0.3174 -or $audit.rows[0][1] -ne 0.15 -or $audit.rows[0][2] -ne 1) {
    throw 'Expected exactly one audit row matching the prediction'
}
```

### 4. Replace the FastAPI Pod and confirm persistence

```powershell
$oldPod = minikube -p credit-default kubectl -- --context credit-default -n credit-default get pod -l app=fastapi -o name
minikube -p credit-default kubectl -- --context credit-default -n credit-default rollout restart deploy/fastapi
minikube -p credit-default kubectl -- --context credit-default -n credit-default rollout status deploy/fastapi --timeout=180s
$newPod = minikube -p credit-default kubectl -- --context credit-default -n credit-default get pod -l app=fastapi -o name
if (@($newPod).Count -ne 1 -or $newPod -eq $oldPod) { throw 'Expected a replacement FastAPI Pod' }
$persisted = Get-Audit
if (($persisted | ConvertTo-Json -Depth 5 -Compress) -ne ($audit | ConvertTo-Json -Depth 5 -Compress)) {
    throw 'Audit rows changed after Pod replacement'
}
'Verified HTTP health, validation, and one persistent audit row.'
```

### 5. Clean up

Stop the port-forward with Ctrl-C. To keep the SQLite data, stop the profile
with `minikube stop -p credit-default`. For a disposable cluster, delete its
namespace and profile only after confirming the SQLite data can be removed:

```powershell
minikube -p credit-default kubectl -- --context credit-default delete namespace credit-default
minikube delete -p credit-default
```
