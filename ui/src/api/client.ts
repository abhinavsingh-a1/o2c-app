import createClient from "openapi-fetch";

import type { paths } from "./schema";

// "/api" is forwarded to the agent by the Vite dev server (configured in Step 2).
export const api = createClient<paths>({ baseUrl: "/api" });
