# E14 Execution Guide — CI/CD, Supply Chain, GitOps and Promotion Pipelines

E14 was done in **fast mode** across all three repositories (`o2c-app`, `o2c-infra`, `o2c-gitops`), with Claude Code implementing each part from a spec. Everything ran locally (GitHub Actions, GHCR, kind, Jenkins in Docker); AWS was not used.

**Result:** every change passes required PR gates; every image is built once, scanned, signed and attested; Git decides what runs (Argo CD); Kyverno admits only signed, pinned, non-root o2c images; Jenkins (configured by Ansible) promotes Dev → Test → Prod with security, load and AI-quality gates, four-eyes approval and automatic rollback; every step is auditable.

## Final evidence

| Area | Evidence |
| --- | --- |
| PR gates (6 required) | Gate-evidence PR #63: lint, python, ui, security, data and PR title all went red for their planted faults; closed, never merged |
| Supply chain | Release run green for all 8 images; `cosign verify` PASS and `gh attestation verify` PASS (agent image) |
| GitOps | Dev deploy PRs auto-merged; Dev pods run exactly the digests in `o2c-gitops` |
| Kyverno | Root pod, unpinned foreign image and unsigned o2c artifact all denied |
| promote-to-test (final) | `Finished: SUCCESS` — integration test PASS, ZAP `FAIL-NEW: 0` (warnings 8/9 → 2/3 informational), k6 p95 8.5 ms / 0% errors, eval gate 13/13 PASS, red team 8/8 |
| promote-to-prod | `Finished: SUCCESS` — approved by `approver` (started by `abhinav`), smoke 8/8, SLO p95 53.6 ms |
| Four-eyes | Self-approval rejected: "abhinav started this build and cannot approve it" |
| Rollback | Two automatic rollbacks after failed deploys (revert PRs #20, #22); simulated SLO breach failed the build with nothing to revert |
| Ansible | Second run `changed=0 failed=0` (idempotent) |
| Audit trail | Jenkins audit log records logins, approvals (`/input/ProdApproval/proceedEmpty by approver`), credential use and build parameters/results |

## Part A — PR gates and Dependabot (ADR-015)

- `o2c-app` `pr.yml`: lint (pre-commit), python (pytest), ui (lint, test, build), security (Semgrep, pip-audit, pnpm audit, Trivy, Checkov), images (build + Trivy per service), data (dbt build --empty, Alembic check), eval (extraction on 10 golden POs, only when prompts/extractor change; separate OpenAI project key with a $2 budget), PR title (conventional commits). Actions pinned by SHA, least-privilege permissions.
- `o2c-infra`: terraform fmt/validate, Checkov (terraform, helm), helm lint/template. `o2c-gitops`: yamllint, render every env with the chart from `o2c-infra` main.
- Required checks in each repo's ruleset `protect-main`, picked from the suggestions (exact names).
- Dependabot weekly for uv, npm, docker, github-actions, terraform; ignore rules for deliberately pinned ML packages (mlflow-skinny major, transformers ≥ 4.47, pandas ≥ 3).
- **Gap found by the evidence PR:** no gate caught a secret committed in a test fixture (the gitleaks hook scans staged changes only; Trivy skips `tests/`). Fix: a gitleaks step in CI scanning every commit of a PR.

## Part B — Release pipeline (ADR-016)

- On merge to `main`: build each image, Trivy scan, push to **GHCR** tagged by commit SHA (no `latest`), Syft SBOM (attested), Cosign **keyless** signing via GitHub OIDC, build-provenance attestation.
- Models baked into images: `make models-publish` pushes the `@champion` models as OCI artifacts; `models.lock.json` pins versions and digests; Dockerfiles pull them at build time.
- Final job opens a PR in `o2c-gitops` updating `envs/dev/*.yaml` digests and enables auto-merge; values are written yamllint-clean and linted before the PR.
- `scripts/verify_image.sh` (Git Bash) runs `cosign verify` and `gh attestation verify`.

## Part C — GitOps platform on kind (ADR-017)

- Infrastructure stays in Docker Compose; app services run in kind and reach it via `host.docker.internal`.
- Namespaces `o2c-dev` (auto-sync, prune, self-heal), `o2c-test` and `o2c-prod` (manual sync, replicas 0 until promoted). Kafka topics and consumer groups prefixed per environment (`O2C_ENV`), so environments never share messages.
- Argo CD app-of-apps + ApplicationSet (chart from `o2c-infra`, values from `o2c-gitops`). Kyverno in Enforce mode: verify signatures (release workflow identity), only `ghcr.io/abhinavsingh-a1/o2c-*`, digests required, no `latest`, `runAsNonRoot`, resource limits.
- `platform/kind/bootstrap.sh`, `secrets.sh` (from `.env`, never printed), `argocd-ui.sh`, `teardown.sh`.

## Part D — Jenkins via Ansible and promotion (ADR-018)

- Ansible runs in a runner container with the Docker socket; role `jenkins` builds a pinned Jenkins image (84 locked plugins, pinned tools), runs it on **http://localhost:18090**, configures it as code (JCasC): users `abhinav` and `approver`, matrix auth, credentials, audit trail, seed jobs. `make jenkins-up` / `jenkins-down` / `prod-down`.
- **promote-to-test:** verify Dev images (signature + provenance) and find the common `o2c-app` commit → PR `envs/test` (skipped when unchanged) → Argo CD sync with retries → integration test (PO through Test) → OWASP ZAP baseline (fails on header regressions) → k6 (20 VUs, 30 s, p95 < 500 ms) → full `make eval` at the promoted commit → reports archived → Test scaled to 0.
- **promote-to-prod:** verify Test images → ServiceNow change request (**skipped until E13**) → four-eyes approval → PR `envs/prod` → deploy → smoke test → SLO check (k6) → automatic revert PR on failure; `SIMULATE_SLO_BREACH` demonstrates the SLO path.

## Part E — Hardening and evidence

- Security headers for the UI (nginx: CSP, frame-ancestors, nosniff, Referrer-Policy, Permissions-Policy, COOP/CORP, `server_tokens off`, cache rules) and the agent API (middleware). ZAP rules for them now FAIL on regression.
- Clear message when an SLO breach has nothing to roll back.
- `docs/gates-evidence.md` (links to every red run of PR #63) and ADR-018 findings.

## Troubleshooting — problems hit during E14 and their fixes

| # | Symptom | Cause | Fix |
| --- | --- | --- | --- |
| 1 | PR "not mergeable: base branch policy" with all checks green | Required check names did not match (`postgres`, names without their bracketed description) | Remove and re-add required checks by picking names from the ruleset suggestions |
| 2 | Merge blocked right after `gh pr create` | Checks had not started yet ("no checks reported") | `sleep 30 && gh pr checks --watch` before merging |
| 3 | Dependabot PR failed pytest | Major bumps of pinned packages (mlflow-skinny 3, transformers 5) | Close PR; Dependabot ignore rules with reasons |
| 4 | `cosign` not found | winget installed it as `cosign-windows-amd64` | Copy as `cosign.exe` into a PATH folder; new terminal |
| 5 | `bash script.sh` in PowerShell fails differently | In PowerShell, `bash` is WSL Linux | Run bash scripts in Git Bash |
| 6 | ORAS winget package not found | Wrong package ID | Python `oras` library in `publish_models.py` |
| 7 | `oras push` denied: token scopes | `gh` login lacked `write:packages` | `gh auth refresh -h github.com -s write:packages,read:packages` |
| 8 | Release failed for classifier and agent | `models.lock.json` never committed; model packages private | `make models-publish`, commit the lock file, make packages public |
| 9 | Verification commands queried repo `/o2c-app` | Shell variable arrived empty | Hard-code the owner in commands |
| 10 | Kafka "Network is unreachable" in kind | `host.docker.internal` resolves to IPv6 inside kind | `KAFKA_ADDRESS_FAMILY=v4` (librdkafka `broker.address.family`) |
| 11 | `localhost` connections reset / Docker "unable to start" | WSL Ubuntu's relay held `[::1]` ports; `wsl --terminate` disturbed Docker | Stop Ubuntu, `wsl --shutdown`, restart Docker Desktop; use `127.0.0.1` |
| 12 | Deploy PR blocked by yamllint | Release wrote values yamllint rejects | YAML-preserving edit + yamllint before the PR |
| 13 | gitleaks: 28 findings in `plugins.txt` | Plugin `name-api:version` lines look like API keys | `.gitleaks.toml` allowlist for that one file |
| 14 | `make jenkins-up` path without backslashes | `make` started from PowerShell | Run `make` in Git Bash |
| 15 | Jenkins `401 Bad credentials` | `.env` quotes copied into the secret | Strip quotes in the Ansible role; check that fails on quoted secrets |
| 16 | `git pull` blocked by a `.pyc` file forever | Tracked `__pycache__` + CRLF conversion of a binary | `core.autocrlf false`, reset to origin, untrack `__pycache__`, `.gitattributes` |
| 17 | Wrong pipeline ran | `promote-to-prod` clicked instead of `promote-to-test` | Check console line 2 (`Jenkinsfile.promote-test`) |
| 18 | "images come from different o2c-app commits" | Unchanged images keep their digest and carry several provenance records | Intersect all commits per image; promote the newest common commit |
| 19 | `zstandard` build failed (`cc` not found) | uv picked Python 3.15 | `UV_PYTHON=3.12` in both pipelines |
| 20 | Fix had no effect in Jenkins | PR not merged; Jenkins reads GitHub `main` | Check `git log origin/main -1` before running |
| 21 | Restart from Stage: `lib.sh` not found | Workspace wiped by `cleanWs`; checkout skipped | Always use Build Now |
| 22 | Eval: MLflow at `localhost:5000` refused | `localhost` is the Jenkins container | All eval hosts via `host.docker.internal` in `lib.sh` |
| 23 | MLflow 500 / InvalidChunkLength on model download | gunicorn 30 s worker timeout for a 491.7 MB file | `--gunicorn-opts "--timeout 600"`, `--workers 4` |
| 24 | RT-08 (40× quantity) posted | pandas 3 drift broke MLflow schema enforcement; anomaly model silently returned 0.0 | Pin `pandas<3`; failure counter, alert and gate check "anomaly model scores" |
| 25 | Argo CD DNS timeouts for minutes; deploy rolled back | kindnet queued DNS of pods covered by NetworkPolicies and lost decisions under load | Argo CD without NetworkPolicies on kind; bootstrap check; retries for transient errors (12/800 → 0/1,600 failures) |
| 26 | Commands kept running after a failed step | Lines run independently | Chain critical steps with `&&` |
| 27 | gitleaks still failed after editing the file | The hook scans the staged version | `git add` the fixed file, then re-run |
| 28 | Worktree removal: "Filename too long" | Windows 260-character path limit | `Remove-Item -LiteralPath "\\?\C:\..." -Recurse -Force` |
| 29 | Eval "judge ran: error" once | One ERP call from the agent timed out | Re-run passed 14/14; retry for safe ERP GET calls is backlog for E16 |

## Backlog carried forward

- Retry transient errors on safe (GET) ERP calls in the agent; make the judge report a per-PO error instead of failing the step (E16).
- Make "Restart from Stage" safe (re-checkout per stage) or document Build Now only.
- Switch on the ServiceNow change-request step after E13.
- Review Dependabot PR #59 (minor/patch updates).

**Lessons:**

- A gate is only proven when it has failed on purpose.
- Build once, sign once, promote the same digest; never rebuild per environment.
- Silent fallbacks need counters, alerts and a gate, or they hide outages.
- Verify what runs (digest in cluster = digest in Git), not what was intended.
- When an environment is flaky, find the mechanism; retries alone only hide it.
