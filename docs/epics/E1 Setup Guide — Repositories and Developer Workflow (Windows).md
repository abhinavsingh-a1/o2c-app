# E1 Setup Guide — Repositories and Developer Workflow (Windows)

Step-by-step instructions to set up the three Order-to-Cash Agent repositories on GitHub and the developer workflow around them. All commands are for **Windows PowerShell**. Repos are **public**: never commit passwords or API keys.

## Step 1 — Get ready

**1.1 Create a GitHub account** at github.com (skip if you have one).

**1.2 Install Git** from git-scm.com. Click Next through the installer; the defaults are fine.

**1.3 Check Git works**

```powershell
git --version
```

**1.4 Tell Git who you are** (use your GitHub email)

```powershell
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
git config --global init.defaultBranch main
```

**1.5 Install the GitHub CLI and log in**

```powershell
winget install GitHub.cli
gh auth login
```

Answers: **GitHub.com → HTTPS → Yes → Login with a web browser**.

## Step 2 — Create the three repositories

```powershell
mkdir o2c
cd o2c
gh repo create o2c-app --public --add-readme --license mit --gitignore Python --clone
gh repo create o2c-infra --public --add-readme --license mit --gitignore Terraform --clone
gh repo create o2c-gitops --public --add-readme --license mit --clone
gh repo list
```

| Repo | Holds |
| --- | --- |
| o2c-app | All services, ML pipelines, dbt, prompts, evals |
| o2c-infra | Terraform, Ansible, Helm chart |
| o2c-gitops | Helm values per environment (dev/test/prod) |

## Step 3 — Protect the main branch

Changes reach `main` only through a pull request (PR). Do this for **each of the three repos**:

1. Repo page → **Settings → Rules → Rulesets → New ruleset → New branch ruleset**
2. Ruleset name: `protect-main`; Enforcement status: **Active**
3. Target branches: **Add target → Include default branch**
4. Tick: **Restrict deletions**, **Block force pushes**, **Require linear history**, **Require a pull request before merging** with **Required approvals = 0** (GitHub does not let you approve your own PR)
5. Click **Create**

**Test it** (the push must fail):

```powershell
cd o2c-app
echo test >> README.md
git commit -am "test"
git push
git reset --hard origin/main
```

## Step 4 — CODEOWNERS

Says who is responsible for which folders. Find your username with `gh api user -q .login` and replace `@USERNAME` below.

```powershell
cd o2c-app
git checkout -b chore/codeowners
mkdir .github
```

Create `.github/CODEOWNERS`:

```
*                @USERNAME
/prompts/        @USERNAME
/eval/golden/    @USERNAME
```

Commit and merge (this is the standard PR workflow, see the cheat sheet at the end):

```powershell
git add .github/CODEOWNERS
git commit -m "chore: add CODEOWNERS"
git push -u origin chore/codeowners
gh pr create --fill
gh pr merge --squash --delete-branch
git checkout main
git pull
```

Repeat in **o2c-infra** with `*  @USERNAME` and `/terraform/  @USERNAME`, and in **o2c-gitops** with `*  @USERNAME`.

## Step 5 — Conventional Commits and PR title check

Every commit message and PR title starts with a type:

| Type | Use for |
| --- | --- |
| `feat:` | new feature |
| `fix:` | bug fix |
| `docs:` | documentation |
| `chore:` | setup or maintenance |
| `ci:` | pipeline changes |
| `test:` | tests |
| `refactor:` | code change without behaviour change |

Example: `feat: add PDF intake webhook`. With squash merges the PR title becomes the commit on `main`, so GitHub checks PR titles automatically.

In **each repo**:

```powershell
git checkout main
git pull
git checkout -b ci/pr-title-check
mkdir .github/workflows
```

Create `.github/workflows/pr-title.yml` (copy exactly, spaces matter):

```yaml
name: PR title
on:
  pull_request:
    types: [opened, edited, synchronize, reopened]
permissions:
  pull-requests: read
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: amannn/action-semantic-pull-request@v5
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

```powershell
git add .github/workflows/pr-title.yml
git commit -m "ci: check PR titles follow Conventional Commits"
git push -u origin ci/pr-title-check
gh pr create --fill
gh pr checks --watch
gh pr merge --squash --delete-branch
git checkout main
git pull
```

## Step 6 — Pre-commit hooks

Checks that run on every commit and stop it if something is wrong (for example a secret in the code).

**6.1 Install uv, pre-commit and Terraform**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Close and reopen PowerShell, then:

```powershell
uv tool install pre-commit
pre-commit --version
winget install Hashicorp.Terraform
terraform -version
```

**6.2 o2c-app** — create `.pre-commit-config.yaml` in the repo root:

```yaml
repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v5.0.0
    hooks:
      - id: trailing-whitespace
      - id: end-of-file-fixer
      - id: check-yaml
      - id: check-added-large-files
  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.18.0
    hooks:
      - id: gitleaks
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.6.9
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
  - repo: https://github.com/sqlfluff/sqlfluff
    rev: 3.2.0
    hooks:
      - id: sqlfluff-lint
        args: [--dialect, postgres]
