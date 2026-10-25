import { useEffect, useState } from "react";
import { apiFetch } from "./access";

type ApiKey = {
  id: string; name: string; token_prefix: string; scopes: string[];
  project_id: string | null; expires_at: string; revoked_at: string | null;
  last_used_at: string | null;
};
type Project = { project_id: string; name: string };
const permissions = [
  ["read", "Projekte, Dateien und Resultate lesen"],
  ["write", "Projekte anlegen, bearbeiten und Dateien hochladen"],
  ["calculate", "Auswertungen und Exporte berechnen"],
  ["delete", "Projekte und Dateien löschen"],
] as const;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(path, init);
  if (!response.ok) throw new Error("Der API-Zugang konnte nicht aktualisiert werden. Bitte erneut versuchen.");
  return response.status === 204 ? undefined as T : response.json();
}

export function ApiKeysPanel() {
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [name, setName] = useState("Codex");
  const [projectId, setProjectId] = useState("");
  const [days, setDays] = useState("30");
  const [scopes, setScopes] = useState(["read", "write", "calculate"]);
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let active = true;
    Promise.all([
      request<ApiKey[]>("/v1/ckw/api-keys"),
      request<Project[]>("/v1/ckw/projects"),
    ]).then(([nextKeys, nextProjects]) => {
      if (active) { setKeys(nextKeys); setProjects(nextProjects); setLoaded(true); }
    }).catch((reason: Error) => { if (active) setError(reason.message); });
    return () => { active = false; };
  }, []);

  async function create(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setError(""); setToken(""); setCopied(false);
    try {
      const issued = await request<ApiKey & { token: string }>("/v1/ckw/api-keys", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, scopes, expires_in_days: Number(days), project_id: projectId || null }),
      });
      const { token: secret, ...metadata } = issued;
      setToken(secret);
      setKeys((current) => [metadata, ...current]);
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  }

  async function revoke(id: string) {
    setBusy(true); setError("");
    try {
      await request(`/v1/ckw/api-keys/${id}`, { method: "DELETE" });
      setKeys((current) => current.map((key) => key.id === id ? { ...key, revoked_at: new Date().toISOString() } : key));
      setToken("");
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  }

  return <section className="apiKeysPanel" aria-labelledby="api-keys-title">
    <h2 id="api-keys-title">API-Zugänge</h2>
    <p>Für Codex und eigene Skripte. Ein Schlüssel greift mit den gewählten Rechten auf deine gespeicherten Projekte zu.</p>
    {error && <p role="alert">{error}</p>}
    {!loaded && !error && <p>Zugänge laden…</p>}
    <form onSubmit={create}>
      <label>Name <input value={name} onChange={(event) => setName(event.target.value)} maxLength={80} required /></label>
      <label>Projekt <select value={projectId} onChange={(event) => setProjectId(event.target.value)}>
        <option value="">Alle eigenen Projekte, auch neue</option>
        {projects.map((project) => <option key={project.project_id} value={project.project_id}>{project.name}</option>)}
      </select></label>
      <label>Gültigkeit <select value={days} onChange={(event) => setDays(event.target.value)}>
        <option value="7">7 Tage</option><option value="30">30 Tage</option><option value="90">90 Tage</option>
      </select></label>
      <fieldset><legend>Rechte</legend>
        {permissions.map(([scope, label]) => <label key={scope}>
          <input type="checkbox" checked={scopes.includes(scope)} onChange={(event) => setScopes((current) => event.target.checked ? [...current, scope] : current.filter((item) => item !== scope))} /> {label}
        </label>)}
      </fieldset>
      <button type="submit" disabled={busy || !loaded || !name.trim() || !scopes.length}>Schlüssel erstellen</button>
    </form>
    {token && <div className="apiKeySecret" role="status">
      <p>Jetzt sicher ablegen. Dieser Schlüssel wird nur einmal angezeigt.</p>
      <label>Neuer API-Schlüssel <input aria-label="Neuer API-Schlüssel" type="password" readOnly value={token} autoComplete="off" spellCheck={false} /></label>
      <button type="button" onClick={() => navigator.clipboard.writeText(token).then(() => setCopied(true)).catch(() => setError("Kopieren nicht verfügbar. Den Schlüssel bitte aus dem Feld kopieren."))}>{copied ? "Kopiert" : "Schlüssel kopieren"}</button>
      <button type="button" onClick={() => setToken("")}>Anzeige schließen</button>
    </div>}
    <ul className="apiKeysList">{keys.map((key) => <li key={key.id}>
      <div><strong>{key.name}</strong> · <code>{key.token_prefix}…</code>
        <p>{key.scopes.join(", ")} · {key.project_id ? "Einzelnes Projekt" : "Eigene Projekte"} · gültig bis {new Date(key.expires_at).toLocaleDateString("de-CH")}</p>
        {key.last_used_at && <p>Zuletzt verwendet: {new Date(key.last_used_at).toLocaleString("de-CH")}</p>}
      </div>
      {key.revoked_at ? <span>Widerrufen</span> : new Date(key.expires_at).getTime() <= Date.now() ? <span>Abgelaufen</span> : <button type="button" disabled={busy} onClick={() => revoke(key.id)}>Widerrufen</button>}
    </li>)}</ul>
  </section>;
}
