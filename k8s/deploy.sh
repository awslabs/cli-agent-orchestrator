#!/usr/bin/env bash
# Render the placeholders in these manifests from CloudFormation stack outputs,
# then apply them.
#
# The manifests are checked in with placeholders (<account-id>, <region>,
# <filesystem-id>, <access-point-id>) rather than real values, because the real
# values differ per account and a checked-in account number is a trap. Editing
# four files by hand before every deploy is the alternative this replaces.
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

REGION="$(aws configure get region)"
[ -n "${AWS_REGION:-}" ] && REGION="$AWS_REGION"
[ -n "$REGION" ] || { echo "error: no region — set AWS_REGION" >&2; exit 1; }

echo "reading outputs from stack '$STACK' in $REGION"
REPO="$(out ServerRepositoryUri)"
HANDLE="$(out WorkspaceVolumeHandle)"
CLUSTER="$(out ClusterName)"

# An output that resolves to the empty string means the stack exists but is not
# the stack these manifests expect — fail here rather than applying manifests
# with a literal "<account-id>" in the image name, which surfaces much later as
# an ImagePullBackOff.
for pair in "ServerRepositoryUri=$REPO" "WorkspaceVolumeHandle=$HANDLE" "ClusterName=$CLUSTER"; do
  [ -n "${pair#*=}" ] || { echo "error: stack output ${pair%%=*} is empty" >&2; exit 1; }
done

ACCOUNT="${REPO%%.*}"
FS_ID="${HANDLE%%::*}"
AP_ID="${HANDLE##*::}"
TAG="${2:-$(grep -E '^\s*newTag:' "$K8S_DIR/kustomization.yaml" | awk '{print $2}')}"

cat <<EOF
  account     $ACCOUNT
  region      $REGION
  cluster     $CLUSTER
  image       $REPO:$TAG
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
    "$f"
  rm -f "$f.bak"
done

# The image tag lives in kustomization.yaml's `images:` block, which overrides
# the tag written in each pod spec — so setting it here is enough.
LC_ALL=C sed -i.bak -E "s|^(\s*)newTag:.*|\1newTag: $TAG|" "$RENDER/kustomization.yaml"
rm -f "$RENDER/kustomization.yaml.bak"

# Any placeholder left over is a manifest this script has not been taught about.
if grep -rn '<[a-z-]*-id>\|<region>' "$RENDER" --include='*.yaml'; then
  echo "error: unrendered placeholders above" >&2
  exit 1
fi

echo "applying"
kubectl apply -k "$RENDER"

kubectl -n cao-cluster rollout status deployment/cao-supervisor --timeout=600s
kubectl -n cao-cluster rollout status statefulset/cao-worker --timeout=900s
