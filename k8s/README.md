# CAO Elastic Workers

These manifests run one persistent CAO supervisor and create one disposable
Kubernetes Job for each `assign_elastic` call. Worker runtime state uses
`emptyDir`; the supervisor owns durable CAO memory on its EBS claim. All nodes
mount the EFS workspace dedicated to this CAO cluster.

| Component | Kubernetes kind | Storage | Lifecycle |
|---|---|---|---|
| `cao-supervisor` | StatefulSet, one replica | EBS state and dedicated EFS workspace | Persistent |
| `cao-worker-broker` | Deployment, one replica | None | Persistent |
| `cao-worker-<id>` | One Job per assignment | `emptyDir` state and dedicated EFS workspace | Deleted after callback |

The broker creates each worker Job and its temporary Service. Workers send
memory operations to the supervisor API, so project memory has one durable
owner instead of being split across per-pod state directories.

## Prerequisites

- AWS CLI, `kubectl`, Docker, and Helm
- AWS credentials allowed to create VPC, EKS, IAM, ECR, EFS, and Secrets
  Manager resources
- A provider base image containing CAO and the selected provider CLI

## Provision AWS Infrastructure

The CloudFormation template creates a two-AZ VPC, EKS cluster and managed node
group, required EKS add-ons, ECR repositories, EFS workspace, provider secret,
and the IAM role used by External Secrets Operator.

```bash
AWS_REGION="<aws-region>"
STACK_NAME="cao-workshop"
CLUSTER_NAME="cao-workshop"

aws cloudformation deploy \
  --region "${AWS_REGION}" \
  --template-file k8s/iac/infrastructure.yaml \
  --stack-name "${STACK_NAME}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides ClusterName="${CLUSTER_NAME}"

aws eks update-kubeconfig \
  --region "${AWS_REGION}" \
  --name "${CLUSTER_NAME}"

aws cloudformation describe-stacks \
  --region "${AWS_REGION}" \
  --stack-name "${STACK_NAME}" \
  --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
  --output table
```

Set the provider API key on the empty secret created by the stack:

```bash
SECRET_NAME="$(
  aws cloudformation describe-stacks \
    --region "${AWS_REGION}" \
    --stack-name "${STACK_NAME}" \
    --query "Stacks[0].Outputs[?OutputKey=='ProviderSecretName'].OutputValue | [0]" \
    --output text
)"

aws secretsmanager put-secret-value \
  --region "${AWS_REGION}" \
  --secret-id "${SECRET_NAME}" \
  --secret-string '{"KIRO_API_KEY":"<provider-api-key>"}'
```

Install External Secrets Operator after the stack completes. Restarting it
ensures pods receive the EKS Pod Identity association created by the stack.

```bash
helm repo add external-secrets https://charts.external-secrets.io
helm repo update
helm upgrade --install external-secrets external-secrets/external-secrets \
  --namespace external-secrets \
  --create-namespace
kubectl -n external-secrets rollout restart deployment/external-secrets
```

## Build

Choose a new immutable tag for every build, then update both tags in
`kustomization.yaml` and the worker image value in `broker.yaml`.

```bash
AWS_ACCOUNT="<account-id>"
AWS_REGION=ap-southeast-2
TAG="cao-$(date +%Y%m%d%H%M)"
REGISTRY="${AWS_ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com"
BASE_PROVIDER_IMAGE="<provider-image>:<tag>"

docker build \
  --build-arg BASE_PROVIDER_IMAGE="${BASE_PROVIDER_IMAGE}" \
  -f docker/Dockerfile \
  -t "${REGISTRY}/cao-server:${TAG}" .
docker build \
  -f docker/Dockerfile.broker \
  -t "${REGISTRY}/cao-worker-broker:${TAG}" .
```

The provider base image must already contain the CAO runtime, its container
entrypoint, the selected provider CLI, and the unprivileged `cao` user. This
branch image replaces the installed CAO wheel while retaining that runtime.

Authenticate to ECR and push both images:

```bash
aws ecr get-login-password --region "${AWS_REGION}" |
  docker login --username AWS --password-stdin "${REGISTRY}"
docker push "${REGISTRY}/cao-server:${TAG}"
docker push "${REGISTRY}/cao-worker-broker:${TAG}"
```

## Deploy

Replace the public placeholders before applying:

| File | Values |
|---|---|
| `kustomization.yaml` | `<account-id>`, `<aws-region>`, and `<immutable-tag>` for both images |
| `broker.yaml` | The same server image URI and immutable tag in `CAO_ELASTIC_WORKER_IMAGE` |
| `storage.yaml` | `<filesystem-id>` and `<access-point-id>` for the stack-created EFS access point |
| `external-secret.yaml` | `<aws-region>` used by the `ClusterSecretStore` |

Use `ServerRepositoryUri`, `WorkerBrokerRepositoryUri`, and
`WorkspaceVolumeHandle` from the CloudFormation outputs. Split
`WorkspaceVolumeHandle` at `::` for the two placeholders in `storage.yaml`.

Create the broker credential outside Git:

