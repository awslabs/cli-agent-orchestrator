# CAO Elastic Workers

One central `cao-server`, one persistent execution pod, one narrow broker, and
one disposable single-replica Deployment per `assign_elastic` call. The server
owns every terminal row and all durable CAO memory on its EBS claim; the pods
that run agents hold no CAO state at all (`emptyDir`) and expose no inbound
port. Everything mounts the shared EFS workspace.

The thing to understand before reading anything else: **"where the agent runs"
and "who owns the orchestration state" are two different pods here.** The
`cao-supervisor` pod runs the participant's agent and nothing else. It reaches
the server over one outbound WebSocket, and the server reaches it the same way —
there is no address at which anything dials `cao-supervisor`.

## Remote execution bridge (CAO 3.0, #745)

This is the topology this example deploys. **One central `cao-server` serves thin
execution-only `cao-bridge` runtimes** — ten agents mean one server, not eleven.
See `docs/issues/745-remote-execution-boundary/design.md` for the full contract.

**What ships and is validated (slice 1 + shared MCP):**

- `cao-bridge` (the `CAO_NODE_MODE=bridge` entrypoint branch) is an
  execution-only runtime: it holds one persistent **outbound** WebSocket to the
  central server's `\/runtime\/channel`, runs the provider beside its own tmux,
  and streams output/status up while commands (launch, input, key, extract,
  teardown) come down with `op_id` correlation and acknowledged retained
  results. No per-worker `cao-server`, no per-worker Service.
- The central server exposes `POST /runtimes/{id}/terminals`, `GET /runtimes`,
  and routes input/output/status/delete for a remote terminal over its bound
  channel. In remote mode the server never touches tmux — it runs in a
  container with no tmux binary.
- Shared **cao-mcp-server** HTTP hosting (`CAO_MCP_TRANSPORT=http`) resolves the
  caller's terminal identity per authenticated request instead of a
  process-global `CAO_TERMINAL_ID`.

**Configuration (bridge worker):** `CAO_NODE_MODE=bridge`,
`CAO_BRIDGE_SERVER_URL=ws://cao-server:9889/runtime/channel`,
`CAO_BRIDGE_RUNTIME_ID=<unique>`, `CAO_RUNTIME_TOKEN=<shared secret>`. The
central server needs the same `CAO_RUNTIME_TOKEN` (fail-closed: unset → the
channel refuses all connections) and durable state on a `ReadWriteOnce` PVC
with `CAO_HOME_DIR` pointed at the mount and an `fsGroup` matching the `cao`
uid (1000), so terminal rows survive a restart.

**Supported scope of the bridge slice:** launch, input, key, output extraction,
worker-derived status, teardown, and multi-worker routing for the existing
`TerminalBackend` terminal path (provider-agnostic — proven with `mock_cli` and
`claude_code`). A server restart preserves the DB row and rebuilds
terminal→runtime routing from each runtime's reconnect (`hello`) snapshot; a
lost worker yields an explicit `503 runtime not connected` rather than false
success.

**Python workflow scripts** also execute in a runtime rather than the server
host: set `CAO_SCRIPT_RUNTIME=<runtime id>` on the central server and
`CAO_ADVERTISED_URL` so script callbacks resolve to the shared Service. The
server keeps journal/cancel/generation ownership; a disconnected runtime is an
explicit failure, never a false success.

