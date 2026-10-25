import { reportError, reportHttpFailure } from "./observability";

export type AccessMode = "guest" | "registered";

export type AccessContext = {
  mode: AccessMode;
  csrfToken?: string;
  idleExpiresAt?: string;
  hardExpiresAt?: string;
  email?: string;
};

type RuntimeConfig = {
  apiBaseUrl?: string;
  auth0Domain?: string;
  auth0ClientId?: string;
  auth0Audience?: string;
  memberCkwUrl?: string;
  glitchtipDsn?: string;
  glitchtipEnvironment?: string;
  release?: string;
};

type Auth0Client = {
  handleRedirectCallback(): Promise<void>;
  isAuthenticated(): Promise<boolean>;
  getTokenSilently(options?: unknown): Promise<string>;
  getUser(): Promise<{ email?: string } | undefined>;
  loginWithRedirect(options?: unknown): Promise<void>;
  logout(options?: unknown): void;
};

declare global {
  interface Window {
    __COMMUNI_CONFIG__?: RuntimeConfig;
    auth0?: {
      createAuth0Client(options: unknown): Promise<Auth0Client>;
    };
  }
}

const runtime = window.__COMMUNI_CONFIG__ ?? {};
const apiBase = (runtime.apiBaseUrl ?? import.meta.env.VITE_API_BASE ?? "").replace(/\/$/, "");
const csrfStorageKey = "communi-ckw-guest-csrf";
const expiryStorageKey = "communi-ckw-guest-expiry";
let authClient: Auth0Client | undefined;
let accessToken = "";
let context: AccessContext | undefined;
let initialization: Promise<AccessContext> | undefined;

function isMemberRoute() {
  return window.location.pathname === "/raum/ckw" || window.location.pathname.startsWith("/raum/ckw/");
}

function authConfigured() {
  return Boolean(runtime.auth0Domain && runtime.auth0ClientId && runtime.auth0Audience);
}

async function createAuthClient(required = false) {
  if (!authConfigured()) return undefined;
  if (!window.auth0?.createAuth0Client) {
    if (required) throw new Error("Die Authentifizierung konnte nicht geladen werden.");
    return undefined;
  }
  return window.auth0.createAuth0Client({
    domain: runtime.auth0Domain,
    clientId: runtime.auth0ClientId,
    cacheLocation: "memory",
    useRefreshTokens: true,
    authorizationParams: {
      audience: runtime.auth0Audience,
      redirect_uri: `${window.location.origin}${isMemberRoute() ? "/raum/ckw/" : "/"}`,
    },
  });
}

async function claimGuestSession(csrfToken: string) {
  const response = await fetch(`${apiBase}/v1/ckw/guest/claim`, {
    method: "POST",
    credentials: "include",
    headers: {
      Authorization: `Bearer ${accessToken}`,
      "X-CSRF-Token": csrfToken,
    },
  });
  if (!response.ok && response.status !== 401) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail ?? "Das Gastprojekt konnte nicht übernommen werden.");
  }
  sessionStorage.removeItem(csrfStorageKey);
  sessionStorage.removeItem(expiryStorageKey);
}

async function initialize(): Promise<AccessContext> {
  authClient = await createAuthClient();
  const callbackParameters = new URLSearchParams(window.location.search);
  const callback = (callbackParameters.has("code") || callbackParameters.has("error"))
    && callbackParameters.has("state");
  if (callback && authClient) {
    try {
      await authClient.handleRedirectCallback();
    } catch {
      const missingApiGrant = callbackParameters.get("error") === "invalid_request"
        && (callbackParameters.get("error_description") ?? "").includes("not authorized to access resource server");
      throw new Error(missingApiGrant
        ? "Die Anmeldung ist für diesen Arbeitsbereich noch nicht freigeschaltet. Bitte kontaktiere den Betreiber."
        : "Die Anmeldung konnte nicht abgeschlossen werden. Bitte versuche es erneut.");
    } finally {
      // OAuth error descriptions and transaction state must not remain in URLs.
      window.history.replaceState({}, document.title, window.location.pathname + window.location.hash);
    }
  }

  if (authClient && await authClient.isAuthenticated()) {
    accessToken = await authClient.getTokenSilently({
      authorizationParams: { audience: runtime.auth0Audience },
    });
    const csrfToken = sessionStorage.getItem(csrfStorageKey);
    if (csrfToken) await claimGuestSession(csrfToken);
    const user = await authClient.getUser();
    context = { mode: "registered", email: user?.email };
    if (!isMemberRoute() && runtime.memberCkwUrl) {
      window.location.replace(runtime.memberCkwUrl);
      return new Promise<AccessContext>(() => undefined);
    }
    return context;
  }

  if (isMemberRoute() && window.location.hostname !== "localhost" && window.location.hostname !== "127.0.0.1") {
    if (!authClient) {
      throw new Error("Auth0 ist für den geschützten CKW-Bereich nicht konfiguriert.");
    }
    await authClient.loginWithRedirect({
      appState: { returnTo: window.location.href },
      authorizationParams: { redirect_uri: `${window.location.origin}/raum/ckw/` },
    });
    return new Promise<AccessContext>(() => undefined);
  }

  return refreshGuestSession();
}

