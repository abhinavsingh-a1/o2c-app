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
