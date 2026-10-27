# Runbooks

Last updated: 2026-10-28. Commands run from `o2c-app` in **Git Bash** unless a block says
otherwise. `../o2c-infra` and `../o2c-gitops` are the sibling checkouts. `ENV=dev|test|prod`
selects the topic prefix of an environment (ADR-017); without it, the Compose pipeline is
meant.

Never paste a secret into a terminal log, a ticket or a chat. The commands below read
secrets from `.env`, and none of them prints one.

Contents:
1. [DLQ growing](#1-dlq-growing)
2. [Consumer lag](#2-consumer-lag)
3. [LLM provider outage or budget exhausted](#3-llm-provider-outage-or-budget-exhausted)
4. [A model misbehaves](#4-a-model-misbehaves)
5. [Roll back a release](#5-roll-back-a-release)
6. [Argo CD DNS on kind](#6-argo-cd-dns-on-kind)
7. [Secret rotation](#7-secret-rotation)
8. [Audit chain broken](#8-audit-chain-broken)

---

## 1. DLQ growing
**Symptoms:**
- alert `DeadLetterQueueReceivedMessages`;
- the dashboard's DLQ panel rises;
- `EventErrorRateHigh` for one group;
- orders missing from the review queue.

**Checks:**
```bash
make prom-alerts                         # what fires
make dlq-list                            # Compose; `make dlq-list ENV=dev` for kind dev
docker compose logs --tail 200 classifier extractor agent | grep -iE "error|dlq"
```
`dlq-list` prints, per message, the topic it would be replayed to, `failed_at` and the
start of the `error` header.

Grafana's DLQ and event-error panels show which group failed and why
(`o2c_events_dlq_total{group, reason}`). The reasons:
- `invalid_envelope`: the message is not an Envelope (a producer bug);
- `permanent_error`: the handler refused the payload, e.g. no purchase order;
- `retries_exhausted`: a dependency was down (LiteLLM, ERP, S3).

The analytics copy is queryable too:
`docker compose exec postgres psql -U o2c -d o2c -c "select headers->>'original_topic', headers->>'error', ingested_at from raw.dlq order by ingested_at desc limit 20"`.

**Fix:**
- `retries_exhausted`: fix or restart the dependency (runbook 3 for the LLM).
- `permanent_error` and `invalid_envelope`: fix the code, then release (runbook 5 order).
  A message that is invalid by content stays in the DLQ. Record why.

**Replay:** `make dlq-replay` (or `ENV=dev`). The DLQ is replayed into each message's
original topic. Consumers are idempotent (CLAUDE.md), so a message that was already
processed is skipped.

**Verification:**
- `make dlq-list` shows no new messages after the replay;
- `o2c_events_dlq_total` stops rising;
- the orders appear in the review UI or as posted in the ERP;
- the alert resolves after 10 minutes.

## 2. Consumer lag
**Symptoms:**
- `ConsumerLagHigh` (lag > 50 for 5m);
- `ServiceDown` for a consumer;
- documents accepted by intake but no orders.

**Checks:**
```bash
make lag                                 # lag per group and partition
make ps                                  # is the consumer running / restarting?
docker compose logs --tail 100 <classifier|extractor|agent|analytics-sink|audit-archiver>
kubectl --context kind-o2c -n o2c-dev get pods                  # kind environments
kubectl --context kind-o2c -n o2c-dev logs deploy/<service> --tail 100
```

**Fix:**
- **Consumer crashed:** `docker compose restart <service>`, or in kind
  `kubectl -n o2c-<env> rollout restart deploy/<service>` (dev self-heals from Git).
- **Consumer slow because LLM calls are slow:** see runbook 3. The p95 of
  `o2c_event_handler_seconds` per group shows it.
- **Topics missing after a Compose restart** (Kafka has no volume): `make topics`, and
  `make topics ENV=dev` (or test, prod) for kind.

**Verification:**
- `make lag` shows the lag falling to 0;
- `ConsumerLagHigh` resolves;
- `o2c_events_consumed_total{group}` rises again.

## 3. LLM provider outage or budget exhausted
**Symptoms:**
- `EventErrorRateHigh` for `classifier` or `extractor`, then DLQ messages
  (`retries_exhausted`);
- agent explanations replaced by plain issue lists;
- LiteLLM log shows 429, 5xx or "budget exceeded".

**Checks:**
```bash
docker compose logs --tail 200 litellm | grep -iE "budget|429|fallback|error"
uv run python ml/litellm_spend.py        # spend, budget, share used, reset date per key
curl -s http://localhost:4000/health/liveliness
```
Also check:
- Grafana "LLM cost per service";
- the [OpenAI status page](https://status.openai.com).

**Fix:**
- **Provider outage:**
  - LiteLLM already retries and then falls back once from `o2c-extract` (gpt-6-luna) to
    `o2c-extract-previous` (gpt-4.1-mini) (`router_settings.fallbacks` in
    `local/litellm/config.yaml`). Gate runs record fallbacks (`x-litellm-attempted-fallbacks`).
  - For a longer outage, point `o2c-extract` at another configured alias's model. Copy
    its `litellm_params`, then `docker compose restart litellm`.
  - Any alias change needs a full `make eval` before it is kept (ADR-008).
  - After recovery, replay the DLQ (runbook 1).
- **Budget exhausted for one key** (ADR-011; only that service is affected):
  - Find the cause first. `LlmCostHigh` fires at $0.50 per hour, and a loop shows as a
    rising `o2c_llm_calls_total{service}`.
  - Then either wait for the monthly reset, or raise the budget in `SERVICES` of
    `ml/litellm_keys.py` and run `uv run python ml/litellm_keys.py`. It corrects
    existing keys' budgets.
- **OpenAI account credit exhausted:** top up in the OpenAI dashboard. Nothing in the
  repo changes.
- **gpt-4.1-mini shutdown (2027-01-14):** remove `o2c-extract-previous` by 2027-01-06
  (ADR-008).

**Verification:**
- `curl -s http://localhost:8300/health`;
- `make send PDF=ml/data/train/pos/PO-0001.pdf` gets an order decided (send a file not
  sent before, or the duplicate is skipped);
- `ml/litellm_spend.py` shows spend rising under the right key;
- the DLQ is empty after the replay.

## 4. A model misbehaves
**Symptoms:**
- `AnomalyScoringFailing`;
- `TouchlessRateLow` with one issue code dominating (`o2c_issues_total{code}`);
- a jump in `o2c_documents_classified_total{classified_by="gpt"}` or `doc_type="other"`;
- `make eval` failing a model check.

**Checks:**
```bash
make eval-quick                          # extraction (10 POs), red team, injection, anomaly scores
docker compose logs agent | grep -iE "anomaly model|anomaly scoring failed"
uv run --group ml python -c "from mlflow import MlflowClient as C; c=C('http://localhost:5000'); [print(n, c.get_model_version_by_alias(n,'champion').version) for n in ('o2c-quantity-anomaly','o2c-doc-classifier')]"
cat models.lock.json                     # what the images bake (environments)
```

**Fix:**
- **Locally (Compose):**
  1. Move `@champion` back to the last good version. Only after confirming that version
     with the promotion rule's MLflow tags (`promoted_to_champion`, `promotion_reason`):
     ```bash
     uv run --group ml python -c "from mlflow import MlflowClient as C; C('http://localhost:5000').set_registered_model_alias('o2c-quantity-anomaly','champion','<good version>')"
     docker compose restart agent        # classifier: docker compose restart classifier
     ```
  2. Normally `@champion` is moved only by the promotion rule. Record a manual rollback
     in an ADR finding.
- **Environments (kind):** the model is baked into the image by digest (ADR-016).
  - Roll back the release (runbook 5), or
  - publish the good version (`make models-publish` after setting `@champion`), open a
    PR with `models.lock.json`, then the release and promotion.
- **LLM prompt or model regression:** revert the alias or prompt PR. `make eval` must
  pass before merging again.
- **Dependency drift (pandas 3 case, ADR-006):** restore the pins. The pin tests must
  fail without them.

**Verification:**
- `make eval-quick`: "anomaly model: scoring errors 0", normal < 0.8, 20x > 0.8;
- the alert resolves;
- the touchless rate recovers.

## 5. Roll back a release
**Rule:** rollback is a Git revert in o2c-gitops, never a change in the cluster (ADR-018).

- **Automatic:** promote-to-prod reverts its own merge when the sync, the smoke test or
  the SLO check fails. It opens a `revert(prod)` PR, merges it, syncs and checks the
  images. Proven by o2c-gitops #20 and #22.

**Checks:**
```bash
gh pr list --repo abhinavsingh-a1/o2c-gitops --state merged --limit 10
git -C ../o2c-gitops fetch && git -C ../o2c-gitops log --oneline origin/main -- envs/prod | head
kubectl --context kind-o2c -n o2c-prod get deploy -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.containers[0].image}{"\n"}{end}'
```

**Fix (manual):**
```bash
cd ../o2c-gitops && git switch main && git pull
git switch -c rollback/prod-<reason>
git revert --no-edit <promotion merge commit>
git push -u origin HEAD
gh pr create --title "revert(prod): roll back promotion <sha>" --body "<why>"
gh pr checks --watch && gh pr merge --squash
```
Then sync prod (prod has no auto-sync):
```bash
../o2c-infra/platform/kind/argocd-ui.sh
```
In the UI, sync the `*-prod` applications, or run `argocd app sync -l` per app.

**Verification:**
- every prod Deployment runs the previous digest (the `kubectl` command above);
- `make audit-check` passes for the reverted images;
- the smoke test: `/health` of each service.

## 6. Argo CD DNS on kind
**Symptoms:** Argo CD sync or refresh fails for minutes with
`lookup github.com on 10.96.0.10:53: ... i/o timeout` (also for `argocd-repo-server`),
while a new pod resolves fine. Retries in Jenkins do not help.

**Cause:** kindnet loses the DNS queries of pods selected by a NetworkPolicy (o2c-infra
`platform/README.md`).

**Checks:**
```bash
kubectl --context kind-o2c get networkpolicy -A          # expect: none
docker exec o2c-control-plane nft list set inet kindnet-network-policies podips-v4   # expect: no elements
docker exec o2c-control-plane cat /proc/net/netfilter/nfnetlink_queue                # 3rd column (waiting) should stay 0
```

**Fix:**
```bash
../o2c-infra/platform/kind/bootstrap.sh       # idempotent: Argo CD without NetworkPolicies; restarts kindnet if packets wait
```
If a policy reappeared, find where it came from (`kubectl get networkpolicy -A -o wide`;
the `kfp-install` target removes kubeflow's). Then re-run the Jenkins build with
**Build Now** (not "Restart from Stage", E14 troubleshooting #21).

**Verification:**
- the `nfnetlink_queue` waiting count stays 0;
- the Argo CD apps refresh within seconds;
- the promotion build passes its sync stage.

## 7. Secret rotation
Secrets live only in `o2c-app/.env` (git-ignored). Copies are made from it:
- Compose reads `.env` at start;
- kind Secrets come from `secrets.sh`;
- Jenkins gets its copy through Ansible.

After any rotation:
1. Update `.env`. Quote values with special characters in single quotes, and check with
   `uv run python ml/check_env.py` (it prints line numbers, never values).
2. Redistribute:
   ```bash
   docker compose up -d                                  # recreates containers whose env changed
   for ns in o2c-dev o2c-test o2c-prod; do ../o2c-infra/platform/kind/secrets.sh $ns; done
   kubectl --context kind-o2c -n o2c-dev rollout restart deploy   # pods read Secrets at start
   make -C ../o2c-infra jenkins-up                       # changed secrets restart Jenkins
   ```

| Secret | Rotate | Then |
|---|---|---|
| `OPENAI_API_KEY` | Create a new key in the OpenAI dashboard (project with budget limit), put it in `.env`, revoke the old one | `docker compose up -d litellm`. Only LiteLLM holds it (CLAUDE.md: all calls through LiteLLM). |
| `LITELLM_KEY_<SERVICE>` | Delete the key in LiteLLM (admin UI http://localhost:4000/ui, or `POST /key/delete` with the master key), then `uv run python ml/litellm_keys.py` prints the new key **once** into your terminal. Copy it into `.env`. | Redistribute (above) |
| `LITELLM_MASTER_KEY` | New random value in `.env` | `docker compose up -d litellm`. Service keys stay valid. Admin scripts use the new value. |
| `JENKINS_ADMIN_PASSWORD`, `JENKINS_APPROVER_PASSWORD` | New values in `.env` | `make -C ../o2c-infra jenkins-up` (JCasC applies them) |
| `JENKINS_GITHUB_TOKEN` (o2c-gitops contents and PRs, o2c-app attestations) | New fine-grained token on GitHub, expiry set; revoke the old | `make -C ../o2c-infra jenkins-up` |
| `GITOPS_TOKEN` (o2c-app Actions secret for the release's gitops PR) | New fine-grained token | `gh secret set GITOPS_TOKEN --repo abhinavsingh-a1/o2c-app` (reads the value from stdin; nothing echoed) |
| `gh auth` token with `write:packages` (models publish) | `gh auth refresh -h github.com -s write:packages,read:packages` | - |
| `AUTH_JWT_SECRET` | New random value (at least 32 bytes) | Restart the agent. All sessions end, and reviewers log in again. |
| `INTAKE_HMAC_SECRET` | New value, shared with the sender (Zapier, E12) | Restart intake; update the sender at the same time |
| DB, S3, Grafana, Langfuse passwords | New value in `.env` and in the store itself (e.g. `ALTER ROLE`) | Redistribute |

**Verification:**
- the old secret is rejected (e.g. a `curl` with the old LiteLLM key gives 401);
- services are healthy;
- `make eval-quick` passes;
- a Jenkins login works for both users;
- `gitleaks git` finds nothing new.

If a secret was **exposed** (pushed, pasted), rotate first and investigate second. A
pushed secret stays in Git history and forks, and rewriting history does not un-leak it.

## 8. Audit chain broken
**Symptoms:** `uv run python ml/verify_audit.py` or `make audit-check` prints
`BROKEN after N valid entries: <object> line <n> ...` and exits 1.

**Checks:**
```bash
uv run python ml/verify_audit.py                        # first broken entry: object, line, expected seq, reason
docker compose logs --tail 200 audit-archiver
docker compose exec postgres psql -U o2c -d o2c -c "select count(*) from audit_archive"
```

What the reason means:
- **hash mismatch at an entry:** that entry was changed.
- **prev_hash mismatch at the next entry:** an entry was changed and its hash
  recomputed.
- **seq gap:** an entry was deleted or inserted (ADR-014).

**Fix:**
1. **Treat it as a security incident.** Do not "repair" the chain. Freeze the bucket:
   no writes by hand, keep the archiver running.
2. Compare the broken entry with its sources: the Kafka event (if still within retention)
   or `raw.events` by `event_id`, and `public.review_decisions` by id.
3. Restore the original objects from a backup, or from S3 object versions on AWS
   (Object Lock keeps earlier versions, ADR-014).
4. If the entry cannot be restored, record the break in an incident note. Start a new
   chain segment whose first entry references the last valid hash and the incident; this
   is a code change, not a hand edit.

**Verification:**
- `ml/verify_audit.py` prints `chain intact: N entries ... head seq ...`;
- the head hash matches the last one recorded by an auditor;
- `make audit-check` passes.
