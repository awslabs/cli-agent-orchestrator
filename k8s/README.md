# Deploying the CAO fleet on Amazon EKS

This directory holds the Kubernetes manifests that run a CAO fleet in a cluster:
a supervisor that delegates work, worker pods that execute it, and the fleet
panel that observes them.

The steps below were validated end to end on EKS 1.35 in `ap-southeast-2`,
including a real `kiro_cli` agent performing a code review inside the cluster
and delegating work across nodes. Every value shown as a concrete ID belongs to
the account it was validated in and must be replaced — see
[Environment-specific values](#environment-specific-values).

Prerequisites are currently manual. They will be replaced by infrastructure as
code; see [Planned automation](#planned-automation) for what changes and what
does not.

---

## What gets deployed

| Component | Kind | Replicas | Role |
|---|---|---|---|
| `cao-supervisor` | Deployment | 1 | Hosts the supervising agent, delegates to workers |
| `cao-worker` | StatefulSet | 3 | One agent per pod (`CAO_MAX_TERMINALS=1`), stable DNS names |
| `cao-fleet-panel` | Deployment | 1 | Read-only web view of the fleet |

Supporting objects: a namespace, the fleet registry ConfigMap, a headless
Service giving each worker a stable hostname, four NetworkPolicies, a shared
EFS workspace (RWX), and a per-pod EBS state volume (RWO).

Two storage tiers, deliberately separated:

- **Shared workspace** — EFS at `/home/cao/workspace`, RWX, so every agent sees
  the same checkout.
- **Per-pod state** — EBS `gp3` at `/home/cao/.cao`, RWO, holding the SQLite
  database, agent store, logs, FIFOs and lock files. This must **not** live on
  EFS: SQLite, `flock`, and named pipes are all unsafe over NFS.

### Manifests in this directory

| File | Purpose |
|---|---|
| `kustomization.yaml` | Entry point: resource list, namespace, image tags |
| `namespace.yaml` | The `cao-cluster` namespace |
| `deployment-supervisor.yaml` | Supervisor pod, its state volume, and env |
| `statefulset-workers.yaml` | Worker pods and their per-pod state volumes |
| `service-workers-headless.yaml` | Headless Service giving each worker stable DNS |
| `deployment-fleet-panel.yaml` | Fleet panel and its authenticated probes |
| `configmap-fleet.yaml` | `fleet.json` registry the panel reads |
| `efs-workspace.yaml` | Shared workspace PV/PVC (holds the EFS IDs) |
| `networkpolicy.yaml` | Four policies: ingress and egress per role |
| `external-secrets.example.yaml` | Credential pipeline — **excluded** from `kustomization.yaml`, applied separately (needs ESO CRDs) |

---

## Prerequisites

### 0. Local tooling

`kubectl` (with built-in kustomize), `docker`, and the AWS CLI, plus credentials
for the target account. To build the provider image you also need an
authenticated Kiro CLI install directory on the build host.

### 1. EKS cluster and add-ons

An EKS cluster (validated on 1.35, `authenticationMode: API_AND_CONFIG_MAP`)
with these managed add-ons:

| Add-on | Why it is required |
|---|---|
| `vpc-cni` | Pod networking, **and** NetworkPolicy enforcement |
| `coredns` | Service DNS — workers are addressed by DNS name |
| `kube-proxy` | Service routing |
| `eks-pod-identity-agent` | Delivers AWS credentials to the ESO controller |
| `aws-efs-csi-driver` | Mounts the shared workspace |
| `aws-ebs-csi-driver` | Provisions per-pod state volumes |
| `metrics-server` | Optional; enables `kubectl top` |

**NetworkPolicy enforcement is not on by default.** The manifests ship four
policies, but without enforcement they are inert — silently, with no error.
Verify:

```bash
kubectl -n kube-system get ds aws-node \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="aws-eks-nodeagent")].args}' \
  | tr ',' '\n' | grep enable-network-policy
# expect: "--enable-network-policy=true"
```

Node capacity: each pod requests 1Gi and may use up to 3Gi, so plan for roughly
4Gi of headroom across the fleet plus the panel. Nodes with under ~2Gi
allocatable cannot schedule a CAO pod at all.

### 2. Storage classes

Two classes are required, named as the manifests reference them:

```bash
kubectl get sc
# efs-sc  efs.csi.aws.com          <- shared workspace
# gp3     (EBS provisioner)        <- per-pod state
```

> On the validated cluster `gp3` uses the legacy in-tree provisioner
> (`kubernetes.io/aws-ebs`) and works only because CSI migration translates it
> to `ebs.csi.aws.com`. For a new cluster, create the class against the CSI
> provisioner directly rather than relying on migration.

### 3. EFS file system, access point, and mount targets

Create an encrypted EFS file system and an access point that pins POSIX
ownership to the container user:

| Setting | Validated value | Notes |
|---|---|---|
| File system | `fs-0fe4b2ecc1b06bb67` | Encrypted, `generalPurpose`, `elastic` throughput |
| Access point | `fsap-0e72f7af255f8b90c` | Root path `/cao-workspace` |
| POSIX user | uid `1000`, gid `1000` | **Must match** the image's `cao` user |
| Mount targets | one per AZ (`2a`, `2b`, `2c`) | Pods cannot mount in an AZ with no mount target |

The access point's uid/gid is what makes the shared volume writable without
granting root. A mismatch here surfaces as permission errors inside
`/home/cao/workspace`.

The mount target security group must allow inbound NFS (TCP 2049) from the node
security group.

Record both IDs — they go into `efs-workspace.yaml`.

### 4. ECR repositories and images

Two repositories: `cao-server` and `cao-fleet-panel`.

The base image ships only `mock_cli`, which echoes prompts rather than doing
work, so it needs no credentials and builds anywhere. A real provider is added
as a second layer. Build both stages:

```bash
# Stage 1 — base image
docker build -f docker/Dockerfile -t cao-server:base .

# Stage 2 — layer in an authenticated Kiro CLI.
# The build context MUST be the Kiro CLI install directory.
docker build -f docker/Dockerfile.provider \
  --build-arg BASE_IMAGE=cao-server:base \
  -t cao-server:<immutable-tag> \
  ~/.toolbox/tools/kiro-cli/<version>/
```

Push under an **immutable tag** and set it in `kustomization.yaml`. Do not
deploy `latest`: on this cluster a mutable `latest` silently ran a build that
predated cross-node placement while the manifests advertised it, which is
difficult to diagnose because nothing errors.

The provider image is not redistributable. Keep it in a private registry.

### 5. Provider API key in Secrets Manager (AWS Console)

The key never appears in a manifest. It lives in Secrets Manager and reaches the
pods as a native Kubernetes Secret.

1. Open **AWS Secrets Manager** → **Store a new secret**.
2. Choose **Other type of secret**.
3. Select the **Plaintext** tab and replace the contents with the raw API key
   only — no JSON, no quotes, no trailing newline. The whole value is projected
   verbatim into the `KIRO_API_KEY` environment variable, so stray characters
   become part of the key.
4. Name it `cao/kiro-api-key`.
5. Leave encryption on the AWS-managed key (`aws/secretsmanager`). A
   customer-managed key works but then the IAM policy in step 6 also needs
   `kms:Decrypt` on that key.
6. Skip rotation. Create the secret and copy its full ARN — it includes a
   six-character suffix (for example `…:secret:cao/kiro-api-key-W6LdT4`) that
   the IAM policy must match exactly.

To rotate later, update the secret value in the console. ESO refreshes the
Kubernetes Secret on its own, but pods read environment variables only at
startup, so a rollout restart is required:

```bash
kubectl -n cao-cluster rollout restart deployment/cao-supervisor statefulset/cao-worker
```

### 6. IAM role for External Secrets Operator

The role is attached to the **ESO controller, not the CAO pods**. This is
deliberate: CAO agents execute arbitrary commands by design, so giving those
pods an AWS identity would let a prompt-injected agent inherit it. With ESO the
agent pods read a plain Kubernetes Secret and hold no AWS credentials at all.

Create role `cao-external-secrets-role` with this trust policy — note Pod
Identity requires `sts:TagSession` alongside `sts:AssumeRole`:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "EksPodIdentity",
    "Effect": "Allow",
    "Principal": { "Service": "pods.eks.amazonaws.com" },
    "Action": ["sts:AssumeRole", "sts:TagSession"]
  }]
}
```

Attach an inline policy scoped to the single secret ARN — no wildcards:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "ReadCaoProviderSecretOnly",
    "Effect": "Allow",
    "Action": ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
    "Resource": "arn:aws:secretsmanager:<region>:<account>:secret:cao/kiro-api-key-XXXXXX"
  }]
}
```

