# Deploying the CAO fleet on Amazon EKS

These manifests run a CAO fleet in a Kubernetes cluster: a supervisor that
delegates work and worker pods that execute it, each running one agent.
Validated on EKS 1.35, arm64 nodes.

Values shown as placeholders (`<account-id>`, `<region>`, `<filesystem-id>`) are
not defaults — the manifests do not work until they are filled in. See
[Values to replace](#values-to-replace).

## What gets deployed

| Component | Kind | Replicas | Role |
|---|---|---|---|
| `cao-supervisor` | Deployment | 1 | Hosts the supervising agent, delegates to workers |
| `cao-worker` | StatefulSet | 1 | One agent per pod (`CAO_MAX_TERMINALS=1`), stable DNS names |

Worker `replicas` is 1 so that a first bring-up is as small as it can be while
still being a genuine cross-pod topology — it exercises `target_host` placement
and the `send_message` callback route. Raise it to 3 to exercise the two things a
second worker adds: a full worker answering 429, and the supervisor moving on to
the next one.

Storage is split in two tiers:

- **Shared workspace** — EFS at `/home/cao/workspace`, RWX, so every agent sees
  the same checkout.
- **Per-pod state** — EBS `gp3` at `/home/cao/.cao`, RWO, holding the SQLite
  database, agent store, logs, FIFOs and locks. This must not go on EFS: SQLite,
  `flock` and named pipes are unsafe over NFS.

| File | Purpose |
|---|---|
| `kustomization.yaml` | Entry point: resource list, namespace, image tag |
| `deploy.sh` | Renders the placeholders below from stack outputs, then applies |
| `namespace.yaml` | The `cao-cluster` namespace |
| `deployment-supervisor.yaml` | Supervisor pod, state volume, env, ServiceAccount |
| `statefulset-workers.yaml` | Worker pods, per-pod state volumes, ServiceAccount |
| `service-workers-headless.yaml` | Headless Service giving workers stable DNS |
| `efs-workspace.yaml` | Shared workspace StorageClass/PV/PVC |
| `networkpolicy.yaml` | Ingress and egress policies per role |
| `storageclass-gp3.yaml` | The `gp3` StorageClass for per-pod state volumes |
| `iac/infrastructure.yaml` | CloudFormation: every AWS resource — VPC, EKS cluster, add-ons, IAM, Pod Identity, ECR, EFS |

## How the agents authenticate

The provider is **Claude Code on Amazon Bedrock**, and it holds no API key. Both
pod specs set `CLAUDE_CODE_USE_BEDROCK=1`, so the CLI signs its model calls with
SigV4 using the pod's own AWS credentials, obtained from **EKS Pod Identity**.
Three consequences worth knowing before you deploy:

- **Pods carry an AWS identity.** `iac/infrastructure.yaml` creates one IAM role
  whose only permissions are Bedrock invoke, model-catalogue reads, and
  Marketplace subscribe scoped to `aws:CalledViaLast = bedrock.amazonaws.com`
  (Bedrock auto-initiates a Marketplace subscription the first time an account
  invokes an Anthropic model it has not activated).
- **The association is per service account, not per pod.** Pod Identity binds a
  role to a `(namespace, service account)` pair, so the supervisor and the whole
  worker StatefulSet need two associations even though they share one role. The
  `serviceAccountName` in each pod spec must match the `SupervisorServiceAccount`
  / `WorkerServiceAccount` stack parameters, or the pods get no credentials and
  every model call fails with an auth error rather than a scheduling error.
- **Credentials arrive over the network, not from a file.** The Pod Identity
  Agent serves them from `169.254.170.23:80`, which is why `networkpolicy.yaml`
  punches a `/32` hole in its link-local block. It is deliberately not a
  relaxation of `169.254.0.0/16`: `169.254.169.254` (IMDS, and therefore the node
  role) stays blocked.

Every model tier is pinned explicitly in both pod specs. Two reasons: Claude Code
falls back across tiers, and only Anthropic 4.5/4.6 models are usable in a
freshly vended account — 5.x returns `401 ... not available for this account`,
which no IAM change fixes. Only two distinct models are pinned across the four
variables, because activation is per model and each one costs a subscription
round-trip on first use.

Pinning has one consequence worth knowing about, because it shows up as a startup
hang rather than as anything model-shaped. When the account *can* invoke a model
newer than the pin, Claude Code opens with a dialog offering to repoint the pin
and restart — and its pre-selected answer is **Yes**. CAO declines it (`Esc`, in
`providers/claude_code.py`), which is the only safe answer unattended: accepting
rewrites the deployment's pins mid-initialization, and the newer model the probe
found on one tier is not necessarily entitled on the account the fleet actually
runs in. The refusal is persisted per tier-transition by the CLI itself, so it is
asked at most once per pod, not once per launch.

## Prerequisites

| # | Step | How |
|---|---|---|
| 1 | Local tools | `kubectl`, `docker`, AWS CLI |
| 2 | AWS resources — VPC, EKS cluster, add-ons, IAM, Pod Identity, ECR, EFS | `aws cloudformation deploy --template-file k8s/iac/infrastructure.yaml --stack-name cao-workshop --parameter-overrides ClusterAdminPrincipalArn=$(aws sts get-caller-identity --query Arn --output text) --capabilities CAPABILITY_NAMED_IAM` (~15 min) |
| 3 | kubectl access | `aws eks update-kubeconfig --region <region> --name cao-workshop` |
| 4 | Server image | Build and push — see [Building the image](#building-the-image) |

Notes on the steps that surprise people:

- **Step 2** creates everything AWS-side in one stack, including the network, so
  it works in a brand-new account with no VPC. It also enables NetworkPolicy
  enforcement in the VPC CNI, which is off by default and without which the
  policies in `networkpolicy.yaml` are accepted and then silently never enforced.
- **`ClusterAdminPrincipalArn` is effectively required.** EKS grants cluster-admin
  only to the principal that created the cluster — which, for a CloudFormation
  stack, is a CloudFormation-internal role no human can assume. Omit this and step
  3 succeeds while every `kubectl` call that follows returns
  `error: You must be logged in to the server (Unauthorized)`. Pass the ARN you
  will actually run `kubectl` as. It is optional in the template only so that a
  caller who manages access entries separately is not forced to supply it.
- **There is no secret to create and no operator to install.** Earlier revisions
  of these manifests needed External Secrets Operator, a Secrets Manager secret,
  a manually pasted provider API key, and an ESO restart to pick up its Pod
  Identity credentials. Bedrock SigV4 removes all four steps: there is no key.

### Building the image

`docker/Dockerfile` builds the whole thing in one pass — Claude Code installs
from the public npm registry, so nothing has to be vendored from an
authenticated local install and there is no second provider layer. The build
runs `claude --version` as its last step, so a broken CLI fails the build
instead of the pod.

It also seeds `~/.claude.json` with `hasCompletedOnboarding`. Every container is
a first run, and a first run opens the interactive theme picker, which is not a
dialog CAO knows how to answer — so without the seed `cao launch` fails with
`Claude Code initialization timed out` and the terminal is left in status
`unknown`. This has to be baked into the image: `~/.claude.json` is on the
container filesystem, not on the state PVC, so seeding it in a running pod is
undone by the next restart.

```bash
ECR=$(aws cloudformation describe-stacks --stack-name cao-workshop \
  --query "Stacks[0].Outputs[?OutputKey=='ServerRepositoryUri'].OutputValue" --output text)
aws ecr get-login-password | docker login --username AWS --password-stdin "${ECR%%/*}"

docker build -f docker/Dockerfile -t "$ECR:2.4.1-cc2" .
docker push "$ECR:2.4.1-cc2"
```

**Match the architecture of your nodes.** The node group defaults to `m7g`
(Graviton, arm64) with `AL2023_ARM_64_STANDARD`, so the image must be arm64. A
build on an arm64 machine needs no flags; from x86_64, add
`--platform linux/arm64` and expect QEMU emulation to make the npm install slow.
An architecture mismatch is not caught at deploy time — the pod pulls
successfully and then crash-loops with `exec format error`.

Push an immutable tag; the repository enforces it.

## Deploy

```bash
k8s/deploy.sh                      # stack cao-workshop, tag from kustomization.yaml
k8s/deploy.sh cao-workshop 2.4.1-cc2
```

The script reads the stack outputs, renders the placeholders into a temporary
directory, applies, and waits for both rollouts. It never edits the tree under
`k8s/`, and it aborts if any placeholder is left unrendered rather than applying
a manifest with a literal `<account-id>` in the image name — which would
otherwise surface much later as an `ImagePullBackOff`.

To do it by hand instead, four values need substituting:

| File | Change |
|---|---|
| `kustomization.yaml` | `<account-id>`/`<region>` in the image name, plus `newTag` |
| `deployment-supervisor.yaml` | `<account-id>`/`<region>` in the image name, `<region>` in `AWS_REGION` |
| `statefulset-workers.yaml` | Same two, plus `replicas` if not 1 |
| `efs-workspace.yaml` | `volumeHandle: <filesystem-id>::<access-point-id>` |

then `kubectl apply -k k8s`.

`AWS_REGION` is set explicitly rather than left to the default chain, and the
failure it prevents is a silent one rather than a loud one. The Pod Identity
webhook injects only `AWS_CONTAINER_CREDENTIALS_FULL_URI`,
`AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE` and `AWS_STS_REGIONAL_ENDPOINTS` — no
region. With neither `AWS_REGION` nor `AWS_DEFAULT_REGION` set, Claude Code does
not error; it falls back to a built-in default region and keeps working, so the
fleet appears healthy while every model call leaves the cluster's region. Both
variables are honoured when set (a bogus value fails with `ENOTFOUND`), so
setting `AWS_REGION` is enough.

Workers roll one at a time, so allow several minutes. First startup is slower
than steady state: the entrypoint makes one throwaway call per pinned model to
force Marketplace activation while the pod is starting anyway, rather than on a
participant's first prompt. Set `CAO_WARM_PROVIDER=0` to skip it.

Both pod specs also raise two CAO timeouts off their defaults, and both defaults
fail in a way that misdirects. `CAO_PROVIDER_INIT_TIMEOUT` (60 → 180) is the
server-side budget for bringing a provider REPL up; Claude Code is a ~330 MB
native binary and took ~45s just to draw its first frame in a cold pod, so 60s
failed intermittently and reported a timeout rather than slowness.
`CAO_MCP_REQUEST_TIMEOUT` (30 → 240) is the client-side HTTP read timeout, and it
has to outlast the init budget: otherwise `cao launch` prints
`Read timed out. (read timeout=30)` while the server goes on to create the
session successfully, so the CLI reports a failure for a launch that worked.
Check `cao session list` before believing a `cao launch` timeout.

## Verify

```bash
# All pods ready, no restarts
kubectl -n cao-cluster get pods

# Provider present on every node
for p in deploy/cao-supervisor cao-worker-0; do
  kubectl -n cao-cluster exec $p -- claude --version
done

# Pod Identity injected its env vars and the endpoint answers. Prints the HTTP
# status only — never the credentials in the response body.
kubectl -n cao-cluster exec deploy/cao-supervisor -- sh -c '
  echo "uri=${AWS_CONTAINER_CREDENTIALS_FULL_URI:-<UNSET — no association for this service account>}"
  curl -s -o /dev/null -w "pod-identity http=%{http_code} (want 200)\n" \
    -H "Authorization: $(cat "$AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE")" \
    "$AWS_CONTAINER_CREDENTIALS_FULL_URI"'

# IMDS must NOT be reachable: this should time out, not return a role name. A
# reply here means the egress NetworkPolicy is not being enforced, and a
# prompt-injected agent can take the node role.
kubectl -n cao-cluster exec deploy/cao-supervisor -- \
  curl -s -m 3 http://169.254.169.254/latest/meta-data/iam/security-credentials/ \
  ; echo "exit=$?  (non-zero is the expected result)"

# A real model call end to end — the load-bearing check, and the one that also
# proves the pod is using the AGENT role rather than the node role, since the
# node role holds no Bedrock permission at all. Note that a successful
# `bedrock-runtime converse` would NOT substitute for this: Claude Code calls the
# Invoke API, which is a different IAM action.
kubectl -n cao-cluster exec deploy/cao-supervisor -- \
  claude -p "Reply with the single word: ok"

# State on the persistent volume, owned 1000:1000 mode 0700
kubectl -n cao-cluster exec cao-worker-0 -- ls -ldn /home/cao/.cao/state

# Workspace is the same volume everywhere
kubectl -n cao-cluster exec deploy/cao-supervisor -- touch /home/cao/workspace/.probe
kubectl -n cao-cluster exec cao-worker-0 -- ls /home/cao/workspace/.probe
kubectl -n cao-cluster exec deploy/cao-supervisor -- rm /home/cao/workspace/.probe

# Each node resolves its own provider — cross-node placement depends on this
kubectl -n cao-cluster exec cao-worker-0 -- python -c \
  "from cli_agent_orchestrator.utils.agent_profiles import resolve_provider; \
   print(resolve_provider('developer', fallback_provider='mock_cli'))"   # want claude_code
```

Then an end-to-end run:

```bash
kubectl -n cao-cluster exec deploy/cao-supervisor -- \
  cao launch --agents code_supervisor --provider claude_code \
    --session-name demo --headless --auto-approve \
    --working-directory /home/cao/workspace

kubectl -n cao-cluster exec deploy/cao-supervisor -- cao session status cao-demo
```

`--session-name demo` creates a session named `cao-demo`; later commands need the
prefix.

## Operations

**Scale workers.** Update `replicas` and re-apply. Scale-down removes the highest
ordinal first; drain that worker beforehand. State volumes are retained.

**Roll out a new image.** Push a new immutable tag, update `newTag`, apply. Never
reuse a tag — a mutable tag can leave the cluster running an old build with
nothing to indicate the mismatch. A rollout restart ends running sessions: the
state database survives on the PVC, but the agent processes do not, so drain
first if agents are mid-task.

**Rotate the provider credentials.** Nothing to do. There is no long-lived
credential in the cluster — Pod Identity hands each pod short-lived credentials
and refreshes them in place. Changing what the agents may do is an IAM edit to
`AgentBedrockPolicy`, which takes effect without restarting anything.

**Change models.** Edit the four `ANTHROPIC_*` variables in both pod specs and
restart. Keep them in agreement across supervisor and workers, and remember each
newly introduced model needs its own Marketplace activation on first use.

**Shut down sessions.** `cao shutdown --all` is per node, so run it on each pod.

## Values to replace

| Value | Where it comes from |
|---|---|
| `<account-id>`, `<region>` | Your AWS account — image names in `kustomization.yaml` and both pod specs |
| Image repository | Stack output `ServerRepositoryUri` |
| Image tag | Chosen at build time → `kustomization.yaml` |
| `volumeHandle` | Stack output `WorkspaceVolumeHandle` → `efs-workspace.yaml` |
| `AWS_REGION` | The cluster's region → both pod specs |

Fixed by the image and safe to leave alone: container user `1000:1000`, server
port `9889`, state path `/home/cao/.cao/state`, workspace path
`/home/cao/workspace`.

Set by the stack, not by these manifests: the IAM role behind
`AgentBedrockRoleArn` and its two Pod Identity associations. If you rename the
namespace or either service account, change it in the stack parameters and the
pod specs together — a mismatch produces credential-less pods that look healthy
until the first model call.
