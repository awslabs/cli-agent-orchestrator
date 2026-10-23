# One cao-server, one execution runtime (EKS)

Runs the first slice of
[#745](https://github.com/awslabs/cli-agent-orchestrator/issues/745) on Amazon
EKS: a central `cao-server` that runs no agents, and an execution runtime
(`cao-bridge`) that runs them. The runtime dials the server; everything is
driven through the server's HTTP API. See
[Execution runtimes](../../../../docs/execution-runtimes.md) for the design.

Which example to use: [`../eks/`](../eks/README.md) runs a supervisor pod that
runs agents itself, plus one disposable worker pod per `assign_elastic` call.
This one keeps every agent out of the server: long-lived runtimes run them,
and the server's API drives them.

| Object | Role |
|---|---|
| `StatefulSet/cao-server`, `Service/cao-server` | the API and central state, on a gp3 volume; `CAO_LOCAL_EXECUTION=0`, so it refuses to start agents itself |
| `StatefulSet/cao-runtime` | `cao-bridge` beside tmux and the provider CLIs; runtime id is the pod name, `cao-runtime-0` |
| `Secret/cao-runtime-token` | the shared channel token, mounted as a file into both pods (you create it) |
| `Secret/cao-api-token` | the API bearer token, given to the server only (you create it) |
| `NetworkPolicy/cao-server-ingress`, `NetworkPolicy/cao-runtime-ingress` | only the runtime may reach the server's API; the runtime accepts no ingress |

Every API call needs the bearer token from `cao-api-token`
(`CAO_AUTH_LOCAL_TOKEN` on the server); `/health` and the runtime channel do
not. The runtime pod has no API token, so agents there cannot drive the API
even though they can reach it. The NetworkPolicies are a second fence, and take
effect only on a cluster whose CNI enforces NetworkPolicy (on EKS, enable
network policy in the VPC CNI).

## Build

Both pods use the image from [`../eks/Dockerfile`](../eks/Dockerfile), which
contains `cao-server`, `cao-bridge`, tmux, Claude Code and `mock_cli`. Build and
push it as described in the [EKS guide](../eks/README.md#build), then point the
example at it: in `kustomization.yaml`, set `newName` to your repository and
`newTag` to your tag. With the `kustomize` CLI installed, this does the same:

```bash
cd examples/cao-clusters/kubernetes/remote-runtime
kustomize edit set image cao-node=<registry>/<repository>:<tag>
```

## Deploy

The server's state volume uses the `gp3` StorageClass. A fresh EKS cluster,
including one from [`../eks/iac/`](../eks/iac/), has only `gp2`, so create
`gp3` first if it is missing, or the server stays Pending:

```bash
kubectl get storageclass gp3 || kubectl apply -f ../eks/storageclass-gp3.yaml
```

Then:

```bash
kubectl apply -f namespace.yaml
kubectl -n cao-remote create secret generic cao-runtime-token \
  --from-literal=token="$(openssl rand -hex 32)"
kubectl -n cao-remote create secret generic cao-api-token \
  --from-literal=token="$(openssl rand -hex 32)"
kubectl apply -k .
kubectl -n cao-remote rollout status statefulset/cao-server
kubectl -n cao-remote rollout status statefulset/cao-runtime
```

The runtime is Ready while its channel to the server is up. If `cao-bridge`
cannot remove its readiness file once the channel goes, it exits, and so does
each restart until the file can be removed.

Claude Code on Bedrock needs AWS credentials in the runtime pod: an IAM role
that allows `bedrock:InvokeModel*`, given to the `cao-runtime` service account
in the `cao-remote` namespace. Either way works, and both work with the pod's
`automountServiceAccountToken: false`:

- EKS Pod Identity (needs the `eks-pod-identity-agent` add-on, and a role that
  trusts `pods.eks.amazonaws.com`):

  ```bash
  aws eks create-pod-identity-association --cluster-name <cluster> \
    --namespace cao-remote --service-account cao-runtime --role-arn <role-arn>
  ```

  The stack template in [`../eks/iac/`](../eks/iac/) creates associations for
  the `../eks/` example's service accounts only, none for `cao-remote` or
  `cao-runtime`: create this one yourself.
- IRSA, on a cluster with an IAM OIDC provider (the stack template creates
  none): uncomment the role annotation in `runtime.yaml`.

See [Provider credentials](../eks/README.md#provider-credentials).
`mock_cli` needs no credentials.

Every agent on a runtime runs as the same user, in the same filesystem, and
can read the runtime token (see
[Security](../../../../docs/execution-runtimes.md#security)): agents on one
runtime are not isolated from one another. To keep workers apart, give each
its own runtime (one terminal per runtime); this example runs one runtime.

## Drive it

```bash
kubectl -n cao-remote port-forward svc/cao-server 9889:9889 &
B=http://localhost:9889
AUTH="Authorization: Bearer $(kubectl -n cao-remote get secret cao-api-token \
  -o jsonpath='{.data.token}' | base64 -d)"

curl -s -H "$AUTH" $B/runtimes
T=$(curl -s -H "$AUTH" -X POST $B/runtimes/cao-runtime-0/terminals \
  -H 'Content-Type: application/json' \
  -d '{"agent_profile": "developer", "provider": "claude_code"}' \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["id"])')

curl -s -H "$AUTH" $B/terminals/$T                          # status: idle
curl -s -H "$AUTH" -X POST "$B/terminals/$T/input?message=Reply%20with%20one%20word%3A%20ok"
curl -s -H "$AUTH" $B/terminals/$T                          # processing, then completed
curl -s -H "$AUTH" "$B/terminals/$T/output?mode=last"
curl -s -H "$AUTH" -X DELETE $B/terminals/$T
```

Use `"provider": "mock_cli"` to try the path without a model. A call without
the token gets `401`.

## Verify

- The agent runs in the runtime pod and never in the server pod:
  `kubectl -n cao-remote exec cao-runtime-0 -- tmux ls` lists its session;
  `kubectl -n cao-remote exec cao-server-0 -- tmux ls` lists none.
- With the runtime stopped (`kubectl -n cao-remote scale statefulset/cao-runtime --replicas=0`),
  operations on its terminals return `503` and their status is `unknown`.
- Started again (`--replicas=1`), the replacement pod runs none of those
  terminals (the old pod's tmux went with it), so the server drops their
  records: `GET /terminals/{id}` answers `404`, and `GET /runtimes` no longer
  lists them.

## Cleanup

Note the server's volume first: `gp3` from `../eks/storageclass-gp3.yaml`
retains volumes, so deleting the claim leaves the PersistentVolume and its EBS
volume behind, still billed.

```bash
PV=$(kubectl -n cao-remote get pvc state-cao-server-0 -o jsonpath='{.spec.volumeName}')
VOL=$(kubectl get pv $PV -o jsonpath='{.spec.csi.volumeHandle}')
kubectl delete -k .
kubectl delete pv $PV
aws ec2 delete-volume --volume-id $VOL   # once it reads "available"
```

This deletes the namespace too, and with it both token Secrets. On a cluster
where `gp3` deletes volumes (`reclaimPolicy: Delete`), `kubectl delete -k .`
is enough.
