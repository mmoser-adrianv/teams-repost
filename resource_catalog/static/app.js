const authStatus = document.querySelector("#authStatus");
const loginLink = document.querySelector("#loginLink");
const logoutButton = document.querySelector("#logoutButton");
const addResourceButton = document.querySelector("#addResourceButton");
const refreshButton = document.querySelector("#refreshButton");
const searchInput = document.querySelector("#searchInput");
const typeFilter = document.querySelector("#typeFilter");
const resourceList = document.querySelector("#resourceList");
const resultCount = document.querySelector("#resultCount");
const updateStatus = document.querySelector("#updateStatus");
const message = document.querySelector("#message");
const resourceDialog = document.querySelector("#resourceDialog");
const resourceForm = document.querySelector("#resourceForm");
const closeDialogButton = document.querySelector("#closeDialogButton");
const cancelDialogButton = document.querySelector("#cancelDialogButton");
const submitResourceButton = document.querySelector("#submitResourceButton");
const formError = document.querySelector("#formError");

const typeLabels = {
  workspace_agent: "Workspace agent",
  plugin: "Plugin",
  skill: "Skill"
};

let allResources = [];
let lastUpdatedAt = null;
let lastCheckedAt = 0;
let pollIntervalMs = 60 * 1000;
let pollTimer = null;
let loading = false;
let signedIn = false;
let submissionEnabled = false;

refreshButton.addEventListener("click", () => loadResources({forceRender: true, announce: true}));
searchInput.addEventListener("input", renderResources);
typeFilter.addEventListener("change", renderResources);
addResourceButton.addEventListener("click", openDialog);
closeDialogButton.addEventListener("click", closeDialog);
cancelDialogButton.addEventListener("click", closeDialog);
logoutButton.addEventListener("click", logout);
resourceForm.addEventListener("submit", submitResource);
resourceDialog.addEventListener("click", (event) => {
  if (event.target === resourceDialog) closeDialog();
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && Date.now() - lastCheckedAt >= pollIntervalMs) loadResources();
});

async function boot() {
  await Promise.all([loadAuth(), loadResources({forceRender: true})]);
}

async function loadAuth() {
  try {
    const response = await fetch("/auth/status", {cache: "no-store"});
    const data = await response.json();
    setAuthState(Boolean(data.authenticated || data.signed_in));
  } catch {
    authStatus.textContent = "Authentication unavailable";
    setAuthState(false);
  }
}

function setAuthState(value) {
  signedIn = value;
  authStatus.textContent = signedIn ? "Signed in" : "Not signed in";
  loginLink.classList.toggle("hidden", signedIn);
  logoutButton.classList.toggle("hidden", !signedIn);
  addResourceButton.disabled = !signedIn || !submissionEnabled;
  addResourceButton.title = !signedIn
    ? "Sign in before adding a resource"
    : (!submissionEnabled ? "Resource submission is not configured" : "");
}

async function logout() {
  logoutButton.disabled = true;
  try {
    const response = await fetch("/auth/logout", {method: "POST"});
    if (!response.ok) throw new Error("Could not sign out");
    setAuthState(false);
    closeDialog();
    showMessage("Signed out.");
  } catch (error) {
    showMessage(error.message || "Could not sign out", true);
  } finally {
    logoutButton.disabled = false;
  }
}

async function loadResources({forceRender = false, announce = false} = {}) {
  if (loading) return;
  loading = true;
  refreshButton.disabled = true;
  resourceList.setAttribute("aria-busy", "true");
  try {
    const response = await fetch("/api/resources", {cache: "no-store"});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(errorDetail(data, `Resource refresh failed with HTTP ${response.status}`));
    const catalogue = data.catalogue;
    if (!catalogue || !Array.isArray(catalogue.resources) || typeof catalogue.updated_at !== "string") {
      throw new Error("The resource response is invalid");
    }
    const wasUpdated = lastUpdatedAt !== null && lastUpdatedAt !== catalogue.updated_at;
    allResources = catalogue.resources;
    submissionEnabled = Boolean(data.submission_enabled);
    setAuthState(signedIn);
    if (forceRender || lastUpdatedAt === null || wasUpdated) renderResources();
    lastUpdatedAt = catalogue.updated_at;
    lastCheckedAt = Date.now();
    setUpdateStatus(catalogue.updated_at, data.checked_at);
    setPollInterval(data.poll_interval_seconds);
    if (wasUpdated) showMessage("The resource catalogue has been updated.");
    else if (announce) showMessage("The resource catalogue is current.");
  } catch (error) {
    updateStatus.textContent = "Refresh failed";
    showMessage(error.message || "Could not refresh resources", true);
    if (lastUpdatedAt === null) renderEmpty("Resources are temporarily unavailable.");
  } finally {
    loading = false;
    refreshButton.disabled = false;
    resourceList.setAttribute("aria-busy", "false");
  }
}

