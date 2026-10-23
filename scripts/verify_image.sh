#!/usr/bin/env bash
# Verify a released image (ADR-016): prints PASS or FAIL, exit 0 or 1.
#
#   scripts/verify_image.sh ghcr.io/abhinavsingh-a1/o2c-agent@sha256:<digest>
#
# 1. cosign verify: a keyless signature whose certificate was issued to this repo's
#    release workflow on main by GitHub Actions' OIDC issuer.
# 2. gh attestation verify: SLSA build provenance and the SPDX SBOM, both signed by the
#    same workflow on main, on a GitHub-hosted runner.
# Only digests are accepted: a tag can be moved after it was verified.
# Needs cosign (v3) and gh (logged in) on PATH.
set -uo pipefail

REPO="abhinavsingh-a1/o2c-app"
WORKFLOW=".github/workflows/release.yml"
REF="refs/heads/main"
ISSUER="https://token.actions.githubusercontent.com"
IDENTITY="https://github.com/$REPO/$WORKFLOW@$REF"
SBOM_PREDICATE="https://spdx.dev/Document/v2.3"

image="${1:-}"
if [[ ! "$image" =~ ^[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$ ]]; then
  echo "usage: $0 <registry/repository@sha256:digest>  (a digest, not a tag)" >&2
  echo "FAIL"
  exit 1
fi
for tool in cosign gh; do
  command -v "$tool" > /dev/null || { echo "$tool not found on PATH" >&2; echo "FAIL"; exit 1; }
done

failed=0
err=$(mktemp)
trap 'rm -f "$err"' EXIT
check() {
  local what="$1"
  shift
  if "$@" > /dev/null 2> "$err"; then
    echo "ok    $what"
  else
    echo "FAIL  $what:" >&2
    sed 's/^/      /' "$err" >&2
    failed=1
  fi
}

check "cosign signature ($IDENTITY)" \
  cosign verify "$image" \
  --certificate-identity "$IDENTITY" \
  --certificate-oidc-issuer "$ISSUER"
check "build provenance attestation" \
  gh attestation verify "oci://$image" --repo "$REPO" \
  --signer-workflow "$REPO/$WORKFLOW" --source-ref "$REF" --deny-self-hosted-runners
check "SBOM attestation (SPDX)" \
  gh attestation verify "oci://$image" --repo "$REPO" \
  --signer-workflow "$REPO/$WORKFLOW" --source-ref "$REF" --deny-self-hosted-runners \
  --predicate-type "$SBOM_PREDICATE"

if [ "$failed" -eq 0 ]; then
  echo "PASS"
else
  echo "FAIL"
fi
exit "$failed"
