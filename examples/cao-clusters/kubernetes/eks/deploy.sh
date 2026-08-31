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
# Modes: bedrock (default), kiro.
# Defaults: stack cao-workshop, tag taken from kustomization.yaml, mode bedrock.
# Honours the usual AWS_PROFILE / AWS_REGION environment.
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
  *)
    echo "error: mode must be 'bedrock' or 'kiro' (got '$MODE')" >&2
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

echo "reading outputs from stack '$STACK' in $REGION"
REPO="$(out ServerRepositoryUri)"
BROKER_REPO="$(out WorkerBrokerRepositoryUri)"
PANEL_REPO="$(out PanelRepositoryUri)"
HANDLE="$(out WorkspaceVolumeHandle)"
CLUSTER="$(out ClusterName)"
VPC_CIDR="$(out VpcCidrBlock)"

# The Kiro overlay includes external-secret.yaml, whose remote key is
# intentionally fixed to the documented name. Catch a Bedrock-mode stack, or a
# differently named provider secret, before kubectl reaches an ExternalSecret
# that can never become Ready.
if [ "$MODE" = "kiro" ]; then
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
# an ImagePullBackOff.
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
if grep -rnE '<[a-z][a-z0-9-]*>' "$RENDER" --include='*.yaml'; then
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

echo "applying"
kubectl apply -k "$RENDER"

# The supervisor is a StatefulSet here, not a Deployment, and there is no worker
# workload to wait for: workers are Jobs the broker mints per task and are not
# created by this apply at all.
kubectl -n cao-cluster rollout status statefulset/cao-supervisor --timeout=900s
kubectl -n cao-cluster rollout status deployment/cao-worker-broker --timeout=300s
kubectl -n cao-cluster rollout status deployment/cao-fleet-panel --timeout=300s
