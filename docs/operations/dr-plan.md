# Disaster recovery plan

Last updated: 2026-10-28. Owner: Abhinav.

The system runs locally:
- Docker Compose for the stores;
- the kind cluster for the dev, test and prod services;
- Jenkins in Docker.

The AWS design (RDS, MSK, S3 with Object Lock, EKS) exists only as plans and the
Terraform audit module, which is not applied (ADR-014). This plan covers both. The local
procedures can be run today; the AWS ones are marked.

## Objectives
| Tier | What | RPO (max data loss) | RTO (max downtime) |
|---|---|---|---|
| 1 | Orders, review decisions, audit archive (Postgres `o2c`, `erp`; S3 `o2c-audit`, `o2c-documents`) | 24 h locally (daily dump); 5 min on AWS (RDS point-in-time recovery) | 4 h |
| 2 | Models and experiments (MLflow; ghcr.io model artifacts) | 0 for production models: pinned by digest in ghcr.io (ADR-016) | 1 h (images carry the model) |
| 3 | Delivery state (o2c-gitops, Argo CD, Kyverno, Jenkins) | 0: Git is the source; Jenkins jobs are code (JCasC) | 2 h (bootstrap scripts) |
| 4 | Observability and traces (Prometheus, Grafana, Langfuse, LiteLLM spend logs, `raw.*`) | Best effort | Best effort |

Kafka holds events for 7 to 30 days and is **not** a store of record. Every consumer is
idempotent and the producers' state is in Postgres and S3. Locally Kafka has no volume, so
a `make down` loses it by design.

## Backups per store
| Store | Holds | Backup (local, today) | Backup (AWS, planned) |
|---|---|---|---|
| Postgres: `o2c`, `erp`, `mlflow`, `litellm`, `langfuse` | Checkpoints, review queue and decisions, `raw` and `analytics`, ERP orders, model registry, spend logs, Langfuse metadata | **None automated.** Manual `pg_dumpall` (below). | RDS automated snapshots plus PITR (7-35 days); a manual snapshot before every prod promotion |
| SeaweedFS `s3data`: `o2c-documents`, `o2c-audit`, `mlflow`, `langfuse` | PDFs, the hash-chain archive, model artifacts, trace payloads | **None automated.** Manual `docker run ... tar` of the volume (below). | S3 versioning; Object Lock on `o2c-audit` (COMPLIANCE in prod); cross-region replication for tier 1 |
| ClickHouse `chdata` (Langfuse) | Traces | None (tier 4) | Langfuse Cloud or a ClickHouse backup; best effort |
| Models | `o2c-doc-classifier` v3, `o2c-quantity-anomaly` v3 | ghcr.io OCI artifacts pinned in `models.lock.json` | Same (or ECR) |
| Images | 8 services | ghcr.io by digest, signed | Same (or ECR) |
| Git (o2c-app, o2c-infra, o2c-gitops) | Code, infrastructure, environment state | GitHub | GitHub |
| Jenkins `o2c-jenkins-home` | Build history, reports, audit trail | None. The configuration is code; history is evidence. | Volume snapshot, or audit trail shipped to the log store |
| `.env`, `local/users.json` | Secrets, reviewer accounts | Password manager copy (manual) | AWS Secrets Manager |

### Manual backup (local, Git Bash)
```bash
mkdir -p backups
docker compose exec -T postgres pg_dumpall -U o2c | gzip > "backups/pg-$(date +%F).sql.gz"
docker compose stop s3 && \
  MSYS_NO_PATHCONV=1 docker run --rm -v o2c-app_s3data:/data -v "$PWD/backups:/backup" alpine \
  tar czf "/backup/s3data-$(date +%F).tgz" -C /data . ; docker compose start s3
uv run python ml/verify_audit.py | tee "backups/audit-head-$(date +%F).txt"   # record the head hash
```
- `backups/` is git-ignored: it holds
  order data.
