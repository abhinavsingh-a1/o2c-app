# ADR-018: Promotion pipelines: Jenkins gates from dev to test to prod

Date: 2026-10-26
Status: Accepted

## Context
A release deploys to dev automatically (ADR-016). test and prod sync only by hand after a
promotion PR (ADR-017), but nothing decided when a release is good enough to promote,
who may promote it to prod, or what happens when prod gets worse after a promotion.

Everything runs on one Windows machine with Docker Desktop:
- the kind cluster `o2c` with Argo CD and Kyverno;
- the infrastructure in Docker Compose.

The cluster's API server and the Compose services are reachable only from this machine.

## Decision

### Two Jenkins pipelines, defined in o2c-infra `jenkins/`
**promote-to-test** (`Jenkinsfile.promote-test`):
1. **Release in dev.** Every dev image must pass two checks:
   - a cosign signature of o2c-app's `release.yml` on main;
   - SLSA build provenance from the same workflow.

   The provenance names the source commit, and all images must come from **one o2c-app
   commit**: the promoted commit. A release cannot be promoted half-built.
2. **Promotion PR.** A PR in o2c-gitops sets `envs/test` to the dev images with 1 replica:
   - `scripts/promote_values.py` makes the change with ruamel.yaml, as the release does;
   - yamllint `--strict` runs before the PR is opened;
   - the PR is merged once o2c-gitops's checks pass.
3. **Deploy.** Argo CD syncs the test applications (hard refresh, sync, wait for health).
   Every Deployment must then run exactly the promoted image.
4. **Gates.** All of them must pass:
   - **integration test** (`scripts/e2e_send_and_check.py order`): a training PO goes
     through the test intake, and the test agent must decide it within 10 minutes. The
     check needs both:
     - its `review_queue` row is `posted` or `awaiting_review`;
     - `o2c_orders_total{decided_by="agent"}` of the test agent went up.

     A run marker after the PDF's `%%EOF` gives each run a new idempotency key, so the
     order is processed again instead of being skipped as a duplicate.
   - **OWASP ZAP baseline** of the test UI and the agent API: no high-risk alerts, and
     no alert of a FAIL rule in o2c-infra `jenkins/zap-baseline.conf`: the security
     headers (10020, 10021, 10036, 10038, 10055, 10063), Spectre site isolation (90004),
     SRI and cross-domain or vulnerable scripts (90003, 10017, 10098, 10003).
     Informational rules (10049, 10109) stay WARN;
   - **k6 load test**: 20 virtual users for 30 s on intake `/health` and the UI. Thresholds:
     p95 < 500 ms and errors < 1%;
   - **`make eval`** of o2c-app at the promoted commit, against the thresholds in
     `eval/thresholds.yaml` (ADR-010).
5. **Always:**
   - archive the reports;
   - scale `o2c-test` to 0, to free memory. Argo CD then shows test OutOfSync, because
     there is no self-heal; the next promotion syncs it back.

**promote-to-prod** (`Jenkinsfile.promote-prod`):
1. **Release in test.** The same verification as above, on the test images.
2. **Change request.**
   - When `SERVICENOW_URL` is set, a ServiceNow change request is created with the
     credential `servicenow`.
   - Until E13, it is recorded as `SKIPPED (E13 pending)`.
3. **Four-eyes approval.** An input step must be answered by a user other than the one
   who started the build:
   - the build fails if the starter approves;
   - the build fails if it was not started by a user at all.

   The audit trail records both users.
4. **Promotion PR, deploy.** The same PR, merge, sync and image check, for `envs/prod`.
5. **Smoke test and SLO check.**
   - The smoke test calls the `healthPath` of every service through its Service.
   - The SLO check is a short k6 run: 10 virtual users for 20 s, p95 < 800 ms, errors < 1%.
   - `SIMULATE_SLO_BREACH` sets the p95 threshold to 0 ms, which demonstrates the rollback.

### Rollback rule
- **Trigger:** anything fails, or the build is aborted, **after the prod PR was merged**.
  This covers the sync, the smoke test and the SLO check.
- **Action:** the pipeline opens a PR that reverts that merge commit, merges it when its
  checks pass, and syncs prod back.
- **It then checks that every Deployment runs the previous image again.**
- **Rollback is a Git revert, never a change in the cluster,** so Git keeps saying what
  runs.