**Broker bridge mode (#745 step 4):** set `CAO_ELASTIC_WORKER_MODE=bridge` and
`CAO_ELASTIC_CENTRAL_URL` on the broker (see `broker.yaml`), and give the
namespace a `cao-runtime-token` Secret shared with the central server. The
broker then mints execution-only workers — `cao-bridge` dialing the central
channel, **no per-worker Service, no worker HTTP API** — and the lease carries
`mode: bridge` + `runtime_id`, which `assign_elastic` routes through the
central `POST /runtimes/{id}/terminals` instead of a worker's `/sessions`.
Worker pods get `CAO_API_HOST`/`CAO_API_PORT`/`CAO_MEMORY_API_URL` pointed at
the central server so the agent's MCP tools (handoff, send_message,
complete_assignment, memory — including `store_lesson`) operate on central
state. Readiness means "runtime connected" (observed via `GET /runtimes` by
both the reaper and the caller's wait), and the `cao worker` operator proxy
keeps its allowlist but answers from the central server, scoped so one
worker's id can never resolve another runtime's terminals or an unfiltered
session list. `CAO_ELASTIC_WORKER_MODE=server` (the default) preserves the
existing full-server topology byte-for-byte.

**CLI against a shared server:** export `CAO_API_BASE_URL=<server url>` and
`cao schedule`/`cao memory` read/change the server's state over HTTP (never a
client-local database), `cao launch` works end-to-end both headless and
interactive — `--runtime <id>` places the terminal on a named execution runtime
when the central server itself hosts no tmux — and operations that need the
server's own filesystem (`cao terminal restore`, `cao memory
repair/import/...`) fail with an explicit error instead of silently operating on
local state.

**Interactive attach across the pod boundary:** the PTY is spawned in the worker
pod, beside its tmux socket, and the server relays bytes over the runtime
channel. `/terminals/{id}/ws` keeps its exact client-facing protocol, so the
browser terminal cannot tell a remote agent from a local one, and interactive
`cao launch` against a shared server attaches through that same endpoint with no
client-local tmux. A terminal whose runtime is not connected closes `4010`
rather than appearing to attach.

**Not yet in this slice:** per-runtime delegated credentials (#774) replacing
the shared `CAO_RUNTIME_TOKEN`. Read [What moved, and what it
costs](#what-moved-and-what-it-costs) before deciding this topology is strictly
better than the one it replaces — one property genuinely regressed.

---

## Topology

| Component | Kubernetes kind | Storage | Inbound port | Lifecycle |
|---|---|---|---|---|
| `cao-server` | StatefulSet, one replica, `OnDelete` | EBS state + shared EFS workspace (read-only) | 9889 (API), 9891 (shared MCP) | Persistent |
| `cao-supervisor` | StatefulSet, one replica | `emptyDir` state + shared EFS workspace | **none** | Persistent |
| `cao-worker-broker` | Deployment, one replica | None | 9890 | Persistent |
| `cao-worker-<id>` | Deployment, one replica, one per assignment | `emptyDir` state + shared EFS workspace | **none** in bridge mode | Released on callback |

Only `cao-server` has an API. The supervisor and every minted worker are
`cao-bridge` runtimes (`CAO_NODE_MODE=bridge`): they dial
`ws://cao-server:9889/runtime/channel` outbound and receive launch, input, key,
extract, attach and teardown commands back down that one connection. The
`cao-supervisor` Service still exists — a StatefulSet requires a `serviceName` —
but it is headless and portless, so there is nothing to dial even from inside the
namespace.

The broker creates each worker Deployment; in bridge mode it creates **no
per-worker Service**, because nothing addresses a worker. Its narrow callback and
memory gateway is still there and is still what a worker-side script would use,
but a bridge worker's MCP tools reach `cao-server` directly (see below).

`CAO_ELASTIC_WORKER_MODE=server` on the broker restores the pre-#745 shape —
a full `cao-server` and a Service per worker — for a cluster that has no
`cao-runtime-token` Secret. Nothing else in this directory assumes it.

Four properties are worth understanding before changing anything here.

**The broker is a security boundary, not a convenience.** It is the only pod in
the namespace that can reach the Kubernetes API, and it is the one that runs no
agent. The request it accepts has exactly two fields, `agent_profile` and
`provider`, both bounded by `^[a-zA-Z0-9_-]{1,64}$`; image, command, volumes,
service account and resource limits are all broker-controlled. So a
prompt-injected agent can ask for "a worker running the reviewer profile" and
cannot ask for "a privileged pod mounting the host filesystem". Its Role has no
`pods/exec` and no `secrets` — it cannot shell into a worker it created, nor read
the token it authenticates callers with. The lease binds the only callback
receiver and the worker's memory session/profile context; the broker validates or
replaces those identity fields rather than trusting the worker's request. Local
CAO remains authentication-free by default; this credential and routing behavior
activates only inside cluster pods.

