# CAO Elastic Workers

One persistent CAO supervisor, one narrow broker, and one disposable
single-replica Deployment per `assign_elastic` call. Worker runtime state is `emptyDir`; the supervisor
owns durable CAO memory on its EBS claim. Both mount the shared EFS workspace.

| Component | Kubernetes kind | Storage | Lifecycle |
|---|---|---|---|
| `cao-supervisor` | StatefulSet, one replica | EBS state + shared EFS workspace | Persistent |
| `cao-worker-broker` | Deployment, one replica | None | Persistent |
| `cao-worker-<id>` | Deployment, one replica, one per assignment | `emptyDir` state + shared EFS workspace | Released on callback |

The broker creates each worker Deployment and its temporary Service. Workers send
authenticated memory and callback requests through the broker's narrow gateway;
the broker forwards only those five routes to the supervisor, so project memory
has one durable owner without exposing the supervisor control API.

Three properties are worth understanding before changing anything here.

**The broker is a security boundary, not a convenience.** It is the only pod in
the namespace that can reach the Kubernetes API, and it is the one that runs no
agent. The request it accepts has exactly two fields, `agent_profile` and
`provider`, both bounded by `^[a-zA-Z0-9_-]{1,64}$`; image, command, volumes,
service account and resource limits are all broker-controlled. So a
prompt-injected supervisor can ask for "a worker running the reviewer profile"
and cannot ask for "a privileged pod mounting the host filesystem". Its Role has
no `pods/exec` and no `secrets` — it cannot shell into a worker it created, nor
read the token it authenticates callers with. The same boundary protects the
persistent supervisor: workers cannot reach its port at all. They present their
per-lease release token to the broker, which forwards only inbox delivery and
the four memory operations. The lease also binds the only callback receiver and
the worker's memory session/profile context; the broker validates or replaces
those identity fields rather than trusting the worker's request. Local CAO
remains authentication-free by default; this credential and routing behavior
activates only inside elastic worker pods.

**A worker cannot drift.** Its profile store is a fresh `emptyDir`, and
`CAO_INSTALL_PROFILES` is set per worker to `<profile>:<provider>`. On a fixed fleet
the store is per pod and long-lived, so a profile installed on one node is
invisible to the others and an unpinned profile falls back to a provider that is
not in the image — the failure being `kiro-cli was not found`, naming a CLI
nobody asked for. Here the profile the task needs is installed with its provider
at pod start, every time.