### 7. External Secrets Operator

Install ESO (validated on v2.9.0) into namespace `external-secrets`, then
associate the role with its service account:

```bash
aws eks create-pod-identity-association \
  --cluster-name <cluster> \
  --namespace external-secrets \
  --service-account external-secrets \
  --role-arn arn:aws:iam::<account>:role/cao-external-secrets-role
```

**Then restart the ESO controller.** Pod Identity injects credentials through a
mutating admission webhook that runs at *pod creation*, so pods already running
when the association was created never receive them — they fall back to the node
instance role and fail. See [Troubleshooting](#troubleshooting) for the
signature of this failure.

```bash
kubectl -n external-secrets rollout restart deployment --all
```

Now apply the store and the secret projection. This file is **not** part of
`kustomization.yaml`, because its objects require the ESO CRDs — including it
would make `kubectl apply -k k8s` fail on any cluster without ESO:

```bash
# Edit the region and secret name first if they differ.
kubectl apply -f k8s/external-secrets.example.yaml
```

Confirm the pipeline works before deploying CAO:

```bash
kubectl get clustersecretstore aws-secretsmanager   # want Ready=True
kubectl -n cao-cluster get externalsecret kiro-api-key  # want SecretSynced=True
kubectl -n cao-cluster get secret kiro-api-key       # created by ESO
```

`ClusterSecretStore` intentionally carries no `auth:` block, so the AWS SDK's
default credential chain picks up Pod Identity.

### 8. Fleet panel token

The panel's token guards its entire origin. Create it before deploying:

```bash
kubectl -n cao-cluster create secret generic cao-panel-secret \
  --from-literal=token="$(openssl rand -hex 32)"
```

The key must be named `token`. The namespace must exist first — either apply
`namespace.yaml` alone, or create this secret after step 2 of the deployment.

---

## Deploy

### 1. Point the manifests at your environment

Edit these before applying:

| File | Change |
|---|---|
| `efs-workspace.yaml` | `volumeHandle: <fs-id>::<access-point-id>` |
| `kustomization.yaml` | ECR registry/account in both `name:` fields, plus `newTag` |
| `configmap-fleet.yaml` | One `machines` entry per worker replica |
| `statefulset-workers.yaml` | `replicas` if not 3 |

The worker count appears in two places — StatefulSet `replicas` and the fleet
ConfigMap. They must agree, or the panel shows nodes that do not exist (or
misses ones that do).

### 2. Apply

```bash
kubectl apply -k k8s
kubectl -n cao-cluster rollout status deployment/cao-supervisor --timeout=600s
kubectl -n cao-cluster rollout status statefulset/cao-worker   --timeout=900s
```

Workers roll one at a time, so allow several minutes.

### 3. Verify

All pods `Ready` with no restarts:

```bash
kubectl -n cao-cluster get pods
```

The provider is present and authenticated on every node:

```bash
for p in deploy/cao-supervisor cao-worker-0 cao-worker-1 cao-worker-2; do
  kubectl -n cao-cluster exec $p -- kiro-cli whoami
done
```

State is on the persistent volume with the right ownership — `1000:1000`, mode
`0700`, and the seven state subdirectories present:

```bash
kubectl -n cao-cluster exec cao-worker-0 -- \
  sh -c 'ls -ldn /home/cao/.cao/state && ls /home/cao/.cao/state'
```

The shared workspace is the same volume everywhere:

```bash
kubectl -n cao-cluster exec deploy/cao-supervisor -- touch /home/cao/workspace/.probe
kubectl -n cao-cluster exec cao-worker-1 -- ls /home/cao/workspace/.probe
kubectl -n cao-cluster exec deploy/cao-supervisor -- rm /home/cao/workspace/.probe
```

Each node resolves the intended provider itself — this is what cross-node
placement depends on:

```bash
kubectl -n cao-cluster exec cao-worker-0 -- python -c \
  "from cli_agent_orchestrator.utils.agent_profiles import resolve_provider; \
   print(resolve_provider('developer', fallback_provider='mock_cli'))"
# expect: kiro_cli  (a 'mock_cli' result means the provider was not persisted)
```

Egress is bounded — public HTTPS reachable, instance metadata not:

```bash
kubectl -n cao-cluster exec cao-worker-0 -- sh -c '
  curl -sS -o /dev/null -w "public 443: %{http_code}\n" https://example.com
  curl -sS -m 5 -o /dev/null http://169.254.169.254/ 2>/dev/null \
    && echo "IMDS: REACHABLE (unexpected)" || echo "IMDS: blocked (expected)"'
```

Finally, an end-to-end run. Launch a supervisor session, then confirm a worker
picks up delegated work:

```bash
kubectl -n cao-cluster exec deploy/cao-supervisor -- \
  cao launch --agents code_supervisor --provider kiro_cli \
    --session-name demo --headless --auto-approve \
    --working-directory /home/cao/workspace

kubectl -n cao-cluster exec deploy/cao-supervisor -- cao session status cao-demo
```

`cao launch --session-name demo` creates a session named **`cao-demo`** — later
commands need the prefix.

### 4. Reach the panel

```bash
kubectl -n cao-cluster port-forward deploy/cao-fleet-panel 8080:8080
```

Then open `http://localhost:8080` with an `Authorization: Bearer <token>`
header. Unauthenticated requests return 401 by design.

---

## Operations

**Scale workers.** Update `replicas` in `statefulset-workers.yaml` *and* the
`machines` list in `configmap-fleet.yaml`, then re-apply. Scaling down removes
the highest ordinal first; drain that worker before shrinking. State volumes are
retained on scale-down.

**Roll out a new image.** Push a new immutable tag, update `newTag`, apply, and
watch both rollouts. Never reuse a tag.

**Rotate the API key.** Update the secret value, then restart both workloads
(see step 5 above).

**Shut down sessions.** `cao shutdown --all` is per node, so run it on each pod.

---

## Troubleshooting

**`AccessDeniedException` naming a node role.** If ESO logs an error like
`User: arn:aws:sts::…:assumed-role/KarpenterNodeRole-…/i-… is not authorized to
perform: secretsmanager:GetSecretValue`, the IAM policy is almost certainly
fine. The role named is the *node instance role*, meaning the pod never received
Pod Identity credentials — it started before the association existed. Restart
the workload rather than editing IAM:

```bash
kubectl -n external-secrets rollout restart deployment --all
```

**`Kiro capability probe for engine 'v2' returned unusable help output`.** The
provider image is incomplete. `kiro-cli` is a launcher that execs
`kiro-cli-chat` and `kiro-cli-term`, resolved through `PATH` rather than
relative to its own location, so copying only the launcher produces an image
where `--version` and `--help` succeed but `chat --help` fails with a bare
`No such file or directory (os error 2)`. Copy the whole install tree and put
all three executables on `PATH`; `docker/Dockerfile.provider` does this and
smoke-tests `chat --help` at build time.

**Pod restarts with exit code 137 / `OOMKilled`.** Memory limits too low for a
real provider. An idle pod uses ~80Mi, but a running agent settles around
660Mi because the provider maps in a large binary. Limits below ~2Gi are unsafe;
the manifests use 1Gi requests and 3Gi limits.

**Sessions vanish after a restart.** Confirm `CAO_HOME_DIR` points into the
mounted volume. If it is unset, CAO writes to
`~/.aws/cli-agent-orchestrator` on the container filesystem — the PVC is mounted
but unused, and every restart discards the database:

```bash
kubectl -n cao-cluster exec cao-worker-0 -- sh -c 'echo $CAO_HOME_DIR; du -sh /home/cao/.cao'
```

**Pod crashes at startup writing to `/home/cao/.cao`.** The EBS volume is
presented as `root:root 0755`, so the unprivileged `cao` user cannot create
anything under it. The pod spec needs `securityContext.fsGroup: 1000`. Note that
`CAO_HOME_DIR` must point at a *subdirectory* of the mount, not the mount root —
CAO's `chmod(0700)` is best-effort and silently fails against a root-owned
directory, leaving the state tree group-readable.

**Panel restart-loops with 401s.** Probes must be `exec`, not `httpGet`.
Kubernetes probe `httpHeaders` values are static strings with no `valueFrom`, so
a bearer token cannot be injected there.

**Remote assignment fails asking for `CAO_ADVERTISED_URL`.** Only the node that
*originates* a remote assignment needs this variable; it is the URL it advertises
so results can route back. The supervisor sets it. Workers do not, because in a
star topology they never originate. To enable worker-to-worker delegation, set
it on the workers **and** add a `cao-worker` selector to the `cao-worker-ingress`
policy — otherwise the callback is dropped and looks like a hang.

**NetworkPolicies appear to do nothing.** Enforcement is off; see prerequisite 1.

---

## Environment-specific values

Every value below is specific to the validated account and must be changed.

| Value | Validated | Where |
|---|---|---|
| Region | `ap-southeast-2` | Secret ARN, `ClusterSecretStore` |
| Account | `418295698799` | ECR image names, IAM/secret ARNs |
| ECR registry | `418295698799.dkr.ecr.ap-southeast-2.amazonaws.com` | `kustomization.yaml` |
| Server image tag | `2.4.1-d0b398d-d3fix-kiro2.18.1-r2` | `kustomization.yaml` |
| EFS file system | `fs-0fe4b2ecc1b06bb67` | `efs-workspace.yaml` |
| EFS access point | `fsap-0e72f7af255f8b90c` | `efs-workspace.yaml` |
| Secret name / ARN | `cao/kiro-api-key` (suffix `-W6LdT4`) | IAM policy, `ExternalSecret` |
| IAM role | `cao-external-secrets-role` | Pod Identity association |
| Namespace | `cao-cluster` | `kustomization.yaml` |

Fixed by the image and safe to leave alone: container user `1000:1000`, server
port `9889`, state path `/home/cao/.cao/state`, workspace path
`/home/cao/workspace`.

---

## Planned automation

Prerequisites 1 through 8 are all AWS-side or cluster-bootstrap resources and are
the intended scope of an IaC deployment (eksctl config, CloudFormation, or CDK):
the cluster and its add-ons, storage classes, the EFS file system with its access
point and mount targets, ECR repositories, the Secrets Manager secret, the IAM
role, the Pod Identity association, and the ESO installation.

The manifests in this directory are **not** in scope — they stay as kustomize
output, and the deploy steps above do not change. Once IaC exists, the
prerequisites section collapses to a pointer at it, while this runbook remains
the reference for verifying and debugging what the automation produced.

Two things IaC cannot remove, because they are genuinely manual: entering the
API key value (a human must paste the secret), and building the provider image
(it layers a non-redistributable CLI from an authenticated install).
