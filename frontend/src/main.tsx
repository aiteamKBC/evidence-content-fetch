import "./styles.css";

type Processing = {
  id: number | null;
  status: string;
  downloaded_file_name: string;
  file_size_bytes: number | null;
  word_count: number | null;
  word_count_threshold: number | null;
  is_under_word_count_threshold: boolean;
  quality_check_status: string;
  quality_check_summary: string;
  has_reflection: boolean;
  reflection_strength: string;
  has_ksbs: boolean;
  ksb_codes: string[];
  failure_reason: string;
};

type SourceItem = {
  evidence_id: number;
  learner_id: number;
  full_name: string;
  program_name: string;
  evidence_name: string;
  evidence_status: string;
  submission_date: string | null;
  processing: Processing;
};

type SourceItemsResponse = {
  total: number;
  results: SourceItem[];
};

type AuthStatus = {
  authenticated: boolean;
  is_staff: boolean;
  username: string;
};

type BatchResponse = {
  total: number;
  successful: number;
  failed: number;
};

type EvidenceDetail = Processing & {
  evidence_id: number;
  evidence_name: string;
  analysis_status: string;
  reflection_evidence: string[];
  ksb_evidence: string[];
  analysis_summary: string;
  text_extracted: boolean;
  extraction_note: string;
  character_count: number | null;
};

let auth: AuthStatus = { authenticated: false, is_staff: false, username: "" };
let rows: SourceItem[] = [];
let total = 0;
let loading = "";
let message = "";
let messageKind: "error" | "success" | "" = "";
let searchTerm = "";
let detail: EvidenceDetail | null = null;
let filters = {
  processingStatus: "",
  qualityStatus: "",
  under500: "",
  hasReflection: "",
  hasKsbs: "",
};
const API_BASE = "/api/evidence-tool";

const root = document.getElementById("root");
if (!root) {
  throw new Error("Root element was not found.");
}
const rootEl = root;

function csrfToken() {
  return document.cookie
    .split(";")
    .map((item) => item.trim())
    .find((item) => item.startsWith("csrftoken="))
    ?.split("=")[1];
}

async function apiRequest<T>(url: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(url, {
    credentials: "same-origin",
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(csrfToken() ? { "X-CSRFToken": csrfToken() as string } : {}),
      ...options.headers,
    },
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(readableError(text, response.status));
  }
  return response.json() as Promise<T>;
}

function readableError(text: string, statusCode: number) {
  if (!text) return `Request failed with ${statusCode}`;
  try {
    const parsed = JSON.parse(text);
    if (parsed.detail && parsed.error) return scrubVendorText(`${parsed.detail} ${parsed.error}`);
    return scrubVendorText(parsed.detail || parsed.error || text);
  } catch {
    return scrubVendorText(text.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim());
  }
}

function scrubVendorText(value: unknown) {
  return String(value ?? "").replace(/aptem/gi, "Source");
}

