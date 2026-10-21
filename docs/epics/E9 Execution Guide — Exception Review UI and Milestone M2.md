# E9 Execution Guide — Exception Review UI and Milestone M2

How the review UI was built on top of the agent, in the order it was done. Key files are shown in their **final** form; the repository holds the complete code. Problems and fixes are in **Troubleshooting** at the end. All commands run in Git Bash from the `o2c-app` folder.

**Result of E9:** a React + TypeScript app (http://localhost:5180 via Docker, http://localhost:5173 in development) where a signed-in finance clerk sees the queue of orders waiting for a decision, opens one to see the original PDF next to GPT's explanation, the issues and the extracted data, and approves, edits or rejects it. Every decision is stored in an append-only audit table and shown as history. With E9, **Milestone M2** is reached: the whole pipeline runs as containers, end to end, crash-safe.

| Layer | What was added |
| --- | --- |
| Agent API | Review queue read model, PDF streaming, typed responses, login (JWT), audit trail |
| TypeScript client | Types generated from the agent's OpenAPI (`make gen-api`) |
| UI | Login page, queue page, detail page with PDF viewer, decision panel, edit form, history |
| Packaging | Vitest tests, nginx container, Compose service `ui` on port 5180 |

## Step 1 — API for the UI

```bash
git checkout -b feat/review-api
uv add --package agent boto3
```

- **Read model:** table `review_queue` (one row per order, upserted on every status change) so the list is a single fast query instead of a scan over checkpoints.
- **PDF streaming:** `GET /reviews/{key}/document` returns the PDF from S3 through the API, instead of a direct S3 link, so the login also protects documents and S3 stays private.
- **Typed responses:** `ReviewSummary`, `ReviewDetail`, `DecisionResult` (later `ReviewRequest`, `AuditEntry`, `LoginResponse`) appear in `/openapi.json`.

| Endpoint | Purpose |
| --- | --- |
| `GET /reviews?status=awaiting_review\|posted\|rejected\|all` | Queue list |
| `GET /reviews/{key}` | Detail for one order |
| `GET /reviews/{key}/document` | Original PDF (inline) |
| `POST /reviews/{key}` | Decision (approve / edit / reject) |
| `GET /reviews/{key}/audit` | Decision history (Step 4) |
| `POST /auth/login`, `GET /auth/me` | Login (Step 4) |

Typed client:

```bash
pnpm --dir ui add openapi-fetch
pnpm --dir ui add -D openapi-typescript
make gen-api   # openapi-typescript http://localhost:8400/openapi.json -o src/api/schema.d.ts
```

`ui/src/api/types.ts` gives short names to generated types. `PurchaseOrder` is derived from `ReviewDetail["purchase_order"]`, because FastAPI may generate separate input and output variants of models containing decimals.

## Step 2 — Queue and detail screens

```bash
git checkout -b feat/review-screens
pnpm --dir ui add react-router
```

**`ui/vite.config.ts`** (final) — the dev server forwards `/api` to the agent, so the browser never needs CORS:

```typescript
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: "http://localhost:8400",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  test: {
    environment: "jsdom",
  },
});
```

Files: `ui/src/pages/QueuePage.tsx` (filterable table, refresh, links), `ui/src/pages/DetailPage.tsx` (PDF left; explanation, issues, order fields, lines right; flagged lines red), `ui/src/format.ts` (`money` in de-AT format, `label`), `ui/src/index.css`.

The PDF is downloaded through the typed client as a blob (`parseAs: "blob"`), turned into a temporary `URL.createObjectURL` address and shown in an `<iframe>`; this keeps working once requests carry a login token. Each `useEffect` uses a `cancelled` flag so slow, outdated answers never overwrite newer ones.

## Step 3 — Decisions: approve, reject, edit

**`ui/src/decimal.ts`** — exact money arithmetic in whole units (JavaScript floats give 170.5 × 2.15 = 366.57499…):

```typescript
export function normalize(value: string): string {
  return value.trim().replace(",", ".");
}

export function isDecimal(value: string): boolean {
  return /^\d+(\.\d+)?$/.test(normalize(value));
}

function toUnits(value: string, scale: number): number {
  return Math.round(Number(normalize(value)) * scale);
}

export function lineTotal(quantity: string, unitPrice: string): string {
  if (!isDecimal(quantity) || !isDecimal(unitPrice)) return "0.00";
  const product = toUnits(quantity, 1000) * toUnits(unitPrice, 100);
  const cents = Math.floor((product + 500) / 1000);
  return (cents / 100).toFixed(2);
}

export function sumMoney(values: string[]): string {
  const cents = values.reduce((sum, v) => sum + Math.round(Number(v) * 100), 0);
  return (cents / 100).toFixed(2);
}
```

`+ 500` before dividing by 1000 is round-half-up, the same rule as the agent (`ROUND_HALF_UP`).

Components:

- `ui/src/components/EditForm.tsx` — every field starts with the extracted value; line totals and order total are always calculated, never typed; basic validation before sending; the agent re-checks everything.
- `ui/src/components/DecisionPanel.tsx` — Approve (with confirmation), Reject (reason required), Edit order; buttons disabled while a request runs; HTTP 409 shows *already decided (maybe in another tab)*.
- `DetailPage.tsx` shows the panel only while `awaiting_review`, then shows a notice and reloads the order after a decision.

Verified: reject without reason refused; edit with the agreed price → all checks run again → posted; approve → posted; deciding the same order in a second tab → 409 message, nothing posted.

## Step 4 — Login and audit trail

```bash
git checkout -b feat/review-login-audit
uv add --package agent pyjwt
python -c "import secrets; print(secrets.token_hex(32))"   # -> AUTH_JWT_SECRET in .env
uv run python -m agent.auth add-user anna "Anna Huber"
uv run python -m agent.auth add-user max "Max Gruber"
```

`.env.example` gets `AUTH_JWT_SECRET=replace-me`; `.gitignore` gets `local/users.json`. Settings: `auth_jwt_secret`, `auth_users_file` (default `local/users.json`).

Design:

- **Users file** `local/users.json` (git-ignored) holds names and **scrypt** password hashes with a random salt; never passwords.
- **`POST /auth/login`** returns a signed **JWT** (HS256, 8 hours) with username and display name.
- **`CurrentUser`** dependency on every `/reviews` endpoint: no or invalid token → **401**. `/health` stays public for Kubernetes.
- **`ReviewRequest`** has no `reviewer` field: the server always takes the reviewer from the token, so nobody can decide under another name.
- Reject without a reason → **422**, enforced on the server as well as in the UI.
- **`review_decisions`** is an **append-only** audit table: who, when, action, comment, PO before, PO after (for edits), result and sales order number. `GET /reviews/{key}/audit` returns it.
- On AWS the login is replaced by Amazon Cognito (also JWT); the protected endpoints stay the same.

Compose additions for the `agent` service:

```yaml
    environment:
      <<: *pipeline-env
      ERP_BASE_URL: http://erp-mock:8100
      AUTH_JWT_SECRET: ${AUTH_JWT_SECRET}
      AUTH_USERS_FILE: /app/local/users.json
    ports:
      - "8400:8400"
    volumes:
      - ./local/users.json:/app/local/users.json:ro
```

The users file must exist before the container starts; otherwise Docker creates a folder with that name.

UI side:

- `ui/src/auth.ts` keeps the session in `sessionStorage` (cleared when the tab closes), wrapped in `try/catch`.
- `ui/src/api/client.ts` adds a middleware that sends `Authorization: Bearer <token>` on every request and returns to `/login` on any 401.
- `ui/src/pages/LoginPage.tsx`, `RequireLogin` in `App.tsx`, the signed-in name and **Sign out** in the top bar.
- `DecisionPanel.tsx` no longer asks for a name.
- `ui/src/components/History.tsx` shows the decision history below the lines.

**`ui/src/api/client.ts`** (final):

```typescript
import createClient, { type Middleware } from "openapi-fetch";

import { clearSession, getSession } from "../auth";
import type { paths } from "./schema";

const authMiddleware: Middleware = {
  onRequest({ request }) {
    const session = getSession();
    if (session) request.headers.set("Authorization", `Bearer ${session.token}`);
    return request;
  },
  onResponse({ response }) {
    if (response.status === 401 && window.location.pathname !== "/login") {
      clearSession();
      window.location.assign("/login");
    }
    return response;
  },
};

export const api = createClient<paths>({ baseUrl: "/api" });
api.use(authMiddleware);
```

Verified: `/reviews` without login → `{"detail":"login required"}`; login via `/docs` and **Authorize** → 200; wrong password → 401; a decision by Anna appears in the history and in `review_decisions`.

## Step 5 — UI tests, container and M2 demo

```bash
git checkout -b feat/ui-tests-container
pnpm --dir ui add -D vitest jsdom @testing-library/react @testing-library/jest-dom
```

- `ui/src/decimal.test.ts` proves 170.5 × 2.15 = **366.58** (same as the agent), comma input, invalid input and drift-free sums.
- `ui/src/components/EditForm.test.tsx` (by Claude Code) checks rendering, live totals, `onSave` payload, validation and Cancel.
- `make ui-test` runs them (`vitest run`).

**`ui/nginx.conf`**:

```nginx
server {
    listen 8080;
    root /usr/share/nginx/html;

    location /api/ {
        proxy_pass http://agent:8400/;
        proxy_set_header Host $host;
        client_max_body_size 20m;
    }

    location / {
        try_files $uri /index.html;
    }
}
```

**`ui/Dockerfile`** (multi-stage; the final image has no Node.js, source or `node_modules`):

```dockerfile
FROM node:22-alpine AS build
ARG PNPM_VERSION=10
RUN npm install -g pnpm@${PNPM_VERSION}
WORKDIR /repo
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY ui/package.json ui/
RUN pnpm install --frozen-lockfile
COPY ui ui
RUN pnpm --dir ui build

FROM nginxinc/nginx-unprivileged:1.27-alpine
COPY ui/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /repo/ui/dist /usr/share/nginx/html
EXPOSE 8080
```

`.dockerignore`: the line `ui` was replaced by `ui/dist`. Compose service `ui` publishes `5180:8080` (8080 is reserved by Windows) and depends on `agent`.

**Plan change:** the M2 demo runs on Docker Compose instead of kind. Running every service in kind would also require Kafka, Postgres and the other infrastructure inside the local cluster or network workarounds; the containers are identical, and all services are deployed to Kubernetes once, properly, on AWS EKS in E3 with Helm and ArgoCD.

## Milestone M2 demo

| Scenario | Steps | Expected result |
| --- | --- | --- |
| Touchless posting | `make send` a clean PO below 10,000 EUR | Appears under **Posted** without any human action |
| Exception decided in the UI | Send a price-deviation PO, open it, **Edit order** with the agreed price | All checks run again → posted; history shows Anna Huber · edit · posted |
| Crash mid-processing | Send three clean POs, `docker compose kill agent` after \~8 s, `docker compose up -d agent` | ERP query returns exactly three `OPEN` orders, one per key: none lost, none duplicated |

Crash check:

```bash
docker compose exec postgres psql -U o2c -d erp -c "SELECT idempotency_key, bstnk, status FROM vbak WHERE idempotency_key IN ('<key1>', '<key2>', '<key3>');"
```

**Makefile targets added in E9:** `gen-api`, `ui-test`.

## Troubleshooting — problems hit during E9 and their fixes

| # | Symptom | Cause | Fix |
| --- | --- | --- | --- |
| 1 | http://localhost:5173 does not open | The Vite dev server was not running, or chose another port | Keep `make ui` running; open the exact `Local:` address it prints |
| 2 | Queue shows *Could not load the review queue* | Agent not reachable | `make ps`; http://localhost:8400/health must return ok |
| 3 | `docker compose ps agent` shows nothing | `ps` hides stopped containers; the agent had crashed | `docker compose ps -a agent` and `docker compose logs agent --tail 40` |
| 4 | Agent crash: `TypeError: 'function' object is not subscriptable` at `-> list[AuditEntry]` | A method named `list` inside `ReviewStore` hid Python's built-in `list` for later annotations in the class | Renamed the method to `query` (and its callers and test fakes); never name methods after built-ins |
| 5 | 10 agent tests fail with `401 Unauthorized` after adding login | The Step 1 tests called `/reviews` without a token | Expected: proves the protection works; Claude Code updated the fixtures to log in (Step 4.13) |
| 6 | Risk: Docker creates a folder named `users.json` | Bind-mounting a file that does not exist yet | Create users with `agent.auth add-user` before starting the container |
| 7 | Port 8080 unusable for the UI container | Windows reserves that port range | Published the UI on 5180 |

**Lessons:**

- Generate API types from the backend so field renames break the build, not the UI at runtime.
- Never calculate money with floating-point numbers; calculate in whole units and round like the backend.
- Take identity from the login token, never from the request body.
- Keep audit tables append-only.
- Run `make test` before rebuilding containers.
