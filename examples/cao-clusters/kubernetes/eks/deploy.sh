#!/usr/bin/env bash
# Render the placeholders in these manifests from CloudFormation stack outputs,
# then apply them.
#
# The manifests are checked in with placeholders (<server-image>, <broker-image>,
# <panel-image>, <account-id>, <region>, <filesystem-id>, <access-point-id>,
# <vpc-cidr>) rather than real values, because the real values differ per account
# and a checked-in account number is a trap.
#
# The three image placeholders are whole repository URIs read from the stack
# outputs, not just an account and a region. The repositories are named
# ${NamePrefix}-server and friends, so a manifest spelling out `cao-server` was
# correct only while NamePrefix kept its default. Under any other prefix every pod
# went ImagePullBackOff, and this script printed the right URIs while applying the
# wrong ones: it read the outputs and used them for nothing but the account number.
# Editing four files by hand before every deploy is the alternative this replaces.
#
# It also generates the broker token. That secret is NOT checked in and NOT read
# from the stack: it is minted here on first run and left alone afterwards, so
# re-running this script does not invalidate the token a running supervisor
# already holds.
#
# Usage:
#   examples/cao-clusters/kubernetes/eks/deploy.sh [stack-name] [image-tag] [mode]
#
# Modes: bedrock (default), kiro, codex.
# Defaults: stack cao-workshop, tag taken from kustomization.yaml, mode bedrock.
# Honours the usual AWS_PROFILE / AWS_REGION environment.
#
# Pass `-` as the stack name to deploy onto a cluster this repo's template did
# not create, supplying the stack's six values through the environment instead:
#
#   CAO_SERVER_REPO_URI  CAO_BROKER_REPO_URI  CAO_PANEL_REPO_URI
#   CAO_WORKSPACE_HANDLE (fs-<id>::fsap-<id>)  CAO_CLUSTER_NAME  CAO_VPC_CIDR
#
# Rendering happens into a temporary directory; this source directory is never
# modified, so a failed run leaves nothing to clean up and `git status` stays
# clean.
set -euo pipefail

STACK="${1:-cao-workshop}"
MODE="${3:-bedrock}"
K8S_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "$MODE" in
  bedrock)
    ;;
  kiro)
    ;;
  codex)
    ;;
  *)
    echo "error: mode must be 'bedrock', 'kiro' or 'codex' (got '$MODE')" >&2
    exit 1
    ;;
esac

out() {
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

# `|| true` is load-bearing. `aws configure get region` exits 1 - rather than
# returning empty with status 0 - when no region is set in ~/.aws/config, so
# under the `set -e` above this line aborted the whole script before either
# fallback below could run. The script then died before its first echo, which
# made a misconfigured box look like a command that silently did nothing:
# no output, no namespace, no pods. Observed on a real deployment.
REGION="$(aws configure get region || true)"
[ -n "${AWS_DEFAULT_REGION:-}" ] && REGION="$AWS_DEFAULT_REGION"
[ -n "${AWS_REGION:-}" ] && REGION="$AWS_REGION"
[ -n "$REGION" ] || { echo "error: no region — set AWS_REGION" >&2; exit 1; }

# Existing-cluster mode. Pass `-` as the stack name and supply the six values the
# stack would otherwise have produced.
#
# This exists because the stack is not the only way to get a cluster, and until
# now it was the only way to run this script: every input came from
# `describe-stacks`, so deploying onto a cluster somebody else built had no
# documented path at all. The manifests never cared where the values came from.
#
# Everything the fleet needs from the environment is in these six. See
# "Deploying onto an existing cluster" in the README for how to obtain each.
if [ "$STACK" = "-" ] || [ "$STACK" = "none" ]; then
  echo "existing-cluster mode: reading inputs from the environment"
  REPO="${CAO_SERVER_REPO_URI:-}"
  BROKER_REPO="${CAO_BROKER_REPO_URI:-}"
  PANEL_REPO="${CAO_PANEL_REPO_URI:-}"
  HANDLE="${CAO_WORKSPACE_HANDLE:-}"
  CLUSTER="${CAO_CLUSTER_NAME:-}"
  VPC_CIDR="${CAO_VPC_CIDR:-}"
  missing=""
  for v in CAO_SERVER_REPO_URI CAO_BROKER_REPO_URI CAO_PANEL_REPO_URI \
           CAO_WORKSPACE_HANDLE CAO_CLUSTER_NAME CAO_VPC_CIDR; do
    eval "val=\${$v:-}"
    [ -n "$val" ] || missing="$missing $v"
  done
  if [ -n "$missing" ]; then
    echo "error: existing-cluster mode needs:$missing" >&2
    exit 1
  fi
  # The same shape the stack output has, checked here because a handle without
  # the access point mounts the filesystem root instead of the subdirectory and
  # every pod then shares one uid-mapped tree.
  case "$HANDLE" in
    fs-*::fsap-*) ;;
    *)
      echo "error: CAO_WORKSPACE_HANDLE must be 'fs-<id>::fsap-<id>' (got '$HANDLE')" >&2
      exit 1
      ;;
  esac