```bash
kubectl create namespace cao-cluster --dry-run=client -o yaml | kubectl apply -f -
kubectl -n cao-cluster create secret generic cao-elastic-broker-token \
  --from-literal=token="$(openssl rand -hex 32)"
kubectl apply -k k8s
```

The supervisor agent uses `assign_elastic`. A disposable worker must finish
with `complete_assignment`; that delivers its result before the broker deletes
only that worker's Job and Service.

## Verify

```bash
kubectl -n cao-cluster get externalsecret,pvc,pod,job,service
kubectl -n cao-cluster rollout status deployment/cao-worker-broker
kubectl -n cao-cluster rollout status statefulset/cao-supervisor
```

Default `project` memory is shared through the supervisor because all elastic
nodes use `CAO_PROJECT_ID=cao-cluster`. Local CAO installations are unchanged
unless `CAO_MEMORY_API_URL` is explicitly configured.

## Run a Demo Assignment

The following demo starts a `code_supervisor` session inside the supervisor
pod. The supervisor creates a producer worker Job and a delayed consumer worker
Job before waiting for callbacks. The producer stores a project memory and the
consumer recalls it from the supervisor-owned memory service.

Set the namespace:

```bash
NAMESPACE=cao-cluster
```

In one terminal, watch disposable worker Jobs and pods appear and disappear:

```bash
kubectl -n "${NAMESPACE}" get jobs,pods \
  -l app.kubernetes.io/name=cao-elastic-worker \
  --watch
```

In another terminal, create the supervisor session and submit the demo task:

```bash
SUPERVISOR_ID="$(
  kubectl -n "${NAMESPACE}" exec -i cao-supervisor-0 -- python - <<'PY'
import requests

task = """
Run this demonstration using elastic workers. Do not perform the worker tasks
yourself.

1. Call assign_elastic with agent_profile="developer" and provider="kiro_cli".
   Tell the worker to store project memory with key "elastic-demo-shared",
   memory_type "project", and content "The elastic producer completed the demo."
   It must finish with complete_assignment and include the stored fact in its
   result.
2. Immediately call assign_elastic again with agent_profile="developer" and
   provider="kiro_cli". Tell this consumer worker to run `sleep 60`, recall
   project memory key "elastic-demo-shared", and finish with
   complete_assignment containing the recalled value.
3. Do not poll or wait with shell commands. After both assignments have been
   accepted, report their worker IDs and end the current turn. Their callbacks
   will arrive through the supervisor inbox.
"""

response = requests.post(
    "http://cao-supervisor:9889/sessions",
    params={
        "agent_profile": "code_supervisor",
        "provider": "kiro_cli",
        "working_directory": "/home/cao/workspace",
    },
    json={"initial_message": task},
    timeout=30,
)
response.raise_for_status()
print(response.json()["id"])
PY
)"

echo "Supervisor terminal: ${SUPERVISOR_ID}"
```

Session creation is asynchronous. Follow the supervisor output:

```bash
kubectl -n "${NAMESPACE}" exec -i \
  cao-supervisor-0 -- env TERMINAL_ID="${SUPERVISOR_ID}" python - <<'PY'
import os
import requests

response = requests.get(
    f"http://cao-supervisor:9889/terminals/{os.environ['TERMINAL_ID']}/output",
    params={"mode": "full"},
    timeout=30,
)
response.raise_for_status()
print(response.json()["output"])
PY
```

Read callbacks delivered by completed workers:

```bash
kubectl -n "${NAMESPACE}" exec -i \
  cao-supervisor-0 -- env TERMINAL_ID="${SUPERVISOR_ID}" python - <<'PY'
import json
import os
import requests
import time

url = (
    f"http://cao-supervisor:9889/terminals/"
    f"{os.environ['TERMINAL_ID']}/inbox/messages"
)
deadline = time.monotonic() + 600

while time.monotonic() < deadline:
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    messages = response.json()
    if len(messages) >= 2:
        print(json.dumps(messages, indent=2))
        break
    time.sleep(5)
else:
    raise TimeoutError("Timed out waiting for two worker callbacks")
PY
```

Verify that the producer's memory is stored on the supervisor EBS volume:

```bash
kubectl -n "${NAMESPACE}" exec cao-supervisor-0 -- \
  cao memory show elastic-demo-shared --scope project
```

After the demo, both worker Jobs and Services should be gone because each
worker called `complete_assignment` only after delivering its result:

```bash
kubectl -n "${NAMESPACE}" get jobs,services \
  -l app.kubernetes.io/name=cao-elastic-worker
```

Remove the demonstration memory when it is no longer needed:

```bash
kubectl -n "${NAMESPACE}" exec cao-supervisor-0 -- \
  cao memory delete elastic-demo-shared --scope project --yes
```

## Cleanup

```bash
kubectl delete -k k8s
kubectl delete namespace cao-cluster
```

The EFS PV and CloudFormation file system use `Retain`; cleanup does not delete
workspace data. The retained EFS filesystem and provider secret must be removed
explicitly when they are no longer required.