let guestRefresh: Promise<AccessContext & { resumed?: boolean }> | undefined;

async function requestGuestSession(): Promise<AccessContext & { resumed?: boolean }> {
  const response = await fetch(`${apiBase}/v1/ckw/guest/session`, {
    method: "POST",
    credentials: "include",
  });
  if (!response.ok) throw new Error("Die private Gastsitzung konnte nicht gestartet werden.");
  const guest = await response.json() as {
    resumed?: boolean;
    csrfToken: string;
    idleExpiresAt: string;
    hardExpiresAt: string;
  };
  sessionStorage.setItem(csrfStorageKey, guest.csrfToken);
  sessionStorage.setItem(expiryStorageKey, JSON.stringify({
    idleExpiresAt: guest.idleExpiresAt,
    hardExpiresAt: guest.hardExpiresAt,
  }));
  const next: AccessContext & { resumed?: boolean } = { mode: "guest", ...guest };
  context = next;
  window.dispatchEvent(new CustomEvent("communi-access-changed", { detail: next }));
  return next;
}

function refreshGuestSession() {
  guestRefresh ??= requestGuestSession().finally(() => { guestRefresh = undefined; });
  return guestRefresh;
}

export function initializeAccess() {
  initialization ??= initialize();
  return initialization;
}

export function getApiBase() {
  return apiBase;
}

export async function apiFetch(input: string, init: RequestInit = {}) {
  await initializeAccess();
  const method = (init.method ?? "GET").toUpperCase();
  const url = input.startsWith("http") ? input : `${apiBase}${input}`;
  // Never disclose the access token to an arbitrary download URL.
  if (new URL(url, window.location.origin).origin !== new URL(apiBase || window.location.origin, window.location.origin).origin) {
    throw new Error("Die API-Adresse gehört nicht zu diesem Arbeitsbereich.");
  }
  async function send(forceTokenRefresh = false) {
    const headers = new Headers(init.headers);
    if (context?.mode === "registered" && authClient) {
      try {
        accessToken = await authClient.getTokenSilently({
          authorizationParams: { audience: runtime.auth0Audience },
          ...(forceTokenRefresh ? { cacheMode: "off" } : {}),
        });
      } catch (error) {
        reportError(error, "auth");
        throw new Error("Die Anmeldung ist abgelaufen. Bitte erneut anmelden; die ausgewählten Dateien bleiben erhalten.");
      }
      headers.set("Authorization", `Bearer ${accessToken}`);
    } else if (method !== "GET" && context?.csrfToken) {
      headers.set("X-CSRF-Token", context.csrfToken);
    }
    try {
      return await fetch(url, { ...init, headers, credentials: "include" });
    } catch (error) {
      reportError(error, "network");
      throw error;
    }
  }
  let response = await send();
  if (response.status === 401) {
    const mode = context!.mode;
    let retry = mode === "registered";
    if (mode === "guest") {
      const renewed = await refreshGuestSession();
      // A 401 occurs before any mutation. Only a stateless billing upload or
      // project listing may restart after the old guest workspace disappeared.
      retry = Boolean(renewed.resumed) || (method === "POST" && input === "/api/vzev/invoices")
        || (method === "GET" && input === "/api/projects");
    }
    if (retry) {
      try { response = await send(true); }
      catch (error) { reportHttpFailure(input, method, 401, mode, "failed"); throw error; }
    }
    reportHttpFailure(input, method, 401, mode, response.ok ? "recovered" : "failed");
    if (response.status === 401) {
      return new Response(JSON.stringify({ detail: mode === "guest"
        ? "Die Gastsitzung ist abgelaufen oder wurde durch einen Serverneustart beendet. Bitte das Projekt erneut öffnen oder anlegen."
        : "Die Anmeldung ist nicht mehr gültig. Bitte erneut anmelden." }), {
        status: 401, headers: { "Content-Type": "application/json" },
      });
    }
  }
  if (!response.ok) reportHttpFailure(input, method, response.status, context!.mode);
  return response;
}

export async function registerAndKeepProjects() {
  authClient ??= await createAuthClient(true);
  if (!authClient) throw new Error("Auth0 ist nicht konfiguriert.");
  await authClient.loginWithRedirect({
    appState: { claimGuest: true },
    authorizationParams: {
      screen_hint: "signup",
      redirect_uri: `${window.location.origin}/`,
    },
  });
}

export async function endGuestSession() {
  const response = await apiFetch("/v1/ckw/guest/session", { method: "DELETE" });
  if (!response.ok) throw new Error("Die Sitzung konnte nicht beendet werden.");
  sessionStorage.removeItem(csrfStorageKey);
  sessionStorage.removeItem(expiryStorageKey);
  context = undefined;
  initialization = undefined;
}

export function logout() {
  authClient?.logout({
    logoutParams: { returnTo: `${window.location.origin}/raum/` },
  });
}
