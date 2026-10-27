# CI gate evidence

Every PR gate of o2c-app (ADR-015) was broken on purpose, once, to show that it goes red.
The breaks live on the branch `gate-evidence` (from main `36a638c`), draft PR
[#63 "gate evidence DO NOT MERGE"](https://github.com/abhinavsingh-a1/o2c-app/pull/63).
One commit breaks one gate in a harmless, obvious way and is labelled DO NOT MERGE;
nothing on the branch is built, imported or run. The PR is never merged.

**Pushed in stages.** The security job stops at its first failing step (Semgrep,
pip-audit, pnpm audit, Trivy, Checkov), and a new push cancels the PR's running checks.
So the branch was pushed in four steps, in the reverse order of the security steps,
each after the previous run's security job had finished:

| Push | Head | Adds |
|---|---|---|
| 1 | `a78ba88` | security / Checkov |
| 2 | `6b40d04` | gitleaks |
| 3 | `1bba7de` | security / pip-audit |
| 4 | `9204dc3` | security / Semgrep, lint, python, ui, data |

## Results

| Gate | What was broken (commit) | Check that went red | Failed run |
|---|---|---|---|
| lint | ruff F821 (undefined name) in `scripts/gate_evidence_lint.py` (`dc280fa`) | lint (pre-commit): `ruff ... Failed`, F821 | [push 4](https://github.com/abhinavsingh-a1/o2c-app/actions/runs/40553125885/job/122362124016) |
| python | `assert 1 + 1 == 3` in `services/agent/tests/test_gate_evidence.py` (`c84bda6`) | python (pytest): 1 failed, 793 passed | [push 4](https://github.com/abhinavsingh-a1/o2c-app/actions/runs/40553125885/job/122362123946) |
| ui | `expect(1 + 1).toBe(3)` in `ui/src/gateEvidence.test.ts` (`53f2d53`) | ui (lint, test, build): step `pnpm --filter ui test` | [push 4](https://github.com/abhinavsingh-a1/o2c-app/actions/runs/40553125885/job/122362124219) |
| security: Semgrep | `subprocess.run(..., shell=True)` in `scripts/gate_evidence_semgrep.py` (`a385e1e`) | security: step Semgrep, `subprocess-shell-true` | [push 4](https://github.com/abhinavsingh-a1/o2c-app/actions/runs/40553125885/job/122362123848) |
| security: pip-audit | `markdown2==2.3.8` in the dev group (`1bba7de`) | security: step pip-audit, PYSEC-2020-65, PYSEC-2021-20 | [push 3](https://github.com/abhinavsingh-a1/o2c-app/actions/runs/40552127818/job/122358377979) |
| security: Checkov | `docker/gate-evidence.Dockerfile` with `USER root` (`a78ba88`) | security: step Checkov, CKV_DOCKER_8 | [push 1](https://github.com/abhinavsingh-a1/o2c-app/actions/runs/40544262170/job/122330507703) |
| data | dbt model selecting a column that does not exist, `analytics/dbt/models/staging/gate_evidence_invalid.sql` (`9204dc3`) | data: step `dbt build --empty`, `column "no_such_column_on_purpose" does not exist` | [push 4](https://github.com/abhinavsingh-a1/o2c-app/actions/runs/40553125885/job/122362123889) |
| gitleaks | fake key ID `AKIA…(redacted)` in `services/agent/tests/fixtures/gate_evidence_aws.env` (`6b40d04`) | **none**: lint passed (`Detect hardcoded secrets ... Passed`), and security failed only on Checkov (Trivy passed) | [push 2: lint](https://github.com/abhinavsingh-a1/o2c-app/actions/runs/40550452374/job/122352429402), [security](https://github.com/abhinavsingh-a1/o2c-app/actions/runs/40550452374/job/122352429510) |
| PR title | title "gate evidence DO NOT MERGE" (no commit) | PR title / check: "No release type found in pull request title" | [every push](https://github.com/abhinavsingh-a1/o2c-app/actions/runs/40553125823/job/122361910164) |

## Findings
- **No CI gate caught a secret in a test fixture.** The gitleaks pre-commit hook runs
  `gitleaks git --pre-commit --staged`; a CI checkout has nothing staged, so in the lint
  job it scans nothing and always passes. Trivy's secret scanner (security job) skips
  `tests/` directories by its built-in allow rules: scanning `services/agent` exits 0,
  only scanning the `fixtures` folder by itself finds the key. Semgrep `p/secrets` has no
  finding for it. Fix (feat/e14-hardening): the lint job runs gitleaks over every commit
  of the PR (`--log-opts base..head`), and the hook is skipped there (ADR-015). Checked
  locally: 1 leak in the 8 gate-evidence commits, none in feat/e14-hardening. The PR is
  based on main, so it shows the gap, not the fix.
- **Every other gate failed for exactly its own break.** Push 4 first carried the dbt
  model under `marts/` (`38da206`, [run](https://github.com/abhinavsingh-a1/o2c-app/actions/runs/40552376322)); that also failed the python
  gate, because `analytics/tests/test_dbt_project.py` checks the exact set of marts. The
  model was moved to `staging/` (`9204dc3`) and pushed again; the table shows that run.
- **The security job stops at its first failing step**, so one PR shows at most one
  security finding. A PR that fixes a Semgrep finding can reveal a pip-audit or Checkov
  finding on its next run.
