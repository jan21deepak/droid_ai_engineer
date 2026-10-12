// Shared helpers for the operator pages (dashboard, task detail, review detail).
(() => {
  const CSRF_TOKEN = document.querySelector('meta[name="csrf-token"]')?.getAttribute("content") || "";

  // Same-origin API calls: send the session CSRF token on writes and return
  // to the sign-in page when the operator session has ended.
  async function apiFetch(url, options = {}) {
    const opts = { credentials: "same-origin", ...options };
    const method = (opts.method || "GET").toUpperCase();
    if (method !== "GET" && method !== "HEAD" && CSRF_TOKEN) {
      opts.headers = { ...(opts.headers || {}), "X-CSRF-Token": CSRF_TOKEN };
    }
    const res = await fetch(url, opts);
    if (res.status === 401) {
      window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
    }
    return res;
  }

  // JSON request that always resolves to {ok, status, data}; network failures become status 0.
  async function apiJson(url, options = {}) {
    const opts = { ...options };
    if (opts.json !== undefined) {
      opts.body = JSON.stringify(opts.json);
      opts.headers = { ...(opts.headers || {}), "Content-Type": "application/json" };
      delete opts.json;
    }
    try {
      const res = await apiFetch(url, opts);
      let data = {};
      try {
        data = await res.json();
      } catch (err) {
        data = {};
      }
      return { ok: res.ok, status: res.status, data };
    } catch (err) {
      return { ok: false, status: 0, data: { detail: "network_error", reason: "The service could not be reached." } };
    }
  }

  const ERROR_HINTS = {
    network_error: "The service could not be reached. Check that Forge is running, then retry.",
    stale_candidate: "The candidate commit changed since this page loaded. Review the new commit; it needs its own checks, review and approval.",
    dirty_workspace: "The task workspace has uncommitted changes, so the candidate cannot be trusted. Resolve the workspace first.",
    workspace_unavailable: "The task workspace could not be read. The candidate cannot be verified.",
    not_ready_for_approval: "The candidate is not ready for approval.",
    not_ready_to_publish: "The candidate is not ready to publish.",
    policy_denied: "Publication is not permitted by the outward-action policy.",
    publication_in_progress: "A publication of this candidate is already in progress.",
    checks_already_running: "Acceptance checks are already running on this candidate.",
    acceptance_checks_not_configured: "No acceptance checks are configured (FORGE_ACCEPTANCE_CHECKS).",
    acceptance_checks_invalid: "The acceptance check configuration is invalid.",
    candidate_not_ready: "There is no clean, committed candidate to act on.",
    task_active: "An agent turn is still active on this task. Wait for it to finish or stop it.",
    review_already_active: "A review is already running for this target.",
    droid_not_configured: "Droid is not configured on this server, so no review or turn can start.",
    base_unknown: "The candidate's base commit is unknown, so its exact diff cannot be reviewed.",
    not_published: "Remote CI exists only after the candidate is published.",
    repository_not_allowed: "This repository is not approved by policy.",
    repository_not_registered: "Add this repository on the Repositories tab first.",
    pull_request_not_found: "No such pull request exists in this repository. Check the number and retry.",
    pull_request_not_open: "This pull request is not open, so it cannot be reviewed.",
    pull_request_lookup_failed: "GitHub could not be read to find this pull request. Retry later.",
  };

  // A readable, actionable message from an API error body.
  function errorMessage(result, fallback = "The request failed.") {
    const data = result?.data || {};
    const detail = data.detail;
    if (Array.isArray(detail)) {
      const fields = detail.map((d) => (d.loc || []).slice(-1)[0]).filter(Boolean);
      return `Invalid input${fields.length ? ` (${fields.join(", ")})` : ""}. Correct it and retry.`;
    }
    const parts = [];
    const hint = ERROR_HINTS[detail];
    parts.push(hint || (typeof detail === "string" && detail ? detail : fallback));
    if (data.reason && data.reason !== hint) parts.push(data.reason);
    if (Array.isArray(data.blockers) && data.blockers.length) {
      parts.push(`Blocked by: ${data.blockers.map((b) => b.message || b.code).join("; ")}.`);
    }
    if (data.current_sha) parts.push(`Current candidate: ${data.current_sha}.`);
    if (result?.status) parts.push(`(HTTP ${result.status})`);
    return parts.join(" ");
  }

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

  // Runs ``fn`` once per click: the button is disabled and labeled busy until it settles.
  async function runBusy(button, busyLabel, fn) {
    if (!button || button.dataset.busy === "1") return undefined;
    const original = button.textContent;
    button.dataset.busy = "1";
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = busyLabel;
    try {
      return await fn();
    } finally {
      button.dataset.busy = "";
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.textContent = original;
    }
  }

  function escapeHtml(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // Only http(s) links are rendered as links; anything else is shown as text.
  function safeUrl(url) {
    if (typeof url !== "string" || !url.trim()) return "";
    try {
      // External links only: relative or empty values must not resolve to this origin.
      const parsed = new URL(url.trim());
      return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : "";
    } catch (err) {
      return "";
    }
  }

  const SGT_FORMATTER = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Singapore",
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });

  function formatSgt(value) {
    if (!value) return "—";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return "—";
    return `${SGT_FORMATTER.format(parsed).replace(",", "")} SGT`;
  }

  function shortSha(sha) {
    return sha ? String(sha).slice(0, 12) : "";
  }

  // Full SHA, abbreviated visually but kept whole for screen readers and copying.
  function shaHtml(sha, emptyText = "none") {
    if (!sha) return `<span class="text-muted">${escapeHtml(emptyText)}</span>`;
    return `<code class="sha" title="${escapeHtml(sha)}">${escapeHtml(sha)}</code>`;
  }

  function badge(text, tone = "secondary") {
    return `<span class="badge text-bg-${tone}">${escapeHtml(text)}</span>`;
  }

  function humanize(value) {
    if (value === null || value === undefined || value === "") return "unknown";
    return String(value).replace(/_/g, " ");
  }

  const REVIEW_READINESS = {
    missing: ["secondary", "not run"],
    incomplete: ["primary", "not complete"],
    approving: ["success", "approved"],
    changes_requested: ["danger", "changes requested"],
    inconclusive: ["warning", "inconclusive"],
  };

  // Readiness state of the restricted candidate review (delivery `review.status`).
  function reviewReadinessBadge(status, prefix = "") {
    const [tone, label] = REVIEW_READINESS[status] || ["secondary", humanize(status)];
    return badge(`${prefix}${label}`, tone);
  }

  window.Forge = {
    apiFetch,
    apiJson,
    badge,
    errorMessage,
    escapeHtml,
    formatSgt,
    hideAlert,
    humanize,
    reviewReadinessBadge,
    runBusy,
    safeUrl,
    shaHtml,
    shortSha,
    showAlert,
  };
})();