Note one change here: the broker now takes its lease requests from `cao-server`,
not from the supervisor pod. Scheduling is a server decision, so the pod that
runs the agent has no broker URL and no network path to port 9890 at all.

<a id="what-moved-and-what-it-costs"></a>
**A pod that runs an agent can no longer be told apart from a caller at the
network layer.** This is the one property #745 gave up, and it is worth stating
plainly rather than discovering from a policy file. Before, workers could not
reach the control API — the broker's five-route gateway was the only way in, and
that was enforced by NetworkPolicy. Now every execution pod holds a channel *to*
`cao-server` and its MCP tools (handoff, send_message, complete_assignment,
memory) dial `cao-server:9889` directly, so `cao-server-ingress` must admit them.
A prompt-injected agent can therefore reach routes the gateway used to withhold,
bounded only by the shared `CAO_RUNTIME_TOKEN` and the server's per-request
caller identity — an application-layer boundary where there used to also be a
network one. #774 closes this by replacing the shared token with per-runtime
delegated credentials. Until then, treat the token as what it is: possession of
it is possession of the control API.

What the move bought in exchange: the pod a participant talks through holds no
durable state, so it can be replaced without replacing the conversation; ten
agents need one server rather than ten; and the terminal registry has exactly one
writer, guarded by a lock rather than by hoping.

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

<a id="the-shared-mcp-endpoint"></a>
## The shared MCP endpoint

The supervisor's agent does not run its own MCP server. It talks to one that runs
as the `cao-mcp` sidecar in the `cao-server` pod, on port 9891.

This exists because of a contradiction the rest of #745 creates. Taking a worker
lease is a scheduling decision, so `CAO_ELASTIC_BROKER_URL` and
`CAO_ELASTIC_BROKER_TOKEN` live only on `cao-server` and the supervisor has no
network path to port 9890. But `assign_elastic` is an MCP *tool*, and a tool runs
wherever the MCP server runs — so with a local MCP server in the supervisor pod,
delegation failed in the pod that holds no credentials:

```
Elastic assignment failed: elastic workers are not configured: set
CAO_ELASTIC_BROKER_URL and CAO_ELASTIC_BROKER_TOKEN on the supervisor
```

The fix is to move the tool, not the credential. One variable does it:

```yaml
- name: CAO_MCP_HTTP_URL
  value: "http://cao-server.cao-cluster.svc.cluster.local:9891/mcp"
```

With that set, `utils/mcp_resolution.py` launches the provider's declared
`cao-mcp-server` as `cao-mcp-stdio-bridge` instead — a stdio shim that registers
no tools and holds no state, forwarding every call to the shared endpoint with
this terminal's id as the caller identity and `CAO_RUNTIME_TOKEN` as the
credential. No provider code knows the endpoint exists; the substitution happens
at the one resolver every provider already funnels through, so it applies to
Claude Code, Copilot, OpenCode, Kiro and Antigravity alike.

What that buys: a pod running a command-capable agent can call a delegation tool
without ever holding the credential that delegation needs. The tool executes in
the server pod, under the server's identity, having already proved it holds the
runtime token.

Three details that are deliberate and easy to get wrong:

- **The sidecar is in the server pod, not a Deployment of its own.** Its tools
  read and write the same SQLite state the server owns, and that state is on a
  `ReadWriteOnce` PVC no second pod can mount. Same pod, same volume, one writer
  set.
- **Port 9891, not the code default 9890.** 9890 is the broker's port everywhere
  else here, and two services answering on one number is a debugging trap.
- **Minted workers are NOT pointed at it.** `complete_assignment` reads
  `CAO_ELASTIC_WORKER_ID` and `CAO_ELASTIC_RELEASE_TOKEN` from the process it runs
  in, and those are minted per worker. Forward a worker's tools and the call
  executes in the server pod, finds neither, and answers "complete_assignment is
  only available inside an elastic worker" — breaking the result path. A worker
  keeps a local MCP server, and gives up nothing: it is handed no broker *token*,
  so it cannot lease further workers either way.

Unset `CAO_MCP_HTTP_URL` and every pod goes back to a local MCP server. That is
the default, and it is what a local (non-cluster) CAO install always does.