else
  echo "reading outputs from stack '$STACK' in $REGION"
  REPO="$(out ServerRepositoryUri)"
  BROKER_REPO="$(out WorkerBrokerRepositoryUri)"
  PANEL_REPO="$(out PanelRepositoryUri)"
  HANDLE="$(out WorkspaceVolumeHandle)"
  CLUSTER="$(out ClusterName)"
  VPC_CIDR="$(out VpcCidrBlock)"
fi

# The Kiro overlay includes external-secret.yaml, whose remote key is
# intentionally fixed to the documented name. Catch a Bedrock-mode stack, or a
# differently named provider secret, before kubectl reaches an ExternalSecret
# that can never become Ready.
if [ "$MODE" = "kiro" ] && [ "$STACK" != "-" ] && [ "$STACK" != "none" ]; then
  PROVIDER_SECRET="$(out ProviderSecretName)"
  if [ -z "$PROVIDER_SECRET" ] || [ "$PROVIDER_SECRET" = "None" ]; then
    echo "error: kiro mode requires the stack parameter ProviderSecretName=cao/provider-credentials" >&2
    exit 1
  fi
  if [ "$PROVIDER_SECRET" != "cao/provider-credentials" ]; then
    echo "error: kiro mode expects ProviderSecretName=cao/provider-credentials; stack has '$PROVIDER_SECRET'" >&2
    exit 1
  fi
fi

# An output that resolves to the empty string means the stack exists but is not
# the stack these manifests expect — fail here rather than applying manifests
# with a literal "<account-id>" in the image name, which surfaces much later as
# an ImagePullBackOff. Existing-cluster mode has already checked the same six,
# so this loop is a no-op there rather than a second source of truth.
for pair in "ServerRepositoryUri=$REPO" "WorkerBrokerRepositoryUri=$BROKER_REPO" \
            "PanelRepositoryUri=$PANEL_REPO" \
            "WorkspaceVolumeHandle=$HANDLE" "ClusterName=$CLUSTER" \
            "VpcCidrBlock=$VPC_CIDR"; do
  [ -n "${pair#*=}" ] || { echo "error: stack output ${pair%%=*} is empty" >&2; exit 1; }
done

ACCOUNT="${REPO%%.*}"
FS_ID="${HANDLE%%::*}"
AP_ID="${HANDLE##*::}"
TAG="${2:-$(grep -E '^[[:space:]]*newTag:' "$K8S_DIR/kustomization.yaml" | head -1 | awk '{print $2}')}"

cat <<EOF
  account     $ACCOUNT
  region      $REGION
  cluster     $CLUSTER
  vpc cidr    $VPC_CIDR
  mode        $MODE
  images      $REPO:$TAG
              $BROKER_REPO:$TAG
              $PANEL_REPO:$TAG
  workspace   $FS_ID / $AP_ID
EOF

RENDER="$(mktemp -d)"
trap 'rm -rf "$RENDER"' EXIT
cp -R "$K8S_DIR"/. "$RENDER/"

# Kustomize Components are optional overlays enabled by a parent. Keeping this
# edit in the throwaway rendered copy means the checked-in root remains the
# Bedrock default while kiro mode is still one deploy command, not a sequence of
# hand-edits that can omit half the provider switch.
if [ "$MODE" = "kiro" ]; then
  cat >>"$RENDER/kustomization.yaml" <<'EOF'

components:
  - components/kiro
EOF
fi

# codex mode needs no stack-output guard, because it adds no secret to project:
# it reaches Bedrock through the same Pod Identity association as the default.
# What it does need is an image built with --build-arg INSTALL_CODEX=1, and that
# cannot be checked from here — the tag is opaque. A tag without codex in it
# fails at the first launch with "codex was not found", not at deploy time.
if [ "$MODE" = "codex" ]; then
  cat >>"$RENDER/kustomization.yaml" <<'EOF'

components:
  - components/codex