```

gitleaks finds secrets, ruff checks Python, sqlfluff checks SQL. ESLint is added later with the React app.

**6.3 o2c-infra** — `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v5.0.0
    hooks:
      - id: trailing-whitespace
      - id: end-of-file-fixer
      - id: check-yaml
        args: [--allow-multiple-documents]
        exclude: ^helm/.*/templates/
      - id: check-added-large-files
  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.18.0
    hooks:
      - id: gitleaks
  - repo: https://github.com/antonbabenko/pre-commit-terraform
    rev: v1.96.1
    hooks:
      - id: terraform_fmt
      - id: terraform_validate
```

**6.4 o2c-gitops** — `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v5.0.0
    hooks:
      - id: trailing-whitespace
      - id: end-of-file-fixer
      - id: check-yaml
        args: [--allow-multiple-documents]
  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.18.0
    hooks:
      - id: gitleaks
```

**6.5 In each repo** (on a branch `chore/pre-commit`):

```powershell
pre-commit autoupdate
pre-commit install
pre-commit run --all-files
```

If something shows Fixed or Failed, run the last command again until all checks pass. Then commit `chore: add pre-commit hooks` and merge through a PR.

## Step 7 — Claude Code and CLAUDE.md

**7.1 Install Claude Code** in PowerShell (the installer shows no progress; wait until the prompt returns):

```powershell
irm https://claude.ai/install.ps1 | iex
```

If you see *'irm' is not recognized*, you are in CMD. Use this instead:

```bat
curl -fsSL https://claude.ai/install.cmd -o install.cmd && install.cmd && del install.cmd
```

Alternative: `winget install Anthropic.ClaudeCode` (does not auto-update, so the installer above is preferred). Check with `claude --version`.

**7.2 Create `CLAUDE.md`** in the o2c-app root (branch `docs/claude-md`). Claude Code reads it every time it starts:

```markdown
# Order-to-Cash Agent

Turns emailed purchase orders (PDF) into ERP sales orders.
Flow: intake → classify → extract → validate (LangGraph agent) → post order → update CRM.
Exceptions go to a human review UI and to ServiceNow.

## Repos
- o2c-app: all services, ML pipelines, dbt, prompts, evals (this repo)
- o2c-infra: Terraform, Ansible, Helm chart
- o2c-gitops: Helm values per environment (dev/test/prod)

## Stack
Python (FastAPI, Flask, LangGraph, LangChain), TypeScript (React),
PostgreSQL + pgvector, Kafka, LiteLLM, Langfuse, MLflow.

## Rules
- Never commit secrets. This repo is public. Use .env (git-ignored).
- All LLM calls go through LiteLLM, never directly to OpenAI.
- Tests use recorded LLM responses; no live LLM calls in tests.
- Every Kafka consumer must be idempotent (use the idempotency key).
- Commit messages and PR titles follow Conventional Commits.
- Work on a branch and open a PR; never push to main.
- Write tests for every new function.
- Record important decisions as ADRs in docs/adr/.

## Commands
(Added in E2 when the local stack exists.)
```

Commit `docs: add CLAUDE.md for Claude Code` and merge through a PR.

## Step 8 — Architecture Decision Records (ADRs)

An ADR records **why** an important decision was made. In o2c-app (branch `docs/adr-001`):

```powershell
mkdir docs/adr
```

Create `docs/adr/0001-three-repo-layout.md`:

```markdown
# ADR-001: Three repositories, build once, promote many

Date: 2026-10-14
Status: Accepted

## Context
The app must be deployed to Dev, Test and Prod with a clear audit trail.

## Decision
- o2c-app holds code; o2c-infra holds infrastructure; o2c-gitops holds what runs in each environment.
- A Docker image is built once per commit; the same image is promoted Dev → Test → Prod.
- Promotion is a Git commit in o2c-gitops, deployed by ArgoCD.

## Consequences
- Every deployment is visible in Git history.
- Code, infrastructure and releases are reviewed separately.
- Three repos to maintain instead of one.
```

Commit `docs: add ADR-001 three-repo layout` and merge through a PR.

## Cheat sheet — the workflow for every change

```powershell
git checkout main
git pull
git checkout -b <type>/<short-name>      # e.g. feat/intake-webhook
# ...edit files...
git add .
git commit -m "<type>: <what changed>"   # e.g. feat: add intake webhook
git push -u origin <type>/<short-name>
gh pr create --fill
gh pr checks --watch
gh pr merge --squash --delete-branch
git checkout main
git pull
```