To check the wiring on a running cluster:

```bash
# The endpoint answers, and refuses an unauthenticated caller (401/406, not a
# hang — a hang here means NetworkPolicy, not MCP).
kubectl -n cao-cluster exec cao-supervisor-0 -c cao-node -- \
  curl -si -m 5 -o /dev/null -w '%{http_code}\n' \
  http://cao-server.cao-cluster.svc.cluster.local:9891/mcp

# The agent's own config points at the shim, not at cao-mcp-server.
kubectl -n cao-cluster exec cao-supervisor-0 -c cao-node -- \
  sh -lc 'cat ~/.cao/state/*/mcp.json 2>/dev/null || cat ~/.claude.json' | grep -i mcp
```

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
_STATIC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
```

An installed wheel does not preserve that layout, so the panel would start and
then serve 404 for every asset.

## Provider credentials

Claude Code on Bedrock is the default and needs no credential plumbing at all:
each pod signs its own requests with SigV4 from its EKS Pod Identity association,
so there is nothing to store, synchronise or rotate. The one secret in the
namespace is the broker token, minted locally by `deploy.sh`.

Two account-level prerequisites are easy to miss, because both fail as a
`403 … not authorized to perform: bedrock:InvokeModel` *inside* the agent rather
than at deploy time:

- **Grant the inference profile, not just the foundation model.** Accounts where
  the Anthropic models are only reachable through a cross-region inference
  profile need `ANTHROPIC_MODEL=us.anthropic.claude-…` and a policy resource
  covering `arn:aws:bedrock:*:<account>:inference-profile/us.anthropic.*`. A
  policy scoped to `foundation-model/anthropic.*` alone is denied.
- **Confirm the pod actually receives the association's credentials.** Check for
  the injected `AWS_CONTAINER_CREDENTIALS_FULL_URI` in a pod on the target
  service account; if it is absent, the pod silently falls back to the *node*
  role, and the 403 names the node instance role rather than yours. On a cluster
  where Pod Identity injection is unavailable, project a web-identity token
  explicitly instead — a `serviceAccountToken` volume with audience
  `sts.amazonaws.com` plus `AWS_ROLE_ARN` and `AWS_WEB_IDENTITY_TOKEN_FILE`
  needs no admission webhook. Set `CAO_ELASTIC_WORKER_IRSA_ROLE_ARN` on the
  broker and it adds exactly that to every worker pod it mints; leave it unset
  and workers rely on Pod Identity as before. The role's trust policy needs
  `sts:AssumeRoleWithWebIdentity` from the cluster's OIDC provider, conditioned
  on `:aud = sts.amazonaws.com` and `:sub = system:serviceaccount:<ns>:<sa>`.

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
  Regenerating it would leave a running server holding a token the broker no
  longer accepts, and every delegation would 401 with nothing visibly changed.
- mints `cao-runtime-token` on the same terms, and for a stronger version of the
  same reason: it is the credential every execution pod authenticates its channel
  with. Rotating it under a running fleet does not degrade anything gracefully —
  the server refuses the reconnect, and each executor's live terminals become
  unreachable while the pod itself stays up. Both the server and the executors
  read it as a **required** `secretKeyRef`, so a namespace without it holds those
  pods in `CreateContainerConfigError` rather than starting a server that accepts
  nobody.
- refuses to apply over the pre-#745 single-node layout instead of trying. The
  supervisor StatefulSet dropped its `volumeClaimTemplates` and its Service became
  headless, and Kubernetes forbids updating either field, so the script prints the
  exact `kubectl delete` to run and stops. It deletes nothing itself: that
  StatefulSet may be mid-task, and its `state-cao-supervisor-0` PVC holds the only
  copy of the conversations the old topology kept there. That PVC is left alone by
  both the guard and the delete it suggests.
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
kubectl -n cao-cluster rollout status statefulset/cao-server
kubectl -n cao-cluster rollout status statefulset/cao-supervisor
kubectl -n cao-cluster rollout status deployment/cao-worker-broker
kubectl -n cao-cluster rollout status deployment/cao-fleet-panel
```

