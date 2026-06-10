const authStatus = document.querySelector("#authStatus");
const loginLink = document.querySelector("#loginLink");
const manageExceptionsButton = document.querySelector("#manageExceptionsButton");
const logoutButton = document.querySelector("#logoutButton");
const refreshButton = document.querySelector("#refreshButton");
const managerTitle = document.querySelector("#managerTitle");
const loadingIndicator = document.querySelector("#loadingIndicator");
const pageSize = document.querySelector("#pageSize");
const prevPageButton = document.querySelector("#prevPageButton");
const nextPageButton = document.querySelector("#nextPageButton");
const pageStatus = document.querySelector("#pageStatus");
const postsEl = document.querySelector("#posts");
const messageEl = document.querySelector("#message");
const sourceLabel = document.querySelector("#sourceLabel");
const exceptionsDialog = document.querySelector("#exceptionsDialog");
const closeExceptionsButton = document.querySelector("#closeExceptionsButton");
const exceptionForm = document.querySelector("#exceptionForm");
const exceptionEmail = document.querySelector("#exceptionEmail");
const exceptionsList = document.querySelector("#exceptionsList");
const template = document.querySelector("#postTemplate");
const defaultConfig = {
  apiBase: "/api/flows/forward",
  exceptionsPath: "/api/exceptions",
  pagePath: "/",
  title: "Teams Repost Manager",
  exceptionsTitle: "Exception List",
  translationTargetLanguage: "zh-Hans",
  translationTargetLabel: "Chinese",
  emptyPostsMessage: "No posts returned from the configured source channel."
};
const managerConfig = {...defaultConfig, ...(window.TEAMS_REPOST_MANAGER_CONFIG || {})};
const translationTargetLanguage = managerConfig.translationTargetLanguage;
const translationTargetLabel = managerConfig.translationTargetLabel;
const autoRefreshIntervalMs = 10 * 60 * 1000;

let currentCursor = null;
let nextCursor = null;
let previousCursors = [];
let pageNumber = 1;
let activeLoadId = 0;
let isLoadingPosts = false;
let isSignedIn = false;
let autoRefreshTimer = null;

refreshButton.addEventListener("click", () => loadPosts({reset: true, refresh: true}));
logoutButton.addEventListener("click", () => logout());
pageSize.addEventListener("change", () => loadPosts({reset: true, refresh: false}));
prevPageButton.addEventListener("click", () => goToPreviousPage());
nextPageButton.addEventListener("click", () => goToNextPage());
manageExceptionsButton.addEventListener("click", () => openExceptionsDialog());
closeExceptionsButton.addEventListener("click", () => closeExceptionsDialog());
exceptionForm.addEventListener("submit", (event) => addException(event));
exceptionsDialog.addEventListener("click", (event) => {
  if (event.target === exceptionsDialog) {
    closeExceptionsDialog();
  }
});
exceptionsDialog.addEventListener("cancel", () => closeExceptionsDialog());

async function boot() {
  configurePage();
  await loadAuth();
  await loadPosts({reset: true, refresh: true});
  startAutoRefresh();
}

function configurePage() {
  document.title = managerConfig.title;
  if (managerTitle) {
    managerTitle.textContent = managerConfig.title;
  }
  const exceptionsHeading = document.querySelector("#exceptionsHeading");
  if (exceptionsHeading) {
    exceptionsHeading.textContent = managerConfig.exceptionsTitle;
  }
  loginLink.href = `/auth/login?return_to=${encodeURIComponent(managerConfig.pagePath)}`;
}

async function loadAuth() {
  try {
    const response = await fetch("/auth/status");
    const status = await response.json();
    setAuthState(status.signed_in);
  } catch {
    authStatus.textContent = "Auth unavailable";
    loginLink.classList.remove("hidden");
    logoutButton.classList.add("hidden");
  }
}

