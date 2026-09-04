(function () {
  const root = document.querySelector("[data-sunshine-calculator]");

  if (!root) {
    return;
  }

  const apiBaseUrl = (root.dataset.apiBaseUrl || "").replace(/\/$/, "");
  const setup = root.querySelector("[data-calculator-setup]");
  const form = root.querySelector("[data-calculator-form]");
  const errorBox = root.querySelector("[data-calculator-error]");
  const resultBox = root.querySelector("[data-calculator-result]");
  const solarTarget = root.querySelector("[data-result-solar]");
  const kwhTarget = root.querySelector("[data-result-kwh]");
  const rowsTarget = root.querySelector("[data-result-rows]");
  const daylightTarget = root.querySelector("[data-result-daylight]");
  const columnsTarget = root.querySelector("[data-result-columns]");
  const timespanTarget = root.querySelector("[data-result-timespan]");

  function formatNumber(value, options = {}) {
    return new Intl.NumberFormat("de-CH", options).format(Number(value || 0));
  }

  function showError(message) {
    if (!errorBox) {
      return;
    }
    errorBox.textContent = message;
    errorBox.classList.remove("hidden");
  }

  function hideError() {
    errorBox?.classList.add("hidden");
  }

  if (!apiBaseUrl) {
    setup?.classList.remove("hidden");
    form?.querySelector("button")?.setAttribute("disabled", "disabled");
    return;
  }

  async function uploadSelectedFile() {
    hideError();
    resultBox?.classList.add("hidden");

    const input = form.file;
    const file = input?.files?.[0];

    if (!file) {
      showError("Bitte wähle zuerst eine CSV-Datei aus.");
      return;
    }

    const formData = new FormData();
    formData.append("file", file, file.name);

    const submitButton = form.querySelector("button");
    const previousLabel = submitButton?.textContent || "";
    if (submitButton) {
      submitButton.disabled = true;
      submitButton.textContent = "Wird berechnet ...";
    }

    try {
      const response = await fetch(`${apiBaseUrl}/v1/calculators/sunshine-share`, {
        method: "POST",
        body: formData,
      });
      const payload = await response.json().catch(() => ({}));

      if (!response.ok) {
        if (response.status === 413) {
          throw new Error("Die CSV-Datei ist zu gross. Bitte lade maximal 10 MB hoch.");
        }
        throw new Error(payload.error || "Die Datei konnte nicht ausgewertet werden.");
      }

      if (payload.ingestionStatus === "pending_ingestion") {
        showError(
          "Die CSV-Datei wurde gespeichert, wird aber noch nicht automatisch ausgewertet. Wir prüfen dieses Format und verbessern die Erkennung.",
        );
        input.value = "";
        return;
      }

      solarTarget.textContent = `${formatNumber(payload.solarProxyPercent, {
        maximumFractionDigits: 1,
      })} %`;
      kwhTarget.textContent = `${formatNumber(payload.totalKwh, {
        maximumFractionDigits: 0,
      })} kWh`;
      rowsTarget.textContent = formatNumber(payload.rowCount, {
        maximumFractionDigits: 0,
      });
      daylightTarget.textContent = `${formatNumber(payload.daylightConsumptionPercent, {
        maximumFractionDigits: 1,
      })} %`;
      columnsTarget.textContent = `${payload.seriesLabel || "Verbrauchsprofil"} erkannt: ${payload.detectedTimestampColumn || "Zeitspalte"} + ${payload.detectedConsumptionColumn || "Verbrauchsspalte"}`;
      timespanTarget.textContent =
        payload.firstTimestamp && payload.lastTimestamp
          ? `Zeitraum: ${payload.firstTimestamp} bis ${payload.lastTimestamp}`
          : "Zeitraum: nicht erkannt";
      resultBox?.classList.remove("hidden");
      input.value = "";
    } catch (error) {
      if (error instanceof TypeError) {
        showError(
          `Die Backend-API ist unter ${apiBaseUrl} nicht erreichbar. Starte die API und versuche es erneut.`,
        );
      } else {
        showError(error instanceof Error ? error.message : "Unerwarteter Fehler.");
      }
    } finally {
      if (submitButton) {
        submitButton.disabled = false;
        submitButton.textContent = previousLabel;
      }
    }
  }

  form?.file?.addEventListener("change", () => {
    if (form.file.files?.length) {
      uploadSelectedFile();
    }
  });

  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    await uploadSelectedFile();
  });
})();
