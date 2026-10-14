// Overview outcome economics: observed credits, dollar coverage, user-entered effort and the capacity scenario.
(() => {
  const F = window.Forge;
  const { apiJson, errorMessage, escapeHtml, formatElapsed, formatNumber, formatUsd, known, parseAmount, runBusy } = F;
  const $ = (sel, root = document) => root.querySelector(sel);

  const form = $("#assumptions-form");
  if (!form) return;
  let dirty = false;

  function setCard(key, value, detail) {
    const valueEl = $(`[data-econ="${key}"]`);
    const detailEl = $(`[data-econ-detail="${key}"]`);
    if (valueEl) valueEl.textContent = value;
    if (detailEl) detailEl.textContent = detail;
  }

  function usdLine(usd) {
    if (!usd) return "";
    const counts = `${Number(usd.attempts_reconciled)} reconciled, ${Number(usd.attempts_estimated)} estimated, ${Number(
      usd.attempts_unknown
    )} unknown`;
    if (known(usd.total_usd)) {
      return `Dollars: ${formatUsd(usd.total_usd)} across all attempts (${counts}). Explicit estimates and reconciled amounts only; not a bill.`;
    }
    const subtotal = known(usd.known_usd_subtotal)
      ? ` Known-only subtotal ${formatUsd(usd.known_usd_subtotal)} is not the total.`
      : "";
    return `Dollars: unknown (coverage ${usd.coverage}; ${counts}).${subtotal} No dollar amount is inferred from credits.`;
  }

  function renderCards(econ) {
    const pop = econ.population;
    setCard(
      "accepted",
      String(Number(pop.accepted)),
      `of ${Number(pop.tasks)} task(s). Accepted means operator-approved after checks and review passed; not publication, CI or merge.`
    );
    const elapsed = econ.elapsed;
    setCard(
      "elapsed",
      elapsed.sample ? `${formatElapsed(elapsed.median_seconds)} median` : "No accepted changes",
      elapsed.sample
        ? `Task created to operator approval, wall clock, across ${Number(elapsed.sample)} accepted change(s). Excludes publication and merge.`
        : "Measured from task creation to operator approval once a change is accepted."
    );
    const credits = econ.credits;
    setCard(
      "credits",
      known(credits.observed) ? formatNumber(credits.observed) : "unknown",
      `Coverage ${credits.coverage} over ${Number(credits.attempts)} attempt(s), failed and retried included. ` +
        `Accepted tasks ${formatNumber(credits.accepted_tasks)}, other tasks ${formatNumber(credits.not_accepted_tasks)}, ` +
        `${Number(credits.unsuccessful_attempt_count)} unsuccessful attempt(s) used ${formatNumber(credits.unsuccessful_attempts)} of them.`
    );
    const cpa = econ.cost_per_accepted_change;
    let cost = "Unknown";
    if (cpa.state === "available") cost = formatUsd(cpa.usd);
    else if (cpa.state === "no_accepted_changes") cost = "No accepted changes";
    const creditsPer = known(cpa.credits) ? ` About ${formatNumber(cpa.credits)} credits per accepted change.` : "";
    setCard("cost", cost, `${cpa.detail}${creditsPer} ${cpa.population}`);
    const effort = econ.human_effort;
    const minutes = known(effort.review_minutes) ? Number(effort.review_minutes) + Number(effort.rework_minutes) : null;
    setCard(
      "effort",
      known(minutes) ? `${formatNumber(minutes, 1)} min` : "Not recorded",
      `User-entered, not measured: ${formatNumber(effort.review_minutes, 1)} review + ${formatNumber(
        effort.rework_minutes,
        1
      )} rework minutes on ${Number(effort.tasks_recorded)} task(s). ${Number(effort.accepted_without_effort)} accepted change(s) have no entry.`
    );
    $("#econ-usd").textContent = usdLine(econ.usd);
  }

  function renderScenario(econ) {
    const s = econ.scenario;
    const a = econ.assumptions;
    const rows = [`<p class="mb-2">${escapeHtml(s.label)}</p>`];
    if (s.state !== "available") {
      rows.push(`<p class="mb-0 text-muted">${escapeHtml(s.detail)}</p>`);
    } else {
      rows.push(`<dl class="detail-dl mb-2">
        <dt>Baseline (assumed)</dt><dd>${escapeHtml(formatNumber(s.baseline_minutes, 1))} min for ${Number(
          econ.population.accepted
        )} accepted change(s)</dd>
        <dt>Recorded human minutes</dt><dd>${escapeHtml(formatNumber(s.recorded_human_minutes, 1))} min (user-entered)</dd>
        <dt>Estimated capacity</dt><dd>${escapeHtml(formatNumber(s.estimated_capacity_hours, 2))} engineering hours</dd>
        <dt>Value of that capacity</dt><dd>${
          known(s.estimated_capacity_value_usd)
            ? `${escapeHtml(formatUsd(s.estimated_capacity_value_usd))} at the assumed hourly cost`
            : "not valued (no hourly cost entered)"
        }</dd>
        <dt>Net of agent cost</dt><dd>${
          known(s.net_of_agent_cost_usd)
            ? escapeHtml(formatUsd(s.net_of_agent_cost_usd))
            : "not calculated: agent dollars are unknown"
        }</dd>
      </dl>
      <p class="mb-0 text-muted">Formula: ${escapeHtml(s.formula)}. ${escapeHtml(s.detail)}</p>`);
    }
    if (a) {
      rows.push(
        `<p class="mt-2 mb-0 text-muted">Assumptions from “${escapeHtml(a.source)}”, saved ${escapeHtml(
          F.formatSgt(a.updated_at)
        )}.</p>`
      );
    }
    $("#econ-scenario").innerHTML = rows.join("");
  }

  function fillForm(assumptions) {
    if (dirty) return;
    $("#assume-baseline").value = assumptions ? assumptions.baseline_minutes_per_change : "";
    $("#assume-hourly").value = assumptions && known(assumptions.loaded_hourly_usd) ? assumptions.loaded_hourly_usd : "";
    $("#assume-source").value = assumptions ? assumptions.source : "";
    $("#assumptions-saved").textContent = assumptions
      ? `Saved ${F.formatSgt(assumptions.updated_at)}`
      : "No assumptions saved yet.";
  }

  function render(econ) {
    if (!econ) return;
    renderCards(econ);
    renderScenario(econ);
    fillForm(econ.assumptions);
    $("#econ-notes").innerHTML = (econ.notes || []).map((n) => `<li>${escapeHtml(n)}</li>`).join("");
  }

  function invalid(input, message) {
    input.setAttribute("aria-invalid", "true");
    F.showAlert($("#assumptions-alert"), message, "danger");
    input.focus();
    return null;
  }

  function readForm() {
    form.querySelectorAll("[aria-invalid]").forEach((el) => el.removeAttribute("aria-invalid"));
    const baselineEl = $("#assume-baseline");
    const hourlyEl = $("#assume-hourly");
    const sourceEl = $("#assume-source");
    const baseline = parseAmount(baselineEl.value, { max: 100000, allowZero: false });
    if (baseline === null) {
      return invalid(baselineEl, "Enter baseline minutes per change as a number above 0 and at most 100000.");
    }
    let hourly = null;
    if (hourlyEl.value.trim() !== "") {
      hourly = parseAmount(hourlyEl.value, { max: 10000 });
      if (hourly === null) return invalid(hourlyEl, "Enter the hourly cost as a number from 0 to 10000, or leave it empty.");
    }
    const source = sourceEl.value.trim();
    if (!source) return invalid(sourceEl, "Say where these assumptions came from.");
    return { baseline_minutes_per_change: baseline, loaded_hourly_usd: hourly, source };
  }

  form.addEventListener("input", () => {
    dirty = true;
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    F.hideAlert($("#assumptions-alert"));
    const body = readForm();
    if (!body) return;
    runBusy($("#assumptions-btn"), "Saving…", async () => {
      const result = await apiJson("/api/economics/assumptions", { method: "PUT", json: body });
      if (!result.ok) {
        F.showAlert($("#assumptions-alert"), errorMessage(result, "The assumptions could not be saved."), "danger");
        return;
      }
      dirty = false;
      F.showAlert($("#assumptions-alert"), "Assumptions saved. They are labeled user-entered everywhere.", "success");
      document.dispatchEvent(new CustomEvent("forge:refresh-overview"));
    });
  });

  window.ForgeEconomics = { render };
})();