async function logout() {
  setMessage("");
  logoutButton.disabled = true;
  try {
    const response = await fetch("/auth/logout", {method: "POST"});
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `Sign out failed with HTTP ${response.status}`);
    }
    setAuthState(false);
    resetPagination();
    postsEl.innerHTML = "";
    sourceLabel.textContent = "";
    renderExceptions([]);
    closeExceptionsDialog();
    setMessage("Signed out of this website.");
  } catch (error) {
    setMessage(error.message || "Sign out failed.");
    await loadAuth();
  } finally {
    logoutButton.disabled = false;
  }
}

function setAuthState(signedIn) {
  isSignedIn = signedIn;
  authStatus.textContent = signedIn ? "Signed in" : "Signed out";
  loginLink.classList.toggle("hidden", signedIn);
  manageExceptionsButton.classList.toggle("hidden", !signedIn);
  logoutButton.classList.toggle("hidden", !signedIn);
  exceptionForm.querySelectorAll("input, button").forEach((element) => {
    element.disabled = !signedIn;
  });
  if (!signedIn) {
    closeExceptionsDialog();
    renderExceptions([]);
  }
}

async function loadExceptions() {
  if (!isSignedIn) {
    renderExceptions([]);
    return;
  }
  try {
    const response = await fetch(managerConfig.exceptionsPath);
    if (response.status === 401) {
      renderExceptions([]);
      return;
    }
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `Exception list failed with HTTP ${response.status}`);
    }
    const data = await response.json();
    renderExceptions(data.emails || []);
  } catch (error) {
    renderExceptions([]);
    setMessage(error.message || "Could not load exception list.");
  }
}

async function openExceptionsDialog() {
  if (!isSignedIn) {
    setMessage("Sign in to manage exceptions.");
    return;
  }
  setMessage("");
  await loadExceptions();
  if (typeof exceptionsDialog.showModal === "function") {
    exceptionsDialog.showModal();
  } else {
    exceptionsDialog.setAttribute("open", "");
  }
  exceptionEmail.focus();
}

function closeExceptionsDialog() {
  if (exceptionsDialog.open) {
    exceptionsDialog.close();
  }
}

async function addException(event) {
  event.preventDefault();
  const email = exceptionEmail.value.trim();
  if (!email) {
    return;
  }
  setMessage("");
  exceptionForm.querySelector("button").disabled = true;
  try {
    const response = await fetch(managerConfig.exceptionsPath, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({email})
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `Exception update failed with HTTP ${response.status}`);
    }
    const data = await response.json();
    exceptionEmail.value = "";
    renderExceptions(data.emails || []);
    await loadPosts({reset: true, refresh: false});
  } catch (error) {
    setMessage(error.message || "Could not add exception.");
  } finally {
    exceptionForm.querySelector("button").disabled = !isSignedIn;
  }
}

async function removeException(email, button) {
  button.disabled = true;
  setMessage("");
  try {
    const response = await fetch(`${managerConfig.exceptionsPath}/${encodeURIComponent(email)}`, {method: "DELETE"});
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `Exception update failed with HTTP ${response.status}`);
    }
    const data = await response.json();
    renderExceptions(data.emails || []);
    await loadPosts({reset: true, refresh: false});
  } catch (error) {
    button.disabled = false;
    setMessage(error.message || "Could not remove exception.");
  }
}

function renderExceptions(emails) {
  exceptionsList.innerHTML = "";
  if (!emails.length) {
    const empty = document.createElement("span");
    empty.className = "empty-exceptions";
    empty.textContent = "No exceptions";
    exceptionsList.append(empty);
    return;
  }
  for (const email of emails) {
    const item = document.createElement("span");
    item.className = "exception-chip";
    const text = document.createElement("span");
    text.textContent = email;
    const button = document.createElement("button");
    button.type = "button";
    button.setAttribute("aria-label", `Remove ${email}`);
    button.textContent = "x";
    button.addEventListener("click", () => removeException(email, button));
    item.append(text, button);
    exceptionsList.append(item);
  }
}

function goToPreviousPage() {
  if (!previousCursors.length) {
    return;
  }
  currentCursor = previousCursors.pop();
  pageNumber = Math.max(1, pageNumber - 1);
  loadPosts({refresh: false});
}