EOF
fi

# LC_ALL=C and the -i.bak form keep this working on both GNU and BSD sed.
find "$RENDER" -name '*.yaml' -print0 | while IFS= read -r -d '' f; do
  LC_ALL=C sed -i.bak \
    -e "s|<server-image>|$REPO|g" \
    -e "s|<broker-image>|$BROKER_REPO|g" \
    -e "s|<panel-image>|$PANEL_REPO|g" \
    -e "s|<account-id>|$ACCOUNT|g" \
    -e "s|<region>|$REGION|g" \
    -e "s|<aws-region>|$REGION|g" \
    -e "s|<filesystem-id>|$FS_ID|g" \
    -e "s|<access-point-id>|$AP_ID|g" \
    -e "s|<vpc-cidr>|$VPC_CIDR|g" \
    "$f"
  rm -f "$f.bak"
done

# The image tag lives in kustomization.yaml's `images:` block, which overrides
# the tag written in each pod spec — so setting it here is enough.
#
# `[[:space:]]` rather than `\s`: `\s` is a GNU extension that BSD sed matches
# as a literal `s`, so on macOS this substitution silently did nothing and the
# manifests kept whatever tag was checked in. The failure surfaced ten minutes
# later as an ImagePullBackOff on a tag that never existed in the registry.
LC_ALL=C sed -i.bak -E "s|^([[:space:]]*)newTag:.*|\1newTag: $TAG|" "$RENDER/kustomization.yaml"
rm -f "$RENDER/kustomization.yaml.bak"

# A no-op substitution must not be survivable. Anything that stops the line
# above from matching - a renamed field, another sed dialect - would otherwise
# deploy the checked-in tag while this script reported the requested one.
#
# EVERY newTag line is checked, not just the first. The server, broker, and panel
# are built from one commit by one CodeBuild run, so a split tag can only mean a
# mistake. A `| head -1` here would have reported success while the broker stayed
# on the checked-in tag.
while read -r rendered; do
  [ "$rendered" = "$TAG" ] || {
    echo "error: asked for tag '$TAG' but the manifests render '$rendered'" >&2
    exit 1
  }
done < <(grep -E '^[[:space:]]*newTag:' "$RENDER/kustomization.yaml" | awk '{print $2}')

# Any placeholder left over is a manifest this script has not been taught about.
#
# The pattern is deliberately ANY <lower-case-token>, not the specific four this
# script renders. The narrow version silently passed <aws-region> and
# <immutable-tag> straight through into the applied manifests, where a literal
# "<immutable-tag>" in an image name surfaces ten minutes later as an
# ImagePullBackOff, and a literal CIDR surfaces as a policy that matches nothing.
#
# Comment lines are excluded: broker.yaml documents the optional worker-IRSA role
# as a commented `arn:aws:iam::<account>:role/<worker-role>` example, and a YAML
# comment cannot become a bad image name or an empty CIDR. Without this the guard
# fired on that example and aborted a clean first deploy of the manifests as
# shipped (guojing1217 on #802). grep -n prefixes each hit with `file:line:`, so
# the filter drops hits whose content (after that prefix) is a `#` comment.
if grep -rnE '<[a-z][a-z0-9-]*>' "$RENDER" --include='*.yaml' \
     | grep -vE '^[^:]+:[0-9]+:[[:space:]]*#'; then
  echo "error: unrendered placeholders above" >&2
  exit 1
fi

# The broker token, minted once. Both halves of the fleet read it from this
# secret - the supervisor to take a lease, the broker to check it - so it has to
# exist before the pods start, and it must NOT be regenerated on a re-run: that
# would leave a running supervisor holding a token the broker no longer accepts,
# and every delegation would 401 with nothing having visibly changed.
kubectl apply -f "$RENDER/namespace.yaml"
if kubectl -n cao-cluster get secret cao-elastic-broker-token >/dev/null 2>&1; then
  echo "broker token already present, keeping it"
else
  echo "minting broker token"
  # `openssl rand -hex` rather than a `tr -dc </dev/urandom | head -c` pipeline:
  # under the `set -o pipefail` above, head closing the pipe early kills tr with
  # SIGPIPE and the pipeline's status becomes 141. This form has no pipe and
  # yields exactly 48 characters.
  command -v openssl >/dev/null || { echo "error: openssl not found" >&2; exit 1; }
  kubectl -n cao-cluster create secret generic cao-elastic-broker-token \
    --from-literal="token=$(openssl rand -hex 24)"
fi

