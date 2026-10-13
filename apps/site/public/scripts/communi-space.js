(function () {
  const root = document.querySelector("[data-communi-workspace]");

  if (!root) {
    return;
  }

  const dataset = root.dataset;
  const auth0Domain = dataset.auth0Domain || "";
  const auth0ClientId = dataset.auth0ClientId || "";
  const auth0Audience = dataset.auth0Audience || "";
  const auth0RedirectUri = dataset.auth0RedirectUri || `${window.location.origin}/raum/`;
  const apiBaseUrl = (dataset.apiBaseUrl || "").replace(/\/$/, "");
  const graphqlApiUrl = dataset.graphqlApiUrl || "";
  const researchEmail = dataset.researchEmail || "kontakt@communi.ch";
  const municipalitySource = dataset.municipalitySource || "/data/municipalities-data-2026.csv";
  const loginRequested = new URLSearchParams(window.location.search).get("login") === "1";
  const REGISTRY_KEY = "communi-municipality-registry";

  const useRestApi = Boolean(apiBaseUrl && auth0Audience);
  const useGraphqlPersistence = Boolean(!useRestApi && graphqlApiUrl && auth0Audience);
  const useRemotePersistence = Boolean(useRestApi || useGraphqlPersistence);

  const panels = Array.from(root.querySelectorAll("[data-workspace-panel]"));
  const loginButtons = Array.from(root.querySelectorAll('[data-auth-action="login"]'));
  const logoutButtons = Array.from(root.querySelectorAll('[data-auth-action="logout"]'));
  const uploadForm = root.querySelector("[data-upload-form]");
  const profileForm = root.querySelector("[data-profile-form]");
  const communityForm = root.querySelector("[data-community-form]");
  const fileList = root.querySelector("[data-file-list]");
  const communityList = root.querySelector("[data-community-list]");
  const emailLink = root.querySelector('[data-action="email-researchers"]');
  const exportButton = root.querySelector('[data-action="export-profile"]');
  const clearFilesButton = root.querySelector('[data-action="clear-files"]');
  const clearCommunityButton = root.querySelector('[data-action="clear-community"]');
  const municipalityDatalist = root.querySelector("#municipality-options");
  const operatorHint = root.querySelector("[data-operator-hint]");
  const municipalityLabelTarget = root.querySelector("[data-municipality-label]");
  const municipalityMetaTarget = root.querySelector("[data-municipality-meta]");
  const operatorLabelTarget = root.querySelector("[data-operator-label]");
  const operatorMetaTarget = root.querySelector("[data-operator-meta]");
  const municipalityCountTarget = root.querySelector("[data-municipality-count]");
  const municipalityCountCaptionTarget = root.querySelector("[data-municipality-count-caption]");
  const municipalityCountLargeTarget = root.querySelector("[data-municipality-count-large]");
  const municipalityCountLargeCaptionTarget = root.querySelector("[data-municipality-count-large-caption]");
  const visibleInterestCountTarget = root.querySelector("[data-visible-interest-count]");
  const visibleInterestList = root.querySelector("[data-visible-interest-list]");
  const uploadCountTarget = root.querySelector("[data-upload-count]");
  const uploadSummaryTarget = root.querySelector("[data-upload-summary]");
  const totalKwhTarget = root.querySelector("[data-total-kwh]");
  const timespanTarget = root.querySelector("[data-timespan]");
  const savingValueTarget = root.querySelector("[data-saving-value]");
  const savingCaptionTarget = root.querySelector("[data-saving-caption]");

  const defaultProfile = {
    municipalityKey: "",
    municipalityLabel: "",
    municipalityName: "",
    canton: "",
    operatorName: "",
    operatorWebsite: "",
    operatorAddress: "",
    operatorPostalCode: "",
    hasBattery: false,
    hasEvCharging: false,
    questions: "",
    shareWithResearchers: false,
    interestedInCommunity: false,
    visibleInterest: false,
    contactEmail: "",
  };

  let auth0Client;
  let auth0User;
  let accessToken = "";
  let municipalityEntries = [];
  let municipalityMap = new Map();
  let workspaceState = {
    profile: { ...defaultProfile },
    files: [],
    questions: [],
  };

  function setPanel(state) {
    const visiblePanels = state === "member" ? ["member"] : [state];
    panels.forEach((panel) => {
      const shouldShow = visiblePanels.includes(panel.dataset.workspacePanel);
      panel.classList.toggle("hidden", !shouldShow);
    });
  }

  function storageKey() {
    return auth0User?.sub ? `communi-workspace:${auth0User.sub}` : "";
  }

  function questionStorageKey() {
    return auth0User?.sub ? `communi-questions:${auth0User.sub}` : "";
  }

  function slugify(value) {
    return String(value || "")
      .toLowerCase()
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "");
  }

  function formatNumber(value, options) {
    return new Intl.NumberFormat("de-CH", options).format(value);
  }

  function formatFileSize(bytes) {
    if (!bytes) {
      return "0 KB";
    }
    const kb = bytes / 1024;
    if (kb < 1024) {
      return `${formatNumber(kb, { maximumFractionDigits: 1 })} KB`;
    }
    return `${formatNumber(kb / 1024, { maximumFractionDigits: 1 })} MB`;
  }

  function parseLocaleNumber(value) {
    const text = String(value || "").trim();
    if (!text) {
      return Number.NaN;
    }

    if (/^\d{1,3}(\.\d{3})*,\d+$/.test(text)) {
      return Number(text.replace(/\./g, "").replace(",", "."));
    }
    if (/^\d{1,3}(,\d{3})*\.\d+$/.test(text)) {
      return Number(text.replace(/,/g, ""));
    }
    if (text.includes(",") && !text.includes(".")) {
      return Number(text.replace(",", "."));
    }
    return Number(text.replace(/\s/g, ""));
  }

  function detectDelimiter(lines) {
    const sample = lines.slice(0, 5).join("\n");
    const candidates = [";", ",", "\t"];
    let best = ";";
    let bestScore = -1;

    candidates.forEach((candidate) => {
      const score = sample.split(candidate).length;
      if (score > bestScore) {
        best = candidate;
        bestScore = score;
      }
    });

    return best;
  }

  function normalizeHeader(value) {
    return String(value || "")
      .trim()
      .toLowerCase()
      .replaceAll("ä", "ae")
      .replaceAll("ö", "oe")
      .replaceAll("ü", "ue")
      .replace(/[ .()/-]/g, "_");
  }

  function isTimestampHeader(header) {
    return /(timestamp|datetime|date_time|zeit|datum|date|time)/i.test(
      normalizeHeader(header),
    );
  }

  function isEnergyHeader(header) {
    const normalized = normalizeHeader(header);
    return (
      !isExportLikeHeader(normalized) &&
      /(kwh|verbrauch|consumption|energie|energy|bezug)/i.test(normalized) &&
      !/(price|preis|tarif)/i.test(normalized)
    );
  }

  function isKnownSurseePvExportHeader(header) {
    const normalized = normalizeHeader(header);
    return (
      (normalized.includes("ruecklieferung") || normalized.includes("rucklieferung")) &&
      normalized.includes("kwh")
    );
  }

  function isExportLikeHeader(normalizedHeader) {
    return /(ruecklieferung|rucklieferung|einspeisung|export|feed_in|production|produktion)/i.test(
      normalizedHeader,
    );
  }

  function hasSurseePvExportMetadata(lines) {
    return lines
      .slice(0, 12)
      .map(normalizeHeader)
      .some((line) => line.includes("vertrag:_ueberschuss_pva"));
  }

  function detectSeriesKind(lines, headers, energyIndex) {
    return hasSurseePvExportMetadata(lines) &&
      isKnownSurseePvExportHeader(headers[energyIndex])
      ? "pv_export_sursee"
      : "consumption";
  }

  function findCsvDataSection(lines) {
    const isKnownPvExport = hasSurseePvExportMetadata(lines);
    for (let index = 0; index < lines.length; index += 1) {
      const delimiter = detectDelimiter([lines[index]]);
      const headers = splitRow(lines[index], delimiter);
      const hasEnergyHeader =
        headers.some(isEnergyHeader) ||
        (isKnownPvExport && headers.some(isKnownSurseePvExportHeader));
      if (headers.length >= 2 && headers.some(isTimestampHeader) && hasEnergyHeader) {
        return { delimiter, lines: lines.slice(index) };
      }
    }

    return null;
  }

  function parseEnergyValue(value, seriesKind) {
    const parsed = parseLocaleNumber(value);
    if (!Number.isFinite(parsed)) {
      return Number.NaN;
    }
    if (parsed < 0 && seriesKind !== "pv_export_sursee") {
      return Number.NaN;
    }
    return seriesKind === "pv_export_sursee" ? Math.abs(parsed) : parsed;
  }

  function splitRow(line, delimiter) {
    return line
      .split(delimiter)
      .map((cell) => cell.replace(/^"|"$/g, "").trim());
  }

  function parseCsvLine(line) {
    const result = [];
    let current = "";
    let inQuotes = false;

    for (let index = 0; index < line.length; index += 1) {
      const char = line[index];

      if (char === '"') {
        const next = line[index + 1];
        if (inQuotes && next === '"') {
          current += '"';
          index += 1;
        } else {
          inQuotes = !inQuotes;
        }
      } else if (char === "," && !inQuotes) {
        result.push(current.trim());
        current = "";
      } else {
        current += char;
      }
    }

    result.push(current.trim());
    return result;
  }

  function parsePossibleDate(value) {
    const text = String(value || "").trim();
    if (!text) {
      return null;
    }

    const direct = Date.parse(text);
    if (!Number.isNaN(direct)) {
      return new Date(direct);
    }

    const match = text.match(/^(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})/);
    if (!match) {
      return null;
    }

    const day = Number(match[1]);
    const month = Number(match[2]) - 1;
    const year = Number(match[3].length === 2 ? `20${match[3]}` : match[3]);
    return new Date(year, month, day);
  }

  function formatDate(date) {
    return date ? date.toLocaleDateString("de-CH") : "";
  }

  function analyseCsv(text) {
    const lines = text
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean);

    if (lines.length < 2) {
      return null;
    }

    const dataSection = findCsvDataSection(lines);
    if (!dataSection) {
      return null;
    }

    const { delimiter, lines: dataLines } = dataSection;
    const headers = splitRow(dataLines[0], delimiter);
    const rows = dataLines.slice(1).map((line) => splitRow(line, delimiter));
    const knownPvExport = hasSurseePvExportMetadata(lines);
    const headerIndex = headers.findIndex(
      (header) => isEnergyHeader(header) || (knownPvExport && isKnownSurseePvExportHeader(header)),
    );
    const numericIndex =
      headerIndex >= 0
        ? headerIndex
        : headers.findIndex((_, index) =>
            rows.some((row) => Number.isFinite(parseLocaleNumber(row[index]))),
          );
    const timestampIndex = headers.findIndex(isTimestampHeader);
    const seriesKind = detectSeriesKind(lines, headers, numericIndex);

    if (numericIndex < 0) {
      return {
        rowCount: rows.length,
        totalKwh: 0,
        detectedColumn: "Keine Energie-Spalte erkannt",
        firstTimestamp: "",
        lastTimestamp: "",
      };
    }

    const usableRows =
      timestampIndex >= 0
        ? rows.filter((row) => parsePossibleDate(row[timestampIndex]))
        : rows;
    const values = usableRows
      .map((row) => parseEnergyValue(row[numericIndex], seriesKind))
      .filter((value) => Number.isFinite(value));
    const timestamps =
      timestampIndex >= 0
        ? usableRows.map((row) => row[timestampIndex]).filter(Boolean)
        : [];

    return {
      rowCount: usableRows.length,
      totalKwh: values.reduce((sum, value) => sum + value, 0),
      detectedColumn: headers[numericIndex] || `Spalte ${numericIndex + 1}`,
      seriesKind,
      seriesLabel:
        seriesKind === "pv_export_sursee" ? "PV-Rücklieferungsprofil" : "Verbrauchsprofil",
      firstTimestamp: timestamps[0] || "",
      lastTimestamp: timestamps[timestamps.length - 1] || "",
    };
  }

  async function filesToEntries(files) {
    const entries = [];

    for (const file of files) {
      let rawCsv = "";
      let stats = null;

      if (/\.csv$/i.test(file.name) || file.type === "text/csv") {
        try {
          rawCsv = await file.text();
          stats = analyseCsv(rawCsv);
        } catch (error) {
      window.__COMMUNI_REPORT_ERROR__?.(error, "workspace");
          rawCsv = "";
        }
      }

      const ingested =
        Number(stats?.rowCount || 0) > 0 &&
        Number(stats?.totalKwh || 0) > 0 &&
        stats?.detectedColumn &&
        stats.detectedColumn !== "Keine Energie-Spalte erkannt";
      const entry = {
        id: crypto.randomUUID(),
        fileName: file.name,
        mimeType: file.type || "text/csv",
        size: file.size,
        rawCsv,
        rowCount: Number(stats?.rowCount || 0),
        totalKwh: Number(stats?.totalKwh || 0),
        detectedColumn: stats?.detectedColumn || "",
        seriesKind: stats?.seriesKind || "consumption",
        seriesLabel: stats?.seriesLabel || "Verbrauchsprofil",
        firstTimestamp: stats?.firstTimestamp || "",
        lastTimestamp: stats?.lastTimestamp || "",
        ingestionStatus: ingested ? "ingested" : "pending_ingestion",
        ingestionMessage: ingested
          ? ""
          : "Die CSV-Datei wurde gespeichert, wird aber noch nicht automatisch ausgewertet. Wir prüfen dieses Format und verbessern die Erkennung.",
        createdAt: new Date().toISOString(),
      };
      Object.defineProperty(entry, "sourceFile", {
        value: file,
        enumerable: false,
      });
      entries.push(entry);
    }

    return entries;
  }

  function totalKwh() {
    return workspaceState.files.reduce(
      (sum, file) => sum + Number(file.totalKwh || 0),
      0,
    );
  }

  function solarProxyPercent() {
    const total = totalKwh();
    if (!total) {
      return 0;
    }

    return (
      workspaceState.files.reduce((sum, file) => {
        const fileTotal = Number(file.totalKwh || 0);
        const percent = Number(file.solarProxyPercent || 0);
        return sum + fileTotal * percent;
      }, 0) / total
    );
  }

  function computeTimeSpan() {
    const dates = workspaceState.files
      .flatMap((file) => [file.firstTimestamp, file.lastTimestamp])
      .map(parsePossibleDate)
      .filter(Boolean);

    if (!dates.length) {
      return "–";
    }

    const sorted = dates.sort((left, right) => left.getTime() - right.getTime());
    return `${formatDate(sorted[0])} – ${formatDate(sorted[sorted.length - 1])}`;
  }

  function updateEnergySummary() {
    const total = totalKwh();
    const proxyPercent = solarProxyPercent();

    if (totalKwhTarget) {
      totalKwhTarget.textContent =
        total > 0 ? `${formatNumber(total, { maximumFractionDigits: 0 })} kWh` : "–";
    }
    if (timespanTarget) {
      timespanTarget.textContent = total > 0 ? computeTimeSpan() : "–";
    }
    if (savingValueTarget) {
      savingValueTarget.textContent =
        total > 0 && proxyPercent > 0
          ? `${formatNumber(proxyPercent, { maximumFractionDigits: 1 })} %`
          : "–";
    }
    if (savingCaptionTarget) {
      savingCaptionTarget.textContent =
        total > 0 && proxyPercent > 0
          ? "Geschätzter Verbrauchsanteil, der zeitlich mit einer lokalen PV-Erzeugungskurve zusammenfällt"
          : "Dateien über die REST-API hochladen, um den Solar-Proxy zu berechnen";
    }
  }

  function renderFileList() {
    if (!fileList) {
      return;
    }

    if (!workspaceState.files.length) {
      fileList.innerHTML =
        '<p class="text-sm text-muted-foreground">Noch keine Dateien analysiert.</p>';
    } else {
      fileList.innerHTML = workspaceState.files
        .slice()
        .reverse()
        .map((file) => {
          const total = Number(file.totalKwh || 0);
          const metrics = total
            ? `${formatNumber(total, { maximumFractionDigits: 1 })} kWh · ${file.rowCount || 0} Zeilen`
            : `${file.rowCount || 0} Zeilen · keine Energie-Spalte erkannt`;
          const solarProxy = Number(file.solarProxyPercent || 0);
          const pendingIngestion = file.ingestionStatus === "pending_ingestion";
          const timeSpan =
            file.firstTimestamp && file.lastTimestamp
              ? `${file.firstTimestamp} bis ${file.lastTimestamp}`
              : "ohne Zeitfenster";

          return `
            <div class="rounded-2xl border border-border bg-muted/40 p-4">
              <div class="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
                <div>
                  <p class="text-sm font-semibold text-foreground">${file.fileName}</p>
                  <p class="mt-1 text-sm text-muted-foreground">${metrics}</p>
                  ${
                    solarProxy > 0
                      ? `<p class="mt-1 text-sm font-semibold text-foreground">Solar-Proxy: ${formatNumber(solarProxy, { maximumFractionDigits: 1 })} %</p>`
                      : ""
                  }
                  ${
                    pendingIngestion
                      ? `<p class="mt-2 text-sm font-semibold text-foreground">${file.ingestionMessage || "Die CSV-Datei wurde gespeichert, wird aber noch nicht automatisch ausgewertet. Wir prüfen dieses Format und verbessern die Erkennung."}</p>`
                      : ""
                  }
                  <p class="mt-1 text-xs text-muted-foreground">${file.seriesLabel || "Verbrauchsprofil"} · ${file.detectedColumn || "CSV-Datei"} · ${timeSpan}</p>
                </div>
                <p class="text-xs text-muted-foreground">${formatFileSize(file.size || 0)}</p>
              </div>
            </div>
          `;
        })
        .join("");
    }

    if (uploadCountTarget) {
      uploadCountTarget.textContent = `${workspaceState.files.length} Dateien`;
    }
    if (uploadSummaryTarget) {
      const total = totalKwh();
      uploadSummaryTarget.textContent =
        total > 0
          ? `${formatNumber(total, { maximumFractionDigits: 0 })} kWh erkannt${solarProxyPercent() > 0 ? ` · ${formatNumber(solarProxyPercent(), { maximumFractionDigits: 1 })} % Solar-Proxy` : ""}`
          : "Noch keine auswertbaren Energiedaten";
    }

    updateEnergySummary();
  }

  function renderQuestions() {
    if (!communityList) {
      return;
    }

    if (!workspaceState.questions.length) {
      communityList.innerHTML =
        '<p class="text-sm text-muted-foreground">Noch keine Fragen gespeichert.</p>';
      return;
    }

    communityList.innerHTML = workspaceState.questions
      .slice()
      .reverse()
      .map(
        (question) => `
          <div class="rounded-2xl border border-border bg-muted/40 p-4">
            <div class="flex items-center justify-between gap-4">
              <p class="text-sm font-semibold text-foreground">${workspaceState.profile.municipalityLabel || "Eigene Frage"}</p>
              <p class="text-xs text-muted-foreground">${new Date(question.createdAt).toLocaleString("de-CH")}</p>
            </div>
            <p class="mt-3 text-sm leading-relaxed text-muted-foreground">${question.body}</p>
          </div>
        `,
      )
      .join("");
  }

  function normalizeMunicipalityKey(municipalityNumber, municipalityName, canton) {
    return `${municipalityNumber}|${municipalityName}|${canton}`;
  }

  async function loadMunicipalities() {
    const response = await fetch(municipalitySource);
    const text = await response.text();
    const lines = text
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean);

    const grouped = new Map();

    for (const line of lines.slice(1)) {
      const [operator, website, municipalityNumber, municipalityName, operatorAddress, operatorPostalCode, canton] =
        parseCsvLine(line);

      if (!municipalityNumber || !municipalityName || !canton) {
        continue;
      }

      const municipalityKey = normalizeMunicipalityKey(
        municipalityNumber,
        municipalityName,
        canton,
      );

      if (!grouped.has(municipalityKey)) {
        grouped.set(municipalityKey, {
          municipalityKey,
          municipalityNumber,
          municipalityName,
          canton,
          label: `${municipalityName} (${canton})`,
          operators: [],
        });
      }

      const entry = grouped.get(municipalityKey);
      const operatorName = String(operator || "").trim();
      if (
        operatorName &&
        !entry.operators.some((item) => item.name === operatorName)
      ) {
        entry.operators.push({
          name: operatorName,
          website: String(website || "").trim(),
          address: String(operatorAddress || "").replace(/^"|"$/g, "").trim(),
          postalCode: String(operatorPostalCode || "").trim(),
        });
      }
    }

    municipalityEntries = Array.from(grouped.values()).sort((left, right) =>
      left.label.localeCompare(right.label, "de-CH"),
    );
    municipalityMap = new Map(
      municipalityEntries.map((entry) => [entry.label.toLowerCase(), entry]),
    );

    if (municipalityDatalist) {
      municipalityDatalist.innerHTML = municipalityEntries
        .map((entry) => `<option value="${entry.label}"></option>`)
        .join("");
    }
  }

  function findMunicipalityByLabel(label) {
    return municipalityMap.get(String(label || "").trim().toLowerCase()) || null;
  }

  function setOperatorOptions(entry, selectedOperatorName = "") {
    if (!profileForm?.operatorName) {
      return;
    }

    if (!entry) {
      profileForm.operatorName.innerHTML =
        '<option value="">Bitte zuerst Gemeinde wählen</option>';
      profileForm.operatorName.value = "";
      if (operatorHint) {
        operatorHint.textContent =
          "Die Auswahl basiert auf `municipalities-data-2026.csv`. Wenn in einer Gemeinde mehrere Netzbetreiber vorkommen, bitte den passenden Eintrag auswählen.";
      }
      return;
    }

    profileForm.operatorName.innerHTML = entry.operators
      .map((operator) => `<option value="${operator.name}">${operator.name}</option>`)
      .join("");

    const nextOperatorName =
      selectedOperatorName &&
      entry.operators.some((operator) => operator.name === selectedOperatorName)
        ? selectedOperatorName
        : entry.operators[0]?.name || "";

    profileForm.operatorName.value = nextOperatorName;
    workspaceState.profile.operatorName = nextOperatorName;

    const selectedOperator =
      entry.operators.find((operator) => operator.name === nextOperatorName) ||
      entry.operators[0];

    workspaceState.profile.operatorWebsite = selectedOperator?.website || "";
    workspaceState.profile.operatorAddress = selectedOperator?.address || "";
    workspaceState.profile.operatorPostalCode = selectedOperator?.postalCode || "";

    if (operatorHint) {
      operatorHint.textContent =
        entry.operators.length > 1
          ? `Für ${entry.label} sind ${entry.operators.length} Netzbetreiber in der Datengrundlage vorhanden. Bitte den passenden Betreiber auswählen.`
          : `${selectedOperator?.name || "Netzbetreiber"} ist aktuell der einzige Eintrag für ${entry.label}.`;
    }
  }

  function applyMunicipalitySelection(label, preferredOperatorName = "") {
    const entry = findMunicipalityByLabel(label);

    if (!entry) {
      workspaceState.profile.municipalityKey = "";
      workspaceState.profile.municipalityLabel = String(label || "").trim();
      workspaceState.profile.municipalityName = "";
      workspaceState.profile.canton = "";
      workspaceState.profile.operatorName = "";
      workspaceState.profile.operatorWebsite = "";
      workspaceState.profile.operatorAddress = "";
      workspaceState.profile.operatorPostalCode = "";
      setOperatorOptions(null);
      renderProfileSummary();
      return null;
    }

    workspaceState.profile.municipalityKey = entry.municipalityKey;
    workspaceState.profile.municipalityLabel = entry.label;
    workspaceState.profile.municipalityName = entry.municipalityName;
    workspaceState.profile.canton = entry.canton;
    setOperatorOptions(entry, preferredOperatorName || workspaceState.profile.operatorName);
    renderProfileSummary();
    return entry;
  }

  function renderProfileSummary() {
    if (municipalityLabelTarget) {
      municipalityLabelTarget.textContent =
        workspaceState.profile.municipalityLabel || "Noch nicht gewählt";
    }
    if (municipalityMetaTarget) {
      municipalityMetaTarget.textContent = workspaceState.profile.municipalityLabel
        ? `${workspaceState.profile.canton} · lokal einordnen`
        : "Gemeinde wählen und bestätigen";
    }
    if (operatorLabelTarget) {
      operatorLabelTarget.textContent =
        workspaceState.profile.operatorName || "Noch offen";
    }
    if (operatorMetaTarget) {
      operatorMetaTarget.textContent =
        workspaceState.profile.operatorAddress || "Wird aus der Gemeindedatei abgeleitet";
    }

    if (emailLink) {
      const subject = encodeURIComponent(
        `Communi.ch Forschungsprofil${workspaceState.profile.municipalityLabel ? ` – ${workspaceState.profile.municipalityLabel}` : ""}`,
      );
      const body = encodeURIComponent([
        "Guten Tag",
        "",
        "ich möchte Angaben aus meinem persönlichen Bereich mit dem Forschungsteam teilen.",
        "",
        `Gemeinde: ${workspaceState.profile.municipalityLabel || "-"}`,
        `Netzbetreiber: ${workspaceState.profile.operatorName || "-"}`,
        `Batterie: ${workspaceState.profile.hasBattery ? "Ja" : "Nein"}`,
        `Ladeinfrastruktur: ${workspaceState.profile.hasEvCharging ? "Ja" : "Nein"}`,
        `Interesse an lokaler Gemeinschaft: ${workspaceState.profile.interestedInCommunity ? "Ja" : "Nein"}`,
        "",
        "Fragen / Ziele:",
        workspaceState.profile.questions || "-",
      ].join("\n"));
      emailLink.href = `mailto:${researchEmail}?subject=${subject}&body=${body}`;
    }
  }

  function loadLocalWorkspaceState() {
    const key = storageKey();
    if (!key) {
      return;
    }

    try {
      const raw = window.localStorage.getItem(key);
      if (raw) {
        const parsed = JSON.parse(raw);
        workspaceState.profile = {
          ...defaultProfile,
          ...(parsed.profile || {}),
        };
        workspaceState.files = Array.isArray(parsed.files) ? parsed.files : [];
      }
    } catch (error) {
      window.__COMMUNI_REPORT_ERROR__?.(error, "workspace");
      workspaceState.profile = { ...defaultProfile };
      workspaceState.files = [];
    }

    try {
      const questionsRaw = window.localStorage.getItem(questionStorageKey());
      workspaceState.questions = questionsRaw ? JSON.parse(questionsRaw) : [];
    } catch (error) {
      window.__COMMUNI_REPORT_ERROR__?.(error, "workspace");
      workspaceState.questions = [];
    }
  }

  function saveLocalWorkspaceState() {
    const key = storageKey();
    if (!key) {
      return;
    }

    window.localStorage.setItem(
      key,
      JSON.stringify({
        profile: workspaceState.profile,
        files: workspaceState.files,
      }),
    );
    window.localStorage.setItem(
      questionStorageKey(),
      JSON.stringify(workspaceState.questions),
    );
  }

  function loadLocalRegistry() {
    try {
      const raw = window.localStorage.getItem(REGISTRY_KEY);
      return raw ? JSON.parse(raw) : [];
    } catch (error) {
      window.__COMMUNI_REPORT_ERROR__?.(error, "workspace");
      return [];
    }
  }

  function saveLocalRegistry(entries) {
    window.localStorage.setItem(REGISTRY_KEY, JSON.stringify(entries));
  }

  function syncLocalRegistry() {
    if (!auth0User?.sub) {
      return;
    }

    const registry = loadLocalRegistry().filter((entry) => entry.userSub !== auth0User.sub);

    if (workspaceState.profile.municipalityKey) {
      registry.push({
        userSub: auth0User.sub,
        municipalityKey: workspaceState.profile.municipalityKey,
        municipalityLabel: workspaceState.profile.municipalityLabel,
        operatorName: workspaceState.profile.operatorName,
        interestedInCommunity: Boolean(workspaceState.profile.interestedInCommunity),
        visibleInterest:
          Boolean(workspaceState.profile.interestedInCommunity) &&
          Boolean(workspaceState.profile.visibleInterest),
        sharedEmail:
          Boolean(workspaceState.profile.interestedInCommunity) &&
          Boolean(workspaceState.profile.visibleInterest)
            ? workspaceState.profile.contactEmail || auth0User.email || ""
            : "",
        updatedAt: new Date().toISOString(),
      });
    }

    saveLocalRegistry(registry);
  }

  async function ensureAccessToken() {
    if (!useRemotePersistence) {
      return "";
    }

    accessToken = await auth0Client.getTokenSilently({
      authorizationParams: {
        audience: auth0Audience,
      },
    });
    return accessToken;
  }

  async function graphqlRequest(query, variables = {}) {
    const token = await ensureAccessToken();
    const response = await fetch(graphqlApiUrl, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ query, variables }),
    });

    const payload = await response.json();
    if (!response.ok || payload.errors?.length) {
      throw new Error(payload.errors?.[0]?.message || "GraphQL request failed");
    }

    return payload.data;
  }

  async function apiRequest(path, options = {}) {
    const token = await ensureAccessToken();
    const headers = new Headers(options.headers || {});

    if (!headers.has("authorization")) {
      headers.set("authorization", `Bearer ${token}`);
    }
    if (options.body && !(options.body instanceof FormData) && !headers.has("content-type")) {
      headers.set("content-type", "application/json");
    }

    const response = await fetch(`${apiBaseUrl}${path}`, {
      ...options,
      headers,
    });
    const payload = await response.json().catch(() => ({}));

    if (!response.ok) {
      throw new Error(payload.error || "API request failed");
    }

    return payload;
  }

  function mapRemoteProfile(profile) {
    if (!profile) {
      return { ...defaultProfile, contactEmail: auth0User?.email || "" };
    }

    return {
      ...defaultProfile,
      ...profile,
      contactEmail: profile.contactEmail || auth0User?.email || "",
    };
  }

  function mapRemoteUpload(upload) {
    return {
      id: upload.id || upload.uploadId,
      fileName: upload.fileName,
      mimeType: upload.mimeType || "text/csv",
      size: 0,
      rawCsv: "",
      rowCount: Number(upload.rowCount || 0),
      totalKwh: Number(upload.totalKwh || 0),
      daylightConsumptionPercent: Number(upload.daylightConsumptionPercent || 0),
      solarProxyPercent: Number(upload.solarProxyPercent || 0),
      seriesKind: upload.seriesKind || "consumption",
      seriesLabel: upload.seriesLabel || "Verbrauchsprofil",
      ingestionStatus:
        upload.ingestionStatus ||
        (Number(upload.rowCount || 0) > 0 && Number(upload.totalKwh || 0) > 0
          ? "ingested"
          : "pending_ingestion"),
      ingestionMessage:
        upload.ingestionStatus === "pending_ingestion" ||
        !(Number(upload.rowCount || 0) > 0 && Number(upload.totalKwh || 0) > 0)
          ? "Die CSV-Datei wurde gespeichert, wird aber noch nicht automatisch ausgewertet. Wir prüfen dieses Format und verbessern die Erkennung."
          : "",
      detectedColumn:
        upload.detectedColumn ||
        upload.detectedConsumptionColumn ||
        upload.detectedTimestampColumn ||
        "",
      firstTimestamp: upload.firstTimestamp || "",
      lastTimestamp: upload.lastTimestamp || "",
      createdAt: upload.createdAt,
      expiresAt: upload.expiresAt || "",
    };
  }

  async function loadRemoteWorkspace() {
    if (useRestApi) {
      const data = await apiRequest("/v1/me/workspace");
      workspaceState.profile = mapRemoteProfile(data.profile);
      workspaceState.files = (data.uploads || []).map(mapRemoteUpload);
      workspaceState.questions = data.questions || [];
      return;
    }

    const data = await graphqlRequest(
      `
        query ViewerSnapshot {
          viewer {
            auth0Sub
            profile {
              municipalityKey
              municipalityLabel
              municipalityName
              canton
              operatorName
              operatorWebsite
              operatorAddress
              operatorPostalCode
              hasBattery
              hasEvCharging
              questions
              shareWithResearchers
              interestedInCommunity
              visibleInterest
              contactEmail
            }
            uploads {
              id
              fileName
              mimeType
              rowCount
              totalKwh
              detectedColumn
              firstTimestamp
              lastTimestamp
              createdAt
            }
            questions {
              id
              municipalityKey
              body
              createdAt
            }
          }
        }
      `,
    );

    workspaceState.profile = mapRemoteProfile(data.viewer.profile);
    workspaceState.files = data.viewer.uploads.map(mapRemoteUpload);
    workspaceState.questions = data.viewer.questions || [];
  }

  async function fetchRemoteMunicipalitySignals() {
    if (!workspaceState.profile.municipalityKey) {
      return {
        municipalityKey: "",
        registeredCount: 0,
        visibleContacts: [],
      };
    }

    if (useRestApi) {
      return apiRequest(
        `/v1/municipality-signals?municipalityKey=${encodeURIComponent(
          workspaceState.profile.municipalityKey,
        )}`,
      );
    }

    const data = await graphqlRequest(
      `
        query MunicipalitySignals($municipalityKey: String!) {
          municipalitySignals(municipalityKey: $municipalityKey) {
            municipalityKey
            registeredCount
            visibleContacts {
              email
              operatorName
              updatedAt
            }
          }
        }
      `,
      { municipalityKey: workspaceState.profile.municipalityKey },
    );

    return data.municipalitySignals;
  }

  async function persistProfile() {
    if (useRemotePersistence) {
      const input = {
        municipalityKey: workspaceState.profile.municipalityKey || null,
        municipalityLabel: workspaceState.profile.municipalityLabel || null,
        municipalityName: workspaceState.profile.municipalityName || null,
        canton: workspaceState.profile.canton || null,
        operatorName: workspaceState.profile.operatorName || null,
        operatorWebsite: workspaceState.profile.operatorWebsite || null,
        operatorAddress: workspaceState.profile.operatorAddress || null,
        operatorPostalCode: workspaceState.profile.operatorPostalCode || null,
        hasBattery: Boolean(workspaceState.profile.hasBattery),
        hasEvCharging: Boolean(workspaceState.profile.hasEvCharging),
        questions: workspaceState.profile.questions || null,
        shareWithResearchers: Boolean(workspaceState.profile.shareWithResearchers),
        interestedInCommunity: Boolean(workspaceState.profile.interestedInCommunity),
        visibleInterest:
          Boolean(workspaceState.profile.interestedInCommunity) &&
          Boolean(workspaceState.profile.visibleInterest),
        contactEmail:
          workspaceState.profile.interestedInCommunity &&
          workspaceState.profile.visibleInterest
            ? workspaceState.profile.contactEmail || auth0User?.email || null
            : null,
      };

      if (useRestApi) {
        await apiRequest("/v1/me/profile", {
          method: "PUT",
          body: JSON.stringify(input),
        });
        return;
      }

      await graphqlRequest(
        `
          mutation UpsertProfile($input: UpsertProfileInput!) {
            upsertProfile(input: $input) {
              municipalityKey
            }
          }
        `,
        { input },
      );
      return;
    }

    saveLocalWorkspaceState();
    syncLocalRegistry();
  }

  async function refreshMunicipalitySignals() {
    if (useRemotePersistence) {
      const signals = await fetchRemoteMunicipalitySignals();
      renderMunicipalitySignals(signals.registeredCount, signals.visibleContacts);
      return;
    }

    const registry = workspaceState.profile.municipalityKey
      ? loadLocalRegistry().filter(
          (entry) => entry.municipalityKey === workspaceState.profile.municipalityKey,
        )
      : [];
    const visibleContacts = registry
      .filter(
        (entry) =>
          entry.userSub !== auth0User?.sub &&
          entry.interestedInCommunity &&
          entry.visibleInterest &&
          entry.sharedEmail,
      )
      .map((entry) => ({
        email: entry.sharedEmail,
        operatorName: entry.operatorName,
        updatedAt: entry.updatedAt,
      }));

    renderMunicipalitySignals(registry.length, visibleContacts);
  }

  function renderMunicipalitySignals(registeredCount, visibleContacts) {
    if (municipalityCountTarget) {
      municipalityCountTarget.textContent = workspaceState.profile.municipalityKey
        ? String(registeredCount)
        : "0";
    }
    if (municipalityCountCaptionTarget) {
      municipalityCountCaptionTarget.textContent = workspaceState.profile.municipalityKey
        ? useRemotePersistence
          ? "Profile aus Neon mit gleicher Gemeinde"
          : "Profile in dieser Browser-Vorschau"
        : "Gemeinde wählen, um Signale zu sehen";
    }
    if (municipalityCountLargeTarget) {
      municipalityCountLargeTarget.textContent = workspaceState.profile.municipalityKey
        ? String(registeredCount)
        : "0";
    }
    if (municipalityCountLargeCaptionTarget) {
      municipalityCountLargeCaptionTarget.textContent = workspaceState.profile.municipalityKey
        ? `Profile mit gleicher Gemeinde: ${workspaceState.profile.municipalityLabel}`
        : "Noch keine Gemeinde gewählt";
    }
    if (visibleInterestCountTarget) {
      visibleInterestCountTarget.textContent = String(visibleContacts.length);
    }

    if (!visibleInterestList) {
      return;
    }

    if (!workspaceState.profile.municipalityKey) {
      visibleInterestList.innerHTML =
        '<p class="text-sm text-muted-foreground">Wähle zuerst eine Gemeinde, um lokale Signale zu sehen.</p>';
      return;
    }

    if (!visibleContacts.length) {
      visibleInterestList.innerHTML =
        '<p class="text-sm text-muted-foreground">Noch keine sichtbaren Interessenssignale aus derselben Gemeinde.</p>';
      return;
    }

    visibleInterestList.innerHTML = visibleContacts
      .map(
        (entry) => `
          <div class="rounded-2xl border border-border bg-muted/40 p-4">
            <div class="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
              <div>
                <p class="text-sm font-semibold text-foreground">${entry.operatorName || "Netzbetreiber nicht gewählt"}</p>
                <p class="mt-1 text-sm text-muted-foreground">${entry.email}</p>
              </div>
              <p class="text-xs text-muted-foreground">${new Date(entry.updatedAt).toLocaleDateString("de-CH")}</p>
            </div>
          </div>
        `,
      )
      .join("");
  }

  async function addUploadEntries(entries) {
    if (useRemotePersistence) {
      if (useRestApi) {
        for (const entry of entries) {
          const formData = new FormData();
          formData.append(
            "file",
            entry.sourceFile ||
              new Blob([entry.rawCsv || ""], { type: entry.mimeType || "text/csv" }),
            entry.fileName,
          );
          await apiRequest("/v1/me/uploads", {
            method: "POST",
            body: formData,
          });
        }

        await loadRemoteWorkspace();
        return;
      }

      for (const entry of entries) {
        await graphqlRequest(
          `
            mutation AddUpload($input: UploadInput!) {
              addUpload(input: $input) {
                id
              }
            }
          `,
          {
            input: {
              fileName: entry.fileName,
              mimeType: entry.mimeType,
              rawCsv: entry.rawCsv,
              rowCount: entry.rowCount,
              totalKwh: entry.totalKwh,
              detectedColumn: entry.detectedColumn || null,
              firstTimestamp: entry.firstTimestamp || null,
              lastTimestamp: entry.lastTimestamp || null,
            },
          },
        );
      }

      await loadRemoteWorkspace();
      return;
    }

    workspaceState.files = workspaceState.files.concat(entries);
    saveLocalWorkspaceState();
  }

  async function clearAllUploads() {
    if (useRemotePersistence) {
      if (useRestApi) {
        await apiRequest("/v1/me/uploads", { method: "DELETE" });
        await loadRemoteWorkspace();
        return;
      }

      await graphqlRequest(
        `
          mutation ClearUploads {
            clearUploads
          }
        `,
      );
      await loadRemoteWorkspace();
      return;
    }

    workspaceState.files = [];
    saveLocalWorkspaceState();
  }

  async function addQuestionEntry(body) {
    if (useRemotePersistence) {
      if (useRestApi) {
        await apiRequest("/v1/me/questions", {
          method: "POST",
          body: JSON.stringify({
            body,
            municipalityKey: workspaceState.profile.municipalityKey || null,
          }),
        });
        await loadRemoteWorkspace();
        return;
      }

      await graphqlRequest(
        `
          mutation AddQuestion($input: AddQuestionInput!) {
            addQuestion(input: $input) {
              id
            }
          }
        `,
        {
          input: {
            body,
            municipalityKey: workspaceState.profile.municipalityKey || null,
          },
        },
      );
      await loadRemoteWorkspace();
      return;
    }

    workspaceState.questions.push({
      id: crypto.randomUUID(),
      municipalityKey: workspaceState.profile.municipalityKey || null,
      body,
      createdAt: new Date().toISOString(),
    });
    saveLocalWorkspaceState();
  }

  async function clearAllQuestions() {
    if (useRemotePersistence) {
      if (useRestApi) {
        await apiRequest("/v1/me/questions", { method: "DELETE" });
        await loadRemoteWorkspace();
        return;
      }

      await graphqlRequest(
        `
          mutation ClearQuestions {
            clearQuestions
          }
        `,
      );
      await loadRemoteWorkspace();
      return;
    }

    workspaceState.questions = [];
    saveLocalWorkspaceState();
  }

  function fillProfileForm() {
    if (!profileForm) {
      return;
    }

    profileForm.municipalityLabel.value = workspaceState.profile.municipalityLabel || "";
    applyMunicipalitySelection(
      workspaceState.profile.municipalityLabel,
      workspaceState.profile.operatorName,
    );
    profileForm.hasBattery.checked = Boolean(workspaceState.profile.hasBattery);
    profileForm.hasEvCharging.checked = Boolean(workspaceState.profile.hasEvCharging);
    profileForm.questions.value = workspaceState.profile.questions || "";
    profileForm.shareWithResearchers.checked = Boolean(
      workspaceState.profile.shareWithResearchers,
    );
    profileForm.interestedInCommunity.checked = Boolean(
      workspaceState.profile.interestedInCommunity,
    );
    profileForm.visibleInterest.checked = Boolean(workspaceState.profile.visibleInterest);
  }

  async function hydrateWorkspace() {
    if (useRemotePersistence) {
      await loadRemoteWorkspace();
      return;
    }
    loadLocalWorkspaceState();
  }

  async function initAuth() {
    if (!auth0Domain || !auth0ClientId) {
      setPanel("setup");
      return;
    }

    if (!window.auth0?.createAuth0Client) {
      setPanel("setup");
      return;
    }

    await loadMunicipalities();

    auth0Client = await window.auth0.createAuth0Client({
      domain: auth0Domain,
      clientId: auth0ClientId,
      authorizationParams: {
        redirect_uri: auth0RedirectUri,
        audience: auth0Audience || undefined,
      },
      cacheLocation: "localstorage",
    });

    const query = new URLSearchParams(window.location.search);
    if (query.get("code") && query.get("state")) {
      await auth0Client.handleRedirectCallback();
      window.history.replaceState({}, document.title, window.location.pathname);
    }

    const isAuthenticated = await auth0Client.isAuthenticated();

    if (!isAuthenticated) {
      if (loginRequested) {
        await auth0Client.loginWithRedirect({
          authorizationParams: {
            redirect_uri: auth0RedirectUri,
            screen_hint: "login",
          },
        });
        return;
      }

      setPanel("guest");
      return;
    }

    auth0User = await auth0Client.getUser();
    workspaceState.profile.contactEmail = auth0User?.email || "";
    await hydrateWorkspace();
    fillProfileForm();
    renderProfileSummary();
    renderFileList();
    renderQuestions();
    await refreshMunicipalitySignals();
    setPanel("member");
  }

  loginButtons.forEach((button) => {
    button.addEventListener("click", async () => {
      if (!auth0Client) {
        return;
      }

      await auth0Client.loginWithRedirect({
        authorizationParams: {
          redirect_uri: auth0RedirectUri,
          screen_hint: "login",
        },
      });
    });
  });

  logoutButtons.forEach((button) => {
    button.addEventListener("click", () => {
      if (!auth0Client) {
        return;
      }

      auth0Client.logout({
        logoutParams: {
          returnTo: auth0RedirectUri,
        },
      });
    });
  });

  profileForm?.municipalityLabel?.addEventListener("change", async () => {
    applyMunicipalitySelection(profileForm.municipalityLabel.value, "");
    await refreshMunicipalitySignals();
  });

  profileForm?.municipalityLabel?.addEventListener("blur", async () => {
    applyMunicipalitySelection(profileForm.municipalityLabel.value, "");
    await refreshMunicipalitySignals();
  });

  profileForm?.operatorName?.addEventListener("change", () => {
    const entry = findMunicipalityByLabel(profileForm.municipalityLabel.value);
    const operator =
      entry?.operators.find((item) => item.name === profileForm.operatorName.value) || null;

    workspaceState.profile.operatorName = profileForm.operatorName.value;
    workspaceState.profile.operatorWebsite = operator?.website || "";
    workspaceState.profile.operatorAddress = operator?.address || "";
    workspaceState.profile.operatorPostalCode = operator?.postalCode || "";
    renderProfileSummary();
  });

  profileForm?.addEventListener("submit", async (event) => {
    event.preventDefault();

    const entry = applyMunicipalitySelection(
      profileForm.municipalityLabel.value,
      profileForm.operatorName.value,
    );

    workspaceState.profile = {
      ...workspaceState.profile,
      municipalityKey: entry?.municipalityKey || "",
      municipalityLabel: entry?.label || profileForm.municipalityLabel.value.trim(),
      municipalityName: entry?.municipalityName || "",
      canton: entry?.canton || "",
      operatorName: profileForm.operatorName.value.trim(),
      hasBattery: profileForm.hasBattery.checked,
      hasEvCharging: profileForm.hasEvCharging.checked,
      questions: profileForm.questions.value.trim(),
      shareWithResearchers: profileForm.shareWithResearchers.checked,
      interestedInCommunity: profileForm.interestedInCommunity.checked,
      visibleInterest:
        profileForm.interestedInCommunity.checked && profileForm.visibleInterest.checked,
      contactEmail: auth0User?.email || "",
    };

    await persistProfile();
    renderProfileSummary();
    await refreshMunicipalitySignals();
  });

  uploadForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = uploadForm.files;
    const files = Array.from(input?.files || []);

    if (!files.length) {
      updateEnergySummary();
      return;
    }

    const entries = await filesToEntries(files);
    await addUploadEntries(entries);
    renderFileList();
    input.value = "";
  });

  clearFilesButton?.addEventListener("click", async () => {
    await clearAllUploads();
    renderFileList();
  });

  exportButton?.addEventListener("click", () => {
    const payload = {
      exportedAt: new Date().toISOString(),
      profile: workspaceState.profile,
      files: workspaceState.files,
      questions: workspaceState.questions,
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    const exportName = slugify(workspaceState.profile.municipalityLabel || "communi-profil");
    link.href = url;
    link.download = `${exportName}-profil.json`;
    link.click();
    URL.revokeObjectURL(url);
  });

  communityForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = communityForm.message.value.trim();

    if (!message) {
      return;
    }

    await addQuestionEntry(message);
    renderQuestions();
    communityForm.message.value = "";
  });

  clearCommunityButton?.addEventListener("click", async () => {
    await clearAllQuestions();
    renderQuestions();
  });

  setPanel("loading");
  initAuth().catch((error) => {
    window.__COMMUNI_REPORT_ERROR__?.(error, "auth");
    setPanel("setup");
  });
})();