function goToNextPage() {
  if (!nextCursor) {
    return;
  }
  previousCursors.push(currentCursor);
  currentCursor = nextCursor;
  pageNumber += 1;
  loadPosts({refresh: false});
}

async function loadPosts(options = {}) {
  if (isLoadingPosts) {
    return;
  }

  if (options.reset) {
    resetPagination();
  }

  const isQuiet = options.quiet === true;
  const shouldRender = options.render !== false;
  if (!isQuiet) {
    setMessage("");
  }
  const loadId = ++activeLoadId;
  const shouldRefresh = options.refresh === true;
  isLoadingPosts = true;
  if (!isQuiet) {
    refreshButton.disabled = true;
    setLoading(true);
  }
  try {
    const params = new URLSearchParams({limit: pageSize.value, refresh: shouldRefresh ? "true" : "false"});
    if (!shouldRefresh && currentCursor) {
      params.set("cursor", currentCursor);
    }
    const response = await fetch(`${managerConfig.apiBase}/posts?${params}`);
    if (response.status === 401) {
      resetPagination();
      setMessage("Sign in to load posts.");
      return;
    }
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `Request failed with HTTP ${response.status}`);
    }
    const data = await response.json();
    if (loadId !== activeLoadId) {
      return;
    }
    sourceLabel.textContent = sourceText(data);
    if (shouldRender) {
      nextCursor = data.pagination?.next_cursor || null;
      postsEl.innerHTML = "";
      for (const post of data.posts) {
        postsEl.append(renderPost(post));
      }
    }
    if (!isQuiet && data.cache?.refresh_failed) {
      setMessage(`Showing saved posts. Refresh failed: ${data.cache.refresh_error || "Could not refresh from Microsoft Graph."}`);
    } else if (!isQuiet && skippedPostMessages(data.cache).length) {
      setMessage(skippedPostMessages(data.cache).join(" "));
    } else if (!isQuiet && shouldRender && !data.posts.length) {
      setMessage(managerConfig.emptyPostsMessage);
    }
  } catch (error) {
    if (!isQuiet) {
      setMessage(error.message || "Could not load posts.");
    }
  } finally {
    if (loadId === activeLoadId) {
      isLoadingPosts = false;
      if (!isQuiet) {
        setLoading(false);
        updatePaginationControls();
      }
    }
  }
}

function startAutoRefresh() {
  if (autoRefreshTimer) {
    return;
  }
  autoRefreshTimer = window.setInterval(() => {
    if (!isSignedIn || isLoadingPosts) {
      return;
    }
    const isOnFirstPage = pageNumber === 1;
    loadPosts({
      reset: isOnFirstPage,
      refresh: true,
      quiet: true,
      render: isOnFirstPage
    });
  }, autoRefreshIntervalMs);
}

function skippedPostMessages(cache) {
  const messages = [];
  const exceptionCount = cache?.posts_skipped_by_exception || 0;
  if (exceptionCount) {
    messages.push(`Skipped ${exceptionCount} post${exceptionCount === 1 ? "" : "s"} from the exception list.`);
  }
  const bodyPrefixCount = cache?.posts_skipped_by_body_prefix || 0;
  if (bodyPrefixCount) {
    messages.push(`Skipped ${bodyPrefixCount} repost${bodyPrefixCount === 1 ? "" : "s"} from the loop-prevention filter.`);
  }
  const graphErrorCount = cache?.posts_skipped_by_graph_error || 0;
  if (graphErrorCount) {
    messages.push(`Skipped ${graphErrorCount} post${graphErrorCount === 1 ? "" : "s"} Microsoft Graph could not load.`);
  }
  return messages;
}

function sourceText(data) {
  const parts = [`Source: ${data.source.team_id} / ${data.source.channel_id}`];
  if (data.cache?.last_refreshed_at) {
    parts.push(`Last checked: ${formatDate(data.cache.last_refreshed_at)}`);
  }
  return parts.join(" | ");
}

function resetPagination() {
  currentCursor = null;
  nextCursor = null;
  previousCursors = [];
  pageNumber = 1;
  updatePaginationControls();
}