- **The build stays failed.** Rolling back does not make a promotion successful.
- **No merge, no rollback.** When the SLO check fails but the build merged no PR
  (`envs/prod` already had the test images), this build did not change prod and there
  is nothing to revert. The log and `reports/rollback.txt` say "SLO breached; nothing to
  roll back: this run did not change prod (envs/prod unchanged)"; the build stays failed.

### Jenkins and Ansible run locally
- **The pipelines need what only this machine has:**
  - the kind API server (`o2c-control-plane:6443` on the Docker network `kind`);
  - the Argo CD API (port-forward);
  - the services (port-forward);
  - the Compose stack (`host.docker.internal`).

  A hosted CI runner (GitHub Actions) reaches none of them without exposing the cluster.
  GitHub Actions stays the PR gate and the release (ADR-015, ADR-016). Jenkins promotes.
- **Ansible** (o2c-infra `ansible/`, playbook `jenkins.yml`) builds and runs Jenkins
  idempotently. The runner is a container with the Docker socket, started by
  `scripts/ansible.sh`, so nothing but Docker is installed on the host. The role:
  - builds the image, tagged with a hash of its build context;
  - creates the container on the `kind` network, on `127.0.0.1:18090`;
  - copies the secrets in;
  - waits until the admin can log in.
- **The Jenkins image is pinned:**
  - Jenkins LTS 2.580.1 by digest;
  - every plugin by version;
  - kubectl, helm, gh, argocd, cosign, the docker CLI and uv by version and SHA-256.
- **Everything is configuration as code (JCasC):**
  - the users `abhinav` (admin) and `approver`;
  - matrix authorization: approver may only read, build and approve;
  - the credentials;
  - the audit-trail log in the volume;
  - the seed jobs, which read the Jenkinsfiles from o2c-infra main.
- **Secrets never enter Git.**
  - The role reads them from `o2c-app/.env` and the kubeconfig from
    `kind get kubeconfig --internal`.
  - It copies them into `/run/secrets` in the container, where JCasC reads them.
  - The pipelines get the `.env` without the `JENKINS_*` keys (credential `o2c-env`).
    They write it into the o2c-app checkout with hosts rewritten to
    `host.docker.internal`, and clean the workspace afterwards.
- **ZAP and k6 run as containers of the host's Docker in the Jenkins container's network
  namespace.** They reach the port-forwards on 127.0.0.1, and their reports are written
  into the workspace through the Jenkins volume.

## Consequences
- **Only releases made after this ADR can be promoted.** The promoted o2c-app commit must
  contain `scripts/promote_values.py` and `scripts/e2e_send_and_check.py`.
- **Before the first promotion to an environment:**
  - `platform/kind/secrets.sh o2c-<env>` (o2c-infra) creates the Secrets;
  - `make topics ENV=<env>` (o2c-app) creates the Kafka topics.
- **The test environment shares the Compose infrastructure with dev** (ADR-017): Postgres,
  Kafka with prefixes, LiteLLM, the ERP database. The integration test therefore creates
  a real order in the shared ERP mock.
- **The eval downloads the @champion models through the MLflow artifact proxy**
  (MLflow → SeaweedFS). The classifier's `model-00001-of-00002.safetensors` (~490 MB)
  streams through one sync gunicorn worker for over a minute, so the MLflow server runs
  with `--gunicorn-opts "--timeout 600"` and `--workers 4` (docker-compose.yml). With the
  30 s default the worker was killed mid-download (`WORKER TIMEOUT`, client
  `InvalidChunkLength`). `--workers` is MLflow's own flag: a `-w` inside
  `--gunicorn-opts` is overridden by the `-w` MLflow appends.
- **The load test and the SLO check measure through `kubectl port-forward`.** Their
  numbers show regressions between promotions, not production capacity.
- **Builds run on the Jenkins controller** (2 executors, no agents). The controller has
  the Docker socket, which is root-equivalent on the Docker host: acceptable for a local
  single-user machine, never for a shared Jenkins.
- **The Jenkinsfiles come from o2c-infra main,** so a pipeline change takes effect only
  after its PR is merged.

