# Deploying the CAO fleet on Amazon EKS

These manifests run a CAO fleet in a Kubernetes cluster: a supervisor that
delegates work, worker pods that execute it, and a panel that observes them.
Validated on EKS 1.35.

Values shown as concrete IDs are examples — replace them with your own. See
[Values to replace](#values-to-replace).

## What gets deployed

| Component | Kind | Replicas | Role |
|---|---|---|---|
| `cao-supervisor` | Deployment | 1 | Hosts the supervising agent, delegates to workers |
| `cao-worker` | StatefulSet | 3 | One agent per pod (`CAO_MAX_TERMINALS=1`), stable DNS names |
| `cao-fleet-panel` | Deployment | 1 | Read-only web view of the fleet |

Storage is split in two tiers:

- **Shared workspace** — EFS at `/home/cao/workspace`, RWX, so every agent sees
  the same checkout.
- **Per-pod state** — EBS `gp3` at `/home/cao/.cao`, RWO, holding the SQLite
  database, agent store, logs, FIFOs and locks. This must not go on EFS: SQLite,
  `flock` and named pipes are unsafe over NFS.

| File | Purpose |
|---|---|
| `kustomization.yaml` | Entry point: resource list, namespace, image tags |
| `namespace.yaml` | The `cao-cluster` namespace |
| `deployment-supervisor.yaml` | Supervisor pod, state volume, env |
| `statefulset-workers.yaml` | Worker pods and their per-pod state volumes |
| `service-workers-headless.yaml` | Headless Service giving workers stable DNS |
| `deployment-fleet-panel.yaml` | Fleet panel and its authenticated probes |
| `configmap-fleet.yaml` | `fleet.json` registry the panel reads |
| `efs-workspace.yaml` | Shared workspace PV/PVC |
| `networkpolicy.yaml` | Ingress and egress policies per role |
| `external-secrets.example.yaml` | Credential pipeline — applied separately (needs ESO CRDs) |
| `iac/infrastructure.yaml` | CloudFormation: every AWS resource — VPC, EKS cluster, add-ons, secret, IAM, ECR, EFS |
| `iac/storageclasses.yaml` | The `gp3` and `efs-sc` StorageClasses the manifests reference |

## Prerequisites

| # | Step | How |
|---|---|---|
| 1 | Local tools | `kubectl`, `docker`, AWS CLI |
| 2 | AWS resources — VPC, EKS cluster, add-ons, provider secret, IAM, ECR, EFS | `aws cloudformation deploy --template-file k8s/iac/infrastructure.yaml --stack-name cao-workshop --capabilities CAPABILITY_NAMED_IAM` (~15 min) |
| 3 | kubectl access | `aws eks update-kubeconfig --region <region> --name cao-workshop` |
| 4 | StorageClasses `gp3` and `efs-sc` | `kubectl apply -f k8s/iac/storageclasses.yaml` |
| 5 | External Secrets Operator | Install into namespace `external-secrets` with Helm — see [external-secrets.io](https://external-secrets.io) |
| 6 | Provider API key | Secrets Manager console → the secret named in the stack output → Edit → **Plaintext** tab → paste the raw key alone. No JSON, quotes or trailing newline |
| 7 | Restart ESO | `kubectl -n external-secrets rollout restart deployment --all` |
| 8 | Panel token | `kubectl -n cao-cluster create secret generic cao-panel-secret --from-literal=token="$(openssl rand -hex 32)"` |
| 9 | Server image | Build and push — see [Building the image](#building-the-image) |
| 10 | Credential pipeline | `kubectl apply -f k8s/external-secrets.example.yaml` |

Notes on the steps that surprise people:

- **Step 2** creates everything AWS-side in one stack, including the network, so
  it works in a brand-new account with no VPC. It also enables NetworkPolicy
  enforcement in the VPC CNI, which is off by default and without which the
  policies in `networkpolicy.yaml` are accepted and then silently never enforced.
- **Step 6** is manual by design: the stack creates the secret *empty*, so
  forgetting this fails loudly instead of syncing a placeholder.
- **Step 7** is required because Pod Identity injects credentials through a
  webhook at pod creation, so ESO pods started before step 2 never receive them.
- **Step 10** is separate from `kustomization.yaml` because its objects need the
  ESO CRDs, which would make `kubectl apply -k k8s` fail on a cluster without ESO.
  Confirm the `ClusterSecretStore` is `Ready=True` and the `ExternalSecret` reports
  `SecretSynced` before deploying.

### Building the image

The base image ships only the credential-free `mock_cli` provider, so it builds
anywhere. A real provider is layered on top from an authenticated CLI install
directory — see [../docs/kiro-cli.md](../docs/kiro-cli.md). Push an immutable tag;
the server repository enforces it.

```bash
docker build -f docker/Dockerfile -t cao-server:base .
docker build -f docker/Dockerfile.provider --build-arg BASE_IMAGE=cao-server:base \
  -t <ServerRepositoryUri>:<tag> <kiro-cli-install-dir>
```

The panel image is expected to already exist in your registry; this repository
does not yet ship a Dockerfile for it.

## Deploy

Point the manifests at your environment first:

| File | Change |
|---|---|
| `kustomization.yaml` | Account and region in both image names, plus `newTag` |
| `efs-workspace.yaml` | `volumeHandle: <fs-id>::<access-point-id>` |
| `configmap-fleet.yaml` | One `machines` entry per worker replica |
| `statefulset-workers.yaml` | `replicas` if not 3 |

The worker count appears twice — StatefulSet `replicas` and the fleet ConfigMap.
They must agree, or the panel reports nodes that do not exist.

```bash
kubectl apply -k k8s
kubectl -n cao-cluster rollout status deployment/cao-supervisor --timeout=600s
kubectl -n cao-cluster rollout status statefulset/cao-worker   --timeout=900s
```

Workers roll one at a time, so allow several minutes.

## Verify

```bash
# All pods ready, no restarts
kubectl -n cao-cluster get pods

# Provider present and authenticated on every node
for p in deploy/cao-supervisor cao-worker-0 cao-worker-1 cao-worker-2; do
  kubectl -n cao-cluster exec $p -- kiro-cli whoami
done

# State on the persistent volume, owned 1000:1000 mode 0700
kubectl -n cao-cluster exec cao-worker-0 -- ls -ldn /home/cao/.cao/state

# Workspace is the same volume everywhere
kubectl -n cao-cluster exec deploy/cao-supervisor -- touch /home/cao/workspace/.probe
kubectl -n cao-cluster exec cao-worker-1 -- ls /home/cao/workspace/.probe
kubectl -n cao-cluster exec deploy/cao-supervisor -- rm /home/cao/workspace/.probe

# Each node resolves its own provider — cross-node placement depends on this
kubectl -n cao-cluster exec cao-worker-0 -- python -c \
  "from cli_agent_orchestrator.utils.agent_profiles import resolve_provider; \
   print(resolve_provider('developer', fallback_provider='mock_cli'))"   # want kiro_cli
```

Then an end-to-end run:

```bash
kubectl -n cao-cluster exec deploy/cao-supervisor -- \
  cao launch --agents code_supervisor --provider kiro_cli \
    --session-name demo --headless --auto-approve \
    --working-directory /home/cao/workspace

kubectl -n cao-cluster exec deploy/cao-supervisor -- cao session status cao-demo
```

`--session-name demo` creates a session named `cao-demo`; later commands need the
prefix.

To reach the panel, `kubectl -n cao-cluster port-forward deploy/cao-fleet-panel
8080:9888` and open `http://localhost:8080` with an `Authorization: Bearer
<token>` header. Unauthenticated requests return 401 by design.

## Operations

**Scale workers.** Update `replicas` and the ConfigMap `machines` list together,
then re-apply. Scale-down removes the highest ordinal first; drain that worker
beforehand. State volumes are retained.

**Roll out a new image.** Push a new immutable tag, update `newTag`, apply. Never
reuse a tag — a mutable tag can leave the cluster running an old build with
nothing to indicate the mismatch.

**Rotate the provider key.** Rotation is two hops and only the first is
automatic: ESO re-reads Secrets Manager on its refresh interval (1 hour), but
pods read environment variables only at startup, so a running pod keeps using the
old key until restarted — with no failing probe to signal it.

```bash
# 1. Update the value in Secrets Manager, then force an immediate sync
kubectl -n cao-cluster annotate externalsecret kiro-api-key force-sync="$(date +%s)" --overwrite

# 2. Confirm the Kubernetes Secret changed (compare fingerprints, never print the key)
kubectl -n cao-cluster get secret kiro-api-key -o jsonpath='{.data.KIRO_API_KEY}' \
  | base64 -d | sha256sum | cut -c1-12

# 3. Restart both workloads so the pods pick it up
kubectl -n cao-cluster rollout restart deployment/cao-supervisor statefulset/cao-worker
```

A rollout restart ends running sessions. The state database survives on the PVC,
but the agent processes do not, so drain first if agents are mid-task. The same
applies to the panel token: update `cao-panel-secret`, then restart the panel.

**Shut down sessions.** `cao shutdown --all` is per node, so run it on each pod.

## Values to replace

| Value | Where it comes from |
|---|---|
| `<account-id>`, region | Your AWS account — image names in `kustomization.yaml` and the pod specs |
| `volumeHandle` | Stack output `WorkspaceVolumeHandle` → `efs-workspace.yaml` |
| Image repositories | Stack outputs `ServerRepositoryUri`, `PanelRepositoryUri` → `kustomization.yaml` |
| Image tag | Chosen at build time → `kustomization.yaml` |
| Secret name | Stack output `ProviderSecretName` → `external-secrets.example.yaml` |
| IAM role | Stack output `ExternalSecretsRoleArn` → Pod Identity association |

Fixed by the image and safe to leave alone: container user `1000:1000`, server
port `9889`, panel port `9888`, state path `/home/cao/.cao/state`, workspace path
`/home/cao/workspace`.
