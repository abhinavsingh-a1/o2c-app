# ADR-016: Supply chain: digests, signatures, SBOMs, provenance and baked models

Date: 2026-10-23
Status: Accepted

## Context
The PR gates (ADR-015) say a change is fine. Nothing yet says that the image running in
an environment is the one those gates built. Images were built locally and tagged by
hand, the services downloaded their models from MLflow at start, and nothing recorded
what an image contained or where it came from.

## Decision

### Tags vs digests
- **Tags are names, digests are content.** A tag (`:latest`, `:1.4`, even `:<git-sha>`)
  can be pushed again and then points at different bytes. A digest
  (`@sha256:...`) is the hash of the manifest and cannot change.
- **The release pushes `ghcr.io/abhinavsingh-a1/o2c-<service>:<git-sha>` only.** The
  SHA tag is for people (which commit is this?); there is no `latest`.
- **Environments deploy by digest.** o2c-gitops `envs/<env>/<service>.yaml` holds
  `image.repository` and `image.digest`, and the chart renders `repository@digest`
  (o2c-infra chart 0.3.0; a malformed digest fails the render). Signatures and
  attestations are made for the digest, so what is verified is exactly what runs.

### The release (`.github/workflows/release.yml`)
On every push to main (and `workflow_dispatch`, main only), for each of intake,
classifier, extractor, agent, analytics-sink, audit-archiver, erp-mock and ui:
1. build with Buildx (GitHub Actions cache) and load the image;
2. Trivy scan, failing on CRITICAL with a fix available (same rule and `.trivyignore`
   as the PR gate), before anything is pushed;
3. push `:<git-sha>` from the same build (builder cache) and take its digest;
4. Syft SBOM (SPDX JSON), uploaded as a workflow artifact and attested to the digest;
5. build provenance with `actions/attest-build-provenance`;
6. cosign keyless signature of the digest.

Attestations are pushed to the registry next to the image (`push-to-registry`).
Permissions: `contents: read`, `packages: write`, `id-token: write`,
`attestations: write`, on the image job only. The final job collects the digests and
opens a PR in o2c-gitops updating `envs/dev/<service>.yaml`, with auto-merge on. Dev
follows main through that PR, whose checks (yamllint, helm render) must pass. Test and
prod are promoted by PRs copying a verified digest.

`hello`, `ml` and `mlflow` are not released: hello is a smoke-test image, the ml image
runs pipelines (E10), and the MLflow server runs only locally.

### Keyless signing
- **No signing keys exist.** cosign gets a short-lived certificate from Sigstore's
  Fulcio for the workflow's GitHub OIDC token. The signature is logged in the public
  Rekor transparency log.
- **The certificate names the signer.** It carries the identity
  `https://github.com/abhinavsingh-a1/o2c-app/.github/workflows/release.yml@refs/heads/main`
  and the issuer `https://token.actions.githubusercontent.com`. A valid signature
  therefore proves that this workflow, on main, built that digest. A PR, a fork, a
  laptop or another workflow cannot produce one.
- **There is nothing to rotate or leak.** The trade-off is a dependency on Sigstore's
  public infrastructure at signing and verification time.

### SBOM
- **Syft lists every OS and Python/Node package in the image** (SPDX 2.3 JSON).
- **It is kept twice:** as a workflow artifact for people, and as a signed attestation
  on the digest for tools.
- **When a new CVE is published,** the SBOMs answer "which released images contain
  package X at version Y" without pulling or rescanning the images.

### Provenance
`actions/attest-build-provenance` signs a SLSA provenance statement: the repository,
commit, workflow, trigger and runner that produced the digest. Together with the
signature it ties a running image to a commit that passed the PR gates.

### Models baked into images
- **Models are published as OCI artifacts.** `ml/publish_models.py`
  (`make models-publish`) takes the `@champion` version of `o2c-doc-classifier` and
  `o2c-quantity-anomaly` from MLflow and pushes the MLflow model directory with oras to
  `ghcr.io/abhinavsingh-a1/o2c-model-<name>:v<version>`.
  - Login is with the token from `gh auth token`, which needs `write:packages`.
  - A version that is already published is never pushed again.
  - The source annotation links the package to this repo, so the release's
    `GITHUB_TOKEN` can pull it.
- **`models.lock.json` pins name, version, repository, tag and digest.** It is
  committed; changing the model in production means a PR that changes this file.
- **`docker/python-service.Dockerfile` pulls the model in a build stage.**
  - The release passes `MODEL_REF=<repository>@<digest>` from the lock file for the
    classifier and the agent.
  - The stage refuses a reference without a digest and checks that an MLflow model
    arrived.
  - The registry token is a BuildKit secret, so it is in no layer or cache.
  - The model lands in `/app/models/<name>`, owned by root and read-only for the
    service user.
- **Services load the baked model first** (`o2c_common.models.baked_model`) and fall
  back to MLflow `@champion` only if no baked model exists: local Compose, PR builds,
  and running the services without Docker. A baked model that fails to load does not
  fall back: the service degrades as before (GPT only, anomaly score 0.0) and warns.
- **Why baked:**
  - The image digest now covers code and model, so the signature, SBOM and provenance
    cover the model too.
  - Pods start without MLflow being reachable, and a rollback is a digest change.
  - The model can no longer change under a running deployment because someone moved
    `@champion`.
- **Promotion is unchanged.** The promotion rule still moves `@champion` in MLflow.
  After that, `make models-publish`, a PR with the new `models.lock.json`, the release,
  then the gitops PR. "Restart the agent to load a new champion" becomes "deploy the
  new digest" in environments; locally it still applies.

### How to verify
`scripts/verify_image.sh ghcr.io/abhinavsingh-a1/o2c-<service>@sha256:<digest>`
prints PASS only if all three checks hold:
- `cosign verify` with the certificate identity of `release.yml` on main and the GitHub
  OIDC issuer;
- `gh attestation verify` of the build provenance, signer workflow `release.yml`,
  source ref main, GitHub-hosted runner;
- the same for the SPDX SBOM attestation.

Tags are rejected: verifying a tag says nothing about what it points to tomorrow.
Cluster admission (a policy controller enforcing the same identity) is planned with
the cloud cluster; until then, the script is run before promoting a digest to test or
prod.

## Consequences
- **Setup steps outside Git:**
  - the `GITOPS_TOKEN` secret: a fine-grained token for o2c-gitops with contents and
    pull requests write;
  - auto-merge and branch protection with required checks on o2c-gitops;
  - the cluster needs pull access to the ghcr.io packages, which are private unless
    made public (imagePullSecret).
- **`models.lock.json` must exist before the first release.** Run
  `make models-publish` after `gh auth refresh -s write:packages`, and commit the lock
  file.
- **Images are bigger:** the classifier image carries the DistilBERT MLflow model (about
  520 MB).
- **The chart needs 0.3.0** (`image.digest`, `command`/`args`) in o2c-infra before the
  dev values deploy anything. Until the first release, `image.digest` is empty and the
  chart falls back to the default tag.
