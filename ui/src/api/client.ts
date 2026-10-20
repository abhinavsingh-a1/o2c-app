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
      clearSession(); // token missing or expired: back to the login page
      window.location.assign("/login");
    }
    return response;
  },
};

// "/api" is forwarded to the agent by the Vite dev server.
export const api = createClient<paths>({ baseUrl: "/api" });
api.use(authMiddleware);
