// Task outcome economics, user-entered human effort and the selected-task evidence export.
(() => {
  const F = window.Forge;
  const { apiJson, errorMessage, escapeHtml, formatElapsed, formatNumber, formatUsd, known, parseAmount, runBusy } = F;
  const $ = (sel, root = document) => root.querySelector(sel);

  const root = $("#task-detail");
  const form = $("#effort-form");
  if (!root || !form) return;
  const taskId = root.dataset.taskId;
  let dirty = false;

  function usdText(usd) {
    if (!usd) return "unknown";
    const counts = `${Number(usd.attempts_reconciled)} reconciled, ${Number(usd.attempts_estimated)} estimated, ${Number(
      usd.attempts_unknown
    )} unknown`;
    if (known(usd.total_usd)) return `${formatUsd(usd.total_usd)} (${counts}; explicit estimates and reconciliations only)`;
    const subtotal = known(usd.known_usd_subtotal) ? `; known-only subtotal ${formatUsd(usd.known_usd_subtotal)} is not the total` : "";
    return `unknown (coverage ${usd.coverage}; ${counts}${subtotal}). No dollar amount is inferred from credits.`;
  }

  function creditsText(econ) {
    const coverage = `credit coverage ${econ.credit_coverage}${F.creditGaps(econ.credit_attempts)}`;
    if (!known(econ.observed_factory_credits)) return `unknown · ${coverage}`;
    const subtotal = econ.credit_coverage === "complete" ? "" : " (known subtotal)";
    return `${formatNumber(econ.observed_factory_credits)}${subtotal} · ${coverage}`;
  }

  function statusCounts(counts) {
    const parts = Object.entries(counts || {}).map(([status, n]) => `${Number(n)} ${F.humanize(status)}`);
    return parts.length ? parts.join(", ") : "none";
  }

  function unsuccessfulText(count, credits) {
    if (!Number(count)) return "no unsuccessful attempts";
    return `${Number(count)} unsuccessful attempt(s) used ${formatNumber(credits)} observed credits`;
  }

  function render(econ) {
    const elapsed = econ.elapsed || {};
    const reviews = econ.candidate_reviews || {};
    const accepted = econ.accepted
      ? F.badge("accepted", "success")
      : F.badge(`not accepted (${F.humanize(econ.stage)})`, "secondary");
    $("#task-economics-body").innerHTML = `
      <p class="small mb-2">${accepted} <span class="text-muted">${escapeHtml(econ.accepted_definition)}</span></p>
      <dl class="detail-dl mb-2">
        <dt>Factory credits (observed)</dt>
        <dd>${escapeHtml(creditsText(econ))}</dd>
        ${
          econ.accepted
            ? `<dt>Credits per accepted change</dt><dd>${
                known(econ.credits_per_accepted_change)
                  ? escapeHtml(formatNumber(econ.credits_per_accepted_change))
                  : `<span class="text-muted">unknown. ${escapeHtml(econ.credits_per_accepted_detail)}</span>`
              }</dd>`
            : ""
        }
        <dt>Attempts included</dt>
        <dd>${Number(econ.attempt_count)} (${escapeHtml(statusCounts(econ.attempt_status_counts))}); ${escapeHtml(
          unsuccessfulText(econ.unsuccessful_attempt_count, econ.unsuccessful_attempt_factory_credits)
        )}</dd>
        <dt>Candidate reviews</dt>
        <dd>${Number(reviews.count || 0)} review(s), ${escapeHtml(formatNumber(reviews.observed_factory_credits))} credits${
          Number(reviews.count || 0) ? ` (credit coverage ${escapeHtml(reviews.credit_coverage)})` : ""
        }</dd>
        <dt>Dollars</dt>
        <dd>${escapeHtml(usdText(econ.usd))}</dd>
        <dt>Created to approval</dt>
        <dd>${
          known(elapsed.seconds)
            ? `${escapeHtml(formatElapsed(elapsed.seconds))} wall clock`
            : `<span class="text-muted">${escapeHtml(elapsed.detail || "unknown")}</span>`
        }</dd>
      </dl>
      <p class="small text-muted mb-0">${escapeHtml(econ.elapsed_definition)}</p>`;
    fillEffort(econ.human_effort);
  }

  function fillEffort(effort) {
    if (dirty) return;
    $("#effort-review").value = effort && effort.recorded ? effort.review_minutes : "";
    $("#effort-rework").value = effort && effort.recorded ? effort.rework_minutes : "";
    $("#effort-note").value = effort && effort.recorded ? effort.note : "";
    $("#effort-saved").textContent =
      effort && effort.recorded
        ? `Saved ${F.formatSgt(effort.updated_at)} · ${formatNumber(effort.total_minutes, 1)} min total (user-entered)`
        : "Not recorded yet.";
  }

  async function load() {
    const result = await apiJson(`/api/tasks/${encodeURIComponent(taskId)}/economics`);
    if (!result.ok) {
      $("#task-economics-body").innerHTML = `<span class="text-danger">${escapeHtml(
        errorMessage(result, "Economics could not be loaded.")
      )}</span>`;
      return;
    }
    render(result.data);
  }

  function invalid(input, message) {
    input.setAttribute("aria-invalid", "true");
    F.showAlert($("#effort-alert"), message, "danger");
    input.focus();
    return null;
  }

  function readEffort() {
    form.querySelectorAll("[aria-invalid]").forEach((el) => el.removeAttribute("aria-invalid"));
    const reviewEl = $("#effort-review");
    const reworkEl = $("#effort-rework");
    const review = parseAmount(reviewEl.value, { max: 100000 });
    if (review === null) return invalid(reviewEl, "Enter review minutes as a number from 0 to 100000.");
    const rework = parseAmount(reworkEl.value, { max: 100000 });
    if (rework === null) return invalid(reworkEl, "Enter rework minutes as a number from 0 to 100000.");
    return { review_minutes: review, rework_minutes: rework, note: $("#effort-note").value.trim() };
  }

  form.addEventListener("input", () => {
    dirty = true;
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    F.hideAlert($("#effort-alert"));
    const body = readEffort();
    if (!body) return;
    runBusy($("#effort-btn"), "Saving…", async () => {
      const result = await apiJson(`/api/tasks/${encodeURIComponent(taskId)}/human-effort`, { method: "PUT", json: body });
      if (!result.ok) {
        F.showAlert($("#effort-alert"), errorMessage(result, "Human effort could not be saved."), "danger");
        return;
      }
      dirty = false;
      F.showAlert($("#effort-alert"), "Human effort saved and labeled user-entered.", "success");
      await load();
    });
  });

  const previewBtn = $("#export-preview-btn");
  const preview = $("#export-preview");
  previewBtn.addEventListener("click", () => {
    if (previewBtn.getAttribute("aria-expanded") === "true") {
      preview.classList.add("d-none");
      previewBtn.setAttribute("aria-expanded", "false");
      previewBtn.textContent = "Preview evidence";
      return;
    }
    runBusy(previewBtn, "Loading…", async () => {
      const result = await apiJson(`/api/tasks/${encodeURIComponent(taskId)}/evidence`);
      preview.textContent = result.ok
        ? JSON.stringify(result.data, null, 2)
        : errorMessage(result, "The evidence export could not be built.");
      preview.classList.remove("d-none");
    }).then(() => {
      if (preview.classList.contains("d-none")) return;
      previewBtn.setAttribute("aria-expanded", "true");
      previewBtn.textContent = "Hide preview";
    });
  });

  document.addEventListener("forge:task-updated", () => {
    load();
  });
  load();
})();
