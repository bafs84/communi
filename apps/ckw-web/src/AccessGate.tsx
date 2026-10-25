import { reportError } from "./observability";
import { ApiKeysPanel } from "./ApiKeysPanel";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  endGuestSession,
  initializeAccess,
  logout,
  registerAndKeepProjects,
  type AccessContext,
} from "./access";

function remainingLabel(value?: string) {
  if (!value) return "";
  const milliseconds = new Date(value).getTime() - Date.now();
  if (milliseconds <= 0) return "abgelaufen";
  const minutes = Math.max(1, Math.ceil(milliseconds / 60_000));
  return minutes >= 60 ? `${Math.ceil(minutes / 60)} Std.` : `${minutes} Min.`;
}

export function AccessGate({ children }: { children: ReactNode }) {
  const [access, setAccess] = useState<AccessContext>();
  const [error, setError] = useState("");
  const [clock, setClock] = useState(Date.now());
  const [ended, setEnded] = useState(false);
  const [showApiKeys, setShowApiKeys] = useState(false);

  useEffect(() => {
    const changed = (event: Event) => setAccess((event as CustomEvent<AccessContext>).detail);
    window.addEventListener("communi-access-changed", changed);
    initializeAccess().then(setAccess).catch((reason) => { reportError(reason, "access"); setError(String(reason?.message ?? reason)); });
    return () => window.removeEventListener("communi-access-changed", changed);
  }, []);

  useEffect(() => {
    if (access?.mode !== "guest") return;
    const timer = window.setInterval(() => setClock(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, [access]);

  const expiryLabel = useMemo(() => {
    void clock;
    return remainingLabel(access?.hardExpiresAt);
  }, [access?.hardExpiresAt, clock]);

  if (error) return <div className="accessScreen"><h1>Zugang nicht verfügbar</h1><p>{error}</p></div>;
  if (!access) return <div className="accessScreen"><p>Privaten Arbeitsbereich vorbereiten…</p></div>;
  if (ended) {
    return (
      <div className="accessScreen">
        <h1>Sitzung beendet</h1>
        <p>Alle Gastprojekte, Dateien und Resultate dieser Sitzung wurden entfernt.</p>
        <button type="button" onClick={() => window.location.reload()}>Neue Gastsitzung</button>
      </div>
    );
  }

  return (
    <>
      <div className={`accessBanner ${access.mode}`}>
        {access.mode === "guest" ? (
          <>
            <span><strong>Private Gastsitzung</strong> · Löschung nach 60 Min. Inaktivität, spätestens in {expiryLabel}.</span>
            <div>
              <button type="button" className="bannerPrimary" onClick={() => registerAndKeepProjects().catch((reason) => { reportError(reason, "access"); setError(String(reason?.message ?? reason)); })}>Registrieren & behalten</button>
              <button type="button" className="bannerSecondary" onClick={() => endGuestSession().then(() => setEnded(true)).catch((reason) => { reportError(reason, "access"); setError(String(reason?.message ?? reason)); })}>Sitzung beenden</button>
            </div>
          </>
        ) : (
          <>
            <span><strong>Gespeicherter Arbeitsbereich</strong>{access.email ? ` · ${access.email}` : ""}</span>
            <div>
              <button type="button" className="bannerSecondary" aria-expanded={showApiKeys} onClick={() => setShowApiKeys((current) => !current)}>API-Zugänge</button>
              <button type="button" className="bannerSecondary" onClick={logout}>Abmelden</button>
            </div>
          </>
        )}
      </div>
      {access.mode === "registered" && showApiKeys && <ApiKeysPanel />}
      {children}
    </>
  );
}
