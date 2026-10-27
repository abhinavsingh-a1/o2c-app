# Security policy

o2c-app is a public portfolio project. It processes synthetic data only and runs locally;
there is no hosted service.

## Reporting a vulnerability
Please report it **privately**, through GitHub's
[private vulnerability reporting](https://github.com/abhinavsingh-a1/o2c-app/security/advisories/new)
("Report a vulnerability" on the Security tab). Do not open a public issue for a security
problem.

Include:
- the affected component and commit;
- the steps to reproduce;
- the impact.

You can expect an acknowledgement within 7 days. This is a one-person project, so there
is no bounty and no SLA.

If you find a credential in the repository or its history, report it the same way. It is
rotated first and investigated second ([runbooks §7](docs/operations/runbooks.md#7-secret-rotation)).

## Supported versions
Only `main`, and the images released from it, are supported. Each release is an image
per service tagged with the commit SHA (`ghcr.io/abhinavsingh-a1/o2c-<service>:<sha>`)
and deployed by digest. Fixes are made on `main` and promoted. Older digests are not
patched.

## What is scanned
On every pull request (`.github/workflows/pr.yml`, ADR-015):

| Check | Scope | Fails on |
|---|---|---|
| gitleaks | Pre-commit hook, plus every commit of the PR in CI | Any secret |
| Semgrep 1.182.1 | `p/python`, `p/typescript`, `p/secrets` | ERROR findings |
| pip-audit 2.10.1 | All Python packages and groups (`uv export`) | Any vulnerability not listed with a reason in `.pip-audit-ignore` (review by 2026-12-31) |
| pnpm audit | UI lockfile | High and critical |
| Trivy 0.76.1 | Filesystem (lock files, secrets) and every built image | CRITICAL with a fix, not in `.trivyignore` (entries expire 2026-12-31) |
| Checkov 3.3.30 | Dockerfiles, secrets in all files; Terraform and Helm in o2c-infra | Failed checks not skipped with a reason |
| OWASP ZAP baseline | UI and agent API, in Jenkins promote-to-test | High-risk alerts and the security-header rules (ADR-018) |

Dependabot opens weekly updates for uv, npm, Docker, GitHub Actions and Terraform.
Actions are pinned by commit SHA. The PR gates were each proven to fail on a planted
fault ([docs/gates-evidence.md](docs/gates-evidence.md)).

## What is signed and attested
On every push to `main` (`.github/workflows/release.yml`, ADR-016), for each of the 8
service images:
- **cosign keyless signature** of the digest. The certificate identity is
  `https://github.com/abhinavsingh-a1/o2c-app/.github/workflows/release.yml@refs/heads/main`
  and the issuer `https://token.actions.githubusercontent.com`. The signature is logged in
  Rekor.
- **SLSA build provenance** (`actions/attest-build-provenance`).
- **SPDX SBOM** (Syft), attested to the digest.
- **Models** are OCI artifacts pinned by digest in `models.lock.json` and baked into the
  images.

Verify an image:
```bash
scripts/verify_image.sh ghcr.io/abhinavsingh-a1/o2c-agent@sha256:<digest>
make audit-check   # every image currently in envs/prod (or test) of o2c-gitops, plus the audit chain
```
The cluster admits only signed images from `ghcr.io/abhinavsingh-a1/o2c-*`, by digest,
non-root, with resource limits (Kyverno, ADR-017).

## Application security
The threat model is in [docs/governance/threat-model.md](docs/governance/threat-model.md).
Highlights:
- HMAC-signed intake.
- JWT-protected review API. The reviewer is taken from the token.
- Prompt-injection defences, tested by an 8-case red team.
- Security headers on the UI and the API.
- Per-service LLM keys with budgets.
- A hash-chained audit archive.