There are no worker pods at rest, and on the default Bedrock path no
`externalsecret` either — both are expected. So is a `cao-supervisor` Service
with no `CLUSTER-IP` and no `PORT(S)`: it exists only to satisfy the
StatefulSet's `serviceName`.

`cao-supervisor` reaching Ready is a stronger statement than it looks, and it is
the check to make after any change to the channel, the token or the policies. The
pod serves no HTTP, so there is nothing to `GET`; its probe is
`test -f /home/cao/.cao/state/bridge-connected`, a marker `cao-bridge` writes
**after** the server accepts its `hello` and removes the moment the channel drops.
Ready therefore means "the server can run an agent here", not "a process
started". Ask the server the same question from the other side:

```bash
kubectl -n cao-cluster exec cao-server-0 -- \
  curl -fsS -H 'Host: localhost' http://localhost:9889/runtimes
```

Every execution pod should appear with a recent heartbeat. A supervisor stuck
`0/1` with the marker absent is almost always one of three things, in this order:
the `cao-runtime-token` Secret differs between the two pods (the server logs
`runtime channel authentication failed`), `cao-server-ingress` does not admit the
pod (the bridge logs connect timeouts and backs off), or the provider install
ahead of the handshake has not finished yet — check `kubectl logs` before assuming
the first two.

There is deliberately no liveness probe on that marker. It is legitimately absent
while a bridge backs off through a server restart, and restarting the pod for that
would destroy live terminals to fix a connection that is already retrying.

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

Run those two against `cao-supervisor-0` and not `cao-server-0`. The server is
deliberately not a Pod Identity subject — no provider CLI is installed there and
nothing in it calls a model — so the agent would not answer for it, and that is
the correct result rather than a broken binding.

The second check is the one that matters. `169.254.170.23` hands out this pod's
scoped credentials, whose only permission is Bedrock invoke; `169.254.169.254`
hands out the **node role's**, which include ECR pull and the CNI's ENI
permissions. The egress policies punch a `/32` for the former and leave the
latter blocked — widening that to `169.254.0.0/16` would hand a prompt-injected
agent the node role.

## Replacing the server pod

`cao-server` is the single writer of the terminal registry, and the manifest makes
that enforceable rather than aspirational: it takes an exclusive `flock` on its
state directory at startup (`services/server_owner.py`) and refuses to serve if
another process holds it —

```
another cao-server already owns /home/cao/.cao/state/... (pid 1 on cao-server-0,
started ...). Only one server may own a state directory: two would write the same
database and answer for the same runtimes.
```

`replicas: 1` does not prevent that second server by itself. A rolling update
overlaps pods on purpose: the replacement starts while the outgoing pod is still
terminating, and with both on the same PVC the new one would hit the lock and
`CrashLoopBackOff` until the old one finished — which reads as a broken image
rather than as the guard doing its job. So the StatefulSet is
`updateStrategy: type: OnDelete`, and replacement is a deliberate, non-overlapping
act:

```bash
# 1. Apply the new spec. Nothing restarts; OnDelete means the controller waits.
examples/cao-clusters/kubernetes/eks/deploy.sh cao-workshop "${TAG}"

# 2. Check what is running before you take it away. Every live session is
#    served by this pod, and an open lease is settled by it.
kubectl -n cao-cluster exec cao-server-0 -- \
  curl -fsS -H 'Host: localhost' http://localhost:9889/sessions

# 3. Delete the pod. The controller recreates it from the new spec only after
#    this one is fully gone, so the lock is free when the replacement opens it.
kubectl -n cao-cluster delete pod cao-server-0

# 4. Wait for the new pod, then confirm every executor came back on its own.
kubectl -n cao-cluster rollout status statefulset/cao-server --timeout=600s
kubectl -n cao-cluster exec cao-server-0 -- \
  curl -fsS -H 'Host: localhost' http://localhost:9889/runtimes
```

Step 4 is the part worth watching. The executors are not restarted and do not need
to be: each `cao-bridge` reconnects with backoff and its `hello` carries a snapshot
of the terminals it holds, from which the server rebuilds terminal→runtime routing.
Terminal rows survive in SQLite on the PVC, and the pods holding the tmux sessions
never stopped, so a server replacement costs the reconnect window rather than the
conversations. During that window the executors read `0/1` — their marker is gone
because the channel is — and an attach closes `4010` instead of appearing to work.

