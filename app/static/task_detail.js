// Task detail: lifecycle, attempts, timeline, SHA-bound delivery evidence and explicit operator actions.
(() => {
  const F = window.Forge;
  const { apiJson, badge, errorMessage, escapeHtml, formatSgt, humanize, reviewReadinessBadge, runBusy, safeUrl, shaHtml } =
    F;
  const $ = (sel, root = document) => root.querySelector(sel);

  const root = $("#task-detail");
  if (!root) return;
  const taskId = Number(root.dataset.taskId);
  const alertEl = $("#detail-alert");
  const POLL_MS = 3000;
  let state = null;
  let pollTimer = null;
  let pending = null;
  // Actions in flight; re-rendered action buttons stay disabled until they settle.
  let inFlight = 0;

  const ACTIVE = ["queued", "running"];
  const CONTINUABLE = ["interrupted", "failed", "stopped"];

  const STATUS_TEXT = {
    queued: ["secondary", "Queued", "Waiting for the agent turn to start."],
    running: ["primary", "Agent turn running", "The agent is working. You can stop the turn."],
    completed: [
      "success",
      "Agent turn completed",
      "The latest agent turn finished. This alone is not acceptance, publication, passing CI or a merge.",
    ],
    failed: ["danger", "Failed", "The latest turn failed. Nothing restarts unless you continue explicitly."],
    stopped: ["warning", "Stopped", "Stopped durably. It will not restart on its own, including after a restart."],
    interrupted: ["warning", "Interrupted", "Interrupted by a service restart. Continue explicitly to resume."],
    blocked: ["warning", "Blocked", "Refused before any paid turn by a run limit or spending check."],
  };
  const ATTEMPT_TONES = {
    starting: "primary",
    running: "primary",
    completed: "success",
    failed: "danger",
    interrupted: "warning",
    stopped: "warning",
    blocked: "warning",
  };
  const READINESS = {
    not_ready: ["secondary", "Not ready for approval"],
    ready_for_approval: ["info", "Ready for your approval"],
    approved: ["success", "Approved; not published"],
    publishing: ["warning", "Publishing"],
    publication_incomplete: ["danger", "Publication incomplete"],
    published: ["success", "Published as draft PR"],
  };
  const CHECK_TONES = { passed: "success", failed: "danger", running: "primary", queued: "secondary" };
  const CI_LABELS = {
    not_published: ["secondary", "Not published: remote CI cannot run yet"],
    not_observed: ["secondary", "Not observed yet"],
    none_reported: ["secondary", "No CI reported by GitHub"],
    pending: ["warning", "Pending"],
    failing: ["danger", "Failing"],
    passing: ["success", "Passing"],
    unavailable: ["dark", "Unavailable"],
  };

  function say(message, type) {
    F.showAlert(alertEl, message, type);
    alertEl.focus({ preventScroll: false });
  }

  function sayHtml(html, type) {
    alertEl.className = `alert alert-${type}`;
    alertEl.innerHTML = html;
    alertEl.classList.remove("d-none");
    alertEl.focus();
  }

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

  function blockerList(blockers) {
    if (!blockers || !blockers.length) return "";
    return `<ul class="blockers mb-2">${blockers
      .map((b) => `<li><code>${escapeHtml(b.code)}</code> ${escapeHtml(b.message)}</li>`)
      .join("")}</ul>`;
  }

  // ---- Lifecycle and controls ----
  function renderLifecycle() {
    const { task, accounting } = state;
    const [tone, label, explanation] = STATUS_TEXT[task.status] || ["secondary", humanize(task.status), ""];
    const credits = accounting?.observed_factory_credits;
    $("#lifecycle-body").innerHTML = `
      <p class="mb-2">${badge(label, tone)} <span class="small ms-1">${escapeHtml(explanation)}</span></p>
      ${dl([
        ["Repository", escapeHtml(task.repository)],
        ["Issue", `#${Number(task.issue_number)}`],
        ["Droid session", txt(task.droid_session_id, "none")],
        ["Attempts", String(Number(accounting?.attempt_count ?? state.attempts.length))],
        [
          "Observed Factory credits",
          credits === null || credits === undefined ? txt(null) : escapeHtml(String(credits)),
        ],
        ["Usage coverage", txt(humanize(accounting?.coverage))],
        task.error ? ["Last error", `<span class="text-danger">${escapeHtml(task.error)}</span>`] : null,
        task.summary ? ["Agent summary", `<div class="summary-text">${escapeHtml(task.summary)}</div>`] : null,
      ])}`;
  }

  function applyControls() {
    if (!state) return;
    const { task } = state;
    const active = ACTIVE.includes(task.status);
    const hasSession = Boolean(task.droid_session_id);
    const followBtn = $("#followup-btn");
    const stopBtn = $("#stop-btn");
    const recoverBtn = $("#recover-btn");
    if (followBtn.dataset.busy !== "1") followBtn.disabled = active || !hasSession;
    if (stopBtn.dataset.busy !== "1") stopBtn.disabled = !active;
    if (recoverBtn.dataset.busy !== "1") recoverBtn.disabled = !(hasSession && CONTINUABLE.includes(task.status));
    let note = "";
    if (active) note = "A turn is active: follow-ups wait until it finishes or is stopped.";
    else if (!hasSession) note = "This task has no Droid session, so it cannot be continued.";
    else if (CONTINUABLE.includes(task.status)) note = "Nothing restarts automatically; continue or follow up explicitly.";
    $("#controls-note").textContent = note;
  }

  // ---- Delivery ----
  function findingHtml(finding) {
    if (finding === null || typeof finding !== "object") return `<li>${escapeHtml(finding)}</li>`;
    const where = [finding.path || finding.file, finding.line].filter((v) => v !== undefined && v !== null && v !== "");
    const head = [finding.severity, where.join(":")].filter(Boolean).map(escapeHtml).join(" · ");
    const body = finding.message || finding.title || finding.body || finding.description || JSON.stringify(finding);
    return `<li>${head ? `<strong>${head}</strong> ` : ""}${escapeHtml(body)}</li>`;
  }

  function candidateSection(d) {
    const c = d.candidate;
    if (!c.sha) {
      return `<p class="text-muted mb-0">No candidate commit is recorded. The agent's turn has not produced a clean, committed change
        (candidate status: ${escapeHtml(humanize(c.status))}).</p>`;
    }
    return dl([
      ["Candidate commit", shaHtml(c.sha)],
      ["Branch", txt(c.branch)],
      ["Status", txt(humanize(c.status))],
      ["Recording", c.generation ? `generation ${Number(c.generation)}` : txt(null)],
      ["Base commit", shaHtml(c.base_sha, "unknown")],
    ]);
  }

  function checksSection(d) {
    const checks = d.checks;
    const configured = checks.configuration?.checks || [];
    const run = checks.latest_run;
    const results = (run?.results || [])
      .map(
        (r) => `<li>${badge(humanize(r.status), CHECK_TONES[r.status] || "secondary")} <strong>${escapeHtml(r.name)}</strong>
          <span class="text-muted small">exit ${txt(r.exit_code)} · ${r.duration_seconds != null ? `${Number(r.duration_seconds).toFixed(1)}s` : "—"}</span>
          ${r.output_tail ? `<details><summary class="small">Output (tail)</summary><pre class="output-tail">${escapeHtml(r.output_tail)}</pre></details>` : ""}</li>`
      )
      .join("");
    const runHtml = run
      ? `<p class="small mb-1">Run ${Number(run.id)} · evaluated ${shaHtml(run.evaluated_sha, "not evaluated")} · ${escapeHtml(humanize(run.status))}</p>
         <ul class="list-unstyled check-results mb-2">${results || "<li class='text-muted'>No results recorded.</li>"}</ul>
         ${run.error ? `<p class="small text-danger mb-2">${escapeHtml(run.error)}</p>` : ""}`
      : `<p class="text-muted small mb-2">No check run on this candidate commit yet.</p>`;
    const canRun =
      d.candidate.status === "ready" && !ACTIVE.includes(state.task.status) && checks.status !== "running" &&
      !["not_configured", "configuration_invalid"].includes(checks.status);
    return `
      <h3 class="h6">Acceptance checks <span class="text-muted small">(run by the app in a clean checkout)</span></h3>
      <p class="mb-2">${badge(humanize(checks.status), checks.status === "passed" ? "success" : checks.status === "failed" ? "danger" : "secondary")}</p>
      <p class="small mb-1">Configured: ${configured.length ? configured.map((c) => `<code>${escapeHtml(c.name)}</code>`).join(", ") : "<span class='text-muted'>none</span>"}</p>
      ${runHtml}
      <button type="button" class="btn btn-sm btn-outline-primary" data-action="checks" ${canRun ? "" : "disabled"}>Run acceptance checks</button>`;
  }

  function reviewSection(d) {
    const review = d.review;
    const latest = review.latest;
    const findings = latest?.findings || [];
    const latestHtml = latest
      ? `${dl([
          ["Review", `<a href="/reviews/${Number(latest.id)}">Review ${Number(latest.id)}</a>`],
          ["Reviewed commit", shaHtml(latest.commit_sha)],
          ["AI verdict", txt(humanize(latest.ai_verdict), "none")],
          ["Exact diff", latest.diff_complete ? "complete" : "incomplete"],
        ])}
        <p class="small mb-1">Findings (${findings.length}):</p>
        <ul class="small findings mb-2">${findings.length ? findings.map(findingHtml).join("") : "<li class='text-muted'>None reported.</li>"}</ul>`
      : `<p class="text-muted small mb-2">No completed restricted review of this candidate commit.</p>`;
    const activeHtml = review.active_review_id
      ? `<p class="small mb-2">${badge("Review running", "primary")} <a href="/reviews/${Number(review.active_review_id)}">Review ${Number(review.active_review_id)}</a></p>`
      : "";
    const canStart = d.candidate.status === "ready" && !ACTIVE.includes(state.task.status) && !review.active_review_id;
    return `
      <h3 class="h6">Restricted review <span class="text-muted small">(AI, read-only)</span></h3>
      <p class="mb-2">${reviewReadinessBadge(review.status, "AI review: ")}</p>
      ${activeHtml}${latestHtml}
      <p class="small text-muted mb-2">${escapeHtml(review.tools)}</p>
      <button type="button" class="btn btn-sm btn-outline-primary" data-action="review" ${canStart ? "" : "disabled"}>Start restricted review</button>`;
  }

  function approvalSection(d) {
    const r = d.readiness;
    const current = d.approval.current;
    const historical = (d.approval.history || []).filter((a) => !a.applies_to_current_candidate);
    const currentHtml = current
      ? dl([
          ["Approved commit", shaHtml(current.candidate_sha)],
          ["Approved at", escapeHtml(formatSgt(current.approved_at))],
          ["Evidence", `check run ${txt(current.check_run_id)}, review ${txt(current.review_id)}`],
          ["Meaning", escapeHtml(d.approval.meaning)],
        ])
      : `<p class="small text-muted mb-2">This candidate commit is not approved.</p>`;
    const historyHtml = historical.length
      ? `<p class="small mb-2">${badge("Stale approval", "warning")} The earlier approval of ${historical
          .map((a) => `<code title="${escapeHtml(a.candidate_sha)}">${escapeHtml(F.shortSha(a.candidate_sha))}</code>`)
          .join(", ")} does not carry over: the candidate commit changed since, so it cannot be published.</p>`
      : "";
    return `
      <h3 class="h6">Operator approval</h3>
      ${r.approval_blockers.length ? `<p class="small mb-1">Approval is blocked:</p>${blockerList(r.approval_blockers)}` : ""}
      ${currentHtml}${historyHtml}
      <button type="button" class="btn btn-sm btn-success" data-action="approve" ${r.ready_for_approval ? "" : "disabled"}>Approve this commit…</button>`;
  }

  function publicationFacts(p) {
    if (!p) return `<p class="small text-muted mb-2">Not published. No push or pull request has been made.</p>`;
    const pr = p.pull_request || {};
    const prUrl = safeUrl(pr.url);
    return dl([
      ["Status", `${badge(humanize(p.status), p.complete ? "success" : "danger")} after ${Number(p.publish_attempts)} attempt(s)`],
      ["Pushed", p.pushed === null ? txt(null, "not attempted") : `${p.pushed ? "yes" : "no"} (${txt(humanize(p.push_detail))})`],
      ["Remote branch head", `${shaHtml(p.remote_head_sha, "not verified")} ${p.remote_head_sha ? (p.remote_head_matches ? badge("matches approved commit", "success") : badge("does not match", "danger")) : ""}`],
      [
        "Pull request",
        pr.number
          ? `${prUrl ? `<a href="${escapeHtml(prUrl)}" target="_blank" rel="noopener">#${Number(pr.number)}</a>` : `#${Number(pr.number)}`}
             ${pr.draft ? badge("draft", "secondary") : ""} ${txt(pr.state)} · ${escapeHtml(humanize(pr.action))}
             ${pr.merged ? badge("merged (observed)", "info") : ""}`
          : txt(null, "not created"),
      ],
      ["PR head", `${shaHtml(pr.head_sha, "unknown")} ${pr.head_sha ? (pr.head_matches ? badge("matches", "success") : badge("does not match", "danger")) : ""}`],
      p.error ? ["Error", `<span class="text-danger">${escapeHtml(p.error)}</span>`] : null,
    ]);
  }

  function publicationSection(d) {
    const dest = d.destination;
    const r = d.readiness;
    const pub = d.publication || d.latest_publication;
    return `
      <h3 class="h6">Publication</h3>
      ${dl([
        ["Destination", `${escapeHtml(dest.repository)} · branch <code>${txt(dest.branch)}</code>`],
        ["Pull request", "draft (created or reused)"],
        ["Base", escapeHtml(dest.base_branch)],
        ["Force push / merge", "never / never"],
        ["Policy", dest.allowed ? badge("permitted", "success") : `${badge("not permitted", "danger")} <span class="small">${escapeHtml(dest.reason || "")}</span>`],
      ])}
      ${r.publish_blockers.length && r.state !== "published" ? `<p class="small mb-1">Publication is blocked:</p>${blockerList(r.publish_blockers)}` : ""}
      ${publicationFacts(pub)}
      <button type="button" class="btn btn-sm btn-primary" data-action="publish" ${r.ready_to_publish ? "" : "disabled"}>Publish draft PR…</button>`;
  }

  function ciSection(d) {
    const ci = d.remote_ci;
    const [tone, label] = CI_LABELS[ci.state] || ["secondary", humanize(ci.state)];
    const checks = (ci.checks || [])
      .map((c) => {
        const url = safeUrl(c.url || c.html_url || c.details_url || c.target_url);
        const name = escapeHtml(c.name || c.context || "check");
        return `<li>${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${name}</a>` : name}
          <span class="text-muted small">${escapeHtml(humanize(c.conclusion || c.state || c.status))}</span></li>`;
      })
      .join("");
    const pushed = Boolean((d.publication || d.latest_publication)?.pushed);
    return `
      <h3 class="h6">Remote CI <span class="text-muted small">(GitHub, observed read-only)</span></h3>
      <p class="mb-1">${badge(label, tone)} ${ci.stale ? badge("observation is for another commit", "warning") : ""}</p>
      <p class="small mb-1">${escapeHtml(ci.detail || "")}</p>
      ${ci.observed_at ? `<p class="small mb-1">Observed ${escapeHtml(formatSgt(ci.observed_at))} on ${shaHtml(ci.observed_sha)}</p>` : ""}
      ${checks ? `<ul class="small mb-2">${checks}</ul>` : ""}
      <p class="small text-muted mb-2">Remote CI is not a publication prerequisite; local checks, review and approval are.</p>
      <button type="button" class="btn btn-sm btn-outline-secondary" data-action="ci" ${pushed ? "" : "disabled"}>Refresh CI (read-only)</button>`;
  }

  function renderDelivery() {
    const d = state.delivery;
    const [tone, label] = READINESS[d.readiness.state] || ["secondary", humanize(d.readiness.state)];
    $("#readiness-badge").innerHTML = badge(label, tone);
    $("#delivery-body").innerHTML = `
      <div class="row g-4">
        <div class="col-lg-6"><h3 class="h6">Candidate commit</h3>${candidateSection(d)}</div>
        <div class="col-lg-6">${approvalSection(d)}</div>
        <div class="col-lg-6">${checksSection(d)}</div>
        <div class="col-lg-6">${reviewSection(d)}</div>
        <div class="col-lg-6">${publicationSection(d)}</div>
        <div class="col-lg-6">${ciSection(d)}</div>
      </div>
      <p class="small text-muted mt-3 mb-0">${escapeHtml(d.merge.detail)}.</p>`;
    if (inFlight) document.querySelectorAll("#delivery-body button[data-action]").forEach((b) => (b.disabled = true));
  }

  // ---- Attempts and timeline ----
  function attemptOutcome(a) {
    const parts = [];
    if (a.outcome_subtype) parts.push(humanize(a.outcome_subtype));
    if (a.stop_reason) parts.push(`stop: ${humanize(a.stop_reason)}`);
    if (a.error) parts.push(a.error);
    else if (a.summary) parts.push(a.summary.slice(0, 160));
    return parts.length ? escapeHtml(parts.join(" · ")) : `<span class="text-muted">—</span>`;
  }

  function renderAttempts() {
    const attempts = state.attempts || [];
    $("#attempts-tbody").innerHTML = attempts.length
      ? attempts
          .map((a) => {
            const credits = a.usage?.factory_credits;
            return `<tr data-attempt-id="${Number(a.id)}">
              <td>${Number(a.sequence)}</td>
              <td>${escapeHtml(humanize(a.kind))}</td>
              <td>${badge(humanize(a.status), ATTEMPT_TONES[a.status] || "secondary")}</td>
              <td class="small">${escapeHtml(formatSgt(a.started_at || a.created_at))}</td>
              <td class="small">${a.duration_seconds != null ? `${Number(a.duration_seconds).toFixed(1)}s` : "—"}</td>
              <td>${credits === null || credits === undefined ? "<span class='text-muted'>unknown</span>" : escapeHtml(String(credits))}</td>
              <td class="small">${escapeHtml(humanize(a.usage?.completeness))}</td>
              <td>${a.head_sha ? `<code title="${escapeHtml(a.head_sha)}">${escapeHtml(F.shortSha(a.head_sha))}</code>` : "<span class='text-muted'>—</span>"}</td>
              <td class="small text-wrap outcome-cell">${attemptOutcome(a)}</td>
            </tr>`;
          })
          .join("")
      : `<tr><td colspan="9" class="text-center text-muted py-3">No attempts recorded${
          state.task.accounting_basis === "legacy_summary" ? " (this task predates per-turn attempts)" : ""
        }.</td></tr>`;
    const acc = state.accounting || {};
    $("#accounting-note").textContent =
      `Accounting basis: ${humanize(acc.accounting_basis)}. Attempts with unknown usage: ${Number(acc.attempts_with_unknown_usage || 0)}. ` +
      "Credits are Factory credits as reported by the SDK; no dollar amount is inferred from them.";
  }

  function payloadSummary(payload) {
    const entries = Object.entries(payload || {}).slice(0, 6);
    if (!entries.length) return "";
    return entries
      .map(([k, v]) => {
        const value = typeof v === "object" && v !== null ? JSON.stringify(v) : String(v);
        return `<span class="tl-kv"><span class="text-muted">${escapeHtml(k)}:</span> ${escapeHtml(value.slice(0, 200))}</span>`;
      })
      .join(" ");
  }

  function renderTimeline(timeline) {
    const list = $("#timeline-list");
    if (!timeline) {
      list.innerHTML = `<li class="text-danger p-3">The timeline could not be loaded.</li>`;
      return;
    }
    const events = timeline.events || [];
    const limits = timeline.event_limits || {};
    $("#timeline-limits").textContent = limits.max_events_per_attempt
      ? `bounded: up to ${limits.max_events_per_attempt} events per attempt`
      : "bounded, scrubbed public events";
    list.innerHTML = events.length
      ? events
          .map(
            (e) => `<li class="timeline-item">
              <span class="tl-time small text-muted">${escapeHtml(formatSgt(e.created_at))}</span>
              <span class="tl-attempt small">attempt ${Number(e.attempt_sequence)} (${escapeHtml(humanize(e.kind))})</span>
              <span class="tl-type">${badge(humanize(e.type), "dark")}</span>
              <span class="tl-payload small">${payloadSummary(e.payload)}</span>
            </li>`
          )
          .join("")
      : `<li class="text-muted p-3">No timeline events recorded yet.</li>`;
  }

  // ---- Loading and polling ----
  function needsPolling() {
    if (!state) return false;
    const d = state.delivery;
    return (
      ACTIVE.includes(state.task.status) ||
      (state.attempts || []).some((a) => ["starting", "running"].includes(a.status)) ||
      d.checks.status === "running" ||
      d.review.status === "pending" ||
      d.readiness.state === "publishing"
    );
  }

  async function load() {
    clearTimeout(pollTimer);
    const [detail, timeline] = await Promise.all([
      apiJson(`/api/tasks/${taskId}`),
      apiJson(`/api/tasks/${taskId}/timeline`),
    ]);
    if (!detail.ok) {
      say(`Could not load this task. ${errorMessage(detail)}`, "danger");
      pollTimer = setTimeout(load, POLL_MS * 3);
      return;
    }
    state = detail.data;
    renderLifecycle();
    applyControls();
    renderDelivery();
    renderAttempts();
    renderTimeline(timeline.ok ? timeline.data : null);
    $("#detail-status").textContent = `Task status: ${humanize(state.task.status)}.`;
    document.dispatchEvent(new CustomEvent("forge:task-updated"));
    if (needsPolling()) pollTimer = setTimeout(load, POLL_MS);
  }

  async function act(button, busyLabel, request, onSuccess) {
    if (inFlight) return;
    inFlight += 1;
    try {
      await runBusy(button, busyLabel, async () => {
        const result = await request();
        if (result.ok || (result.status === 502 && result.data?.publication)) onSuccess(result);
        else say(errorMessage(result), "danger");
      });
    } finally {
      inFlight -= 1;
    }
    await load();
    applyControls();
  }

  // ---- Agent controls ----
  $("#followup-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const text = $("#followup-text").value.trim();
    if (!text) {
      say("Write a follow-up instruction first.", "danger");
      $("#followup-text").focus();
      return;
    }
    act(
      $("#followup-btn"),
      "Sending…",
      () => apiJson(`/api/tasks/${taskId}/follow-up`, { method: "POST", json: { instruction: text } }),
      (r) => {
        $("#followup-text").value = "";
        say(
          `Follow-up accepted as attempt ${Number(r.data.sequence)} (${humanize(r.data.status)}). Earlier attempts and their usage are unchanged.`,
          "success"
        );
      }
    );
  });

  $("#stop-btn").addEventListener("click", (event) =>
    act(
      event.currentTarget,
      "Stopping…",
      () => apiJson(`/api/tasks/${taskId}/stop`, { method: "POST" }),
      (r) =>
        say(
          r.data.already_stopped
            ? "This task was already stopped."
            : "Stop recorded. The active turn was asked to interrupt; the task stays stopped and will not restart on its own.",
          "warning"
        )
    )
  );

  $("#recover-btn").addEventListener("click", (event) =>
    act(
      event.currentTarget,
      "Continuing…",
      () => apiJson(`/api/tasks/${taskId}/recover`, { method: "POST" }),
      (r) => say(`Continuation started as attempt ${Number(r.data.sequence)}. The earlier attempt keeps its outcome.`, "success")
    )
  );

  // ---- Delivery actions ----
  const modalEl = $("#confirm-modal");
  const modal = new bootstrap.Modal(modalEl);
  let lastConfirmKind = null;
  modalEl.addEventListener("shown.bs.modal", () => modalEl.querySelector("[data-bs-dismiss=modal].btn")?.focus());
  // Polling re-renders the delivery section, so the button that opened the dialog may no longer exist.
  modalEl.addEventListener("hidden.bs.modal", () => {
    const trigger = $(`#delivery-body button[data-action="${lastConfirmKind}"]`);
    if (trigger && !trigger.disabled) trigger.focus();
    else alertEl.focus();
  });

  function openConfirm(kind) {
    const d = state.delivery;
    const sha = d.candidate.sha;
    pending = { kind, sha };
    lastConfirmKind = kind;
    const confirmBtn = $("#confirm-btn");
    if (kind === "approve") {
      $("#confirm-title").textContent = "Approve this exact commit for publication";
      confirmBtn.textContent = "Approve commit";
      confirmBtn.className = "btn btn-success";
      $("#confirm-body").innerHTML = `
        <p>You are approving publication of exactly this commit:</p>
        <p>${shaHtml(sha)}</p>
        ${dl([
          ["Task", `Task ${taskId} · ${escapeHtml(state.task.repository)}`],
          ["Branch", txt(d.candidate.branch)],
          ["Acceptance checks", `${escapeHtml(humanize(d.checks.status))} (run ${txt(d.checks.latest_run?.id)})`],
          ["Restricted review", `${escapeHtml(humanize(d.review.status))} (review ${txt(d.review.latest?.id)})`],
        ])}
        <p class="small mb-0">This is delivery approval only. It is not a GitHub review and grants no merge permission.
        Nothing is published until you publish separately. A new commit needs fresh checks, review and approval.</p>`;
    } else {
      const dest = d.destination;
      $("#confirm-title").textContent = "Publish the approved commit as a draft pull request";
      confirmBtn.textContent = "Publish draft PR";
      confirmBtn.className = "btn btn-primary";
      $("#confirm-body").innerHTML = `
        ${dl([
          ["Repository", `<strong>${escapeHtml(dest.repository)}</strong>`],
          ["Branch", `<code>${escapeHtml(dest.branch)}</code> (pushed without force)`],
          ["Pull request", "draft, created or reused"],
          ["Base", escapeHtml(dest.base_branch)],
          ["Commit", shaHtml(sha)],
          ["Merge", "never; Forge does not merge or enable auto-merge"],
        ])}
        <p class="small mb-0">Forge rechecks the commit, the clean workspace and the approval, then verifies the remote branch head and the
        PR head equal this commit. A partial failure is reported with every known fact and can be retried without a duplicate PR.</p>`;
    }
    modal.show();
  }

  $("#confirm-btn").addEventListener("click", (event) => {
    if (!pending) return;
    const { kind, sha } = pending;
    act(
      event.currentTarget,
      kind === "approve" ? "Approving…" : "Publishing…",
      () => apiJson(`/api/tasks/${taskId}/${kind}`, { method: "POST", json: { candidate_sha: sha } }),
      (r) => {
        if (kind === "approve") {
          say(
            r.data.already_approved
              ? `Commit ${sha} was already approved.`
              : `Approved commit ${sha} for publication. This is not a GitHub review and grants no merge permission.`,
            "success"
          );
          return;
        }
        const pub = r.data.publication || {};
        const pr = pub.pull_request || {};
        const url = safeUrl(pr.url);
        const prLink = url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener" class="alert-link">draft PR #${Number(pr.number)}</a>` : "no PR";
        if (r.data.published) {
          sayHtml(`Published ${escapeHtml(sha)} to ${escapeHtml(pub.branch)}; ${prLink} (${escapeHtml(humanize(pr.action))}). Not merged.`, "success");
        } else {
          sayHtml(`Publication incomplete (${escapeHtml(humanize(pub.status))}). Known facts are shown below; retrying continues without a duplicate PR. ${escapeHtml(pub.error || "")}`, "warning");
        }
      }
    );
    pending = null;
    modal.hide();
  });

  $("#delivery-body").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button || !state) return;
    const action = button.dataset.action;
    if (action === "approve" || action === "publish") {
      openConfirm(action);
      return;
    }
    const routes = {
      checks: [`/api/tasks/${taskId}/checks`, "Starting checks…", (r) => say(`Acceptance checks started (run ${Number(r.data.check_run_id)}) on ${r.data.candidate_sha}.`, "info")],
      review: [`/api/tasks/${taskId}/candidate-review`, "Starting review…", (r) => say(`Restricted review ${Number(r.data.review_id)} started on ${r.data.candidate_sha}. Nothing is posted to GitHub.`, "info")],
      ci: [`/api/tasks/${taskId}/ci/refresh`, "Refreshing…", (r) => say(`Remote CI observed: ${humanize(r.data.remote_ci?.state)}.`, "info")],
    };
    const [url, label, onSuccess] = routes[action] || [];
    if (url) act(button, label, () => apiJson(url, { method: "POST" }), onSuccess);
  });

  load();
})();