function setLoading(isLoading) {
  loadingIndicator.classList.toggle("hidden", !isLoading);
  refreshButton.disabled = isLoading;
  pageSize.disabled = isLoading;
  prevPageButton.disabled = isLoading || !previousCursors.length;
  nextPageButton.disabled = isLoading || !nextCursor;
}

function updatePaginationControls() {
  pageStatus.textContent = `Page ${pageNumber}`;
  pageSize.disabled = false;
  refreshButton.disabled = false;
  prevPageButton.disabled = !previousCursors.length;
  nextPageButton.disabled = !nextCursor;
}

function renderPost(post) {
  const node = template.content.firstElementChild.cloneNode(true);
  const title = node.querySelector(".post-title");
  const meta = node.querySelector(".post-meta");
  const badge = node.querySelector(".badge");
  const body = node.querySelector(".post-body");
  const links = node.querySelector(".link-row");
  const attachments = node.querySelector(".attachments");
  const images = node.querySelector(".images");
  const warnings = node.querySelector(".warnings");
  const requiredElements = [title, meta, badge, body, links, attachments, images, warnings];
  if (requiredElements.some((element) => !element)) {
    throw new Error("The post template is missing required elements. Refresh the page to load the current assets.");
  }

  meta.textContent = [post.author || "Unknown author", post.author_email, formatDate(post.created_date_time)].filter(Boolean).join(" | ");
  post.showTranslation = false;
  setPostContent(node, post);
  node.postBadge = badge;
  node.actionRow = links;
  updatePostBadge(node, post);

  renderActions(node, post);

  attachments.dataset.label = "Attachments";
  for (const attachment of post.attachments || []) {
    addLink(attachments, attachment.name || "Attachment", attachment.content_url);
  }

  images.dataset.label = "Embedded images";
  if (post.embedded_images_zip_url) {
    addLink(images, "Download all", post.embedded_images_zip_url);
  }
  for (const image of post.embedded_images || []) {
    addLink(images, `Image ${image.occurrence}`, image.download_url);
  }

  for (const warning of post.warnings || []) {
    const item = document.createElement("li");
    item.textContent = warning;
    warnings.append(item);
  }

  return node;
}

function renderActions(node, post) {
  const links = node.actionRow;
  links.innerHTML = "";

  addLink(links, "Open original", post.web_url);
  if (post.subject || post.body_html || post.body_preview) {
    const translateButton = document.createElement("button");
    translateButton.type = "button";
    translateButton.classList.add("secondary-action");
    updateTranslateButton(translateButton, post);
    translateButton.addEventListener("click", () => toggleTranslation(post, node, translateButton));
    links.append(translateButton);
  }

  if (post.reposted) {
    if (post.repost?.web_url) {
      addLink(links, "Open repost", post.repost.web_url);
    }
  } else {
    const repostButton = document.createElement("button");
    repostButton.type = "button";
    updateRepostButton(repostButton, post);
    repostButton.addEventListener("click", () => repost(post, node, repostButton));
    links.append(repostButton);
  }

  links.append(createManualToggle(post, node));
}

function setPostContent(node, post) {
  const title = node.querySelector(".post-title");
  const body = node.querySelector(".post-body");
  const translation = post.showTranslation ? translationFor(post) : null;
  const subject = translation?.subject || post.subject;
  const bodyHtml = translation?.body_html || post.body_html || "";
  const preview = translation?.body_preview || post.body_preview;

  title.textContent = subject || firstText(preview) || "Teams message";
  body.innerHTML = bodyHtml;
  body.classList.toggle("hidden", !bodyHtml);
}

function translationFor(post) {
  return post.translations?.[translationTargetLanguage] || null;
}

function updateTranslateButton(button, post) {
  if (post.showTranslation) {
    button.textContent = "Show original";
  } else if (translationFor(post)) {
    button.textContent = `Show ${translationTargetLabel}`;
  } else {
    button.textContent = "Translate";
  }
}