Two things the lock does not need: a stale-lock expiry (the kernel releases an
`flock` when the holder dies, including on SIGKILL) and an opt-out in this
namespace. `CAO_SERVER_OWNER_LOCK=0` exists for developing against one state
directory with two servers locally, and is a documented footgun rather than a
supported topology — do not set it here.

Replacing an **executor** pod is the opposite case and needs no procedure: delete
it, and its terminals die with its tmux server. The state that survives is the
registry row on `cao-server`, not the session, which is why the supervisor's state
volume is an `emptyDir` — it makes "nothing durable lives in the pod that runs the
agent" a property of the manifest instead of a claim in this README.

<a id="restart-limitations"></a>
### Restart limitations

Stated as a list because the difference between "survives" and "resumes" is where
the surprises are.

| Event | What survives | What does not |
|---|---|---|
| `cao-server` replaced | Terminal rows on the PVC; every executor's tmux session and running agent; routing, rebuilt from each `hello` snapshot, including each terminal's status | The reconnect window: pods read `0/1`, an attach closes `4010`, and a call to a route that needs a runtime gets `503 runtime not connected` |
| `cao-supervisor` replaced | The terminal rows and the conversation history in central state | **The live session.** tmux dies with the pod, so a running agent turn is lost. There is no automatic resumption — nothing re-launches the agent or replays its turn |
| A worker pod replaced | The lease record on the broker, which the reaper settles | The assignment. A worker is per-task and is not meant to be replaced; a worker lost mid-task is reaped and reported, not retried |

The middle row is the honest limitation of this slice. Durable *state* moved to
the server, which is what makes a supervisor pod replaceable at all, but a live
agent process is not state and does not move with it. Replacing a supervisor pod
costs the profile install (~45s for Claude Code), two Bedrock warm-ups, and any
in-flight turn. Plan a supervisor replacement for a quiet moment the same way you
would plan the server's, and check for live work first:

```bash
kubectl -n cao-cluster exec cao-server-0 -- \
  curl -fsS -H 'Host: localhost' http://localhost:9889/sessions
```

<a id="version-compatibility"></a>
## Version compatibility and upgrades

Every component in this namespace runs the **same image**, and that is the
supported configuration. The runtime channel carries an explicit
`PROTOCOL_VERSION` (`runtime_channel/protocol.py`, currently `1`), checked for
**equality** on both sides of the `hello` exchange — there is no negotiation and no
compatibility window.

| Pairing | Result |
|---|---|
| Server and bridge on the same image | Supported |
| Server and bridge on different images, same `PROTOCOL_VERSION` | Works, unsupported. Nothing checks anything else, so a route one side does not implement fails at call time rather than at connect time |
| Different `PROTOCOL_VERSION` | Refused at `hello`, before any work is accepted |
| Bridge with no `CAO_RUNTIME_TOKEN`, or the wrong one | Refused at connect (401/403) |

What a version mismatch actually looks like matters, because it is not a crash.
The server answers with its own `hello` and closes `1002`; the bridge raises
`protocol version mismatch: server N, bridge M`, and its reconnect loop treats
that like any other connection failure — it backs off and retries, with the
readiness marker cleared in `finally`. So the pod **never becomes Ready and is
never dispatched work**: it sits at `0/1`, logs the mismatch on every attempt, and
keeps whatever tmux sessions it already had. Nothing half-works.

An auth failure (401/403) is the one case handled differently: it is re-raised
rather than retried, because retrying a rejected credential is noise. `cao-bridge`
is PID 1, so the container exits and the pod goes to `CrashLoopBackOff` with
`runtime channel authentication rejected` in its logs — a loud failure for a
misconfiguration that no amount of waiting fixes.

That gives the upgrade procedure its shape: the mismatch window is safe but not
free, and a pod that is `0/1` for a long time is the thing to look for.

