// Review detail: reviewed head, AI verdict and findings, and the separate GitHub outcome.
(() => {
  const F = window.Forge;
  const { apiJson, badge, errorMessage, escapeHtml, formatSgt, humanize, runBusy, safeUrl, shaHtml } = F;
  const $ = (sel, root = document) => root.querySelector(sel);

  const root = $("#review-detail");
  if (!root) return;
  const reviewId = Number(root.dataset.reviewId);
  const alertEl = $("#review-alert");
  const ACTIVE = ["queued", "running"];
  const POLL_MS = 3000;
  let state = null;
  let pollTimer = null;

  const STATUS = {
    queued: ["secondary", "Queued"],
    running: ["primary", "Review running"],
    completed: ["success", "Review completed"],
    failed: ["danger", "Failed"],
    stopped: ["warning", "Stopped"],
    interrupted: ["warning", "Interrupted"],
    blocked: ["warning", "Blocked"],
  };

  function dl(rows) {
    return `<dl class="detail-dl">${rows
      .filter(Boolean)
      .map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${v}</dd>`)
      .join("")}</dl>`;
  }

  function txt(value, empty = "unknown") {
    return value === null || value === undefined || value === ""
      ? `<span class="text-muted">${escapeHtml(empty)}</span>`
      : escapeHtml(value);
  }

  function verdictBadge(verdict) {
    if (!verdict) return `<span class="text-muted">No verdict yet</span>`;
    const tone = verdict === "APPROVE" ? "success" : verdict === "REQUEST_CHANGES" ? "danger" : "secondary";
    return badge(humanize(verdict), tone);
  }

  function findingHtml(finding) {
    if (finding === null || typeof finding !== "object") return `<li>${escapeHtml(finding)}</li>`;
    const where = [finding.path || finding.file, finding.line].filter((v) => v !== undefined && v !== null && v !== "");
    const head = [finding.severity, where.join(":")].filter(Boolean).map(escapeHtml).join(" · ");
    const body = finding.message || finding.title || finding.body || finding.description || JSON.stringify(finding);
    return `<li>${head ? `<strong>${head}</strong> ` : ""}${escapeHtml(body)}</li>`;
  }

  function render() {
    const { review, attempts } = state;
    const isCandidate = review.review_target === "candidate";
    const prUrl = safeUrl(review.pr_url);
    const [tone, label] = STATUS[review.status] || ["secondary", humanize(review.status)];
    $("#review-context").innerHTML = dl([
      isCandidate
        ? ["Target", `Local candidate of <a href="/tasks/${Number(review.task_id)}">task ${Number(review.task_id)}</a> (never posted to GitHub)`]
        : ["Pull request", prUrl ? `<a href="${escapeHtml(prUrl)}" target="_blank" rel="noopener">#${Number(review.pr_number)}</a>` : `#${Number(review.pr_number)}`],
      ["Repository", escapeHtml(review.repository)],
      ["Reviewed head commit", shaHtml(review.commit_sha, "not bound yet")],
      ["Base commit", shaHtml(review.base_sha, "unknown")],
      ["Exact diff", review.diff_complete === null ? txt(null) : review.diff_complete ? "complete" : "incomplete"],
      ["Reviewer tools", !review.review_policy
        ? txt(null)
        : review.review_policy.startsWith("restricted:")
          ? "Read, Grep and Glob only (no edits, no shell)"
          : `Not the restricted policy: ${escapeHtml(review.review_policy)}`],
    ]);
    const outcome = review.github_outcome || {};
    const outcomeBadge = outcome.pending
      ? `${badge("GitHub post pending", "info")} `
      : outcome.kind === "interrupted"
        ? `${badge("unconfirmed", "warning")} `
        : "";
    let formal = outcome.formal_github_approval ? badge("yes", "success") : badge("no", "secondary");
    if ((!outcome.kind || outcome.kind === "unknown") && ACTIVE.includes(review.status)) formal = `<span class="text-muted">not known yet</span>`;
    else if (outcome.pending) formal = `<span class="text-muted">not known yet (GitHub post pending)</span>`;
    else if (outcome.kind === "interrupted") formal = `<span class="text-muted">unconfirmed (check the pull request on GitHub)</span>`;
    $("#review-result").innerHTML = `
      <p class="mb-2">${badge(label, tone)}</p>
      ${dl([
        ["AI verdict", verdictBadge(review.ai_verdict)],
        ["GitHub outcome", `${outcomeBadge}${escapeHtml(F.reviewOutcomeLabel(review))}${outcome.event ? ` <code>${escapeHtml(outcome.event)}</code>` : ""}`],
        ["Formal GitHub approval", formal],
        outcome.recorded_at ? ["Recorded", escapeHtml(formatSgt(outcome.recorded_at))] : null,
        ["Merged", review.merged ? `${badge("merged (observed on GitHub)", "info")}` : "no"],
        review.error ? ["Error", `<span class="text-danger">${escapeHtml(review.error)}</span>`] : null,
      ])}
      <p class="small text-muted mb-0">The AI verdict is the reviewer's opinion. It does not approve delivery and Forge never merges.</p>`;
    const findings = review.findings || [];
    $("#review-findings").innerHTML = `
      ${findings.length ? `<ul class="findings mb-2">${findings.map(findingHtml).join("")}</ul>` : `<p class="text-muted mb-2">${review.status === "completed" ? "No findings reported." : "No findings yet."}</p>`}
      ${review.summary ? `<details><summary class="small">Reviewer summary</summary><div class="summary-text small mt-2">${escapeHtml(review.summary)}</div></details>` : ""}`;
    $("#review-attempts").innerHTML = (attempts || []).length
      ? attempts
          .map((a) => {
            const credits = a.usage?.factory_credits;
            return `<tr>
              <td>${Number(a.sequence)}</td>
              <td>${badge(humanize(a.status), (STATUS[a.status] || ["secondary"])[0])}</td>
              <td class="small">${escapeHtml(formatSgt(a.started_at || a.created_at))}</td>
              <td class="small">${a.duration_seconds != null ? `${Number(a.duration_seconds).toFixed(1)}s` : "—"}</td>
              <td>${credits === null || credits === undefined ? "<span class='text-muted'>unknown</span>" : escapeHtml(String(credits))}</td>
              <td class="small text-wrap outcome-cell">${escapeHtml([a.outcome_subtype, a.error].filter(Boolean).join(" · ") || "—")}</td>
            </tr>`;
          })
          .join("")
      : `<tr><td colspan="6" class="text-center text-muted py-3">No attempts recorded yet.</td></tr>`;
    const stopBtn = $("#review-stop-btn");
    if (stopBtn.dataset.busy !== "1") stopBtn.disabled = !ACTIVE.includes(review.status);
    $("#review-status").textContent = `Review status: ${humanize(review.status)}.${
      outcome.pending ? " GitHub outcome pending." : ""
    }`;
  }

  // The AI result lands before its GitHub post is recorded, so keep polling through both.
  function needsPolling(review) {
    return ACTIVE.includes(review.status) || Boolean(review.github_outcome && review.github_outcome.pending);
  }

  async function load() {
    clearTimeout(pollTimer);
    const result = await apiJson(`/api/reviews/${reviewId}`);
    if (!result.ok) {
      F.showAlert(alertEl, `Could not load this review. ${errorMessage(result)}`, "danger");
      pollTimer = setTimeout(load, POLL_MS * 3);
      return;
    }
    state = result.data;
    render();
    if (needsPolling(state.review)) pollTimer = setTimeout(load, POLL_MS);
  }

  $("#review-stop-btn").addEventListener("click", async (event) => {
    await runBusy(event.currentTarget, "Stopping…", async () => {
      const result = await apiJson(`/api/reviews/${reviewId}/stop`, { method: "POST" });
      if (result.ok) F.showAlert(alertEl, "Stop recorded. The review stays stopped and will not restart on its own.", "warning");
      else F.showAlert(alertEl, errorMessage(result), "danger");
      alertEl.focus();
    });
    await load();
  });

  load();
})();
