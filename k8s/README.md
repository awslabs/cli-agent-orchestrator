# Deploying the CAO fleet on Amazon EKS

This directory holds the Kubernetes manifests that run a CAO fleet in a cluster:
a supervisor that delegates work, worker pods that execute it, and the fleet
panel that observes them.

The steps below were validated end to end on EKS 1.35 in `ap-southeast-2`,
including a real `kiro_cli` agent performing a code review inside the cluster
and delegating work across nodes. Every value shown as a concrete ID belongs to
the account it was validated in and must be replaced — see
[Environment-specific values](#environment-specific-values).

The CAO-specific AWS resources are created by a CloudFormation stack; the cluster
itself is still manual. See [Automation status](#automation-status) for the
current split.

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

Locally: `kubectl`, `docker`, the AWS CLI, and an authenticated Kiro CLI install
directory to build the provider image.

On the cluster, owned by a separate cluster stack: EKS (validated on 1.35) with
add-ons `vpc-cni`, `coredns`, `kube-proxy`, `eks-pod-identity-agent`,
`aws-efs-csi-driver`, `aws-ebs-csi-driver`; StorageClasses `efs-sc` and `gp3`;
External Secrets Operator in namespace `external-secrets`. Allow ~1Gi requested
and 3Gi limit per pod. Verify NetworkPolicy enforcement is on — without it the
four shipped policies are silently inert (`kubectl -n kube-system get ds aws-node
-o yaml | grep enable-network-policy` must show `=true`).

Deploy the AWS resources with [`iac/cao-resources.yaml`](iac/cao-resources.yaml)
(provider secret, ESO role and Pod Identity association, ECR repositories, EFS
workspace), then read its stack outputs for the values the manifests need. The
template header documents every parameter and the ordering constraints.

```bash
aws cloudformation deploy --template-file k8s/iac/cao-resources.yaml \
  --stack-name cao-resources --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides ClusterName=<cluster> VpcId=<vpc> \
      NodeSecurityGroupId=<node-sg> WorkspaceSubnetId1=<subnet-a> \
      WorkspaceSubnetId2=<subnet-b> WorkspaceSubnetId3=<subnet-c>
```

Four steps remain manual:

1. **Set the secret value** — created empty deliberately, so a missed value fails
   loudly. Secrets Manager console → `cao/kiro-api-key` → Edit → **Plaintext**
   tab → paste the raw key alone. No JSON, quotes, or trailing newline: the whole
   value becomes `KIRO_API_KEY`.
2. **Restart ESO** so Pod Identity's creation-time webhook applies:
   `kubectl -n external-secrets rollout restart deployment --all`
3. **Create the panel token** (key named `token`, namespace must exist first):
   `kubectl -n cao-cluster create secret generic cao-panel-secret --from-literal=token="$(openssl rand -hex 32)"`
4. **Build and push both images** — the base ships only credential-free
   `mock_cli`, and the provider is a second layer built with the Kiro CLI install
   directory as context. Push an immutable tag; `cao-server` enforces it.
   ```bash
   docker build -f docker/Dockerfile -t cao-server:base .
   docker build -f docker/Dockerfile.provider --build-arg BASE_IMAGE=cao-server:base \
     -t <repo-uri>:<tag> ~/.toolbox/tools/kiro-cli/<version>/
   ```

Finally `kubectl apply -f k8s/external-secrets.example.yaml` — excluded from
`kustomization.yaml` because its objects need the ESO CRDs. Confirm the
`ClusterSecretStore` is `Ready=True` and the `ExternalSecret` reports
`SecretSynced` before deploying.

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
kubectl -n cao-cluster port-forward deploy/cao-fleet-panel 8080:9888
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

**Shut down sessions.** `cao shutdown --all` is per node, so run it on each pod.

---

## Rotating the provider key

Rotation takes two hops — Secrets Manager to the Kubernetes Secret, then the
Secret into the pods — and **only the first happens on its own**. Environment
variables are read once when a container starts, so a running pod keeps serving
the old credential indefinitely after the Secret changes.

That staleness is silent. Nothing degrades, no probe fails, and the pod stays
`Ready` with zero restarts, because everything inside it consistently uses the
value it started with. Verified directly: after rotating a token, the Kubernetes
Secret held the new value while the running pod still accepted only the old one
and rejected the new one with 401. Treat the restart as part of rotation, not as
an optional follow-up.

1. **Update the value** in Secrets Manager (console or
   `aws secretsmanager put-secret-value`). Same rules as the initial paste: raw
   value, no JSON wrapper, no trailing newline.

2. **Pull it into the cluster.** ESO re-reads on its `refreshInterval` — 1 hour
   as shipped — so force it when you do not want to wait:

   ```bash
   kubectl -n cao-cluster annotate externalsecret kiro-api-key \
     force-sync="$(date +%s)" --overwrite
   ```

3. **Confirm the Secret actually changed** before restarting anything, so a
   failed sync is not mistaken for a failed rollout. Compare fingerprints rather
   than printing the key:

   ```bash
   kubectl -n cao-cluster get externalsecret kiro-api-key \
     -o jsonpath='{.status.refreshTime}{"  "}{.status.conditions[0].type}={.status.conditions[0].status}{"\n"}'
   kubectl -n cao-cluster get secret kiro-api-key \
     -o jsonpath='{.data.KIRO_API_KEY}' | base64 -d | sha256sum | cut -c1-12
   ```

4. **Restart both workloads** so the pods pick it up:

   ```bash
   kubectl -n cao-cluster rollout restart deployment/cao-supervisor statefulset/cao-worker
   kubectl -n cao-cluster rollout status statefulset/cao-worker --timeout=900s
   ```

   Drain first if agents are mid-task — a rollout restart kills running sessions,
   and while the state database survives on the PVC, the tmux processes do not.

5. **Verify the pods now hold the new value** and can still authenticate:

   ```bash
   for p in deploy/cao-supervisor cao-worker-0 cao-worker-1 cao-worker-2; do
     kubectl -n cao-cluster exec $p -- \
       sh -c 'printf %s "$KIRO_API_KEY" | sha256sum | cut -c1-12'   # match step 3
     kubectl -n cao-cluster exec $p -- kiro-cli whoami
   done
   ```

Two ways to remove the manual restart, neither currently wired up:

- **A reload controller** (for example Stakater Reloader) watches the Secret and
  restarts the referencing workloads automatically. This is the pragmatic option,
  at the cost of another cluster component.
- **Mounting the Secret as a volume** instead of injecting env vars: kubelet
  refreshes projected secret files in place, so no restart is needed. This does
  not help here, because the provider CLI reads its key only from
  `KIRO_API_KEY`, with no file-based equivalent — it would require support in the
  provider first.

Rotating the panel token follows the same shape: update `cao-panel-secret`, then
`kubectl -n cao-cluster rollout restart deployment/cao-fleet-panel`. Until that
restart, the panel keeps accepting the previous token and rejects the new one.

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

Most of these now come from the `cao-resources` stack outputs rather than being
edited by hand. The "validated" column records the account this was proven in —
treat those IDs as examples, never defaults.

| Value | Validated | Source |
|---|---|---|
| Region | `ap-southeast-2` | Deploy-time; `ClusterSecretStore` |
| Account | `418295698799` | Deploy-time |
| `volumeHandle` (EFS) | `fs-0fe4b2ecc1b06bb67::fsap-0e72f7af255f8b90c` | Stack output `WorkspaceVolumeHandle` → `efs-workspace.yaml` |
| ECR image names | `…dkr.ecr.ap-southeast-2.amazonaws.com/cao-server` | Stack outputs `ServerRepositoryUri`, `PanelRepositoryUri` → `kustomization.yaml` |
| Server image tag | `2.4.1-d0b398d-d3fix-kiro2.18.1-r2` | You choose at build time → `kustomization.yaml` |
| Secret name / ARN | `cao/kiro-api-key` (suffix `-W6LdT4`) | Stack outputs `ProviderSecretName`, `ProviderSecretArn` |
| IAM role | `cao-external-secrets-role` | Stack output `ExternalSecretsRoleArn` |
| Namespace | `cao-cluster` | `kustomization.yaml` |

Fixed by the image and safe to leave alone: container user `1000:1000`, server
port `9889`, panel port `9888`, state path `/home/cao/.cao/state`, workspace path
`/home/cao/workspace`.

---

## Automation status

**Done — CAO-specific AWS resources.** [`iac/cao-resources.yaml`](iac/cao-resources.yaml)
owns the provider secret, the ESO IAM role and managed policy, the Pod Identity
association, both ECR repositories, and the EFS workspace with its access point,
security group and mount targets. It is deliberately parameterised over the
cluster, VPC, subnets and node security group so it can be applied to a cluster
it did not create.

**Not yet — the cluster itself.** The VPC, EKS cluster, node capacity, managed
add-ons, StorageClasses and the External Secrets Operator installation remain
manual. These are shared infrastructure that usually outlives any one
application, which is why they are a separate stack rather than part of the one
above: CAO can then be torn down and redeployed without touching the cluster.

**Never automated.** Two steps stay manual by nature — pasting the API key value,
and building the provider image, which layers a non-redistributable CLI from an
authenticated local install. The stack therefore expects a pre-existing image tag
and creates the secret empty.

The manifests in this directory stay as kustomize output and are not in scope for
either stack.