```bash
# 1. Build and push ONE tag, and apply it everywhere. deploy.sh renders every
#    manifest from the same tag, which is what keeps this from drifting.
examples/cao-clusters/kubernetes/eks/deploy.sh cao-workshop "${TAG}"

# 2. Drain: check for live sessions and open leases before replacing anything.
kubectl -n cao-cluster exec cao-server-0 -- \
  curl -fsS -H 'Host: localhost' http://localhost:9889/sessions
kubectl -n cao-cluster exec deploy/cao-worker-broker -- \
  curl -fsS -H "X-CAO-Broker-Token: $(kubectl -n cao-cluster get secret \
    cao-elastic-broker-token -o jsonpath='{.data.token}' | base64 -d)" \
  http://localhost:9890/workers

# 3. Executors first, server last. An old bridge against a new server is refused
#    at hello and retries; replacing the executors first means the window is
#    spent on pods that are already being replaced.
kubectl -n cao-cluster rollout restart statefulset/cao-supervisor
kubectl -n cao-cluster rollout status statefulset/cao-supervisor --timeout=900s

# 4. Then the server, by the OnDelete procedure above.
kubectl -n cao-cluster delete pod cao-server-0
kubectl -n cao-cluster rollout status statefulset/cao-server --timeout=600s

# 5. Confirm the fleet reassembled. Every executor must appear here; one that
#    does not is either 0/1 (look for the mismatch or auth log) or gone.
kubectl -n cao-cluster exec cao-server-0 -- \
  curl -fsS -H 'Host: localhost' http://localhost:9889/runtimes
kubectl -n cao-cluster get pods -l app.kubernetes.io/part-of=cao-elastic-fleet
```

Rollback is the same procedure with the previous tag, and it is safe in the
direction that matters: the state PVC is the only thing not replaced, and schema
migrations are forward-only. So rolling **back** across a migration is not
supported — a server on an older image against a migrated database is the one
combination to avoid. Keep the tag you are rolling back to, and if the rollback
crosses a schema change, snapshot the EBS volume first:

```bash
kubectl -n cao-cluster get pvc state-cao-server-0 \
  -o jsonpath='{.spec.volumeName}{"\n"}'
aws ec2 create-snapshot --volume-id <handle> --description 'pre-rollback'
```

A mismatched bridge is diagnosed from its own logs, not from the server's:

```bash
kubectl -n cao-cluster logs cao-supervisor-0 -c cao-node --tail=40 | \
  grep -i 'protocol\|hello\|401\|403'
```

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

The fleet view lists `cao-server` from `configmap-fleet.yaml`, plus whichever
workers hold a lease. The panel cannot discover a worker on its own -- each is
created on demand with a generated id -- so the broker publishes each one into that ConfigMap
when it leases it and withdraws it on release. That ConfigMap is therefore
jointly owned, and the live object differing from the checked-in file is expected
rather than drift.

The single checked-in entry is the server, not the supervisor, and that is a
consequence of the topology rather than a naming choice: every terminal in the
fleet is owned and served by `cao-server`, so it is the only host with an API to
probe. Worker entries the broker adds in bridge mode name that same host for the
same reason, which is what makes a worker's terminals visible in the panel
without a per-worker Service.

A worker is published when it is placed, not when it is ready, because a lease
asserts placement and `POST /workers` no longer waits for readiness. The panel
probes what it is given, so a worker still booting shows as unreachable for a few
seconds. The alternative -- gating the fleet view on `CAO_ELASTIC_GATE_ON_READY`
-- would hide every worker on the default non-blocking path.

The panel re-reads the file on every request, so no restart is needed. A mounted
ConfigMap refreshes on the kubelet's sync period; in testing it appeared within
15 seconds.

Re-running `deploy.sh` resets that ConfigMap to the server alone. The broker
republishes on the next lease, but a worker running at that moment drops off the
view until it is released, so avoid re-deploying while a fleet is busy.

## Reading the lease ledger

The broker holds a ledger of every lease it has issued and why each one ended.
This is the endpoint to read after a delegation that claimed success and produced
nothing:

Run it from `cao-server-0`, not from the supervisor pod. The supervisor holds no
broker token and `cao-supervisor-egress` does not allow 9890 — an execution pod
reaching the one privileged API in the namespace is exactly what #745 removed.

