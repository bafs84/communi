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

  useEffect(() => {
    initializeAccess().then(setAccess).catch((reason) => setError(String(reason?.message ?? reason)));
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
              <button type="button" className="bannerPrimary" onClick={() => registerAndKeepProjects().catch((reason) => setError(String(reason?.message ?? reason)))}>Registrieren & behalten</button>
              <button type="button" className="bannerSecondary" onClick={() => endGuestSession().then(() => setEnded(true)).catch((reason) => setError(String(reason?.message ?? reason)))}>Sitzung beenden</button>
            </div>
          </>
        ) : (
          <>
            <span><strong>Gespeicherter Arbeitsbereich</strong>{access.email ? ` · ${access.email}` : ""}</span>
            <button type="button" className="bannerSecondary" onClick={logout}>Abmelden</button>
          </>
        )}
      </div>
      {children}
    </>
  );
}
