# ADR-017: GitOps platform: environments as namespaces, topic prefixes, policies

Date: 2026-10-26
Status: Accepted

## Context
The release (ADR-016) produces signed images and a values PR per digest, but nothing ran
them. The platform needs:
- dev, test and prod environments;
- deployments driven only from Git;
- a cluster that refuses images it cannot trust.

It runs locally on one machine, which already runs the infrastructure (Postgres, Kafka,
SeaweedFS, LiteLLM, MLflow, Langfuse) in Docker Compose.

## Decision

### Environments are namespaces in one kind cluster
- **One namespace per environment** in the kind cluster `o2c`: `o2c-dev`, `o2c-test` and
  `o2c-prod` (o2c-gitops `platform/namespaces`).
- **Each environment has its values in o2c-gitops** `envs/<env>/<service>.yaml`. The
  files are the same across environments except `o2cEnv` and `replicaCount`.
- **dev runs one replica per service. test and prod run 0 replicas** until something is
  promoted to them. They exist so that promotion, manual sync and the policies can be
  exercised without three copies of every pod on a laptop.
- **Promotion is a PR** that copies a verified digest (`scripts/verify_image.sh`) from
  dev to test, then prod.

### Kafka topic and consumer group prefixes
- **The environments share one Kafka (Compose), so names carry the environment.**
  `O2C_ENV` (dev, test or prod) prefixes every topic and consumer group: `docs.received`
  becomes `dev.docs.received`, and group `classifier` becomes `dev.classifier`.
- **Without `O2C_ENV`** (Compose, tests, scripts on the host) the names are unchanged.
- **The code keeps using logical names.** `o2c_common` applies the prefix in one place:
  - `EventProducer` prefixes the topics it sends to;
  - `EventConsumer` prefixes subscriptions and the group, including the processed-events
    key. The environments may share the o2c database, so the same event is processed
    once per environment, not once overall;
  - the DLQ tool and `python -m o2c_common.topics` take `--env` (`make topics ENV=dev`).
- **Metrics labels, DLQ headers and the analytics-sink's raw tables keep the logical
  names.** Dashboards, alerts and dbt models work in every environment, and the
  Prometheus target says which environment it is.
- **The chart renders `o2cEnv` as `O2C_ENV`.** It rejects unknown values and refuses
  `env.O2C_ENV`, so the environment cannot be misspelled in one file.

### Infrastructure stays in Docker Compose locally
- **Postgres, Kafka, SeaweedFS, LiteLLM, MLflow and Langfuse are not deployed to kind.**
  Pods reach them on `host.docker.internal`, Docker Desktop's name for the host.
  - Kafka gets a third listener, advertised as `host.docker.internal:29092`, next to
    `kafka:19092` (Compose) and `localhost:9092` (host).
  - `o2c-infra platform/kind/secrets.sh` rewrites the hosts in the database URLs.
- **Why:**
  - The stack already runs and holds the local data (ERP history, contracts, MLflow
    models, Langfuse traces).
  - Running a second copy in the cluster would double the memory on one machine. Keeping
    the two in sync would also add operators (Strimzi, CloudNativePG) that the cloud
    replaces with managed services anyway (MSK, RDS, S3).
  - The cluster then holds exactly what is ours: the services, their policies and their
    delivery.
- **What this costs:**
  - Environments share infrastructure. Topics and groups are separated by the prefix;
    databases and buckets are not. This is acceptable while test and prod run 0
    replicas.
  - Isolation per environment comes with the cloud.

### Credentials stay out of Git
- **`secrets.sh <namespace>` creates one Secret per service** from `o2c-app/.env`, with
  only the keys that service reads (the LLM services get their own LiteLLM virtual key).
  It also creates `agent-users` (reviewer accounts, mounted as a file).
- **It never prints a value,** and running it again updates in place.
- **The values files name the Secrets** (`secretName`, `secretFiles`) and list their
  keys in comments.
- **Argo CD does not manage these Secrets,** so a sync never prunes them.

### Admission policies (Kyverno, Enforce, namespaces `o2c-*`)
- **verify-image-signatures:** keyless, issuer `https://token.actions.githubusercontent.com`,
  subject `https://github.com/abhinavsingh-a1/o2c-app/.github/workflows/release.yml@refs/heads/main`.
  The release signs with cosign v3, which stores a Sigstore bundle as an OCI referrer,
  so the rule uses `type: SigstoreBundle`.
- **restrict-image-registries:** only `ghcr.io/abhinavsingh-a1/o2c-*`.
- **require-image-digest:** no `:latest`, and no tag without `@sha256:`.
- **require-run-as-non-root:** `runAsNonRoot: true` on the pod or every container, and
  no `runAsUser: 0`.
- **require-resource-limits:** CPU and memory limits on every container.

Notes on the policies:
- They match Pods; Kyverno also checks Deployments, so a bad Deployment fails at sync,
  not later at pod creation.
- `allowExistingViolations: false`: a violating pod cannot be updated either.
- o2c-gitops `platform/tests` holds three manifests that must be rejected, each by a
  different policy:
  - an unsigned Docker Hub image;
  - a root pod on a signed image;
  - an unsigned artifact in the allowed registry.

  `verify-policies.sh` runs them, plus every dev service, as server-side dry runs.
- The policies are `kyverno.io/v1` ClusterPolicies. Kyverno 1.19 marks them deprecated
  in favour of the CEL-based ValidatingPolicy and ImageValidatingPolicy; moving to those
  is a later change.

### Argo CD: app of apps, one Application per service and environment
- **The root Application `o2c-root` is the only manifest applied by hand**
  (`platform/kind/bootstrap.sh`). Everything else comes from o2c-gitops
  `platform/argocd/apps`:
  - AppProjects: `o2c-platform` may create namespaces and ClusterPolicies; `o2c-services`
    may only create Deployments and Services in the three namespaces;
  - the namespaces (sync wave -2), then the policies (wave -1);
  - the ApplicationSet (wave 0). Child-Application health is enabled in Argo CD, so the
    waves wait.
- **The ApplicationSet creates `<service>-<env>`** from `envs/<env>/<service>.yaml`, with
  two sources: the shared chart from o2c-infra `helm/o2c-service` (main) and the values
  file from o2c-gitops (main).
- **Sync modes:**
  - **dev** syncs automatically, with prune and selfHeal: merging the release's gitops
    PR deploys, and a change made by hand in the cluster is reverted.
  - **test and prod** sync only by hand, after a promotion PR. Git says what should run;
    a person decides when.
- **Pinned versions:** Kyverno chart 3.9.1 (v1.19.1) and Argo CD chart 10.10.3 (v3.5.5),
  from the projects' OCI registries.

## Consequences
- **Order of merges:**
  1. o2c-infra (chart 0.4.0, platform scripts);
  2. o2c-app (`O2C_ENV`), then a release so the images carry the prefix;
  3. o2c-gitops.

  Images built before this change ignore `O2C_ENV`. Until a new release, dev pods would
  join the Compose services' unprefixed consumer groups.
- **After a Compose restart,** `make topics ENV=dev` creates the dev topics. Kafka has no
  volume, so topics are recreated after every `make down`.
- **The platform needs:**
  - Docker Desktop (`host.docker.internal`);
  - public ghcr.io images, which Kyverno verifies on admission against Sigstore's public-good
    instance (Fulcio, Rekor, TUF).
- **Memory:** measured on kind, Kyverno uses about 340 MiB and Argo CD about 310 MiB,
  next to about 1.4 GiB for the eight dev services.