**A reported success is not proof the work happened.** CAO decides a turn is over
by watching the agent's TUI, so an agent that emits prose before its first tool
call trips the detector: CAO reads the settled text as end-of-turn, kills the
window, and reports `"success": true`. Measured at 3.1s on a task needing 36s.
`assign_elastic` releases the lease only when the result is *not* successful, so
this case — the one where nothing happened — is the case where the lease is never
returned. The broker reaps it and records why; see [Reading the lease
ledger](#reading-the-lease-ledger). Custom profiles should say "do all tool calls
first, speak once at the end".

## Prerequisites

- AWS CLI, `kubectl`, Docker with `buildx`. Images are built `linux/arm64` to
  match the node group, which is Graviton. The Workshop Studio code editor is
  arm64, so that build is native there; on an x86_64 host it cross-builds under
  emulation, which is slow and not universally reliable
- Credentials allowed to create VPC, EKS, IAM, KMS, ECR and EFS resources
- On the default path: no Helm, no External Secrets Operator, and no provider
  API key. Claude Code signs Bedrock requests with SigV4 using credentials from
  EKS Pod Identity, so there is no secret to sync. The one secret in the
  namespace is the broker token, minted locally by `deploy.sh`.
- Before the first Anthropic model invocation, submit Anthropic's First Time Use
  form from the Bedrock model catalog or with `PutUseCaseForModelAccess`.
  Marketplace auto-subscription does not satisfy this separate prerequisite.
  It is required once per account, or once in the AWS Organizations management
  account where the root-account submission is inherited by member accounts;
  opt-in Regions require a separate submission. See [Add or remove access to
  Amazon Bedrock foundation
  models](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html).
- For a provider that authenticates with a key instead, see [Provider
  credentials](#provider-credentials) — read it before committing to that path.

## Provision AWS infrastructure

The template creates a two-AZ VPC, an EKS cluster and managed node group, the
required add-ons, two ECR repositories, the EFS workspace, the KMS key that
envelope-encrypts Kubernetes Secrets, and, on the default Bedrock path, the Pod
Identity associations that give the supervisor and workers model access.

The KMS key is worth one note: `EncryptionConfig` is **create-time only** on an
EKS cluster, so a cluster built without it has to be replaced to get it. It is
there because the broker token is a real credential — it authorises asking for a
pod — and it is the only thing this fleet puts in etcd.

```bash
export AWS_REGION=us-east-1
STACK_NAME=cao-workshop

CALLER_ARN="$(aws sts get-caller-identity --query Arn --output text)"
if [[ "${CALLER_ARN}" == arn:*:sts::*:assumed-role/*/* ]]; then
  ROLE_NAME="${CALLER_ARN#*:assumed-role/}"
  ROLE_NAME="${ROLE_NAME%%/*}"
  CLUSTER_ADMIN_PRINCIPAL_ARN="$(
    aws iam get-role --role-name "${ROLE_NAME}" --query Role.Arn --output text
  )"
else
  CLUSTER_ADMIN_PRINCIPAL_ARN="${CALLER_ARN}"
fi
case "${CLUSTER_ADMIN_PRINCIPAL_ARN}" in
  arn:*:iam::*:role/*|arn:*:iam::*:user/*) ;;
  *)
    echo "Cluster admin must be a permanent IAM role or user ARN" >&2
    exit 1
    ;;
esac

aws cloudformation deploy \
  --region "${AWS_REGION}" \
  --template-file examples/cao-clusters/kubernetes/eks/iac/cfn-infrastructure.yaml \
  --stack-name "${STACK_NAME}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides ClusterAdminPrincipalArn="${CLUSTER_ADMIN_PRINCIPAL_ARN}"

aws eks update-kubeconfig --region "${AWS_REGION}" --name cao-workshop
```

`ClusterAdminPrincipalArn` is load-bearing when the stack is deployed by a
different principal from the one running `kubectl`:
`BootstrapClusterCreatorAdminPermissions` grants admin to the *deploying*
principal only, so without this every `kubectl` command fails `Unauthorized`.
EKS access entries reject temporary `arn:...:sts::...:assumed-role/...` session
principals. The command above resolves that session to its permanent IAM role
with `iam:GetRole`; if that permission is unavailable, set
`CLUSTER_ADMIN_PRINCIPAL_ARN` explicitly to the participant role ARN.

Nodes are Graviton (`m7g.xlarge`, `AL2023_ARM_64_STANDARD`), matching the arm64
code editor the images are built on. Build for the same architecture: a mismatch
is not caught at deploy time, it surfaces as a crash loop with `exec format
error` rather than as a pull failure.

## Build

Three images, one tag, built from one commit:

```bash
export AWS_REGION=us-east-1
TAG="cao-$(date +%Y%m%d%H%M)"
REGISTRY="$(aws sts get-caller-identity --query Account --output text).dkr.ecr.${AWS_REGION}.amazonaws.com"

aws ecr get-login-password --region "${AWS_REGION}" |
  docker login --username AWS --password-stdin "${REGISTRY}"

docker buildx build --platform linux/arm64 \
  -f examples/cao-clusters/kubernetes/eks/Dockerfile \
  -t "${REGISTRY}/cao-server:${TAG}" --push .
docker buildx build --platform linux/arm64 \
  -f examples/cao-clusters/kubernetes/eks/Dockerfile.broker \
  -t "${REGISTRY}/cao-worker-broker:${TAG}" --push .
docker buildx build --platform linux/arm64 \
  -f examples/cao-clusters/kubernetes/eks/Dockerfile.panel \
  -t "${REGISTRY}/cao-fleet-panel:${TAG}" --push .
```

`Dockerfile` builds the server image self-contained: it installs CAO and Claude
Code from public registries and carries `entrypoint.sh`, which runs
`cao init`, installs the profiles named in `CAO_INSTALL_PROFILES`, and execs
`cao-server`. Nothing out-of-tree is required to reproduce it.

ECR tags are `IMMUTABLE` in both repositories, deliberately: a mutable `latest`
once left a cluster running a build that predated a fix while the manifests
advertised it, with nothing to indicate the mismatch.

The panel build uses `Dockerfile.panel.dockerignore` rather than the
repository-root `.dockerignore`. The root file excludes `examples/`, which is
where the panel's source lives, so without the narrower context its own image is
built without it. BuildKit prefers a `<dockerfile>.dockerignore` when one exists,
so this is scoped to that image and leaves every other build's context alone.

The panel image copies the source tree instead of installing the wheel that
`examples/fleet/panel/pyproject.toml` builds, because the panel resolves its
frontend as a sibling of the app package:

```python
_ROOT = os.path.dirname(os.path.dirname(__file__))
```

An installed wheel does not preserve that layout, so the panel would start and
then serve 404 for every asset.

The panel serves CAO's own dashboard, so a fleet is operated with the same UI a
single server serves. A first build stage (`node:24-slim` — vite 8 builds
through rolldown, which ships no binding for Node 20) runs `npm run build` in
`web/` and the result is copied to `/panel/web_ui`. The panel prefers it and
falls back to its own `static/` UI when that directory holds no `index.html`,
which is what `examples/fleet/panel` does when run straight from a checkout.

Vite resolves the app's base path at build time, so an image reached under a
reverse-proxy prefix needs it baked in:

```bash
docker buildx build --platform linux/arm64 \
  --build-arg WEB_BASE=/proxy/panel/ \
  -f examples/cao-clusters/kubernetes/eks/Dockerfile.panel \
  -t "${REGISTRY}/cao-fleet-panel:${TAG}" --push .
```

The default is `/`, which is what the `kubectl port-forward` below wants. A
trailing slash is required — the value is emitted verbatim.

## Provider credentials

Claude Code on Bedrock is the default and needs no credential plumbing at all:
each pod signs its own requests with SigV4 from its EKS Pod Identity association,
so there is nothing to store, synchronise or rotate. The one secret in the
namespace is the broker token, minted locally by `deploy.sh`.

Everything below is for a provider that authenticates with a key instead.

**Read this first.** For kiro-cli specifically, the credential path does not
currently reach the interactive session CAO runs. Kiro's API key is documented for
`kiro-cli chat --no-interactive` only — *"For interactive sessions, use
browser-based sign-in instead"* — and CAO launches `kiro-cli --v3 chat`, an
interactive TUI it drives through tmux. Device-flow login needs a human to enter a
one-time code, which is not available to a pod that exists for one task. Kiro's own
precedence order puts an active `kiro-cli login` session *above* the API key, so
pre-seeding that session credential into the secret below is the plausible route,
but it is undocumented and unverified here. What this tree provides is a complete
and tested seam, not a working kiro deployment.

The seam has three parts.

*The image.* `Dockerfile` can install kiro-cli itself — no out-of-tree
image needed:

```bash
docker buildx build --platform linux/arm64 \
  -f examples/cao-clusters/kubernetes/eks/Dockerfile \
  --build-arg INSTALL_KIRO_CLI=1 --build-arg INSTALL_CLAUDE_CODE=0 \
  --build-arg CAO_PROFILE_PROVIDER=kiro_cli \
  -t "${REGISTRY}/cao-server:${TAG}" --push .
```

It fetches the pinned `musl` archive and runs the vendor installer with
`Q_INSTALL_GLOBAL=1 Q_SKIP_SETUP=1`, which is the unattended combination. Expect
the image to roughly double: the archive is ~689MB for `aarch64` and installs to
1.1GB, of which `kiro-cli-chat` is 943MB. The URL is built from `$(uname -m)`, so
it follows the build platform and needs no change if that ever moves. The `musl` build is not a preference: the *gnu*
archive requires glibc 2.39 and bookworm ships 2.36, so its own installer refuses
it and points at musl, which skips the glibc check. `ARG BASE_PROVIDER_IMAGE` remains for any other provider: point it at
an image that already carries that CLI, pass `INSTALL_CLAUDE_CODE=0`, and set
`CAO_PROFILE_PROVIDER` to the provider name CAO uses. That last argument pins
the image's seeded supervisor and worker profiles to the CLI the image actually
contains instead of seeding unusable Claude profiles.

*The credential.* Set `ProviderSecretName=cao/provider-credentials` on the
infrastructure stack, which creates an empty Secrets Manager secret plus the IAM
role and Pod Identity association the External Secrets Operator needs. Put the
value and install ESO before deploying Kiro mode. Its Kustomize Component adds
the `ExternalSecret`; both the supervisor and every worker read the resulting
Kubernetes Secret with `envFrom` and `optional: true`, so whatever keys the
Secrets Manager JSON holds become environment variables in the pods. Setting
`ProviderSecretName` also omits the Bedrock policy, role, and both agent Pod
Identity associations, so only ESO receives AWS credentials in API-key mode.

*The provider mode.* Pass `kiro` as the third argument to `deploy.sh`. The
Component sets the supervisor profile to `code_supervisor:kiro_cli`, sets the
broker's worker default to `kiro_cli`, removes the Bedrock model variables and
Pod Identity endpoint egress, and includes the credential projection. These are
one switch because changing only the broker leaves the persistent supervisor
trying to execute a `claude` binary the image does not contain.

Whatever the provider, it has to authenticate with no human present. That is the
constraint the whole topology imposes — pods here are disposable and nobody is at a
browser when one starts.

## Deploy

```bash
# Default Claude Code on Bedrock:
examples/cao-clusters/kubernetes/eks/deploy.sh cao-workshop "${TAG}"

# Alternate image built for Kiro:
examples/cao-clusters/kubernetes/eks/deploy.sh cao-workshop "${TAG}" kiro
```

That is the whole deploy. Do not hand-edit the manifests — `deploy.sh` renders
`<account-id>`, `<region>`, `<filesystem-id>`, `<access-point-id>` and
`<vpc-cidr>` from stack outputs into a temporary copy, so this source directory
is never modified and a failed run leaves nothing to clean up. It also:

- mints `cao-elastic-broker-token` on first run and **keeps** it afterwards.
  Regenerating it would leave a running supervisor holding a token the broker no
  longer accepts, and every delegation would 401 with nothing visibly changed.
- rewrites every `newTag:` in `kustomization.yaml` to the tag you pass, and
  verifies every one of them afterwards. A no-op substitution must not be
  survivable.
- enables `components/kiro` only when the third argument is `kiro`, and refuses
  that mode unless the stack output confirms
  `ProviderSecretName=cao/provider-credentials`.
- aborts if any `<placeholder>` survives rendering. A literal `<immutable-tag>`
  in an image name otherwise surfaces ten minutes later as an
  `ImagePullBackOff`, and a literal CIDR as a policy that matches nothing.

The worker image is **not** configured by hand. `kustomization.yaml` has a
`replacements:` block that copies the supervisor's already-tag-rewritten image
into the broker's `CAO_ELASTIC_WORKER_IMAGE`, because kustomize's `images:`
transformer rewrites container `image:` fields and not an image name sitting in an
env var. Without it the supervisor would move to a new tag while the broker kept
minting workers on the old one.

## Verify

```bash
kubectl -n cao-cluster get pvc,pod,job,service,networkpolicy
kubectl -n cao-cluster rollout status statefulset/cao-supervisor
kubectl -n cao-cluster rollout status deployment/cao-worker-broker
kubectl -n cao-cluster rollout status deployment/cao-fleet-panel
```

There are no worker pods at rest, and on the default Bedrock path no
`externalsecret` either — both are expected.

Two things must be probed rather than read, because a manifest that is not
enforced looks byte-identical to one that is:

```bash
# NetworkPolicy enforcement is a VPC CNI add-on setting, and it is OFF by
# default. Kubernetes accepts policy objects either way.
aws eks describe-addon --cluster-name cao-workshop --addon-name vpc-cni \
  --query 'addon.configurationValues' --output text

# From inside a pod: the Pod Identity agent answers, IMDS must NOT.
kubectl -n cao-cluster exec cao-supervisor-0 -- \
  curl -s -o /dev/null -w 'pod-identity=%{http_code}\n' http://169.254.170.23/
kubectl -n cao-cluster exec cao-supervisor-0 -- \
  curl -s --max-time 3 -o /dev/null -w 'imds=%{http_code}\n' http://169.254.169.254/ \
  || echo "imds unreachable (expected: curl exit 28)"
```

The second check is the one that matters. `169.254.170.23` hands out this pod's
scoped credentials, whose only permission is Bedrock invoke; `169.254.169.254`
hands out the **node role's**, which include ECR pull and the CNI's ENI
permissions. The egress policies punch a `/32` for the former and leave the
latter blocked — widening that to `169.254.0.0/16` would hand a prompt-injected
agent the node role.

## The fleet panel

Reach it over a port-forward; it is not exposed through an Ingress. `deploy.sh`
mints the token and keeps it across runs:

```bash
kubectl -n cao-cluster port-forward svc/cao-fleet-panel 9888:9888
TOKEN=$(kubectl -n cao-cluster get secret cao-panel-secret \
  -o jsonpath='{.data.token}' | base64 -d)
curl -fsS -H "Authorization: Bearer ${TOKEN}" http://127.0.0.1:9888/api/fleet
```

The token guards the whole origin, so a browser prompts once and reuses it. That
is also why the pod's probes are `exec` running `curl` with the header rather
than `httpGet`: a probe cannot read a token from a secret, so an HTTP probe would
be answered 401 and restart the pod forever.

The fleet view lists the supervisor from `configmap-fleet.yaml`, plus whichever
workers hold a lease. The panel cannot discover a worker on its own -- each is
created on demand with a generated id -- so the broker publishes each one into that ConfigMap
when it leases it and withdraws it on release. That ConfigMap is therefore
jointly owned, and the live object differing from the checked-in file is expected
rather than drift.

A worker is published when it is placed, not when it is ready, because a lease
asserts placement and `POST /workers` no longer waits for readiness. The panel
probes what it is given, so a worker still booting shows as unreachable for a few
seconds. The alternative -- gating the fleet view on `CAO_ELASTIC_GATE_ON_READY`
-- would hide every worker on the default non-blocking path.

The panel reads that ConfigMap from the **API server**, not from its mount, so a
published worker is visible within its poll interval (5s by default) and no
restart is needed. The mount would work but not well enough: a mounted ConfigMap
only refreshes on the kubelet's sync period, and a leased worker can be placed,
used and released inside that window. The view would then show a worker that had
already gone while missing the one that was running.

Measured on a 1.35 cluster, leasing one elastic worker and releasing it while
watching both views at once -- `/api/fleet` against `/config/fleet.json` inside
the panel pod:

| | panel reads the API server | panel reads its mount |
|---|---|---|
| lease appears | 7.9s | 24.1s |
| lease disappears | 4.1s | 87.5s |

The withdrawal is the direction that matters and the one that is worse: for 83
seconds the mounted copy advertised a worker whose Service had already been
deleted, so the panel would have proxied terminal traffic to an address that no
longer resolved. There is no upper bound on that number to design against -- it
is a resync, not a delivery guarantee.

The mount is kept as a fallback. If the panel's RoleBinding is missing or wrong it
logs the failure and serves the mounted copy, so you get a stale panel rather than
a CrashLooping one. Which source answered is reported by `/api/fleet`:

```bash
curl -fsS -H "Authorization: Bearer ${TOKEN}" http://127.0.0.1:9888/api/fleet \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["source"])'
```

`{"kind": "configmap", ..., "live": true, "error": null}` is the intended state.

**Read `error`, not `live`.** A non-null `error` is the signal that reads are
failing now; `live` only says whether the fleet on screen came from a successful
read at some point, and it stays `true` while a last-good snapshot is being
served. A panel that read the ConfigMap once and has been failing ever since
reports `live: true` with an error -- which is the correct answer to both
questions, but only the second one tells you to go and fix something.

The error text names which of the two quiet failures you have:

| `error` | cause |
|---|---|
| `403 Forbidden` | the `cao-fleet-panel` Role or RoleBinding in `rbac.yaml` |
| `ConnectTimeout` | the API-server egress rule in `networkpolicy.yaml` |

Both were confirmed by removing each in turn on a live cluster, and both recover
on the next poll once restored -- no restart. Without this field they are
indistinguishable from a fleet whose workers are not registering: the panel comes
up, the probes pass, and no worker ever appears.

The grant is one `get` on one ConfigMap by name. `list` and `watch` are not
granted -- `resourceNames` cannot restrict them, so either would widen it to every
ConfigMap in the namespace -- which is why the panel polls rather than watches.

Re-running `deploy.sh` resets that ConfigMap to the supervisor alone. The broker
republishes on the next lease, but a worker running at that moment drops off the
view until it is released, so avoid re-deploying while a fleet is busy.

### Terminals through the panel

The dashboard's terminal attaches to a real PTY on the node, so the panel bridges
a WebSocket rather than proxying a response. Two things in this deployment exist
only to let that work:

`CAO_WS_ALLOWED_CLIENTS="*"` on the supervisor and on every worker Job.
`cao-server` restricts terminal attaches to loopback by default and closes 4003
on anything else, and the peer here is the panel pod, whose IP is assigned at
schedule time -- so no manifest can name it. `*` disables that IP check only. It
is the redundant check in this namespace: `cao-supervisor-ingress` and
`cao-worker-ingress` already restrict 9889 to the supervisor and the panel **by
pod label**, which is a stronger bound than an address list and does not go stale
when a pod is rescheduled. Origin enforcement is untouched on both sides.

The panel guards the socket itself, because ASGI does not run HTTP middleware for
a WebSocket scope -- the panel token would not have covered it. It requires the
token (header, or the `HttpOnly`/`SameSite=strict` cookie a browser gets, since a
browser cannot set a header on a handshake) and a same-origin `Origin`, and it
forwards no `Origin` upstream: the node would compare it against its own `Host`,
which a proxied origin never matches, and a header-less handshake is what
`cao-server` treats as a non-browser client.

Behind a reverse proxy the browser's `Origin` is matched against
`X-Forwarded-Host` as well as `Host`, since a proxy's `Host` is the upstream it
dialled. That is safe for this one check because JavaScript cannot set any header
on a WebSocket handshake, so the cross-site page the check exists to stop is the
one caller that cannot forge it.

## Reading the lease ledger

The broker holds a ledger of every lease it has issued and why each one ended.
This is the endpoint to read after a delegation that claimed success and produced
nothing:

```bash
TOKEN="$(kubectl -n cao-cluster get secret cao-elastic-broker-token \
  -o jsonpath='{.data.token}' | base64 -d)"
kubectl -n cao-cluster exec cao-supervisor-0 -- \
  curl -s -H "X-CAO-Broker-Token: ${TOKEN}" http://cao-worker-broker:9890/workers
```

| `state` | Meaning |
|---|---|
| `leased` | Open. A worker is running and has not called back. |
| `completed` | The worker called `complete_assignment`. The normal path. |
| `terminated` | The pod ended while the lease was open. Usually the turn-detection race above: the task was **not** necessarily done, and the supervisor's own transcript shows a clean success. |
| `expired` | The pod was still healthy but never completed within `CAO_ELASTIC_COMPLETION_TIMEOUT` (900s). |
| `failed` | The lease never opened — the Deployment could not be created, or the pod never became Ready inside `CAO_ELASTIC_READY_TIMEOUT`. |

A `terminated` or `expired` entry also means the broker released the worker on
your behalf. Without that reaper the pod squats a node's worth of memory until the
orphan sweep collects it, an hour later (`CAO_ELASTIC_WORKER_TIMEOUT`). That sweep
is the backstop for a broker restart: the ledger is in memory, so a restarted
broker cannot settle a lease it never made, and the sweep walks the cluster instead
of the ledger to find those workers. It is also why the broker is pinned to
`replicas: 1` with a `Recreate` strategy — a second reaper would know only its own
half of the ledger.

## Run a demo assignment

Starts a `code_supervisor` session in the supervisor pod, which creates a
producer worker and a delayed consumer worker. The producer stores a project
memory; the consumer recalls it from the supervisor-owned memory service.

> Not re-run since the port to Bedrock — the cluster it was verified on has been
> torn down. Treat the commands as the intended shape, and check the lease ledger
> if a step reports success without an artifact.

Watch workers and their pods appear and disappear:

```bash
kubectl -n cao-cluster get deployments,pods \
  -l app.kubernetes.io/name=cao-elastic-worker --watch
```

In another terminal, create the session:

```bash
kubectl -n cao-cluster exec -i cao-supervisor-0 -- python - <<'PY'
import requests

task = """
Run this demonstration using elastic workers. Do not perform the worker tasks
yourself. Make all tool calls before you say anything: a reply that begins with
prose ends your turn early and kills the terminal.

1. Call assign_elastic with agent_profile="developer" and provider="claude_code".
   Tell the worker to store project memory with key "elastic-demo-shared",
   memory_type "project", and content "The elastic producer completed the demo."
   It must finish with complete_assignment and include the stored fact in its
   result.
2. Immediately call assign_elastic again with agent_profile="developer" and
   provider="claude_code". Tell this consumer worker to run `sleep 60`, recall
   project memory key "elastic-demo-shared", and finish with complete_assignment
   containing the recalled value.
3. Do not poll or wait with shell commands. After both assignments have been
   accepted, report their worker IDs and end the turn. Their callbacks arrive
   through the supervisor inbox.
"""

response = requests.post(
    "http://cao-supervisor:9889/sessions",
    params={
        "agent_profile": "code_supervisor",
        "provider": "claude_code",
        "working_directory": "/home/cao/workspace",
    },
    json={"initial_message": task},
    timeout=30,
)
response.raise_for_status()
print(response.json()["id"])
PY
```

`provider="claude_code"` in both places is required, not cosmetic. A delegation
resolves the provider from the *target's* profile store, and none of CAO's
built-in profiles pin one in their frontmatter, so an unpinned profile falls back
to `DEFAULT_PROVIDER` — `kiro_cli`, which is not in this image.

Follow the supervisor's output, then read the callbacks, substituting the
terminal id printed above:

```bash
kubectl -n cao-cluster exec -i cao-supervisor-0 -- env TERMINAL_ID="<id>" python - <<'PY'
import os, requests
r = requests.get(
    f"http://cao-supervisor:9889/terminals/{os.environ['TERMINAL_ID']}/output",
    params={"mode": "full"}, timeout=30)
r.raise_for_status()
print(r.json()["output"])
PY

kubectl -n cao-cluster exec -i cao-supervisor-0 -- env TERMINAL_ID="<id>" python - <<'PY'
import json, os, requests, time
url = f"http://cao-supervisor:9889/terminals/{os.environ['TERMINAL_ID']}/inbox/messages"
deadline = time.monotonic() + 600
while time.monotonic() < deadline:
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    messages = r.json()
    if len(messages) >= 2:
        print(json.dumps(messages, indent=2))
        break
    time.sleep(5)
else:
    raise TimeoutError("timed out waiting for two worker callbacks")
PY
```

Verify by artifact, never by the reported status — the memory the producer stored
is on the supervisor's EBS volume, and both workers should be gone because each
called `complete_assignment`:

```bash
kubectl -n cao-cluster exec cao-supervisor-0 -- \
  cao memory show elastic-demo-shared --scope project
kubectl -n cao-cluster get deployments,services -l app.kubernetes.io/name=cao-elastic-worker
```

If the workers are gone but the memory is absent, read the lease ledger: a
`terminated` entry is the turn-detection race, not a memory bug.

```bash
kubectl -n cao-cluster exec cao-supervisor-0 -- \
  cao memory delete elastic-demo-shared --scope project --yes
```

## Cleanup

Order matters, because every PV here is `Retain`. Deleting the namespace or the
stack first orphans EBS volumes that then have to be found and deleted by ID.

```bash
# 1. Capture the volume handles BEFORE deleting anything.
kubectl get pv -o custom-columns=\
'NAME:.metadata.name,RECLAIM:.spec.persistentVolumeReclaimPolicy,CLAIM:.spec.claimRef.name,HANDLE:.spec.csi.volumeHandle'

# 2. Delete the namespace, which detaches the volumes.
kubectl delete namespace cao-cluster

# 3. Delete the stack.
aws cloudformation delete-stack --region "${AWS_REGION}" --stack-name cao-workshop

# 4. Delete the captured EBS volumes explicitly.
aws ec2 delete-volume --region "${AWS_REGION}" --volume-id vol-...
```

The EFS file system is `DeletionPolicy: Delete` and goes with the stack, so
workspace data is **not** preserved. Switch it to `Retain` in
`iac/cfn-infrastructure.yaml` if the checkout holds anything you cannot recreate.

One thing does outlive the stack by design: `SecretsKey`, the KMS key that
envelope-encrypts Kubernetes Secrets. KMS never deletes a key outright, only
schedules it, so the delete leaves it `PendingDeletion` for 7 days — the shortest
window KMS allows. Nothing else references it and it costs $1/month prorated;
cancel the deletion only if you need to read an etcd backup from that cluster.

## Testing the broker

`broker.py` is not part of the CAO package and needs `fastapi` plus the
Kubernetes client, so its test runs in a throwaway environment. It stubs the API
server and pushes every `V1*` object through the client's real serializer, which
is what actually rejects a bad field name — so a mistake fails on a laptop rather
than at the first lease on a live cluster.

```bash
uv venv /tmp/brokertest --python 3.12
VIRTUAL_ENV=/tmp/brokertest uv pip install \
  "fastapi>=0.104.0" "kubernetes>=30.0.0,<35.0.0" "requests>=2.32.0" httpx
/tmp/brokertest/bin/python examples/cao-clusters/kubernetes/eks/test_broker.py
```

It covers the lease lifecycle over HTTP, broker and per-worker token checks,
the allowlisted callback/memory gateway, both reaper paths, the
`ownerReference` on the per-worker Service, and the startup check that refuses
to boot when a forwarded model pin is missing.