## Findings from E14 (2026-10-27)
The first promotions found problems that no unit test or PR gate had found. Each is fixed;
the evidence is in the Jenkins builds (promote-to-test #1-#9, promote-to-prod #1-#7) and
the linked documents.

- **kindnet loses the DNS queries of pods a NetworkPolicy selects** (o2c-infra
  `platform/README.md`). kind's CNI enforces NetworkPolicies in userspace: every DNS query
  of a selected pod waits in netfilter queue 101 for kindnet's verdict. kindnet lost
  verdicts (665 to 882 packets stuck, `no buffer space available`) and stalled under load,
  so Argo CD's lookups timed out for minutes, for `github.com` and for
  `argocd-repo-server` alike. The Argo CD chart creates a policy per component, so exactly
  Argo CD was affected.
  - Fix: Argo CD without NetworkPolicies (`global.networkPolicy.create: false`);
    `bootstrap.sh` warns about any remaining policy and restarts kindnet when packets
    wait in its queue. Measured: 12 of 800 lookups failed before, 0 of 1600 after.
  - Trade-off: any pod may reach Argo CD. Acceptable on a local single-node cluster only.
- **Retries cover blips, not outages.** The Argo CD and kubectl steps retry transient
  network errors (`retry_transient`, pauses 20, 40, 60 s; `transient_error` tells a
  lookup timeout from a failed sync and is tested by `jenkins/tests/transient_error.sh`).
  promote-to-prod #5 retried four times over six minutes and still failed: the retries
  could not fix kindnet's queue, only the platform fix did.
- **MLflow's sync gunicorn worker was killed mid-download.** The eval stage downloads the
  ~490 MB classifier weights through MLflow's artifact proxy; with the 30 s default worker
  timeout, promote-to-test #7 failed with `InvalidChunkLength` (`WORKER TIMEOUT` in
  MLflow). Fix: `--gunicorn-opts "--timeout 600"` and `--workers 4` (see Consequences).
- **The eval gate caught dependency drift that every test missed.** promote-to-test #8
  failed red team 7/8: RT-08 (40x quantity) was posted. Dependabot's major bumps had moved
  pandas to 3, MLflow 2.17's schema enforcement rejected every prediction, and the agent's
  0.0 fallback hid it (ADR-006). Fix: pins with a test that fails when they are lifted, a
  counter and a critical alert for every fallback, and an "anomaly model scores" step in
  the eval gate; promote-to-test #9 passed it. The full eval in promote-to-test is the
  gate that found it, not the PR checks.
- **Four-eyes approval works.** promote-to-prod #3 was approved by the user who started it
  and failed: "four-eyes rule: abhinav started this build and cannot approve it". Every
  later build records `started by: abhinav`, `approved by: approver` in
  `reports/approval.txt`.
- **The rollback works, and it was needed for real.** promote-to-prod #4 and #5 merged their
  prod PR (o2c-gitops `6c19131`, `e8b038f`), then the Argo CD sync failed on the kindnet DNS
  problem. Both reverted the merge (`b375173`, `ce21c35`), synced prod back and checked
  the previous images; both stayed failed. #6 promoted successfully (SLO check p95 53.6 ms).
- **An SLO breach without a merge has nothing to roll back.** promote-to-prod #7
  (`SIMULATE_SLO_BREACH`) found `envs/prod` up to date, so it merged no PR, and the SLO
  check then failed on purpose. The rollback rule correctly did nothing, but the build
  did not say so. It now does (Rollback rule, "No merge, no rollback"). To see a real
  rollback, the breach must be simulated on a build that changes prod.
- **ZAP warnings became failures.** promote-to-test #5-#9 passed ZAP with 8 (UI) and
  9 (agent API) warnings: no CSP, no anti-clickjacking or nosniff headers, the nginx
  version, no Permissions-Policy or site isolation, and Swagger UI from a CDN without
  SRI. The UI's nginx and the agent (`agent.security`, Swagger UI served by the agent)
  now send these headers, and the matching rules FAIL the gate. Locally only
  informational alerts remain. Self-hosted, Swagger UI is scanned by ZAP, which a CDN
  copy never was: the first package tried (fastapi-offline, Swagger UI 5.32.6) bundles
  DOMPurify 3.4.0 with known CVEs, and rule 10003 (now FAIL) caught it before it shipped.
  The agent uses fastapi-swagger (Swagger UI 5.34.0, DOMPurify 3.4.16), which follows
  Swagger UI's releases.
- **The CI gates were proven red on purpose** (docs/gates-evidence.md). That exercise
  found that no CI gate caught a fake AWS key in a test fixture. The lint job's gitleaks
  hook scans only staged changes, so it never failed in CI, and Trivy's secret scanner
  skips `tests/` directories. The lint job now runs gitleaks over every commit of the PR
  (ADR-015).
