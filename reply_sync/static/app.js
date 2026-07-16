const state = {
  signedIn: false,
  enabled: false,
  returnEnabled: false,
  returnQueue: {},
  returnIntervalMinutes: 10,
  returnNextSendAt: null,
  threads: [],
};

const authStatus = document.querySelector("#authStatus");
const loginLink = document.querySelector("#loginLink");
const discoverButton = document.querySelector("#discoverButton");
const refreshButton = document.querySelector("#refreshButton");
const threadCount = document.querySelector("#threadCount");
const returnQueueStatus = document.querySelector("#returnQueueStatus");
const threadsNode = document.querySelector("#threads");
const template = document.querySelector("#threadTemplate");
const messageNode = document.querySelector("#message");
const automationBanner = document.querySelector("#automationBanner");

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status})`);
  return payload;
}

async function checkAuth() {
  const status = await api("/auth/status");
  state.signedIn = Boolean(status.signed_in);
  authStatus.textContent = state.signedIn ? "Signed in" : "Not signed in";
  loginLink.classList.toggle("hidden", state.signedIn);
  discoverButton.disabled = !state.signedIn;
  refreshButton.disabled = !state.signedIn;
}

async function loadThreads() {
  if (!state.signedIn) return;
  const payload = await api("/api/reply-sync/threads");
  state.enabled = payload.enabled;
  state.returnEnabled = payload.return_enabled;
  state.returnQueue = payload.return_queue || {};
  state.returnIntervalMinutes = payload.return_send_interval_minutes || 10;
  state.returnNextSendAt = payload.return_next_send_at;
  state.threads = payload.threads || [];
  automationBanner.classList.toggle("hidden", state.enabled);
  if (state.enabled) {
    automationBanner.textContent = "Reply synchronization is enabled. Active threads can send replies.";
  } else {
    automationBanner.classList.remove("hidden");
    automationBanner.textContent = "Safety lock: REPLY_SYNC_ENABLED=false. Discovery and setup work, but no reply can be sent.";
  }
  render();
}

function render() {
  threadsNode.replaceChildren();
  threadCount.textContent = `${state.threads.length} thread${state.threads.length === 1 ? "" : "s"}`;
  const counts = state.returnQueue.counts || {};
  const completed = (counts.sent || 0) + (counts.degraded || 0) + (counts.recovered || 0) + (counts.skipped_deleted || 0);
  const pending = Math.max(0, (state.returnQueue.total || 0) - completed);
  returnQueueStatus.textContent = ` · Return backlog: ${pending} pending, ${counts.ready || 0} translated · one send every ${state.returnIntervalMinutes} minutes`;
  returnQueueStatus.classList.toggle("hidden", !state.returnEnabled);
  for (const thread of state.threads) threadsNode.append(renderThread(thread));
}

function renderThread(thread) {
  const node = template.content.firstElementChild.cloneNode(true);
  const direction = thread.direction === "return" ? "return" : "primary";
  const directionAvailable = thread.status !== "superseded" && (direction !== "return" || state.returnEnabled);
  node.querySelector(".thread-title").textContent = thread.source?.subject || thread.source?.message_id || "Translated thread";
  node.querySelector(".thread-meta").textContent = `${direction} · ${thread.flow} · ${thread.target_language} · ${thread.origin}`;
  node.querySelector(".badge").textContent = thread.status || "preview";
  const metrics = node.querySelector(".metrics");
  for (const [label, value] of [
    ["Replies found", thread.discovered_reply_count],
    ["Queued", thread.queued_reply_count],
    ...(direction === "return" ? [["Return backlog", thread.return_queue_count], ["Translated", thread.return_ready_count]] : []),
    ["Completed", thread.completed_reply_count],
    ["Stable scans", thread.stable_scans],
    ["Last sequence", thread.last_contiguous_sequence],
  ]) {
    const wrapper = document.createElement("div");
    wrapper.innerHTML = `<dt>${label}</dt><dd>${value ?? 0}</dd>`;
    metrics.append(wrapper);
  }
  if (thread.error) {
    const error = node.querySelector(".error");
    error.textContent = thread.error;
    error.classList.remove("hidden");
  }
  const links = node.querySelector(".links");
  addLink(links, direction === "return" ? "Translated post" : "Source post", thread.source?.web_url);
  addLink(links, direction === "return" ? "Original post" : "Translated post", thread.destination?.web_url);
  const actions = node.querySelector(".actions");
  if (!thread.destination?.message_id) {
    addButton(actions, "Link destination", () => linkDestination(thread));
  } else if (!thread.enabled) {
    addButton(actions, "Backfill all", () => activate(thread, "backfill_all"), "", !directionAvailable);
    addButton(actions, "Future only", () => activate(thread, "future_only"), "secondary", !directionAvailable);
  } else {
    addButton(actions, "Run now", () => act(`/api/reply-sync/threads/${encodeURIComponent(thread.thread_key)}/run`), "secondary", !state.enabled);
    addButton(actions, "Pause", () => act(`/api/reply-sync/threads/${encodeURIComponent(thread.thread_key)}/pause`), "secondary");
  }
  if (thread.status === "blocked") {
    addButton(actions, "Retry", () => act(`/api/reply-sync/threads/${encodeURIComponent(thread.thread_key)}/retry`), "secondary", !state.enabled);
    if (thread.blocked_reply_id) {
      addButton(actions, "Send degraded", () => sendDegraded(thread), "warning", !state.enabled);
    }
  }
  return node;
}

function addLink(parent, label, url) {
  if (!url) return;
  const link = document.createElement("a");
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = label;
  parent.append(link);
}

function addButton(parent, label, handler, style = "", disabled = false) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `button ${style}`.trim();
  button.textContent = label;
  button.disabled = disabled;
  button.addEventListener("click", handler);
  parent.append(button);
}

async function activate(thread, startMode) {
  await act(`/api/reply-sync/threads/${encodeURIComponent(thread.thread_key)}/activate`, { start_mode: startMode });
}

async function linkDestination(thread) {
  const destinationUrl = window.prompt("Paste the Teams URL for the translated root post:");
  if (!destinationUrl) return;
  await act(`/api/reply-sync/threads/${encodeURIComponent(thread.thread_key)}/link`, { destination_url: destinationUrl });
}

async function sendDegraded(thread) {
  const confirmed = window.confirm("Send this reply without unsupported media/attachments, then allow the ordered queue to continue?");
  if (!confirmed) return;
  await act(
    `/api/reply-sync/threads/${encodeURIComponent(thread.thread_key)}/replies/${encodeURIComponent(thread.blocked_reply_id)}/send-degraded`,
    { confirm: true },
  );
}

async function act(path, body) {
  try {
    showMessage("Working…");
    await api(path, { method: "POST", body: body ? JSON.stringify(body) : undefined });
    await loadThreads();
    showMessage("Completed.");
  } catch (error) {
    showMessage(error.message, true);
  }
}

function showMessage(text, error = false) {
  messageNode.textContent = text;
  messageNode.classList.remove("hidden");
  messageNode.classList.toggle("error", error);
}

discoverButton.addEventListener("click", () => act("/api/reply-sync/discover"));
refreshButton.addEventListener("click", () => loadThreads().catch((error) => showMessage(error.message, true)));

(async () => {
  try {
    await checkAuth();
    await loadThreads();
  } catch (error) {
    showMessage(error.message, true);
  }
})();
