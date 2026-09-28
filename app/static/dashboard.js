(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  function showAlert(el, message, type = "success") {
    if (!el) return;
    el.className = `alert alert-${type}`;
    el.textContent = message;
    el.classList.remove("d-none");
  }

  function hideAlert(el) {
    if (!el) return;
    el.classList.add("d-none");
  }

  function formatMoney(n) {
    return `$${Number(n || 0).toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;
  }

  const SGT_FORMATTER = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Singapore",
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });

  function formatSgt(value) {
    if (!value) return "—";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return "—";
    return `${SGT_FORMATTER.format(parsed).replace(",", "")} SGT`;
  }

  function statusBadge(status) {
    const map = {
      completed: "success",
      failed: "danger",
      running: "primary",
      queued: "secondary",
    };
    const cls = map[status] || "secondary";
    const label = (status || "unknown").replace(/^./, (c) => c.toUpperCase());
    return `<span class="badge text-bg-${cls}">${label}</span>`;
  }

  function escapeHtml(str) {
    return String(str || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // ---- Repositories ----
  let droidModels = [];

  function modelOptionsHtml(selected) {
    const current = (selected || "").trim();
    const ids = droidModels.map((m) => m.id);
    const options = ['<option value="">Default (auto)</option>'];
    if (current && !ids.includes(current)) {
      options.push(
        `<option value="${escapeHtml(current)}" selected>${escapeHtml(current)} (saved)</option>`
      );
    }
    for (const model of droidModels) {
      const selectedAttr = model.id === current ? " selected" : "";
      const label = model.display_name && model.display_name !== model.id
        ? `${model.display_name} (${model.id})`
        : model.id;
      options.push(
        `<option value="${escapeHtml(model.id)}"${selectedAttr}>${escapeHtml(label)}</option>`
      );
    }
    return options.join("");
  }

  async function loadDroidModels() {
    try {
      const res = await fetch("/api/droid/models");
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to load models");
      droidModels = data.models || [];
    } catch (err) {
      droidModels = [];
    }
  }

  async function loadRepos() {
    const tbody = $("#repos-tbody");
    tbody.innerHTML = `<tr><td colspan="5" class="text-center text-muted py-4">Loading…</td></tr>`;
    try {
      const res = await fetch("/api/repositories");
      const data = await res.json();
      const repos = data.repositories || [];
      if (!repos.length) {
        tbody.innerHTML = `<tr><td colspan="5" class="text-center text-muted py-4">No repositories yet. Add one above.</td></tr>`;
        return;
      }
      tbody.innerHTML = repos
        .map(
          (r) => `
        <tr data-repo-id="${r.id}">
          <td>
            <a href="${r.url}" target="_blank" rel="noopener">${escapeHtml(r.full_name)}</a>
            <div class="text-muted small text-truncate" style="max-width:260px">${escapeHtml(r.description || "")}</div>
          </td>
          <td style="min-width:160px">
            <select class="form-select form-select-sm repo-model-input" data-id="${r.id}"
                    title="Model id for this repository's Droid sessions">
              ${modelOptionsHtml(r.droid_model || "")}
            </select>
          </td>
          <td style="min-width:200px">
            <input type="text" class="form-control form-control-sm repo-setup-input" data-id="${r.id}"
                   value="${escapeHtml(r.setup_command || "")}"
                   placeholder="e.g. npm ci (optional)"
                   title="Shell command run in the workspace clone before the session">
          </td>
          <td class="text-muted small">${r.created_at_display || formatSgt(r.created_at)}</td>
          <td class="text-end text-nowrap">
            <button class="btn btn-sm btn-outline-primary me-1 save-repo-btn"
                    data-id="${r.id}">Save</button>
            <button class="btn btn-sm btn-primary me-1 create-issues-open"
                    data-id="${r.id}" data-repo="${escapeHtml(r.full_name)}">Add Issues</button>
            <button class="btn btn-sm btn-outline-danger remove-repo-btn" data-id="${r.id}">Remove</button>
          </td>
        </tr>`
        )
        .join("");
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="5" class="text-center text-danger py-4">Failed to load repositories</td></tr>`;
    }
  }

  $("#add-repo-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const alert = $("#repo-alert");
    hideAlert(alert);
    const btn = $("#add-repo-btn");
    const url = $("#repo-url").value.trim();
    btn.disabled = true;
    try {
      const res = await fetch("/api/repositories", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to add repository");
      showAlert(alert, data.detail === "already registered" ? "Repository already registered." : "Repository added. Set model / setup command on the row below if needed.", "success");
      $("#repo-url").value = "";
      await loadRepos();
    } catch (err) {
      showAlert(alert, err.message, "danger");
    } finally {
      btn.disabled = false;
    }
  });

  $("#repos-tbody").addEventListener("click", async (e) => {
    const createBtn = e.target.closest(".create-issues-open");
    const removeBtn = e.target.closest(".remove-repo-btn");
    const saveBtn = e.target.closest(".save-repo-btn");
    const alert = $("#repo-alert");
    if (saveBtn) {
      const id = saveBtn.dataset.id;
      const row = saveBtn.closest("tr");
      const droid_model = row.querySelector(".repo-model-input")?.value.trim() || "";
      const setup_command = row.querySelector(".repo-setup-input")?.value.trim() || "";
      hideAlert(alert);
      saveBtn.disabled = true;
      saveBtn.textContent = "Saving…";
      try {
        const res = await fetch(`/api/repositories/${id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ droid_model, setup_command }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed to update repository");
        const model = data.repository?.droid_model;
        showAlert(
          alert,
          model
            ? `Saved model "${model}" for ${data.repository.full_name}.`
            : `Saved config for ${data.repository.full_name}; sessions use the default model.`,
          "success"
        );
      } catch (err) {
        showAlert(alert, err.message, "danger");
      } finally {
        saveBtn.disabled = false;
        saveBtn.textContent = "Save";
      }
      return;
    }
    if (createBtn) {
      hideAlert(alert);
      createBtn.disabled = true;
      createBtn.textContent = "Adding 5…";
      try {
        const response = await fetch(
          `/api/repositories/${createBtn.dataset.id}/issues`,
          { method: "POST" }
        );
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "Failed to add issues");
        const message = data.created.length
          ? `Added ${data.created.length} issue(s) from ${data.parent} to ${data.repository}. They are ready in the Issues tab.`
          : `No new parent issues were available to import from ${data.parent}.`;
        showAlert(alert, message, data.created.length ? "success" : "info");
        await loadIssues();
      } catch (err) {
        showAlert(alert, err.message, "danger");
      } finally {
        createBtn.disabled = false;
        createBtn.textContent = "Add Issues";
      }
      return;
    }
    if (removeBtn) {
      const id = removeBtn.dataset.id;
      if (!confirm("Remove this repository and its synced issues?")) return;
      await fetch(`/api/repositories/${id}`, { method: "DELETE" });
      await loadRepos();
      await loadIssues();
    }
  });

  $("#refresh-repos").addEventListener("click", () => {
    loadDroidModels().then(() => loadRepos());
  });

  // ---- Issues ----
  const ISSUES_PAGE_SIZE = 10;
  /** @type {Record<string, number>} */
  let issuePages = {};
  /** @type {Array<{repository: string, issues: Array}>} */
  let issueGroups = [];

  function updateAssignButton() {
    const selected = $$(".issue-check:checked:not(:disabled)");
    $("#assign-droid-btn").disabled = selected.length === 0;
    $("#assign-droid-btn").textContent =
      selected.length > 0 ? `Assign to Droid (${selected.length})` : "Assign to Droid";
  }

  function renderIssueRow(issue) {
    const disabled = issue.assigned ? "disabled" : "";
    const badge = issue.assigned
      ? `<span class="badge text-bg-secondary badge-assigned">Already assigned</span>`
      : "";
    return `
      <div class="issue-row d-flex align-items-start gap-3 px-3 py-2 border-bottom">
        <input class="form-check-input mt-1 issue-check" type="checkbox"
               value="${issue.id}" ${disabled}
               data-repo="${issue.repository}" data-number="${issue.issue_number}">
        <div class="flex-grow-1">
          <div class="d-flex justify-content-between gap-2 flex-wrap">
            <div>
              <a href="${issue.html_url}" target="_blank" rel="noopener" class="fw-semibold text-decoration-none">
                #${issue.issue_number} ${escapeHtml(issue.title)}
              </a>
              ${badge}
            </div>
          </div>
          <div class="text-muted small text-truncate" style="max-width:720px">${escapeHtml((issue.body || "").slice(0, 140))}</div>
        </div>
      </div>`;
  }

  function renderIssueGroup(group) {
    const issues = group.issues || [];
    const total = issues.length;
    const pages = Math.max(1, Math.ceil(total / ISSUES_PAGE_SIZE));
    let page = issuePages[group.repository] || 1;
    if (page > pages) page = pages;
    issuePages[group.repository] = page;
    const start = (page - 1) * ISSUES_PAGE_SIZE;
    const slice = issues.slice(start, start + ISSUES_PAGE_SIZE);
    const rows = slice.map(renderIssueRow).join("");
    const from = total === 0 ? 0 : start + 1;
    const to = Math.min(start + ISSUES_PAGE_SIZE, total);
    const pager =
      total > ISSUES_PAGE_SIZE
        ? `<div class="d-flex justify-content-between align-items-center gap-2 px-3 py-2 border-top">
            <span class="text-muted small">${from}–${to} of ${total}</span>
            <div class="btn-group btn-group-sm" role="group" aria-label="Issue pages">
              <button type="button" class="btn btn-outline-secondary issues-page-btn"
                      data-repo="${escapeHtml(group.repository)}" data-page="${page - 1}"
                      ${page <= 1 ? "disabled" : ""}>Prev</button>
              <button type="button" class="btn btn-outline-secondary" disabled>${page} / ${pages}</button>
              <button type="button" class="btn btn-outline-secondary issues-page-btn"
                      data-repo="${escapeHtml(group.repository)}" data-page="${page + 1}"
                      ${page >= pages ? "disabled" : ""}>Next</button>
            </div>
          </div>`
        : "";
    return `
      <div class="issue-group" data-repo="${escapeHtml(group.repository)}">
        <div class="issue-group-header d-flex justify-content-between align-items-center">
          <span>${escapeHtml(group.repository)}</span>
          <button type="button" class="btn btn-sm btn-outline-secondary select-repo-btn"
                  data-repo="${escapeHtml(group.repository)}">Select all open</button>
        </div>
        ${rows || `<div class="text-muted small p-3">No open issues</div>`}
        ${pager}
      </div>`;
  }

  function renderIssues() {
    const container = $("#issues-container");
    if (!issueGroups.length) {
      container.innerHTML = `<div class="text-center text-muted py-5">No issues yet. Add a repository and click <strong>Add Issues</strong>.</div>`;
      updateAssignButton();
      return;
    }
    container.innerHTML = issueGroups.map(renderIssueGroup).join("");
    updateAssignButton();
  }

  async function loadIssues() {
    const container = $("#issues-container");
    container.innerHTML = `<div class="text-center text-muted py-5">Loading…</div>`;
    try {
      const res = await fetch("/api/issues");
      const data = await res.json();
      issueGroups = data.repositories || [];
      renderIssues();
    } catch (err) {
      container.innerHTML = `<div class="text-center text-danger py-5">Failed to load issues</div>`;
    }
  }

  $("#issues-container").addEventListener("change", (e) => {
    if (e.target.classList.contains("issue-check")) updateAssignButton();
  });

  $("#issues-container").addEventListener("click", (e) => {
    const pageBtn = e.target.closest(".issues-page-btn");
    if (pageBtn) {
      const repo = pageBtn.dataset.repo;
      const page = Number(pageBtn.dataset.page);
      if (!repo || !page || page < 1) return;
      issuePages[repo] = page;
      renderIssues();
      return;
    }
    const btn = e.target.closest(".select-repo-btn");
    if (!btn) return;
    const repo = btn.dataset.repo;
    $$(`.issue-check[data-repo="${CSS.escape(repo)}"]:not(:disabled)`).forEach((cb) => {
      cb.checked = true;
    });
    updateAssignButton();
  });

  $("#refresh-issues").addEventListener("click", loadIssues);

  $("#assign-droid-btn").addEventListener("click", async () => {
    const alert = $("#issues-alert");
    hideAlert(alert);
    const ids = $$(".issue-check:checked:not(:disabled)").map((cb) => Number(cb.value));
    if (!ids.length) return;
    const btn = $("#assign-droid-btn");
    btn.disabled = true;
    btn.textContent = "Assigning…";
    try {
      const res = await fetch("/api/issues/assign", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ issue_ids: ids }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Assignment failed");
      showAlert(alert, `Assigned ${data.accepted} issue(s) to Droid.`, "success");
      await loadIssues();
      await refreshMetrics();
      const tab = new bootstrap.Tab($("#metrics-tab"));
      tab.show();
    } catch (err) {
      showAlert(alert, err.message, "danger");
    } finally {
      updateAssignButton();
    }
  });

  // ---- Daily activity chart ----
  let dailyChart = null;

  const NEON = {
    cyan: "#00e5ff",
    pink: "#ff2fd0",
    purple: "#a855f7",
    lime: "#39ff9e",
    red: "#ff4d6d",
    amber: "#ffc23d",
    text: "#e9e9fb",
    muted: "#8d8db5",
    grid: "rgba(124, 92, 255, 0.16)",
  };

  function neonFill(ctx, hex) {
    const { chartArea, ctx: canvasCtx } = ctx.chart;
    if (!chartArea) return hex;
    const gradient = canvasCtx.createLinearGradient(0, chartArea.bottom, 0, chartArea.top);
    gradient.addColorStop(0, `${hex}33`);
    gradient.addColorStop(1, hex);
    return gradient;
  }

  function renderDailyChart(daily) {
    const canvas = $("#daily-chart");
    if (!canvas || typeof Chart === "undefined") return;
    const rows = daily || [];
    const labels = rows.map((d) => d.label);
    const bars = [
      { key: "fixes_completed", label: "Fixes completed", color: NEON.lime },
      { key: "fixes_failed", label: "Fixes failed", color: NEON.red },
      { key: "reviews_completed", label: "Reviews completed", color: NEON.purple },
      { key: "prs_opened", label: "PRs opened", color: NEON.cyan },
    ];
    const datasets = bars.map((bar) => ({
      type: "bar",
      label: bar.label,
      data: rows.map((d) => d[bar.key] || 0),
      backgroundColor: (ctx) => neonFill(ctx, bar.color),
      hoverBackgroundColor: bar.color,
      borderColor: bar.color,
      borderWidth: 1,
      borderRadius: 4,
      yAxisID: "y",
    }));
    datasets.push({
      type: "line",
      label: "Droid runtime (min)",
      data: rows.map((d) => d.runtime_minutes || 0),
      borderColor: NEON.amber,
      backgroundColor: NEON.amber,
      pointBackgroundColor: NEON.amber,
      pointBorderColor: "#06060f",
      borderWidth: 2,
      tension: 0.35,
      pointRadius: 3,
      yAxisID: "yRuntime",
    });

    if (dailyChart) {
      dailyChart.data.labels = labels;
      dailyChart.data.datasets.forEach((ds, i) => {
        ds.data = datasets[i].data;
      });
      dailyChart.update();
      return;
    }

    Chart.defaults.color = NEON.muted;
    Chart.defaults.font.family =
      '"Space Grotesk", ui-sans-serif, system-ui, -apple-system, sans-serif';

    dailyChart = new Chart(canvas, {
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: {
            position: "bottom",
            labels: { boxWidth: 12, usePointStyle: true, color: NEON.text },
          },
          tooltip: {
            backgroundColor: "#0b0b1a",
            borderColor: "rgba(0, 229, 255, 0.55)",
            borderWidth: 1,
            titleColor: NEON.cyan,
            bodyColor: NEON.text,
            padding: 10,
            callbacks: {
              title: (items) => `${items[0].label} (SGT)`,
            },
          },
        },
        scales: {
          x: {
            grid: { display: false },
            ticks: { color: NEON.muted },
            border: { color: NEON.grid },
          },
          y: {
            beginAtZero: true,
            title: { display: true, text: "Count", color: NEON.muted },
            ticks: { precision: 0, color: NEON.muted },
            grid: { color: NEON.grid },
            border: { color: NEON.grid },
          },
          yRuntime: {
            beginAtZero: true,
            position: "right",
            title: { display: true, text: "Runtime (min)", color: NEON.amber },
            ticks: { color: NEON.amber },
            grid: { drawOnChartArea: false },
            border: { color: NEON.grid },
          },
        },
      },
    });
  }

  // ---- Metrics / Dashboard ----
  let activityPage = Number($("#activity-pager")?.dataset.page || 1) || 1;
  const ACTIVITY_PAGE_SIZE = Number($("#activity-pager")?.dataset.pageSize || 10) || 10;

  function renderActivityPager({ page, page_size, total, total_pages }) {
    const summary = $("#activity-pager-summary");
    const list = $("#activity-pagination");
    const pager = $("#activity-pager");
    if (!summary || !list || !pager) return;

    activityPage = page;
    pager.dataset.page = String(page);
    pager.dataset.pageSize = String(page_size);
    pager.dataset.total = String(total);
    pager.dataset.totalPages = String(total_pages);

    if (!total) {
      summary.textContent = "No activity yet";
      list.innerHTML = "";
      return;
    }

    const from = (page - 1) * page_size + 1;
    const to = Math.min(page * page_size, total);
    summary.textContent = `Showing ${from}–${to} of ${total}`;

    const pages = [];
    const windowSize = 5;
    let start = Math.max(1, page - Math.floor(windowSize / 2));
    let end = Math.min(total_pages, start + windowSize - 1);
    start = Math.max(1, end - windowSize + 1);

    const item = (label, target, { disabled = false, active = false } = {}) => {
      const cls = ["page-item", disabled ? "disabled" : "", active ? "active" : ""]
        .filter(Boolean)
        .join(" ");
      const attrs = disabled
        ? `class="${cls}" aria-disabled="true"`
        : `class="${cls}"`;
      const linkAttrs = disabled
        ? `class="page-link" tabindex="-1" aria-disabled="true"`
        : `class="page-link" href="#" data-page="${target}"`;
      return `<li ${attrs}><a ${linkAttrs}>${label}</a></li>`;
    };

    pages.push(item("‹", page - 1, { disabled: page <= 1 }));
    if (start > 1) {
      pages.push(item("1", 1));
      if (start > 2) pages.push(item("…", page, { disabled: true }));
    }
    for (let p = start; p <= end; p += 1) {
      pages.push(item(String(p), p, { active: p === page }));
    }
    if (end < total_pages) {
      if (end < total_pages - 1) pages.push(item("…", page, { disabled: true }));
      pages.push(item(String(total_pages), total_pages));
    }
    pages.push(item("›", page + 1, { disabled: page >= total_pages }));
    list.innerHTML = pages.join("");
  }

  function renderActivityRows(tasks) {
    const tbody = $("#tasks-tbody");
    if (!tasks.length) {
      tbody.innerHTML = `<tr><td colspan="8" class="text-center text-muted py-4">No tasks yet.</td></tr>`;
      return;
    }
    tbody.innerHTML = tasks
      .map((t) => {
        const runtime =
          t.duration_seconds != null ? `${(t.duration_seconds / 60).toFixed(1)}m` : "—";
        const completed = t.completed_at_display || formatSgt(t.completed_at);
        const isReview = t.kind === "review";
        const typeBadge = isReview
          ? `<span class="badge text-bg-dark">Review</span>`
          : `<span class="badge text-bg-primary">Fix</span>`;
        const target = isReview
          ? `PR #${t.pr_number} <span class="text-muted small">${escapeHtml((t.pr_title || "").slice(0, 60))}</span>`
          : `#${t.issue_number} <span class="text-muted small">${escapeHtml((t.issue_title || "").slice(0, 60))}</span>`;
        const linkUrl = isReview ? t.pr_url : t.pull_request_url;
        const link = linkUrl
          ? `<a href="${linkUrl}" target="_blank" rel="noopener">View PR</a>`
          : `<span class="text-muted">—</span>`;
        let statusLabel = statusBadge(t.status);
        if (t.status === "completed" && t.merged) {
          statusLabel = `<span class="badge text-bg-success">Completed · Merged</span>`;
        }
        const session = t.droid_session_url
          ? `<a class="btn btn-sm btn-outline-dark" href="${t.droid_session_url}" target="_blank" rel="noopener">${isReview ? "Droid Review" : "Droid Fix"}</a>`
          : t.droid_session_id
            ? `<span class="badge text-bg-dark" title="Droid session ${escapeHtml(t.droid_session_id)}">${escapeHtml(String(t.droid_session_id).slice(0, 8))}</span>`
            : `<span class="text-muted">—</span>`;
        return `
          <tr>
            <td>${typeBadge}</td>
            <td>${escapeHtml(t.repository)}</td>
            <td>${target}</td>
            <td>${statusLabel}</td>
            <td>${session}</td>
            <td>${link}</td>
            <td class="text-muted small">${runtime}</td>
            <td class="text-muted small">${completed}</td>
          </tr>`;
      })
      .join("");
  }

  async function loadActivity(page = activityPage) {
    const res = await fetch(
      `/api/tasks?page=${encodeURIComponent(page)}&page_size=${encodeURIComponent(ACTIVITY_PAGE_SIZE)}`
    );
    const payload = await res.json();
    renderActivityRows(payload.tasks || []);
    renderActivityPager(payload);
  }

  async function refreshMetrics() {
    try {
      const metricsRes = await fetch("/metrics");
      const stats = await metricsRes.json();

      const set = (key, value) => {
        $$(`[data-metric="${key}"]`).forEach((el) => {
          el.textContent = value;
        });
      };

      const fix = stats.fix || {};
      const review = stats.review || {};
      set("running", stats.running);
      set("completed", stats.completed);
      set("failed", stats.failed);
      set("success_rate", `${stats.success_rate}%`);
      set("split.running", `${fix.running ?? 0} fixes · ${review.running ?? 0} reviews`);
      set("split.completed", `${fix.completed ?? 0} fixes · ${review.completed ?? 0} reviews`);
      set("split.failed", `${fix.failed ?? 0} fixes · ${review.failed ?? 0} reviews`);
      set(
        "split.success",
        `${stats.completed} of ${stats.completed + stats.failed} finished`
      );
      set("fix.average_runtime_minutes", stats.fix?.average_runtime_minutes ?? 0);
      set("fix.total_runtime_minutes", stats.fix?.total_runtime_minutes ?? 0);
      set("review.average_runtime_minutes", stats.review?.average_runtime_minutes ?? 0);
      set("review.total_runtime_minutes", stats.review?.total_runtime_minutes ?? 0);
      set("productivity_gained_usd", formatMoney(stats.productivity_gained_usd));
      set("cost_avoidance_percent", `(${stats.cost_avoidance_percent}%)`);

      const eng = stats.engineering || {};
      set("eng.pr_cycle_time_hours", eng.pr_cycle_time_hours ?? 0);
      set("eng.prs_last_7_days", eng.prs_last_7_days ?? 0);
      set("eng.merge_rate_percent", eng.merge_rate_percent ?? 0);
      set(
        "eng.merge_detail",
        `${eng.prs_merged ?? 0} of ${eng.prs_total ?? 0} PRs merged`
      );
      set("eng.change_failure_rate_percent", eng.change_failure_rate_percent ?? 0);
      set(
        "eng.failure_detail",
        `${eng.runs_failed ?? 0} of ${eng.runs_finished ?? 0} runs failed`
      );

      renderDailyChart(stats.daily);
      await loadActivity(activityPage);
    } catch (err) {
      // keep existing UI on transient failures
    }
  }

  function initTooltips() {
    $$('[data-bs-toggle="tooltip"]').forEach((el) => {
      if (!bootstrap.Tooltip.getInstance(el)) {
        new bootstrap.Tooltip(el);
      }
    });
  }

  // Auto-refresh metrics tab every 15s when visible
  setInterval(() => {
    const metricsPane = $("#metrics");
    if (metricsPane && metricsPane.classList.contains("active")) {
      refreshMetrics();
    }
  }, 15000);

  // Chart.js sizes to its container, so (re)draw once the pane is actually visible.
  $("#metrics-tab").addEventListener("shown.bs.tab", () => {
    refreshMetrics().then(() => dailyChart && dailyChart.resize());
  });

  $("#activity-pagination")?.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-page]");
    if (!link) return;
    event.preventDefault();
    const next = Number(link.dataset.page);
    if (!Number.isFinite(next) || next < 1 || next === activityPage) return;
    loadActivity(next).catch(() => {});
  });

  // Seed pager controls from the server-rendered page before the first refresh.
  const pager = $("#activity-pager");
  if (pager) {
    renderActivityPager({
      page: Number(pager.dataset.page) || 1,
      page_size: Number(pager.dataset.pageSize) || ACTIVITY_PAGE_SIZE,
      total: Number(pager.dataset.total) || 0,
      total_pages: Number(pager.dataset.totalPages) || 1,
    });
  }

  // Initial load
  loadDroidModels().then(() => loadRepos());
  loadIssues();
  initTooltips();
  refreshMetrics();
})();
