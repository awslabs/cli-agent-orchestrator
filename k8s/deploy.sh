#!/usr/bin/env bash
# Render the placeholders in these manifests from CloudFormation stack outputs,
# then apply them.
#
# The manifests are checked in with placeholders (<account-id>, <region>,
# <filesystem-id>, <access-point-id>, <vpc-cidr>) rather than real values, because
# the real values differ per account and a checked-in account number is a trap.
# Editing four files by hand before every deploy is the alternative this replaces.
#
# It also generates the broker token. That secret is NOT checked in and NOT read
# from the stack: it is minted here on first run and left alone afterwards, so
# re-running this script does not invalidate the token a running supervisor
# already holds.
#
# Usage:
#   k8s/deploy.sh [stack-name] [image-tag]
#
# Defaults: stack cao-workshop, tag taken from kustomization.yaml.
# Honours the usual AWS_PROFILE / AWS_REGION environment.
#
# Rendering happens into a temporary directory; the tree under k8s/ is never
# modified, so a failed run leaves nothing to clean up and `git status` stays
# clean.
set -euo pipefail

STACK="${1:-cao-workshop}"
K8S_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
BROKER_REPO="$(out BrokerRepositoryUri)"
HANDLE="$(out WorkspaceVolumeHandle)"
CLUSTER="$(out ClusterName)"
VPC_CIDR="$(out VpcCidrBlock)"

# An output that resolves to the empty string means the stack exists but is not
# the stack these manifests expect — fail here rather than applying manifests
# with a literal "<account-id>" in the image name, which surfaces much later as
# an ImagePullBackOff.
for pair in "ServerRepositoryUri=$REPO" "BrokerRepositoryUri=$BROKER_REPO" \
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
  images      $REPO:$TAG
              $BROKER_REPO:$TAG
  workspace   $FS_ID / $AP_ID
EOF

RENDER="$(mktemp -d)"
trap 'rm -rf "$RENDER"' EXIT
cp -R "$K8S_DIR"/. "$RENDER/"

# LC_ALL=C and the -i.bak form keep this working on both GNU and BSD sed.
find "$RENDER" -name '*.yaml' -print0 | while IFS= read -r -d '' f; do
  LC_ALL=C sed -i.bak \
    -e "s|<account-id>|$ACCOUNT|g" \
    -e "s|<region>|$REGION|g" \
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
# EVERY newTag line is checked, not just the first. There are two images now
# (cao-server and cao-worker-broker) and they are built from one commit by one
# CodeBuild run, so a split tag can only mean a mistake. A `| head -1` here would
# have reported success while the broker stayed on the checked-in tag.
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

echo "applying"
kubectl apply -k "$RENDER"

# The supervisor is a StatefulSet here, not a Deployment, and there is no worker
# workload to wait for: workers are Jobs the broker mints per task and are not
# created by this apply at all.
kubectl -n cao-cluster rollout status statefulset/cao-supervisor --timeout=900s
kubectl -n cao-cluster rollout status deployment/cao-worker-broker --timeout=300s