```bash
TOKEN="$(kubectl -n cao-cluster get secret cao-elastic-broker-token \
  -o jsonpath='{.data.token}' | base64 -d)"
kubectl -n cao-cluster exec cao-server-0 -- \
  curl -s -H "X-CAO-Broker-Token: ${TOKEN}" http://cao-worker-broker:9890/workers
```

| `state` | Meaning |
|---|---|
| `leased` | Open. A worker is running and has not called back. |
| `completed` | The worker called `complete_assignment`. The normal path. |
| `terminated` | The pod ended while the lease was open. Usually the turn-detection race above: the task was **not** necessarily done, and the supervisor's own transcript shows a clean success. |
| `expired` | The pod was still healthy but never completed within `CAO_ELASTIC_COMPLETION_TIMEOUT` (900s). |
| `failed` | The lease never opened — the Deployment could not be created, or the pod never became Ready inside `CAO_ELASTIC_READY_TIMEOUT`. In bridge mode "Ready" means its runtime channel is registered on `cao-server` (observed through `GET /runtimes`), not that a per-worker Service answered `/health`. |

A `terminated` or `expired` entry also means the broker released the worker on
your behalf. Without that reaper the pod squats a node's worth of memory until the
orphan sweep collects it, an hour later (`CAO_ELASTIC_WORKER_TIMEOUT`). That sweep
is the backstop for a broker restart: the ledger is in memory, so a restarted
broker cannot settle a lease it never made, and the sweep walks the cluster instead
of the ledger to find those workers. It is also why the broker is pinned to
`replicas: 1` with a `Recreate` strategy — a second reaper would know only its own
half of the ledger.

## Run a demo assignment

Starts a `code_supervisor` agent **on** the supervisor runtime by asking
`cao-server` for it, which creates a producer worker and a delayed consumer
worker. The producer stores a project memory; the consumer recalls it from the
server-owned memory service.

Note the shape of the request: it is `POST /runtimes/cao-supervisor-0/terminals`
on the central server, not `POST /sessions` on the supervisor. The path names
where the agent runs, the host names who owns the record, and those are now two
different pods. A runtime that is not connected answers `404 runtime
'...' is not connected` rather than starting anything.

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
kubectl -n cao-cluster exec -i cao-server-0 -- python - <<'PY'
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
    "http://localhost:9889/runtimes/cao-supervisor-0/terminals",
    json={
        "agent_profile": "code_supervisor",
        "provider": "claude_code",
        "working_directory": "/home/cao/workspace",
        "initial_message": task,
    },
    # Generous: the launch travels down the channel, the supervisor spawns the
    # provider beside its own tmux, and only then does the ack come back.
    timeout=180,
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
terminal id printed above. Both reads go to the server: the bytes were captured in
the supervisor pod and streamed up the channel, and the server answers from that
stream, which is why nothing here dials the pod that produced them.

```bash
kubectl -n cao-cluster exec -i cao-server-0 -- env TERMINAL_ID="<id>" python - <<'PY'
import os, requests
r = requests.get(
    f"http://localhost:9889/terminals/{os.environ['TERMINAL_ID']}/output",
    params={"mode": "full"}, timeout=30)
r.raise_for_status()
print(r.json()["output"])
PY

kubectl -n cao-cluster exec -i cao-server-0 -- env TERMINAL_ID="<id>" python - <<'PY'
import json, os, requests, time
url = f"http://localhost:9889/terminals/{os.environ['TERMINAL_ID']}/inbox/messages"
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
is on the **server's** EBS volume, and both workers should be gone because each
called `complete_assignment`:

```bash
kubectl -n cao-cluster exec cao-server-0 -- \
  cao memory show elastic-demo-shared --scope project
kubectl -n cao-cluster get deployments,services -l app.kubernetes.io/name=cao-elastic-worker
```

In bridge mode there were never any per-worker Services to disappear, so that
second command showing Deployments gone and no Services at all is the expected
result rather than a partial cleanup.

If the workers are gone but the memory is absent, read the lease ledger: a
`terminated` entry is the turn-detection race, not a memory bug.

```bash
kubectl -n cao-cluster exec cao-server-0 -- \
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