- The volume name `o2c-app_s3data` assumes the Compose project name `o2c-app`; check it
  with `docker volume ls`.
- *Not executed in E16:* these backup and restore commands were written from the Compose
  configuration and have not been run end to end.

## Restore steps

### A. Local stack lost (disk, `make clean`)
1. `make up`, then `make buckets` and `make topics` (and `ENV=dev|test|prod` for kind).
2. Postgres:
   ```bash
   gunzip -c backups/pg-<date>.sql.gz | docker compose exec -T postgres psql -U o2c -d postgres
   ```
3. S3:
   ```bash
   docker compose stop s3
   MSYS_NO_PATHCONV=1 docker run --rm -v o2c-app_s3data:/data -v "$PWD/backups:/backup" alpine \
     sh -c "rm -rf /data/* && tar xzf /backup/s3data-<date>.tgz -C /data"
   docker compose start s3
   ```
4. Audit chain:
   - `uv run python ml/verify_audit.py` must report the head recorded at backup time.
   - Then `docker compose restart audit-archiver`. At start it loads the chain head and
     re-indexes (ADR-014).
5. Replay:
   - Events after the backup are gone if Kafka was lost too. Re-send the affected
     documents with `make send PDF=...`.
   - Intake's idempotency key is the content hash, so documents processed before the
     backup are skipped, and the rest are processed again.
   - The ERP is idempotent on the key (ADR-004).
6. Models: if MLflow data is lost, the images still carry the pinned models. For local
   Compose, register again from the ghcr.io artifacts, or retrain with
   `make ml-pipeline-local`.
7. Verify:
   - `make eval-quick` passes;
   - `make audit-check` passes;
   - `make dbt-build` rebuilds the marts from `raw`.

### B. kind cluster lost
```bash
make cluster-down; make cluster-up
../o2c-infra/platform/kind/bootstrap.sh                 # Kyverno, Argo CD, root app from o2c-gitops
for ns in o2c-dev o2c-test o2c-prod; do ../o2c-infra/platform/kind/secrets.sh $ns; done
```
Argo CD redeploys dev from Git. Sync test and prod by hand to the digests in o2c-gitops.
Verify with `make audit-check` and the `kubectl get deploy` image check in
[runbooks.md](runbooks.md) §5.

### C. Jenkins lost
`make -C ../o2c-infra jenkins-up` rebuilds the image and the configuration (JCasC). Build
history and the audit trail are lost with the volume. The promotion evidence remains in
the o2c-gitops PRs and the archived reports, if they were copied out.

### D. AWS: RDS snapshot restore and Kafka replay (planned)
**Status: to be executed in the AWS session (E3).** No AWS environment exists yet
(project plan E16: "restore RDS snapshot into Test and replay Kafka").

Planned test:
1. Take a manual snapshot of prod RDS.
2. Restore it into a new instance in the test account (`aws rds restore-db-instance-from-db-snapshot`).
3. Point the test environment's database URLs at the new instance (Secrets Manager plus
   External Secrets).
4. Reset the test consumer groups to the snapshot time:
   `kafka-consumer-groups.sh --reset-offsets --to-datetime <snapshot time> --execute --group test.<group> --all-topics`.
   Idempotency skips what the snapshot already contains.
5. Measure RPO (the last order in the snapshot vs. the last order in prod) and RTO (start
   to `make eval-quick` passing in test).
6. Record the result here and in an ADR finding.

## Exercises
| Exercise | Last run | Result |
|---|---|---|
| Release rollback (GitOps revert) | 2026-10-27, promote-to-prod #4 and #5 | Automatic revert PRs #20 and #22; previous images verified (ADR-018) |
| Audit chain restart (crash between writes) | E15 | Resumed at seq 47 without duplicates (ADR-014) |
| Local backup and restore (A) | Not run | - |
| RDS snapshot restore and Kafka replay (D) | **To be executed in the AWS session (E3)** | - |