function updateRepostButton(button, post) {
  const hasTranslation = Boolean(translationFor(post));
  button.textContent = `Repost ${translationTargetLabel}`;
  button.disabled = post.reposted || !hasTranslation;
  button.title = hasTranslation ? "" : `Translate this post before reposting ${translationTargetLabel}.`;
}

function createManualToggle(post, node) {
  const label = document.createElement("label");
  label.className = "manual-toggle";

  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = Boolean(post.reposted);
  input.disabled = Boolean(post.reposted);
  input.addEventListener("change", () => {
    if (input.checked) {
      markManualReposted(post, node, input);
    }
  });

  const text = document.createElement("span");
  text.textContent = "Already reposted";
  label.append(input, text);
  return label;
}

async function toggleTranslation(post, node, button) {
  if (post.showTranslation) {
    post.showTranslation = false;
    setPostContent(node, post);
    updateTranslateButton(button, post);
    return;
  }

  if (translationFor(post)) {
    post.showTranslation = true;
    setPostContent(node, post);
    updateTranslateButton(button, post);
    return;
  }

  button.disabled = true;
  button.textContent = "Translating";
  try {
    await requestTranslation(post);
    post.showTranslation = true;
    setPostContent(node, post);
    renderActions(node, post);
    setMessage("");
  } catch (error) {
    setMessage(error.message || "Translation failed.");
  } finally {
    button.disabled = false;
    updateTranslateButton(button, post);
  }
}

async function requestTranslation(post) {
  const response = await fetch(`${managerConfig.apiBase}/posts/${encodeURIComponent(post.id)}/translations`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({target_language: translationTargetLanguage})
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `Translation failed with HTTP ${response.status}`);
  }
  const data = await response.json();
  post.translations = post.translations || {};
  post.translations[data.target_language || translationTargetLanguage] = data.translation;
  return data.translation;
}

async function repost(post, node, button) {
  if (!translationFor(post)) {
    setMessage(`Translate this post before reposting ${translationTargetLabel}.`);
    updateRepostButton(button, post);
    return;
  }

  button.disabled = true;
  const originalLabel = button.textContent;
  try {
    button.textContent = "Reposting";
    const response = await fetch(`${managerConfig.apiBase}/reposts`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({source_message_id: post.id, target_language: translationTargetLanguage})
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `Repost failed with HTTP ${response.status}`);
    }
    const data = await response.json();
    markPostReposted(post, node, data.record);
    setMessage("");
  } catch (error) {
    setMessage(error.message || "Repost failed.");
    button.disabled = false;
    button.textContent = originalLabel;
    updateRepostButton(button, post);
  }
}

async function markManualReposted(post, node, input) {
  input.disabled = true;
  try {
    const response = await fetch(`${managerConfig.apiBase}/reposts/manual`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({source_message_id: post.id, target_language: translationTargetLanguage})
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `Manual status update failed with HTTP ${response.status}`);
    }
    const data = await response.json();
    markPostReposted(post, node, data.record);
    setMessage("");
  } catch (error) {
    input.checked = false;
    input.disabled = false;
    setMessage(error.message || "Manual status update failed.");
  }
}

function markPostReposted(post, node, record) {
  post.reposted = true;
  post.repost = record?.destination || post.repost || null;
  post.reposted_at = record?.reposted_at || post.reposted_at || null;
  post.manual = record?.manual || post.manual || false;
  updatePostBadge(node, post);
  renderActions(node, post);
}

function updatePostBadge(node, post) {
  if (!node.postBadge) {
    return;
  }
  node.postBadge.textContent = post.reposted ? "Reposted" : "Pending";
  node.postBadge.classList.toggle("done", post.reposted);
  node.postBadge.classList.toggle("pending", !post.reposted);
}

function addLink(parent, label, href) {
  if (!href) {
    return null;
  }
  const link = document.createElement("a");
  link.href = href;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = label;
  parent.append(link);
  return link;
}

function setMessage(text) {
  messageEl.textContent = text;
  messageEl.classList.toggle("hidden", !text);
}

function formatDate(value) {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
}

function firstText(value) {
  return (value || "").split(/\s+/).slice(0, 8).join(" ");
}

boot();
