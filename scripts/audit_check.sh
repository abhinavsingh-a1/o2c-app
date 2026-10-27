#!/usr/bin/env bash
# Audit check (E16, docs/operations/runbooks.md): one command an auditor can run.
#
#   make audit-check          (Git Bash; needs cosign, gh logged in, docker, uv)
#
# 1. Images: every image in o2c-gitops envs/prod (envs/test if prod has none) on
#    origin/main is verified with scripts/verify_image.sh: cosign signature of
#    release.yml on main, build provenance and SBOM attestations (ADR-016).
# 2. Promotion history: the merged promotion and rollback PRs of o2c-gitops (ADR-018).
# 3. Jenkins: the last entries of the audit-trail log in the o2c-jenkins container
#    (rotated files, .lck lock files excluded).
# 4. Decision archive: ml/verify_audit.py walks the hash chain in s3://o2c-audit (ADR-014).
# Prints a PASS/FAIL table and exits 1 on any FAIL. Never prints a secret: it reads no
# .env value itself, and the tools it calls print none.
#
# Overrides (for tests and other checkouts): GITOPS_DIR, GITOPS_REPO, GITOPS_REF,
# GITOPS_FETCH (0: no git fetch), VERIFY_IMAGE, JENKINS_CONTAINER, AUDIT_LINES.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GITOPS_DIR="${GITOPS_DIR:-$ROOT/../o2c-gitops}"
GITOPS_REPO="${GITOPS_REPO:-abhinavsingh-a1/o2c-gitops}"
GITOPS_REF="${GITOPS_REF:-origin/main}"
GITOPS_FETCH="${GITOPS_FETCH:-1}"
VERIFY_IMAGE="${VERIFY_IMAGE:-$ROOT/scripts/verify_image.sh}"
JENKINS_CONTAINER="${JENKINS_CONTAINER:-o2c-jenkins}"
AUDIT_LINES="${AUDIT_LINES:-15}"

results=()   # "STATUS|check|detail"
record() { results+=("$1|$2|$3"); }
section() { printf '\n== %s ==\n' "$1"; }

# --- 1. images of the promoted environment -----------------------------------------
# Prints "service repository@digest" for every values file of envs/<env> with a digest.
images_of() {
  local env="$1" file service repo digest
  for file in $(git -C "$GITOPS_DIR" ls-tree --name-only "$GITOPS_REF" "envs/$env/" 2> /dev/null); do
    service="$(basename "$file" .yaml)"
    repo="$(git -C "$GITOPS_DIR" show "$GITOPS_REF:$file" | sed -n 's/^  repository: *"\{0,1\}\([^"]*\)"\{0,1\} *$/\1/p' | head -n 1)"
    digest="$(git -C "$GITOPS_DIR" show "$GITOPS_REF:$file" | sed -n 's/^  digest: *"\{0,1\}\(sha256:[0-9a-f]\{64\}\)"\{0,1\} *$/\1/p' | head -n 1)"
    [ -n "$repo" ] && [ -n "$digest" ] && echo "$service $repo@$digest"
  done
}

section "1. Signed images in o2c-gitops ($GITOPS_REF)"
if [ ! -d "$GITOPS_DIR/.git" ]; then
  echo "o2c-gitops checkout not found at $GITOPS_DIR"
  record FAIL "images" "no o2c-gitops checkout at $GITOPS_DIR"
else
  if [ "$GITOPS_FETCH" = "1" ] && ! git -C "$GITOPS_DIR" fetch -q origin 2> /dev/null; then
    echo "warning: git fetch failed; using the local $GITOPS_REF"
  fi
  env_name=prod
  images="$(images_of prod)"
  if [ -z "$images" ]; then
    env_name=test
    images="$(images_of test)"
  fi
  if [ -z "$images" ]; then
    echo "no image digests in envs/prod or envs/test"
    record FAIL "images" "no image digests in envs/prod or envs/test"
  else
    echo "environment: $env_name"
    while read -r service image; do
      [ -z "$service" ] && continue
      if out="$("$VERIFY_IMAGE" "$image" 2>&1)"; then
        echo "PASS  $service  $image"
        record PASS "image $env_name/$service" "${image##*@}"
      else
        echo "FAIL  $service  $image"
        echo "$out" | sed 's/^/      /'
        record FAIL "image $env_name/$service" "verify_image.sh failed for ${image##*@}"
      fi
    done <<< "$images"
  fi
fi

# --- 2. promotion and rollback history ----------------------------------------------
section "2. Promotion and rollback PRs ($GITOPS_REPO, merged)"
if history="$(gh pr list --repo "$GITOPS_REPO" --state merged --limit 100 \
  --json number,title,mergedAt,mergedBy \
  --jq '.[] | select(.title | test("^(chore\\((test|prod)\\): promote|revert\\()")) | "\(.mergedAt)  #\(.number)  \(.title)  (merged by \(.mergedBy.login // "?"))"' 2>&1)"; then
  if [ -n "$history" ]; then
    echo "$history"
    promotions=$(grep -c ': promote' <<< "$history")
    rollbacks=$(grep -c 'revert(' <<< "$history")
    record PASS "promotion history" "$promotions promotions, $rollbacks rollbacks"
  else
    echo "no promotion PRs found"
    record FAIL "promotion history" "no merged promotion PRs in $GITOPS_REPO"
  fi
else
  echo "$history"
  record FAIL "promotion history" "gh pr list failed"
fi

# --- 3. Jenkins audit trail ----------------------------------------------------------
section "3. Jenkins audit trail ($JENKINS_CONTAINER, last $AUDIT_LINES entries)"
# The audit-trail plugin rotates audit-trail.log.0, .0.1, ...; .lck files are locks.
# MSYS_NO_PATHCONV: Git Bash must not turn /var/... into a Windows path.
if trail="$(MSYS_NO_PATHCONV=1 docker exec "$JENKINS_CONTAINER" sh -c \
  'cd /var/jenkins_home && f=$(ls -t audit-trail.log* 2>/dev/null | grep -v "\.lck$" | head -n 1) && [ -n "$f" ] && echo "file: $f" && tail -n '"$AUDIT_LINES"' "$f"' 2>&1)"; then
  echo "$trail"
  record PASS "jenkins audit trail" "$(head -n 1 <<< "$trail")"
else
  echo "$trail"
  record FAIL "jenkins audit trail" "no audit-trail log readable in $JENKINS_CONTAINER"
fi

# --- 4. hash-chained decision archive ------------------------------------------------
section "4. Decision archive (ml/verify_audit.py)"
if chain="$(cd "$ROOT" && uv run python ml/verify_audit.py 2>&1)"; then
  echo "$chain"
  record PASS "audit chain" "$(tail -n 1 <<< "$chain" | cut -c1-90)"
else
  echo "$chain"
  record FAIL "audit chain" "$(tail -n 1 <<< "$chain" | cut -c1-90)"
fi

# --- summary -------------------------------------------------------------------------
section "Summary"
failed=0
printf '%-6s %-28s %s\n' "RESULT" "CHECK" "DETAIL"
for row in "${results[@]}"; do
  IFS='|' read -r status check detail <<< "$row"
  printf '%-6s %-28s %s\n' "$status" "$check" "$detail"
  [ "$status" = "FAIL" ] && failed=1
done
if [ "$failed" -eq 0 ]; then
  echo "AUDIT CHECK: PASS"
else
  echo "AUDIT CHECK: FAIL"
fi
exit "$failed"
