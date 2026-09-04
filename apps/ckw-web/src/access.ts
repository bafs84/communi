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
  const callback = new URLSearchParams(window.location.search).has("code")
    && new URLSearchParams(window.location.search).has("state");
  if (callback && authClient) {
    await authClient.handleRedirectCallback();
    window.history.replaceState({}, document.title, window.location.pathname);
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

  const storedCsrf = sessionStorage.getItem(csrfStorageKey);
  const storedExpiry = sessionStorage.getItem(expiryStorageKey);
  if (storedCsrf && storedExpiry) {
    const expiry = JSON.parse(storedExpiry) as { idleExpiresAt: string; hardExpiresAt: string };
    context = { mode: "guest", csrfToken: storedCsrf, ...expiry };
    return context;
  }

  const response = await fetch(`${apiBase}/v1/ckw/guest/session`, {
    method: "POST",
    credentials: "include",
  });
  if (!response.ok) throw new Error("Die private Gastsitzung konnte nicht gestartet werden.");
  const guest = await response.json() as {
    csrfToken: string;
    idleExpiresAt: string;
    hardExpiresAt: string;
  };
  sessionStorage.setItem(csrfStorageKey, guest.csrfToken);
  sessionStorage.setItem(expiryStorageKey, JSON.stringify({
    idleExpiresAt: guest.idleExpiresAt,
    hardExpiresAt: guest.hardExpiresAt,
  }));
  context = { mode: "guest", ...guest };
  return context;
}

export function initializeAccess() {
  initialization ??= initialize();
  return initialization;
}

export function getApiBase() {
  return apiBase;
}

export async function apiFetch(input: string, init: RequestInit = {}) {
  const current = await initializeAccess();
  const headers = new Headers(init.headers);
  if (current.mode === "registered" && accessToken) {
    headers.set("Authorization", `Bearer ${accessToken}`);
  } else if ((init.method ?? "GET").toUpperCase() !== "GET" && current.csrfToken) {
    headers.set("X-CSRF-Token", current.csrfToken);
  }
  return fetch(input.startsWith("http") ? input : `${apiBase}${input}`, {
    ...init,
    headers,
    credentials: "include",
  });
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
}

export function logout() {
  authClient?.logout({
    logoutParams: { returnTo: `${window.location.origin}/raum/` },
  });
}
