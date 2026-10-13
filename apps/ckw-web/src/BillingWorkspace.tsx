import { reportError } from "./observability";
import { FormEvent, useEffect, useState } from "react";
import { Download, PlugZap, Upload } from "lucide-react";
import { WorkspaceNavigation } from "./WorkspaceNavigation";
import type { Workspace } from "./workspaceRouting";
import { apiFetch } from "./access";

type BillingWorkspaceProps = {
  onNavigate: (workspace: Workspace) => void;
  onGenerated?: () => Promise<unknown>;
};

type BillingProject = {
  project_id: string;
  name: string;
  config?: { billing?: Record<string, unknown> };
};

type BillingResult = {
  result_id: string;
  filename: string;
  byte_size: number;
  created_at: string;
  download_url: string;
};

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

export function BillingWorkspace({ onNavigate, onGenerated }: BillingWorkspaceProps) {
  const [csvFiles, setCsvFiles] = useState<File[]>([]);
  const [issuer, setIssuer] = useState("vZEV");
  const [invoiceDate, setInvoiceDate] = useState(todayIso());
  const [paymentTerms, setPaymentTerms] = useState("Zahlbar gemäss vZEV-Vereinbarung");
  const [year, setYear] = useState("");
  const [pvTariff, setPvTariff] = useState("");
  const [pvTariffMethod, setPvTariffMethod] = useState("80%-pauschale");
  const [peakAllocationMode, setPeakAllocationMode] = useState("community_peak_share");
  const [feedInEnergyRate, setFeedInEnergyRate] = useState("");
  const [feedInHknRate, setFeedInHknRate] = useState("");
  const [indirectMeterCount, setIndirectMeterCount] = useState("");
  const [noVat, setNoVat] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [history, setHistory] = useState<BillingResult[]>([]);

  async function refreshHistory() {
    const projectsResponse = await apiFetch("/api/projects");
    if (!projectsResponse.ok) return;
    const projects = await projectsResponse.json() as BillingProject[];
    const billingProjects = projects.filter((project) => project.config?.billing && Object.keys(project.config.billing).length > 0);
    const results = await Promise.all(billingProjects.map(async (project) => {
      const response = await apiFetch(`/v1/ckw/projects/${project.project_id}/results`);
      return response.ok ? response.json() as Promise<BillingResult[]> : [];
    }));
    setHistory(results.flat().sort((left, right) => right.created_at.localeCompare(left.created_at)));
  }

  useEffect(() => {
    refreshHistory().catch((error) => reportError(error, "billing-history"));
  }, []);

  async function downloadHistoryResult(result: BillingResult) {
    const response = await apiFetch(result.download_url);
    if (!response.ok) throw new Error("Die gespeicherte Abrechnung konnte nicht geladen werden.");
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = result.filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  async function generateInvoices(event: FormEvent) {
    event.preventDefault();
    if (csvFiles.length === 0) return;

    setBusy(true);
    setError("");
    setSuccess("");
    try {
      const body = new FormData();
      csvFiles.forEach((csvFile) => body.append("csv_file", csvFile));
      body.append("issuer", issuer);
      body.append("invoice_date", invoiceDate);
      body.append("payment_terms", paymentTerms);
      body.append("pv_tariff_method", pvTariffMethod);
      body.append("peak_allocation_mode", peakAllocationMode);
      body.append("no_vat", String(noVat));
      if (year) body.append("year", year);
      if (pvTariff) body.append("pv_tariff_rp_per_kwh", pvTariff);
      if (feedInEnergyRate) body.append("feed_in_energy_rp_per_kwh", feedInEnergyRate);
      if (feedInHknRate) body.append("feed_in_hkn_rp_per_kwh", feedInHknRate);
      if (indirectMeterCount) body.append("indirect_meter_count", indirectMeterCount);

      const response = await apiFetch("/api/vzev/invoices", { method: "POST", body });
      if (!response.ok) {
        const responseBody = await response.json().catch(() => ({}));
        throw new Error(responseBody.detail ?? response.statusText);
      }

      const disposition = response.headers.get("Content-Disposition") ?? "";
      const filename = disposition.match(/filename="([^"]+)"/)?.[1] ?? "vzev-rechnungen.zip";
      const invoiceCount = response.headers.get("X-Invoice-Count") ?? "";
      const periodCount = response.headers.get("X-Period-Count") ?? String(csvFiles.length);
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
      await onGenerated?.();
      await refreshHistory();
      const periodLabel = `${periodCount} ${periodCount === "1" ? "Monat" : "Monate"}`;
      const invoiceLabel = `${invoiceCount || "Alle"} ${
        invoiceCount === "1" ? "editierbaren Word-Rechnung" : "editierbaren Word-Rechnungen"
      }`;
      const overviewLabel = periodCount === "1" ? "einer Gemeinschaftsübersicht" : "den Gemeinschaftsübersichten";
      const batchOverviewLabel = periodCount === "1" ? "" : " sowie einer Mehrmonatsübersicht";
      setSuccess(
        `${periodLabel} mit ${invoiceLabel}, ${overviewLabel}${batchOverviewLabel} ${periodCount === "1" ? "wurde" : "wurden"} erstellt.`,
      );
    } catch (exc) {
      reportError(exc, "billing");
      setError(String((exc as Error).message ?? exc));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main>
      <aside>
        <div className="brand">
          <PlugZap size={24} />
          <div>
            <strong>Communi</strong>
            <span>CKW Werkzeuge</span>
          </div>
        </div>

        <WorkspaceNavigation current="billing" onNavigate={onNavigate} />

        <div className="workspaceHint">
          <strong>Mehrere Monate, getrennte Rechnungen</strong>
          <span>Die CKW-Monatsexporte werden geprüft und je Periode und Teilnehmer in DOCX-Dateien umgewandelt.</span>
        </div>
      </aside>

      <section className="content billingContent">
        <header>
          <div>
            <span className="eyebrow">vZEV-Betrieb</span>
            <h1>Monatliche Rechnungen erstellen</h1>
          </div>
        </header>

        {error ? <div className="alert error">{error}</div> : null}
        {success ? <div className="alert success">{success}</div> : null}

        {history.length ? (
          <details className="panel savedResults">
            <summary>Gespeicherte Abrechnungen ({history.length})</summary>
            <div className="savedResultList">
              {history.map((result) => (
                <button
                  type="button"
                  className="secondaryButton"
                  key={result.result_id}
                  onClick={() => downloadHistoryResult(result).catch((reason) => { reportError(reason, "billing"); setError(String(reason?.message ?? reason)); })}
                >
                  <Download size={15} />
                  {result.filename}
                </button>
              ))}
            </div>
          </details>
        ) : null}

        <section className="panel billingIntro">
          <h2>CKW-Monatsdateien verarbeiten</h2>
          <p>
            Lade eine oder mehrere Dateien aus dem Format <code>data/vzev/</code> hoch. Der Download enthält je
            Monat separat editierbare Word-Rechnungen, eine visuelle Gemeinschaftsübersicht und eine
            JSON-Kontrolldatei. Bei mehreren Monaten wird zusätzlich eine Teilnehmerübersicht über den gesamten
            Zeitraum erstellt.
          </p>
        </section>

        <form className="panel billingForm" onSubmit={generateInvoices}>
          <div className="billingFileRow">
            <label className="fileInput billingFileInput">
              <Upload size={18} />
              <input
                type="file"
                accept=".csv,text/csv"
                multiple
                onChange={(event) => setCsvFiles(Array.from(event.target.files ?? []))}
              />
              <span>
                {csvFiles.length > 0
                  ? `${csvFiles.length} ${csvFiles.length === 1 ? "Monatsdatei" : "Monatsdateien"} ausgewählt`
                  : "Monatliche vZEV-CSV-Dateien wählen"}
              </span>
            </label>
            {csvFiles.length > 0 ? (
              <ul className="billingSelectedFiles" aria-label="Ausgewählte Monatsdateien">
                {csvFiles.map((file, index) => (
                  <li key={`${file.name}-${file.size}-${file.lastModified}-${index}`}>{file.name}</li>
                ))}
              </ul>
            ) : null}
          </div>

          <div className="billingGrid">
            <label>
              Rechnungssteller
              <input value={issuer} required onChange={(event) => setIssuer(event.target.value)} />
            </label>
            <label>
              Rechnungsdatum
              <input type="date" value={invoiceDate} required onChange={(event) => setInvoiceDate(event.target.value)} />
            </label>
            <label>
              Zahlungsfrist
              <input value={paymentTerms} onChange={(event) => setPaymentTerms(event.target.value)} />
            </label>
            <label>
              Tarifjahr
              <input
                type="number"
                min="2025"
                placeholder="Automatisch aus CSV"
                value={year}
                onChange={(event) => setYear(event.target.value)}
              />
            </label>
            <label>
              PV-Tarif [Rp./kWh]
              <input
                type="number"
                min="0"
                step="0.01"
                placeholder="Automatisch je Teilnehmer: 80% Standardprodukt"
                value={pvTariff}
                onChange={(event) => setPvTariff(event.target.value)}
              />
            </label>
            <label>
              Herleitung PV-Tarif
              <select value={pvTariffMethod} onChange={(event) => setPvTariffMethod(event.target.value)}>
                <option value="80%-pauschale">80%-Pauschale</option>
                <option value="effektive-kosten">Effektive Kosten</option>
                <option value="manuell">Manuell</option>
              </select>
            </label>
            <label>
              Verteilung Leistungspreis
              <select value={peakAllocationMode} onChange={(event) => setPeakAllocationMode(event.target.value)}>
                <option value="community_peak_share">Interner Verteilschlüssel</option>
                <option value="individual_peak_pro_rata">Nach individueller Monatsleistung</option>
                <option value="peak_in_grid_kwh">Im Arbeitspreis eingerechnet</option>
              </select>
            </label>
            <label>
              Rückliefervergütung Energie [Rp./kWh]
              <input
                type="number"
                min="0"
                step="0.001"
                placeholder="Automatisch nach Quartal"
                value={feedInEnergyRate}
                onChange={(event) => setFeedInEnergyRate(event.target.value)}
              />
            </label>
            <label>
              HKN-Vergütung [Rp./kWh]
              <input
                type="number"
                min="0"
                step="0.01"
                placeholder="Standard: 2.00"
                value={feedInHknRate}
                onChange={(event) => setFeedInHknRate(event.target.value)}
              />
            </label>
            <label>
              Anzahl indirekte Messungen
              <input
                type="number"
                min="0"
                placeholder="Automatisch aus Blindenergiezählern"
                value={indirectMeterCount}
                onChange={(event) => setIndirectMeterCount(event.target.value)}
              />
            </label>
            <label className="billingCheckbox">
              <input type="checkbox" checked={noVat} onChange={(event) => setNoVat(event.target.checked)} />
              Keine MWST ausweisen
            </label>
          </div>

          <div className="billingActions">
            <div className="billingNotes">
              <p>
                Blindenergie und indirekte Messungen werden aus neueren CKW-Exporten erkannt. Die
                Rückliefervergütung wird quartalsweise gewählt und kann hier überschrieben werden. Die
                80%-Pauschale wird je Teilnehmer aus dem vollständigen hypothetischen CKW-Standardproduktpreis
                ohne vZEV hergeleitet. Dieser umfasst Energie, Netznutzung, Leistung, Messung, öffentliche Abgaben
                und MWST. Rechnung und Gemeinschaftsübersicht zeigen kWh, Rp./kWh und CHF separat.
                {" "}
                <a href="https://www.fedlex.admin.ch/eli/cc/2017/763/de#art_16" target="_blank" rel="noreferrer">
                  EnV Art. 16
                </a>
                {" · "}
                <a href="https://pubdb.bfe.admin.ch/de/publication/download/9329" target="_blank" rel="noreferrer">
                  BFE-Leitfaden
                </a>
              </p>
              <p>
                <strong>Verteilung des Leistungspreises:</strong> Beim internen Verteilschlüssel zählt der
                Verbrauchsanteil im 15-Minuten-Intervall der höchsten Gemeinschaftsleistung. Nach individueller
                Monatsleistung wird derselbe CKW-Leistungspreis proportional zu den persönlichen Monatsspitzen
                verteilt, auch wenn diese zu unterschiedlichen Zeiten auftreten. Im Arbeitspreis eingerechnet wird
                der Gesamtbetrag als Rp./kWh auf den abgerechneten CKW-Netzbezug umgelegt. Der Gesamtbetrag der
                Gemeinschaft bleibt bei allen Varianten gleich; nur die interne Aufteilung ändert sich.
              </p>
            </div>
            <button type="submit" disabled={csvFiles.length === 0 || busy}>
              <Download size={16} />
              {busy ? "Monats-Batch wird erstellt..." : "Editierbare Rechnungen herunterladen"}
            </button>
          </div>
        </form>
      </section>
    </main>
  );
}