# The runtime-channel token, on the same terms. Shared by the central server and
# every execution pod: the server accepts a bridge only with it, and a bridge
# refuses to start without it, so a regenerated token would silently strand every
# running executor - hence kept across runs like the two above.
if kubectl -n cao-cluster get secret cao-runtime-token >/dev/null 2>&1; then
  echo "runtime token already present, keeping it"
else
  echo "minting runtime token"
  kubectl -n cao-cluster create secret generic cao-runtime-token \
    --from-literal="token=$(openssl rand -hex 24)"
fi

# The panel token, on the same terms. Not optional: panel.yaml reads it through a
# secretKeyRef with no `optional: true`, so a missing secret stops the pod at
# CreateContainerConfigError rather than starting it unauthenticated. Kept across
# runs too, so a browser that has been given the token keeps working.
if kubectl -n cao-cluster get secret cao-panel-secret >/dev/null 2>&1; then
  echo "panel token already present, keeping it"
else
  echo "minting panel token"
  kubectl -n cao-cluster create secret generic cao-panel-secret \
    --from-literal="token=$(openssl rand -hex 24)"
fi

# The pre-#745 layout cannot be upgraded in place, and kubectl's error for that
# is two screens of field diffs. Both objects below changed in ways Kubernetes
# forbids updating:
#
#   * the supervisor StatefulSet dropped its `volumeClaimTemplates` (its state is
#     an emptyDir now - nothing there needs to outlive the pod);
#   * the supervisor Service became headless, and `spec.clusterIP` is immutable.
#
# Deleting them is the operator's call, not this script's: the StatefulSet may be
# running an agent mid-task, and its old `state-cao-supervisor-0` PVC holds the
# only copy of the conversation this topology used to keep there. So say exactly
# what to run and stop.
LEGACY=""
if [ -n "$(kubectl -n cao-cluster get statefulset cao-supervisor \
             -o jsonpath='{.spec.volumeClaimTemplates}' 2>/dev/null)" ]; then
  LEGACY="statefulset/cao-supervisor"
fi
if [ "$(kubectl -n cao-cluster get service cao-supervisor \
          -o jsonpath='{.spec.clusterIP}' 2>/dev/null)" != "None" ] &&
   kubectl -n cao-cluster get service cao-supervisor >/dev/null 2>&1; then
  LEGACY="${LEGACY:+$LEGACY }service/cao-supervisor"
fi
if [ -n "$LEGACY" ]; then
  cat >&2 <<MSG
error: this namespace still runs the pre-#745 single-node layout, which cannot be
       updated in place. Finish or drain any running task, then:

         kubectl -n cao-cluster delete $LEGACY

       The old state PVC is left alone deliberately. Nothing in the new layout
       reads it, so keep it until you are sure you want it gone:

         kubectl -n cao-cluster get pvc state-cao-supervisor-0

       Re-run this script afterwards.
MSG
  exit 1
fi

echo "applying"
kubectl apply -k "$RENDER"

# The server first: a bridge cannot become Ready until the server it dials
# answers, so waiting on the supervisor before the server would just spend the
# supervisor's timeout watching a backoff loop.
#
# Both are StatefulSets, not Deployments, and there is no worker workload to wait
# for: a worker is a Deployment the broker mints per task, and none is created by
# this apply at all.
#
# cao-server uses updateStrategy OnDelete (server.yaml), and `kubectl rollout
# status` rejects any StatefulSet strategy other than RollingUpdate up front with
# "rollout status is only available for RollingUpdate strategy type" and exit 1 —
# it does not fall through to a readiness check. Under `set -euo pipefail` that
# aborts the deploy immediately after `kubectl apply -k`, so the gates below never
# run and a fleet that is in fact coming up looks like a failed deploy
# (guojing1217 on #802, measured on a live server StatefulSet). `kubectl wait` is
# strategy-agnostic and is the readiness gate this actually wants.
kubectl -n cao-cluster wait --for=condition=ready pod/cao-server-0 --timeout=600s
# The supervisor's Ready means its runtime channel is established, not just that
# uvicorn bound a port - the probe is an exec on the marker cao-bridge writes
# after the hello is accepted. Generous, because it is behind a provider install
# and two Bedrock warm-ups.
kubectl -n cao-cluster rollout status statefulset/cao-supervisor --timeout=900s
kubectl -n cao-cluster rollout status deployment/cao-worker-broker --timeout=300s
kubectl -n cao-cluster rollout status deployment/cao-fleet-panel --timeout=300s
