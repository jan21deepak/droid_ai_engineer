// Executive overview, PR review assignment and hash-addressable dashboard sections.
(() => {
  const {
    apiJson,
    badge,
    errorMessage,
    escapeHtml,
    hideAlert,
    humanize,
    reviewReadinessBadge,
    runBusy,
    safeUrl,
    shaHtml,
    showAlert,
  } = window.Forge;
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  // ---- Sections addressable by URL hash (#overview, #repos, ...) ----
  function showSection(name, { focus = false } = {}) {
    const button = $(`#appTabs [data-hash="${CSS.escape(name)}"]`);
    if (!button) return false;
    bootstrap.Tab.getOrCreateInstance(button).show();
    if (focus) button.focus();
    return true;
  }

  $$("#appTabs [data-hash]").forEach((button) => {
    button.addEventListener("shown.bs.tab", () => {
      history.replaceState(null, "", `#${button.dataset.hash}`);
    });
  });

  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-tab-link]");
    if (!link) return;
    event.preventDefault();
    showSection(link.dataset.tabLink, { focus: true });
  });

  window.addEventListener("hashchange", () => showSection(window.location.hash.slice(1)));

  // ---- Overview ----
  const STAGE_TONES = {
    agent_running: "primary",
    agent_stopped: "warning",
    agent_interrupted: "warning",
    agent_failed: "danger",
    agent_blocked: "warning",
    no_candidate: "secondary",
    awaiting_evidence: "warning",
    ready_for_approval: "info",
    approved: "success",
    publication_incomplete: "danger",
    published: "success",
  };

  const CHECK_TONES = { passed: "success", failed: "danger", running: "primary", missing: "secondary" };
  const CI_LABELS = {
    not_published: ["secondary", "Not published"],
    not_observed: ["secondary", "Not observed yet"],
    none_reported: ["secondary", "No CI reported"],
    pending: ["warning", "Pending"],
    failing: ["danger", "Failing"],
    passing: ["success", "Passing"],
    unavailable: ["dark", "Unavailable"],
  };

  function ciBadge(state) {
    const [tone, label] = CI_LABELS[state] || ["secondary", humanize(state)];
    return badge(label, tone);
  }

  function overviewRow(task) {
    const prUrl = safeUrl(task.pull_request_url);
    const pr = prUrl
      ? `<a href="${escapeHtml(prUrl)}" target="_blank" rel="noopener">Draft PR #${Number(task.pull_request_number)}</a>`
      : `<span class="text-muted">Not created</span>`;
    const merged = task.merged_observed ? ` ${badge("Merged (observed)", "info")}` : "";
    const stale = task.approval_stale ? ` ${badge("Earlier approval stale", "warning")}` : "";
    return `
      <tr>
        <td><a href="/tasks/${Number(task.id)}" class="detail-link">Task ${Number(task.id)} · #${Number(task.issue_number)}</a>
            <div class="text-muted small text-truncate overview-title">${escapeHtml(task.issue_title || "")}</div>
            <div class="text-muted small">${escapeHtml(task.repository)}</div></td>
        <td>${badge(task.stage_label, STAGE_TONES[task.stage] || "secondary")}${stale}${merged}</td>
        <td>${badge(humanize(task.checks), CHECK_TONES[task.checks] || "secondary")}</td>
        <td>${reviewReadinessBadge(task.review)}</td>
        <td>${ciBadge(task.remote_ci)}</td>
        <td>${pr}</td>
      </tr>`;
  }

  function renderOverview(data) {
    const counts = data.counts || {};
    $$("[data-ov]").forEach((el) => {
      const value = counts[el.dataset.ov];
      el.textContent = value === undefined || value === null ? "—" : String(value);
    });
    $$("[data-def]").forEach((el) => {
      el.textContent = (data.definitions || {})[el.dataset.def] || "";
    });
    const ci = counts.remote_ci || {};
    const ciItems = Object.keys(ci).map(
      (state) => `<li class="list-inline-item me-3">${ciBadge(state)} <span class="ms-1">${Number(ci[state])}</span></li>`
    );
    $("#overview-ci").innerHTML = counts.published
      ? ciItems.join("")
      : `<li class="list-inline-item text-muted">Nothing is published yet, so there is no remote CI to observe.</li>`;

    const tasks = data.tasks || [];
    const tbody = $("#overview-tbody");
    if (!tasks.length) {
      tbody.innerHTML = `<tr><td colspan="6" class="text-center text-muted py-4">
        No tasks yet. Assign an issue on the <a href="#issues" data-tab-link="issues">Issues</a> tab to start one.</td></tr>`;
    } else {
      tbody.innerHTML = tasks.map(overviewRow).join("");
    }
    $("#overview-shown").textContent = counts.tasks_total
      ? `Showing ${data.tasks_shown} of ${counts.tasks_total} task(s)`
      : "";
    const reviews = data.reviews || {};
    $("#overview-reviews").innerHTML = `PR reviews: ${Number(reviews.pr_reviews_total || 0)} total,
      ${Number(reviews.pr_reviews_active || 0)} active, ${Number(reviews.pr_reviews_completed || 0)} completed.
      <a href="#reviews" data-tab-link="reviews">Assign or inspect PR reviews</a>.`;
    window.ForgeEconomics?.render(data.economics);
    $("#overview-status").textContent = "Overview updated.";
  }

  async function loadOverview() {
    const alert = $("#overview-alert");
    const result = await apiJson("/api/overview");
    if (!result.ok) {
      showAlert(alert, `Could not load the overview. ${errorMessage(result)}`, "danger");
      return;
    }
    hideAlert(alert);
    renderOverview(result.data);
  }

  $("#refresh-overview").addEventListener("click", (event) =>
    runBusy(event.currentTarget, "Refreshing…", loadOverview)
  );
  document.addEventListener("forge:refresh-overview", loadOverview);
  $("#overview-tab").addEventListener("shown.bs.tab", loadOverview);

  // ---- PR reviews ----
  const REVIEW_STATUS_TONES = {
    completed: "success",
    failed: "danger",
    running: "primary",
    queued: "secondary",
    stopped: "warning",
    interrupted: "warning",
    blocked: "warning",
  };

  function verdictBadge(verdict) {
    if (!verdict) return `<span class="text-muted">None yet</span>`;
    const tone = verdict === "APPROVE" ? "success" : verdict === "REQUEST_CHANGES" ? "danger" : "secondary";
    return badge(`AI: ${humanize(verdict)}`, tone);
  }

  function reviewRow(review) {
    const outcome = review.github_outcome || {};
    const prUrl = safeUrl(review.pr_url);
    const pr = prUrl
      ? `<a href="${escapeHtml(prUrl)}" target="_blank" rel="noopener">#${Number(review.pr_number)}</a>`
      : `#${Number(review.pr_number)}`;
    return `
      <tr>
        <td><a href="/reviews/${Number(review.id)}" class="detail-link">Review ${Number(review.id)}</a></td>
        <td>${pr} <span class="text-muted small">${escapeHtml(review.repository)}</span>
            <div class="text-muted small text-truncate overview-title">${escapeHtml(review.pr_title || "")}</div></td>
        <td>${badge(humanize(review.status), REVIEW_STATUS_TONES[review.status] || "secondary")}</td>
        <td>${shaHtml(review.commit_sha, "not bound yet")}</td>
        <td>${verdictBadge(review.ai_verdict)}</td>
        <td class="small text-wrap outcome-cell">${escapeHtml(outcome.label || "Not recorded")}</td>
      </tr>`;
  }

  async function loadReviews() {
    const tbody = $("#reviews-tbody");
    const result = await apiJson("/api/reviews?limit=20");
    if (!result.ok) {
      tbody.innerHTML = `<tr><td colspan="6" class="text-center text-danger py-4">${escapeHtml(
        `Could not load reviews. ${errorMessage(result)}`
      )}</td></tr>`;
      return;
    }
    const reviews = result.data.reviews || [];
    tbody.innerHTML = reviews.length
      ? reviews.map(reviewRow).join("")
      : `<tr><td colspan="6" class="text-center text-muted py-4">No pull request reviews yet. Assign one above.</td></tr>`;
  }

  async function loadRepositoryOptions() {
    const select = $("#assign-pr-repo");
    const result = await apiJson("/api/repositories");
    const repos = result.ok ? result.data.repositories || [] : [];
    if (!repos.length) {
      select.innerHTML = `<option value="">No repositories registered; add one on the Repositories tab</option>`;
      return;
    }
    const current = select.value;
    select.innerHTML = repos
      .map((r) => `<option value="${escapeHtml(r.full_name)}">${escapeHtml(r.full_name)}</option>`)
      .join("");
    if (current && repos.some((r) => r.full_name === current)) select.value = current;
  }

  function pullRow(pull) {
    const url = safeUrl(pull.html_url);
    const title = url
      ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">#${Number(pull.pr_number)}</a>`
      : `#${Number(pull.pr_number)}`;
    const state = pull.draft ? badge("Draft", "secondary") : badge("Ready for review", "info");
    const action = pull.assigned
      ? `<span class="text-muted small">Review in progress</span>`
      : `<button type="button" class="btn btn-sm btn-outline-primary review-pull-btn"
            data-repo="${escapeHtml(pull.repository)}" data-number="${Number(pull.pr_number)}">Review</button>`;
    return `
      <tr>
        <td>${title} <span class="small">${escapeHtml(pull.title || "")}</span></td>
        <td>${escapeHtml(pull.repository)}</td>
        <td>${shaHtml(pull.head_sha, "unknown")}</td>
        <td>${state}</td>
        <td class="text-end">${action}</td>
      </tr>`;
  }

  async function loadPulls() {
    const tbody = $("#pulls-tbody");
    tbody.innerHTML = `<tr><td colspan="5" class="text-center text-muted py-4">Loading…</td></tr>`;
    const result = await apiJson("/api/pulls");
    if (!result.ok) {
      tbody.innerHTML = `<tr><td colspan="5" class="text-center text-danger py-4">${escapeHtml(
        `Could not load pull requests. ${errorMessage(result)}`
      )}</td></tr>`;
      return;
    }
    const pulls = (result.data.repositories || []).flatMap((group) => group.pulls || []);
    tbody.innerHTML = pulls.length
      ? pulls.map(pullRow).join("")
      : `<tr><td colspan="5" class="text-center text-muted py-4">No open pull requests are known. If GitHub could not be read, enter a PR number above.</td></tr>`;
  }

  async function assignReview(repository, prNumber) {
    const alert = $("#reviews-alert");
    hideAlert(alert);
    const result = await apiJson("/api/pulls/assign-number", {
      method: "POST",
      json: { repository, pr_number: prNumber },
    });
    const data = result.data || {};
    const link = data.review_id
      ? ` <a href="/reviews/${Number(data.review_id)}" class="alert-link">Open review ${Number(data.review_id)}</a>.`
      : "";
    if (result.ok) {
      alert.className = "alert alert-success";
      alert.innerHTML = `Review ${Number(data.review_id)} started for ${escapeHtml(repository)} #${Number(
        prNumber
      )}${data.draft ? " (draft PR)" : ""}. It only reads the diff; nothing is published or merged.${link}`;
    } else {
      // An existing review for the same target is duplicate protection, not a failure.
      alert.className = data.detail === "review_already_active" ? "alert alert-warning" : "alert alert-danger";
      alert.innerHTML = `${escapeHtml(errorMessage(result, "The review could not be started."))}${link}`;
    }
    alert.classList.remove("d-none");
    await loadReviews();
    loadOverview();
    return result.ok;
  }

  $("#assign-pr-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const alert = $("#reviews-alert");
    const repository = $("#assign-pr-repo").value;
    const raw = $("#assign-pr-number").value.trim();
    if (!repository) {
      showAlert(alert, "Select a registered repository first.", "danger");
      $("#assign-pr-repo").focus();
      return;
    }
    if (!/^[1-9][0-9]{0,7}$/.test(raw)) {
      showAlert(alert, `“${raw || "(empty)"}” is not a pull request number. Enter a positive whole number such as 12.`, "danger");
      $("#assign-pr-number").setAttribute("aria-invalid", "true");
      $("#assign-pr-number").focus();
      return;
    }
    $("#assign-pr-number").removeAttribute("aria-invalid");
    runBusy($("#assign-pr-btn"), "Assigning…", () => assignReview(repository, Number(raw)));
  });

  $("#pulls-tbody").addEventListener("click", (event) => {
    const button = event.target.closest(".review-pull-btn");
    if (!button) return;
    runBusy(button, "Assigning…", async () => {
      if (await assignReview(button.dataset.repo, Number(button.dataset.number))) await loadPulls();
    });
  });

  $("#refresh-pulls").addEventListener("click", (event) => runBusy(event.currentTarget, "Loading…", loadPulls));
  $("#refresh-reviews").addEventListener("click", (event) => runBusy(event.currentTarget, "Refreshing…", loadReviews));
  $("#reviews-tab").addEventListener("shown.bs.tab", () => {
    loadRepositoryOptions();
    loadReviews();
  });

  // ---- Initial load ----
  loadOverview();
  loadRepositoryOptions();
  loadReviews();
  if (window.location.hash) showSection(window.location.hash.slice(1));
})();