function setPollInterval(seconds) {
  const parsed = Number(seconds);
  const nextInterval = Number.isFinite(parsed) && parsed >= 15 ? parsed * 1000 : 60 * 1000;
  if (pollTimer && nextInterval === pollIntervalMs) return;
  pollIntervalMs = nextInterval;
  if (pollTimer) window.clearInterval(pollTimer);
  pollTimer = window.setInterval(() => {
    if (!document.hidden) loadResources();
  }, pollIntervalMs);
}

function renderResources() {
  const query = searchInput.value.trim().toLocaleLowerCase();
  const selectedType = typeFilter.value;
  const filtered = allResources.filter((resource) => {
    if (selectedType && resource.type !== selectedType) return false;
    if (!query) return true;
    return [resource.name, resource.description, resource.author]
      .some((value) => String(value || "").toLocaleLowerCase().includes(query));
  });
  resourceList.replaceChildren();
  resultCount.textContent = `${filtered.length} ${filtered.length === 1 ? "resource" : "resources"}`;
  if (!filtered.length) {
    renderEmpty(allResources.length ? "No resources match these filters." : "No resources are available yet.");
    return;
  }
  const fragment = document.createDocumentFragment();
  filtered.forEach((resource) => fragment.appendChild(resourceCard(resource)));
  resourceList.appendChild(fragment);
}

function resourceCard(resource) {
  const article = document.createElement("article");
  article.className = "resource-card";
  const header = document.createElement("div");
  header.className = "card-header";
  const heading = document.createElement("h2");
  heading.textContent = resource.name;
  const badge = document.createElement("span");
  badge.className = "badge";
  badge.textContent = typeLabels[resource.type] || resource.type;
  header.append(heading, badge);
  const description = document.createElement("p");
  description.className = "resource-description";
  description.textContent = resource.description;
  const meta = document.createElement("p");
  meta.className = "resource-meta";
  meta.textContent = `By ${resource.author} · Added ${formatDate(resource.submitted_at)}`;
  const link = document.createElement("a");
  link.className = "resource-link";
  link.href = safeUrl(resource.url);
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = "Open resource";
  article.append(header, description, meta, link);
  return article;
}

function renderEmpty(text) {
  resourceList.replaceChildren();
  const empty = document.createElement("div");
  empty.className = "empty-state";
  empty.textContent = text;
  resourceList.appendChild(empty);
  resultCount.textContent = "0 resources";
}

function setUpdateStatus(updatedAt, checkedAt) {
  updateStatus.textContent = `Updated ${formatDate(updatedAt)} · Checked ${formatDate(checkedAt)}`;
}

function openDialog() {
  if (!signedIn) return showMessage("Sign in before adding a resource.", true);
  if (!submissionEnabled) return showMessage("Resource submission is not configured.", true);
  hideFormError();
  resourceDialog.showModal();
  document.querySelector("#resourceUrl").focus();
}

function closeDialog() {
  if (resourceDialog.open) resourceDialog.close();
  hideFormError();
  addResourceButton.focus();
}

async function submitResource(event) {
  event.preventDefault();
  hideFormError();
  submitResourceButton.disabled = true;
  const form = new FormData(resourceForm);
  const payload = {
    url: String(form.get("url") || "").trim(),
    name: String(form.get("name") || "").trim(),
    description: String(form.get("description") || "").trim(),
    type: String(form.get("type") || ""),
    author: String(form.get("author") || "").trim()
  };
  try {
    const response = await fetch("/api/resources", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    const data = await response.json().catch(() => ({}));
    // This upstream API reports validation failures with HTTP 200, so status is authoritative.
    if (data.status === "failed") {
      showFormError(data.error || "The resource was rejected.");
      return;
    }
    if (!response.ok) throw new Error(errorDetail(data, `Resource submission failed with HTTP ${response.status}`));
    if (data.status !== "success" && data.status !== "exists") {
      throw new Error("The resource submission response is invalid");
    }
    const alreadyExists = data.status === "exists";
    resourceForm.reset();
    closeDialog();
    showMessage(alreadyExists ? "That resource already exists." : "Resource added successfully.");
    await loadResources({forceRender: true});
  } catch (error) {
    showFormError(error.message || "Could not submit the resource");
  } finally {
    submitResourceButton.disabled = false;
  }
}

function showFormError(text) {
  formError.textContent = text;
  formError.classList.remove("hidden");
  formError.focus();
}
function hideFormError() { formError.textContent = ""; formError.classList.add("hidden"); }
function showMessage(text, isError = false) { message.textContent = text; message.classList.toggle("error", isError); message.classList.toggle("hidden", !text); }
function errorDetail(data, fallback) { return typeof data.detail === "string" ? data.detail : fallback; }
function formatDate(value) { const date = new Date(value); return Number.isNaN(date.getTime()) ? String(value || "Unknown") : date.toLocaleString(); }
function safeUrl(value) { const url = new URL(value); if (!["http:", "https:"].includes(url.protocol)) throw new Error("Invalid resource URL"); return url.href; }

boot();
