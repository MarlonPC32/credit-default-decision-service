# Local Kubernetes with Minikube

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
