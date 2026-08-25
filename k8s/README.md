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

Choose a new immutable tag for every build, then update all three tags in
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
docker build \
  -f docker/Dockerfile.panel \
  -t "${REGISTRY}/cao-fleet-panel:${TAG}" .
```

The provider base image must already contain the CAO runtime, its container
entrypoint, the selected provider CLI, and the unprivileged `cao` user. This
branch image replaces the installed CAO wheel while retaining that runtime.

The panel build is driven by `docker/Dockerfile.panel.dockerignore` rather than
the repository-root `.dockerignore`, which excludes `examples/` and would leave
the panel's own source out of its image. Build it from the repository root so
both `app/` and `static/` are in context.

Authenticate to ECR and push all three images:

```bash
aws ecr get-login-password --region "${AWS_REGION}" |
  docker login --username AWS --password-stdin "${REGISTRY}"
docker push "${REGISTRY}/cao-server:${TAG}"
docker push "${REGISTRY}/cao-worker-broker:${TAG}"
docker push "${REGISTRY}/cao-fleet-panel:${TAG}"
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

Create the two credentials outside Git. The panel token is not optional: the
panel reads it from `cao-panel-secret`, and without that secret the pod stops at
`CreateContainerConfigError`.

```bash
kubectl create namespace cao-cluster --dry-run=client -o yaml | kubectl apply -f -
kubectl -n cao-cluster create secret generic cao-elastic-broker-token \
  --from-literal=token="$(openssl rand -hex 32)"
kubectl -n cao-cluster create secret generic cao-panel-secret \
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
kubectl -n cao-cluster rollout status deployment/cao-fleet-panel
```

Reach the panel over a port-forward; it is not exposed through an Ingress. Send
the token you generated above — it guards the whole origin, so a browser prompts
once and reuses it:

```bash
kubectl -n cao-cluster port-forward svc/cao-fleet-panel 9888:9888
TOKEN=$(kubectl -n cao-cluster get secret cao-panel-secret \
  -o jsonpath='{.data.token}' | base64 -d)
curl -fsS -H "Authorization: Bearer ${TOKEN}" http://127.0.0.1:9888/api/fleet
```

The fleet view lists the supervisor from `configmap-fleet.yaml` plus whichever
elastic workers hold a lease: the broker publishes each worker into that
ConfigMap once it is ready and withdraws it on release, since Job-backed workers
cannot be enumerated ahead of time. The panel re-reads the file per request, so
no restart is needed, but a mounted ConfigMap refreshes on the kubelet's sync
period and the view can lag a lease by up to about a minute.

Re-running `kubectl apply -k k8s` resets that ConfigMap to the supervisor alone.
The broker republishes on the next lease, but workers running at that moment drop
off the panel until they are released, so avoid re-applying while a fleet is
busy.

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

Remove the Kubernetes objects:

```bash
kubectl delete -k k8s
kubectl delete namespace cao-cluster
```

That leaves the AWS resources running — the EKS control plane, the node group and
the NAT gateway keep billing until the stack goes too:

```bash
aws cloudformation delete-stack --stack-name <stack-name>
aws cloudformation wait stack-delete-complete --stack-name <stack-name>
```

Both ECR repositories set `EmptyOnDelete`, so the images pushed during Build do
not block the delete. Without it the stack ends in `DELETE_FAILED` and takes the
VPC with it, because the subnets fail alongside the repositories.

Two resources deliberately survive the stack, because losing them to an
accidental delete is worse than keeping them: `WorkspaceFileSystem` (the shared
workspace) and `ProviderSecret` (the API key). Both use `DeletionPolicy: Retain`.

Deleting the file system is a cost decision — it keeps charging until removed.
Deleting the secret is a **prerequisite for redeploying**: the name survives, so
a new stack using the same `ProviderSecretName` fails with a name conflict.
Recreating a stack under the same name therefore needs this first:

```bash
aws secretsmanager delete-secret --secret-id <provider-secret-name> \
  --force-delete-without-recovery
aws efs delete-file-system --file-system-id <fs-id>
```

`--force-delete-without-recovery` matters: a normal delete schedules the secret
for removal but keeps the name reserved through the recovery window, so the
redeploy still collides.