function escapeHtml(value: unknown) {
  return scrubVendorText(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function label(value: string) {
  return escapeHtml(value.replace(/_/g, " "));
}

function formatDate(value: string | null) {
  return value ? new Date(value).toLocaleString() : "-";
}

function formatBytes(value: number | null) {
  if (!value) return "-";
  const units = ["B", "KB", "MB", "GB"];
  let size = value;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  return `${size.toFixed(unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
}

function formatKsbCodes(value: unknown) {
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value);
      return Array.isArray(parsed) ? parsed.join(", ") : value;
    } catch {
      return value;
    }
  }
  return "";
}

function activeFilterCount() {
  return Object.values(filters).filter(Boolean).length + (searchTerm ? 1 : 0);
}

function setLoading(nextLoading: string) {
  loading = nextLoading;
  render();
}

function setMessage(kind: "error" | "success", nextMessage: string) {
  messageKind = kind;
  message = nextMessage;
  render();
}

async function loadAuth() {
  try {
    auth = await apiRequest<AuthStatus>(`${API_BASE}/auth/status/`);
    if (auth.is_staff) {
      await loadRows(false);
    }
  } catch (error) {
    setMessage("error", error instanceof Error ? error.message : "Could not load auth status.");
  }
  render();
}

async function loadRows(shouldRender = true) {
  const params = new URLSearchParams({ limit: "50", offset: "0" });
  if (searchTerm) params.set("search", searchTerm);
  if (filters.processingStatus) params.set("processing_status", filters.processingStatus);
  if (filters.qualityStatus) params.set("quality_status", filters.qualityStatus);
  if (filters.under500) params.set("under_500", filters.under500);
  if (filters.hasReflection) params.set("has_reflection", filters.hasReflection);
  if (filters.hasKsbs) params.set("has_ksbs", filters.hasKsbs);
  const data = await apiRequest<SourceItemsResponse>(`${API_BASE}/evidence/source-items/?${params}`);
  rows = data.results;
  total = data.total;
  if (shouldRender) render();
}

async function login(event: Event) {
  event.preventDefault();
  const username = (document.getElementById("username") as HTMLInputElement).value;
  const password = (document.getElementById("password") as HTMLInputElement).value;
  if (!username.trim() || !password.trim()) {
    setMessage("error", "Enter the admin username and password first.");
    return;
  }
  setLoading("login");
  try {
    auth = await apiRequest<AuthStatus>(`${API_BASE}/auth/login/`, {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    await loadRows(false);
    setMessage("success", `Signed in as ${auth.username}.`);
  } catch (error) {
    setMessage("error", error instanceof Error ? error.message : "Login failed.");
  } finally {
    setLoading("");
  }
}

async function refreshRows() {
  setLoading("refresh");
  try {
    await loadRows(false);
    setMessage("success", "Table refreshed.");
  } catch (error) {
    setMessage("error", error instanceof Error ? error.message : "Could not refresh table.");
  } finally {
    setLoading("");
  }
}

async function processNextBatch() {
  setLoading("next-five");
  try {
    const response = await apiRequest<BatchResponse>(`${API_BASE}/evidence/process-source-next/`, {
      method: "POST",
      body: JSON.stringify({ limit: 50 }),
    });
    await loadRows(false);
    setMessage(
      "success",
      `Processed ${response.total} PDFs: ${response.successful} successful, ${response.failed} failed.`,
    );
  } catch (error) {
    setMessage("error", error instanceof Error ? error.message : "Processing failed.");
  } finally {
    setLoading("");
  }
}

async function loadDetail(processingId: number) {
  setLoading(`detail-${processingId}`);
  try {
    detail = await apiRequest<EvidenceDetail>(`${API_BASE}/evidence/${processingId}/`);
    render();
  } catch (error) {
    setMessage("error", error instanceof Error ? error.message : "Could not load result details.");
  } finally {
    setLoading("");
  }
}

function render() {
  const processed = rows.filter((row) => row.processing.id).length;
  const analysed = rows.filter((row) => row.processing.status === "analysed").length;
  const underLimit = rows.filter((row) => row.processing.is_under_word_count_threshold).length;
  const needsReview = rows.filter((row) => row.processing.quality_check_status === "needs_review").length;
  const failed = rows.filter((row) =>
    ["failed", "authentication_failed"].includes(row.processing.status),
  ).length;

  rootEl.innerHTML = `
    <main class="app-shell">
      <aside class="rail">
        <div class="brand-row">
          <div class="brand-mark">EQ</div>
          <div>
            <strong>Evidence</strong>
            <span>Queue</span>
          </div>
        </div>
        <nav>
          <span class="nav-item active">Queue</span>
          <span class="nav-item">Quality rules</span>
          <span class="nav-item">Exports</span>
        </nav>
        <div class="rail-note">
          <span>PDF only</span>
          <strong>Batch size 50</strong>
        </div>
      </aside>

      <section class="workspace">
        <header class="app-header">
          <div class="title-block">
            <div class="eyebrow">Evidence automation</div>
            <h1>Evidence Review Queue</h1>
            <p>Automated PDF checks for short evidence, reflection, and explicit KSB coverage.</p>
            <div class="title-chips">
              <span>PDF only</span>
              <span>Under 1000 quality check</span>
              <span>Joined by evidence_id</span>
            </div>
          </div>
          <div class="header-actions">
            <div class="session-chip ${auth.is_staff ? "ok" : ""}">${auth.is_staff ? `Session: ${escapeHtml(auth.username)}` : "Staff login required"}</div>
            <button class="ghost-button icon-button" id="refresh" title="Refresh table">${loading === "refresh" ? "..." : "Refresh"}</button>
            <button class="primary-button" id="process-next" ${loading === "next-five" || !auth.is_staff ? "disabled" : ""}>
              ${loading === "next-five" ? "Processing 50 PDFs" : "Process Next 50"}
            </button>
          </div>
        </header>

        ${message ? `<div class="banner ${messageKind}">${escapeHtml(message)}</div>` : ""}

        ${
          auth.is_staff
            ? ""
            : `<section class="login-band">
                <form class="login-panel" id="login-form">
                  <div class="login-copy">
                    <span class="panel-kicker">Admin access</span>
                    <h2>Sign in to load the Neon queue</h2>
                    <p>The processing buttons stay locked until the staff session is active.</p>
                  </div>
                  <label>
                    Username
                    <input id="username" value="admin" autocomplete="username" required />
                  </label>
                  <label>
                    Password
                    <input id="password" type="password" autocomplete="current-password" required />
                  </label>
                  <button class="primary-button" id="sign-in-button" type="submit" ${loading === "login" ? "disabled" : ""}>
                    ${loading === "login" ? "Signing in" : "Sign In"}
                  </button>
                </form>
              </section>`
        }

        <section class="metrics">
          ${metric("Eligible PDFs", total, "Visible queue after exclusions")}
          ${metric("Processed", processed, "Rows with saved processing results")}
          ${metric("Analysed", analysed, "Completed AI/text checks")}
          ${metric("Under 1000", underLimit, "Short evidence in current view", "warn")}
          ${metric("Needs Review", needsReview, "Failed quality rule", "danger")}
          ${metric("Failed", failed, "Download or analysis failures", "danger")}
        </section>

        <section class="source-panel">
          <div class="results-toolbar">
            <div>
              <h2>Evidence items</h2>
              <span>${rows.length} visible of ${total} eligible PDFs</span>
            </div>
            <form class="search-box" id="search-form">
              <input id="search" value="${escapeHtml(searchTerm)}" placeholder="Search learner, evidence, ID..." />
              <button class="secondary-button" ${loading === "search" ? "disabled" : ""}>
                ${loading === "search" ? "Searching" : "Search"}
              </button>
            </form>
          </div>
          <div class="filter-head">
            <div>
              <strong>Filters</strong>
              <span>${activeFilterCount() ? `${activeFilterCount()} active` : "No active filters"}</span>
            </div>
            <span class="criteria-chip">Criteria: short evidence must include learning reflection and explicit KSBs</span>
          </div>
          <form class="filters-bar" id="filters-form">
            ${selectFilter("processing-status", "Processing", filters.processingStatus, [
              ["", "All"],
              ["not_processed", "Not processed"],
              ["pending", "Pending"],
              ["processing", "Processing"],
              ["downloaded", "Downloaded"],
              ["analysed", "Analysed"],
              ["failed", "Failed"],
              ["authentication_failed", "Auth failed"],
            ])}
            ${selectFilter("quality-status", "Quality", filters.qualityStatus, [
              ["", "All"],
              ["not_checked", "Not checked"],
              ["acceptable", "Acceptable"],
              ["needs_review", "Needs review"],
            ])}
            ${selectFilter("under-500", "Under 1000", filters.under500, [
              ["", "All"],
              ["true", "Yes"],
              ["false", "No"],
            ])}
            ${selectFilter("has-reflection", "Reflection", filters.hasReflection, [
              ["", "All"],
              ["true", "Yes"],
              ["false", "No"],
            ])}
            ${selectFilter("has-ksbs", "KSBs", filters.hasKsbs, [
              ["", "All"],
              ["true", "Yes"],
              ["false", "No"],
            ])}
            <div class="filter-actions">
              <button class="secondary-button" ${loading === "filters" ? "disabled" : ""}>
                ${loading === "filters" ? "Applying" : "Apply"}
              </button>
              <button class="ghost-button" id="clear-filters" type="button">Clear</button>
            </div>
          </form>
          <div class="rule-strip">
            <span>Excluding: certificates, BIL, break in learning, reviews, hours, welcome files, progress reports, learner handbooks.</span>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Learner</th>
                  <th>Programme</th>
                  <th>Evidence</th>
                  <th>Processing</th>
                  <th>Words</th>
                  <th>Quality</th>
                  <th>Reflection</th>
                  <th>KSBs</th>
                  <th>File</th>
                  <th>Result</th>
                </tr>
              </thead>
              <tbody>
                ${rows.length ? rows.map(rowHtml).join("") : emptyRow()}
              </tbody>
            </table>
          </div>
        </section>
      </section>
      ${detail ? detailPanel(detail) : ""}
    </main>
  `;

  document.getElementById("login-form")?.addEventListener("submit", login);
  document.getElementById("sign-in-button")?.addEventListener("click", () => {
    const form = document.getElementById("login-form") as HTMLFormElement | null;
    form?.requestSubmit();
  });
  document.getElementById("refresh")?.addEventListener("click", refreshRows);
  document.getElementById("process-next")?.addEventListener("click", processNextBatch);
  document.getElementById("search-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    searchTerm = (document.getElementById("search") as HTMLInputElement | null)?.value.trim() || "";
    setLoading("search");
    try {
      await loadRows(false);
    } catch (error) {
      setMessage("error", error instanceof Error ? error.message : "Search failed.");
    } finally {
      setLoading("");
    }
  });
  document.getElementById("filters-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    filters = readFilters();
    setLoading("filters");
    try {
      await loadRows(false);
    } catch (error) {
      setMessage("error", error instanceof Error ? error.message : "Filter failed.");
    } finally {
      setLoading("");
    }
  });
  document.getElementById("clear-filters")?.addEventListener("click", async () => {
    filters = {
      processingStatus: "",
      qualityStatus: "",
      under500: "",
      hasReflection: "",
      hasKsbs: "",
    };
    searchTerm = "";
    setLoading("filters");
    try {
      await loadRows(false);
    } catch (error) {
      setMessage("error", error instanceof Error ? error.message : "Clear filters failed.");
    } finally {
      setLoading("");
    }
  });
  document.querySelectorAll<HTMLButtonElement>("[data-detail-id]").forEach((button) => {
    button.addEventListener("click", () => loadDetail(Number(button.dataset.detailId)));
  });
  document.getElementById("close-detail")?.addEventListener("click", () => {
    detail = null;
    render();
  });
}

function readFilters() {
  return {
    processingStatus: valueOf("processing-status"),
    qualityStatus: valueOf("quality-status"),
    under500: valueOf("under-500"),
    hasReflection: valueOf("has-reflection"),
    hasKsbs: valueOf("has-ksbs"),
  };
}

function valueOf(id: string) {
  return (document.getElementById(id) as HTMLSelectElement | null)?.value || "";
}

function selectFilter(id: string, title: string, value: string, options: string[][]) {
  return `
    <label class="filter-control">
      ${escapeHtml(title)}
      <select id="${escapeHtml(id)}">
        ${options
          .map(
            ([optionValue, optionLabel]) =>
              `<option value="${escapeHtml(optionValue)}" ${value === optionValue ? "selected" : ""}>${escapeHtml(optionLabel)}</option>`,
          )
          .join("")}
      </select>
    </label>
  `;
}

function metric(title: string, value: number, helper: string, tone = "default") {
  return `
    <div class="metric ${tone}">
      <span class="metric-dot"></span>
      <div>
        <strong>${value}</strong>
        <small>${escapeHtml(title)}</small>
        <em>${escapeHtml(helper)}</em>
      </div>
    </div>
  `;
}

function rowHtml(row: SourceItem) {
  const processing = row.processing;
  return `
    <tr>
      <td class="mono id-cell">#${row.evidence_id}</td>
      <td>
        <strong>${escapeHtml(row.full_name || "-")}</strong>
        <div class="muted">Learner ${row.learner_id}</div>
      </td>
      <td class="wide-text">${escapeHtml(row.program_name || "-")}</td>
      <td>
        <div class="evidence-name">${escapeHtml(row.evidence_name || "-")}</div>
        <div class="muted">${escapeHtml(formatDate(row.submission_date))}</div>
      </td>
      <td><span class="pill status ${escapeHtml(processing.status)}">${label(processing.status)}</span></td>
      <td>
        <div class="word-cell">
          <strong>${processing.word_count ?? "-"}</strong>
          ${
            processing.is_under_word_count_threshold
              ? `<span>&lt; ${processing.word_count_threshold}</span>`
              : ""
          }
        </div>
      </td>
      <td>
        <span class="pill quality ${escapeHtml(processing.quality_check_status)}">
          ${label(processing.quality_check_status)}
        </span>
        <div class="muted quality-note">${escapeHtml(processing.quality_check_summary || processing.failure_reason || "")}</div>
      </td>
      <td>${processing.has_reflection ? escapeHtml(processing.reflection_strength) : "No"}</td>
      <td>${processing.has_ksbs ? escapeHtml(formatKsbCodes(processing.ksb_codes) || "Yes") : "No"}</td>
      <td>
        <div>${escapeHtml(processing.downloaded_file_name || "-")}</div>
        <div class="muted">${escapeHtml(formatBytes(processing.file_size_bytes))}</div>
      </td>
      <td>
        <div class="row-actions">
          ${
            processing.id
              ? `<button class="mini-button" data-detail-id="${processing.id}">${loading === `detail-${processing.id}` ? "..." : "Result"}</button>
                 <a class="mini-link" href="${API_BASE}/evidence/${processing.id}/file/" title="Download saved file">File</a>`
              : `<span class="queued-label">Awaiting batch</span>`
          }
        </div>
      </td>
    </tr>
  `;
}

function detailPanel(item: EvidenceDetail) {
  return `
    <div class="detail-backdrop">
      <section class="detail-panel">
        <header>
          <div>
            <div class="eyebrow">Evidence #${item.evidence_id}</div>
            <h2>${escapeHtml(item.evidence_name || "Analysis result")}</h2>
          </div>
          <button class="ghost-button" id="close-detail">Close</button>
        </header>
        <div class="detail-banner ${escapeHtml(item.quality_check_status)}">
          <strong>${label(item.quality_check_status)}</strong>
          <span>${escapeHtml(item.quality_check_summary || item.failure_reason || "No summary saved.")}</span>
        </div>
        <div class="detail-grid">
          ${detailMetric("Status", label(item.status))}
          ${detailMetric("Quality", label(item.quality_check_status))}
          ${detailMetric("Words", String(item.word_count ?? "-"))}
          ${detailMetric("Under 1000", item.is_under_word_count_threshold ? "Yes" : "No")}
          ${detailMetric("Reflection", item.has_reflection ? item.reflection_strength : "No")}
          ${detailMetric("KSBs", item.has_ksbs ? formatKsbCodes(item.ksb_codes) || "Yes" : "No")}
        </div>
        <dl class="detail-list">
          <dt>Quality summary</dt>
          <dd>${escapeHtml(item.quality_check_summary || "-")}</dd>
          <dt>Analysis summary</dt>
          <dd>${escapeHtml(item.analysis_summary || "-")}</dd>
          <dt>Reflection evidence</dt>
          <dd>${escapeHtml(formatEvidenceList(item.reflection_evidence))}</dd>
          <dt>KSB evidence</dt>
          <dd>${escapeHtml(formatEvidenceList(item.ksb_evidence))}</dd>
          <dt>Failure reason</dt>
          <dd>${escapeHtml(item.failure_reason || "-")}</dd>
          <dt>Extraction</dt>
          <dd>${escapeHtml(item.text_extracted ? "Text extracted" : item.extraction_note || "No extracted text")}</dd>
        </dl>
      </section>
    </div>
  `;
}

function detailMetric(title: string, value: string) {
  return `
    <div class="detail-metric">
      <span>${escapeHtml(title)}</span>
      <strong>${value}</strong>
    </div>
  `;
}

function formatEvidenceList(values: string[]) {
  return values && values.length ? values.join(" | ") : "-";
}

function emptyRow() {
  return `
    <tr>
      <td colspan="11">
        <div class="empty-state">
          <strong>${auth.is_staff ? "No evidence items found" : "Staff session required"}</strong>
          <span>${auth.is_staff ? "Try clearing filters or refreshing the queue." : "Sign in, then refresh the table to load PDF evidence items."}</span>
        </div>
      </td>
    </tr>
  `;
}

window.addEventListener("error", (event) => {
  rootEl.innerHTML = `<main class="app-shell"><section class="workspace"><div class="banner error">${escapeHtml(event.message)}</div></section></main>`;
});

render();
void loadAuth();
