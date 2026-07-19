"use strict";

const SESSION_TOKEN_KEY = "qveris-proxy.admin-token.v1";
const BOOTSTRAP_TICKET_FRAGMENT_KEY = "bootstrap_ticket";
const ADMIN_BROWSER_SESSION_HEADER = "X-QVeris-Admin-Session";

const state = {
  token: "",
  status: null,
  config: null,
  draft: null,
  draftBaseline: "",
  operations: [],
  tests: new Map(),
  apiKeyVisible: false,
  deletingAccountId: null,
  proxyKeys: [],
  proxyKeyEditingId: null,
  proxyKeyBusyIds: new Set(),
  createdProxySecret: "",
};

const byId = (id) => document.getElementById(id);

function takeBootstrapTicket() {
  if (!window.location.hash) {
    return "";
  }
  const parameters = new URLSearchParams(window.location.hash.slice(1));
  if (!parameters.has(BOOTSTRAP_TICKET_FRAGMENT_KEY)) {
    return "";
  }
  const ticket = (parameters.get(BOOTSTRAP_TICKET_FRAGMENT_KEY) || "").trim();
  const cleanUrl = new URL(window.location.href);
  cleanUrl.hash = "";
  cleanUrl.searchParams.delete("launch");
  window.history.replaceState(
    null,
    "",
    `${cleanUrl.pathname}${cleanUrl.search}`,
  );
  return ticket;
}

function readSessionToken() {
  try {
    return (window.sessionStorage.getItem(SESSION_TOKEN_KEY) || "").trim();
  } catch {
    return "";
  }
}

function storeSessionToken(token) {
  try {
    window.sessionStorage.setItem(SESSION_TOKEN_KEY, token);
  } catch {
    // The console still works in the current page when browser storage is blocked.
  }
}

function clearSessionToken() {
  try {
    window.sessionStorage.removeItem(SESSION_TOKEN_KEY);
  } catch {
    // Storage may be unavailable in hardened browser contexts.
  }
}

async function copyText(text) {
  if (
    window.navigator.clipboard &&
    typeof window.navigator.clipboard.writeText === "function"
  ) {
    try {
      await window.navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Fall through for HTTP origins and browsers without clipboard permission.
    }
  }

  const field = document.createElement("textarea");
  field.value = text;
  field.setAttribute("readonly", "");
  field.setAttribute("aria-hidden", "true");
  field.style.position = "fixed";
  field.style.top = "0";
  field.style.left = "0";
  field.style.width = "1px";
  field.style.height = "1px";
  field.style.opacity = "0";
  document.body.append(field);
  field.select();
  field.setSelectionRange(0, field.value.length);
  try {
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    field.remove();
  }
}

async function copyApiKey() {
  if (!state.token) {
    showToast("请先连接代理", true);
    return;
  }
  const copied = await copyText(state.token);
  showToast(copied ? "API Key 已复制" : "复制失败，请检查剪贴板权限", !copied);
}

function apiBaseUrl() {
  return `${window.location.origin}/api/v1`;
}

async function copyBaseUrl() {
  const copied = await copyText(apiBaseUrl());
  showToast(copied ? "API Base URL 已复制" : "复制失败，请检查剪贴板权限", !copied);
}

async function copyConnection() {
  if (!state.token) {
    showToast("请先连接代理", true);
    return;
  }
  const copied = await copyText(
    `Base URL: ${apiBaseUrl()}\nAPI Key: ${state.token}`,
  );
  showToast(copied ? "接入配置已复制" : "复制失败，请检查剪贴板权限", !copied);
}

function maskedApiKey(token) {
  if (token.length <= 10) {
    return "•".repeat(token.length);
  }
  return `${token.slice(0, 4)}${"•".repeat(8)}${token.slice(-4)}`;
}

function setApiKeyVisibility(visible) {
  state.apiKeyVisible = Boolean(visible && state.token);
  byId("api-key-display").textContent = state.token
    ? state.apiKeyVisible
      ? state.token
      : maskedApiKey(state.token)
    : "";
  byId("toggle-api-key").textContent = state.apiKeyVisible ? "隐藏" : "显示";
  byId("toggle-api-key").setAttribute(
    "aria-pressed",
    state.apiKeyVisible ? "true" : "false",
  );
  byId("toggle-api-key").setAttribute(
    "aria-label",
    state.apiKeyVisible ? "隐藏代理 API Key" : "显示代理 API Key",
  );
}

function setManualKeyVisibility(visible) {
  const field = byId("access-token");
  field.type = visible ? "text" : "password";
  byId("toggle-manual-key").textContent = visible ? "隐藏" : "显示";
  byId("toggle-manual-key").setAttribute("aria-pressed", visible ? "true" : "false");
  byId("toggle-manual-key").setAttribute(
    "aria-label",
    visible ? "隐藏代理 API Key 输入值" : "显示代理 API Key 输入值",
  );
}

async function exchangeBootstrapTicket(ticket) {
  const response = await fetch("/admin/v1/bootstrap/exchange", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-QVeris-Bootstrap": "1",
    },
    body: JSON.stringify({ ticket }),
    cache: "no-store",
    credentials: "omit",
    redirect: "error",
  });
  return accessTokenFromResponse(response);
}

async function accessTokenFromResponse(response, emptyStatuses = []) {
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (emptyStatuses.includes(response.status)) {
    return "";
  }
  if (
    !response.ok ||
    !payload ||
    typeof payload.access_token !== "string" ||
    !payload.access_token
  ) {
    throw new Error("自动连接链接已失效，请重新运行启动脚本");
  }
  return payload.access_token;
}

async function rememberBrowserSession() {
  const response = await fetch("/admin/v1/browser-session", {
    method: "POST",
    headers: authHeaders(),
    cache: "no-store",
    credentials: "same-origin",
    redirect: "error",
  });
  await accessTokenFromResponse(response);
}

async function resumeBrowserSession() {
  const response = await fetch("/admin/v1/browser-session", {
    headers: { [ADMIN_BROWSER_SESSION_HEADER]: "1" },
    cache: "no-store",
    credentials: "same-origin",
    redirect: "error",
  });
  return accessTokenFromResponse(response, [401]);
}

async function claimFirstBrowserSession() {
  const response = await fetch("/admin/v1/browser-session/claim", {
    method: "POST",
    headers: { [ADMIN_BROWSER_SESSION_HEADER]: "1" },
    cache: "no-store",
    credentials: "same-origin",
    redirect: "error",
  });
  return accessTokenFromResponse(response, [403, 409]);
}

async function forgetBrowserSession() {
  const response = await fetch("/admin/v1/browser-session", {
    method: "DELETE",
    cache: "no-store",
    credentials: "same-origin",
    redirect: "error",
  });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
}

function node(tag, options = {}) {
  const element = document.createElement(tag);
  if (options.className) {
    element.className = options.className;
  }
  if (options.text !== undefined) {
    element.textContent = String(options.text);
  }
  if (options.type) {
    element.type = options.type;
  }
  return element;
}

function clear(element) {
  while (element.firstChild) {
    element.removeChild(element.firstChild);
  }
}

function showToast(message, isError = false) {
  const toast = byId("toast");
  toast.textContent = message;
  toast.dataset.state = isError ? "error" : "ok";
  toast.hidden = false;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    toast.hidden = true;
  }, 4200);
}

function setBusy(button, busy) {
  button.disabled = busy;
  button.dataset.busy = busy ? "true" : "false";
}

function formatNumber(value) {
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value);
}

function formatTime(epochSeconds) {
  if (!epochSeconds) {
    return "—";
  }
  return new Date(epochSeconds * 1000).toLocaleString("zh-CN", { hour12: false });
}

function authHeaders(extra = {}) {
  return {
    Authorization: `Bearer ${state.token}`,
    ...extra,
  };
}

async function requestJson(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: authHeaders(options.headers || {}),
  });
  let payload = null;
  const text = await response.text();
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { detail: text.slice(0, 500) };
    }
  }
  if (response.status === 401) {
    disconnect();
  }
  if (!response.ok) {
    const detail = payload && payload.detail ? payload.detail : `HTTP ${response.status}`;
    throw new Error(detail);
  }
  return payload;
}

async function connect(token) {
  state.token = token;
  storeSessionToken(token);
  try {
    const [status, config, catalog, proxyKeys] = await Promise.all([
      requestJson("/admin/v1/accounts"),
      requestJson("/admin/v1/config"),
      requestJson("/admin/v1/operations"),
      requestJson("/admin/v1/proxy-keys"),
    ]);
    state.status = status;
    state.config = config;
    state.draft = structuredClone(config);
    state.operations = catalog.operations;
    state.proxyKeys = Array.isArray(proxyKeys.keys) ? proxyKeys.keys : [];
    try {
      await rememberBrowserSession();
    } catch {
      // The current tab remains connected when persistent browser storage is blocked.
    }
    markPersistedAccounts();
    rememberDraftBaseline();
    byId("api-version").textContent = `API ${catalog.api_version}`;
    byId("locked-state").hidden = true;
    byId("workspace").hidden = false;
    byId("topbar-actions").hidden = false;
    byId("disconnect").hidden = false;
    byId("api-base-url").textContent = apiBaseUrl();
    byId("access-token").value = "";
    byId("manual-connect").open = false;
    setManualKeyVisibility(false);
    setApiKeyVisibility(false);
    setGateway("ok", "已连接");
    renderAll();
  } catch (error) {
    resetWorkspace();
    setGateway("error", "连接失败");
    showToast(error.message, true);
  }
}

function resetWorkspace() {
  state.status = null;
  state.config = null;
  state.draft = null;
  state.draftBaseline = "";
  state.operations = [];
  state.tests.clear();
  state.deletingAccountId = null;
  state.proxyKeys = [];
  state.proxyKeyEditingId = null;
  state.proxyKeyBusyIds.clear();
  closeProxyKeyEditor();
  closeCreatedProxySecret();
  byId("workspace").hidden = true;
  byId("locked-state").hidden = false;
  byId("topbar-actions").hidden = true;
  byId("disconnect").hidden = true;
  byId("access-token").value = "";
  setManualKeyVisibility(false);
  setApiKeyVisibility(false);
}

async function disconnect() {
  clearSessionToken();
  state.token = "";
  resetWorkspace();
  setGateway("idle", "未连接");
  try {
    await forgetBrowserSession();
  } catch {
    showToast("当前页面已断开，浏览器自动连接记录清除失败", true);
  }
}

async function bootstrap() {
  const ticket = takeBootstrapTicket();
  let token = "";
  if (ticket) {
    try {
      token = await exchangeBootstrapTicket(ticket);
    } catch (error) {
      resetWorkspace();
      setGateway("error", "自动连接失败");
      showToast(error.message, true);
      return;
    }
  }
  token ||= readSessionToken();
  if (!token) {
    try {
      token = await resumeBrowserSession();
      token ||= await claimFirstBrowserSession();
    } catch (error) {
      resetWorkspace();
      setGateway("error", "自动连接失败");
      showToast(error.message, true);
      return;
    }
  }
  if (token) {
    await connect(token);
  }
}

function setGateway(status, label) {
  byId("gateway-state").dataset.state = status;
  byId("gateway-label").textContent = label;
}

function renderAll() {
  renderStatus();
  renderProxyKeys();
  renderConfig();
  renderConsole();
}

function activateTab(tabName) {
  for (const candidate of document.querySelectorAll(".tab")) {
    candidate.classList.toggle("active", candidate.dataset.tab === tabName);
  }
  for (const panel of document.querySelectorAll(".panel")) {
    panel.classList.toggle("active", panel.dataset.panel === tabName);
  }
}

function accountManagementMessage(code) {
  return {
    persistent_editing_disabled: "持久编辑未启用",
    accounts_file_unavailable: "账号配置文件不可写",
    default_account_locked: "显式默认账号，请先修改 QVP_DEFAULT_ACCOUNT",
  }[code] || code || "当前账号不可管理";
}

function managementForAccount(accountId) {
  const accounts = state.status && Array.isArray(state.status.accounts)
    ? state.status.accounts
    : [];
  return accounts.find((account) => account.id === accountId)?.management || null;
}

async function editAccount(accountId) {
  if (!state.draft || !Array.isArray(state.draft.accounts)) {
    showToast("账号配置尚未加载，请刷新后重试", true);
    return;
  }

  try {
    const config = await requestJson("/admin/v1/config");
    const accountMissing = !state.draft.accounts.some(
      (account) => account.id === accountId,
    );
    const revisionChanged = config.revision !== state.config?.revision;
    if (revisionChanged || accountMissing) {
      if (
        configDraftIsDirty() &&
        !window.confirm(
          "服务端账号配置已更新。载入最新配置会放弃当前未保存修改，是否继续？",
        )
      ) {
        showToast("已保留未保存修改；载入最新配置后再编辑此账号", true);
        return;
      }
      state.config = config;
      state.draft = structuredClone(config);
      markPersistedAccounts();
      rememberDraftBaseline();
    } else {
      state.config = config;
    }
  } catch (error) {
    showToast(error.message, true);
    return;
  }

  const accountIndex = state.draft.accounts.findIndex(
    (account) => account.id === accountId,
  );
  if (accountIndex < 0) {
    showToast(`账号 ${accountId} 已不存在，请刷新状态`, true);
    return;
  }

  activateTab("config");
  renderConfig();
  const editor = byId("account-editors").children[accountIndex];
  if (!editor) {
    showToast("账号编辑器尚未就绪，请刷新后重试", true);
    return;
  }
  editor.scrollIntoView({ block: "start" });
  editor.classList.add("edit-target");
  const firstEditable = editor.querySelector(
    "input:not([readonly]):not([disabled])",
  );
  if (firstEditable) {
    firstEditable.focus();
  }
  window.setTimeout(() => editor.classList.remove("edit-target"), 1600);
}

function appendMetric(container, label, value) {
  const metric = node("div", { className: "metric" });
  metric.append(node("span", { text: label }), node("strong", { text: value }));
  container.append(metric);
}

function renderStatus() {
  const payload = state.status;
  if (!payload) {
    return;
  }
  const accounts = payload.accounts || [];
  const routingMode = state.config && state.config.routing
    ? state.config.routing.mode
    : "round_robin";
  byId("pool-summary").textContent = routingMode === "round_robin"
    ? `${accounts.length} 个账号 · 可用账号按权重轮询`
    : `${accounts.length} 个账号 · 显式选择账号`;
  const metrics = byId("metrics");
  clear(metrics);
  appendMetric(metrics, "账号", accounts.length);
  appendMetric(
    metrics,
    "可用 API Key",
    accounts.reduce((sum, account) => sum + account.available_keys, 0),
  );
  appendMetric(
    metrics,
    "可用 OAuth",
    accounts.reduce((sum, account) => sum + account.available_oauth_tokens, 0),
  );
  appendMetric(
    metrics,
    "异常账号",
    accounts.filter(
      (account) =>
        account.forbidden_cooldown > 0 ||
        account.upstream_cooldown > 0 ||
        account.credit_depleted,
    ).length,
  );

  const reload = payload.credential_reload || {};
  const reloadStrip = byId("reload-state");
  reloadStrip.dataset.state = reload.error ? "warning" : "ok";
  reloadStrip.textContent = reload.error
    ? `配置代次 ${reload.generation} · ${reload.error}`
    : `配置代次 ${reload.generation} · 最近成功 ${formatTime(reload.last_success_at)}`;

  const body = byId("accounts-status");
  clear(body);
  if (!accounts.length) {
    const empty = node("td", { className: "empty-row", text: "暂无已配置账号" });
    empty.colSpan = 7;
    const row = node("tr");
    row.append(empty);
    body.append(row);
  }
  for (const account of accounts) {
    const row = node("tr");
    const identity = node("td");
    identity.append(
      node("strong", { text: account.name || account.id }),
      node("span", { text: `ID ${account.id} · 权重 ${account.weight}` }),
    );

    const credentials = node("td");
    credentials.append(
      node("strong", {
        text: `Key ${account.available_keys}/${account.total_keys}`,
      }),
      node("span", {
        text: `OAuth ${account.available_oauth_tokens}/${account.total_oauth_tokens}`,
      }),
    );

    const quota = node("td");
    const creditEntries = account.quota && account.quota.credits
      ? Object.entries(account.quota.credits)
      : [];
    quota.append(
      node("strong", {
        text: creditEntries.length ? formatNumber(creditEntries[0][1]) : "—",
      }),
      node("span", {
        text: account.quota
          ? `${account.quota.stale ? "过期" : "已更新"} · HTTP ${account.quota.http_status}`
          : "暂无快照",
      }),
    );

    const rate = node("td");
    rate.append(
      node("strong", {
        text: `${formatNumber(account.rate_limit.requests_per_minute)} RPM`,
      }),
      node("span", {
        text: `令牌 ${formatNumber(account.rate_limit.available_tokens)}/${account.rate_limit.burst}`,
      }),
    );

    const cooldown = node("td");
    const maximumCooldown = Math.max(
      account.credit_cooldown,
      account.forbidden_cooldown,
      account.upstream_cooldown,
      ...Object.values(account.route_cooldowns || {}),
    );
    cooldown.append(
      node("strong", { text: maximumCooldown ? `${maximumCooldown}s` : "正常" }),
      node("span", {
        text: account.credit_depleted
          ? "额度耗尽"
          : `失败 ${account.upstream_failure_count}`,
      }),
    );

    const network = node("td");
    network.append(
      node("strong", {
        text: account.network.proxy_configured ? "固定代理" : "直连",
      }),
      node("span", { text: account.network.accept_language }),
    );

    const action = node("td", { className: "command-column" });
    const actionGroup = node("div", { className: "row-actions" });
    const writable = Boolean(
      state.config && state.config.capabilities.persistent_editing,
    );
    const management = account.management || {};
    const canEdit = typeof management.can_edit === "boolean"
      ? management.can_edit
      : writable;
    const editReason = canEdit
      ? null
      : accountManagementMessage(
        management.edit_reason || "persistent_editing_disabled",
      );
    const testButton = node("button", {
      className: "secondary",
      text: state.tests.get(account.id) || "测试",
      type: "button",
    });
    testButton.setAttribute("aria-label", `测试账号 ${account.id}`);
    testButton.addEventListener("click", () => testAccount(account.id, testButton));
    const editButton = node("button", {
      className: "secondary",
      text: "编辑",
      type: "button",
    });
    editButton.dataset.accountEdit = account.id;
    editButton.disabled = Boolean(state.deletingAccountId);
    editButton.title = canEdit ? "编辑已保存的账号配置" : editReason;
    editButton.setAttribute(
      "aria-label",
      canEdit ? `编辑账号 ${account.id}` : `${account.id}：${editReason}`,
    );
    editButton.addEventListener("click", () => {
      if (!canEdit) {
        showToast(editReason, true);
        return;
      }
      void editAccount(account.id);
    });
    const deleteButton = node("button", {
      className: "danger",
      text: "删除",
      type: "button",
    });
    deleteButton.dataset.accountDelete = "true";
    const deleteInProgress = Boolean(state.deletingAccountId);
    const fallbackDeleteReason = !writable
      ? "persistent_editing_disabled"
      : state.config?.routing?.configured_default_account === account.id
        ? "default_account_locked"
        : null;
    const canDelete = typeof management.can_delete === "boolean"
      ? management.can_delete
      : fallbackDeleteReason === null;
    const deleteBlockedReason = canDelete
      ? null
      : accountManagementMessage(management.delete_reason || fallbackDeleteReason);
    deleteButton.disabled = deleteInProgress;
    deleteButton.title = deleteBlockedReason || "删除账号及已保存凭据";
    deleteButton.textContent = state.deletingAccountId === account.id
      ? "删除中"
      : "删除";
    deleteButton.setAttribute(
      "aria-label",
      canDelete
        ? `删除账号 ${account.id}`
        : `删除账号 ${account.id}（${deleteButton.title}）`,
    );
    deleteButton.addEventListener("click", () => {
      if (!canDelete) {
        showToast(deleteBlockedReason, true);
        return;
      }
      deleteAccount(account.id, deleteButton);
    });
    actionGroup.append(testButton, editButton, deleteButton);
    action.append(actionGroup);

    row.append(identity, credentials, quota, rate, cooldown, network, action);
    body.append(row);
  }
  byId("status-updated").textContent = `刷新于 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
}

function proxyKeyMask(key) {
  if (typeof key.masked_key === "string" && key.masked_key) {
    return key.masked_key;
  }
  const prefix = typeof key.prefix === "string" && key.prefix ? key.prefix : "sk-";
  const suffix = typeof key.suffix === "string" && key.suffix ? key.suffix : "••••";
  return `${prefix}${"•".repeat(8)}${suffix}`;
}

function proxyKeyStatus(key) {
  const now = Date.now() / 1000;
  if (!key.enabled) {
    return { label: "已停用", state: "idle" };
  }
  if (key.expires_at && key.expires_at <= now) {
    return { label: "已到期", state: "warning" };
  }
  if (
    key.request_limit !== null &&
    key.request_limit !== undefined &&
    Number(key.requests_used || 0) >= Number(key.request_limit)
  ) {
    return { label: "已耗尽", state: "warning" };
  }
  return { label: "已启用", state: "ok" };
}

function renderProxyKeys() {
  const keys = state.proxyKeys || [];
  const enabledCount = keys.filter((key) => key.enabled).length;
  const requestsUsed = keys.reduce(
    (sum, key) => sum + Number(key.requests_used || 0),
    0,
  );
  const activeRequests = keys.reduce(
    (sum, key) => sum + Number(key.active_requests || 0),
    0,
  );
  byId("proxy-key-summary").textContent = `${keys.length} 个 Key · ${enabledCount} 个已启用`;

  const metrics = byId("proxy-key-metrics");
  clear(metrics);
  appendMetric(metrics, "Key 总数", keys.length);
  appendMetric(metrics, "已启用", enabledCount);
  appendMetric(metrics, "累计请求", formatNumber(requestsUsed));
  appendMetric(metrics, "当前并发", formatNumber(activeRequests));

  const body = byId("proxy-keys");
  clear(body);
  if (!keys.length) {
    const empty = node("td", { className: "empty-row", text: "暂无代理 Key" });
    empty.colSpan = 8;
    const row = node("tr");
    row.append(empty);
    body.append(row);
    return;
  }

  for (const key of keys) {
    const row = node("tr");
    const identity = node("td");
    identity.append(node("strong", { text: key.name || "未命名" }));
    if (key.kind === "primary") {
      identity.append(node("span", { className: "key-kind", text: "主 Key" }));
    }

    const masked = node("td");
    masked.append(node("code", { className: "masked-key", text: proxyKeyMask(key) }));

    const status = proxyKeyStatus(key);
    const enabled = node("td");
    const toggle = node("label", { className: "key-toggle" });
    const checkbox = node("input");
    checkbox.type = "checkbox";
    checkbox.checked = Boolean(key.enabled);
    checkbox.disabled = state.proxyKeyBusyIds.has(key.id);
    checkbox.setAttribute(
      "aria-label",
      `${checkbox.checked ? "停用" : "启用"}代理 Key ${key.name || key.id}`,
    );
    checkbox.addEventListener("change", () => {
      setProxyKeyEnabled(key, checkbox.checked);
    });
    const statusText = node("span", { text: status.label });
    statusText.dataset.state = status.state;
    toggle.append(checkbox, statusText);
    enabled.append(toggle);

    const usage = node("td");
    const requestLimit = key.request_limit === null || key.request_limit === undefined
      ? "不限"
      : formatNumber(key.request_limit);
    usage.append(
      node("strong", { text: `${formatNumber(key.requests_used || 0)} / ${requestLimit}` }),
    );

    const rpm = node("td");
    rpm.append(node("strong", {
      text: key.requests_per_minute === null || key.requests_per_minute === undefined
        ? "不限"
        : formatNumber(key.requests_per_minute),
    }));

    const concurrency = node("td");
    concurrency.append(node("strong", {
      text: `${formatNumber(key.active_requests || 0)} / ${formatNumber(key.max_concurrency)}`,
    }));

    const expiry = node("td");
    expiry.append(node("span", {
      text: key.expires_at ? formatTime(key.expires_at) : "永不过期",
    }));

    const action = node("td", { className: "command-column" });
    const actionGroup = node("div", { className: "row-actions" });
    const edit = node("button", { className: "secondary", text: "编辑", type: "button" });
    const reset = node("button", { className: "secondary", text: "重置用量", type: "button" });
    const remove = node("button", { className: "danger", text: "删除", type: "button" });
    const busy = state.proxyKeyBusyIds.has(key.id);
    edit.disabled = busy;
    reset.disabled = busy;
    remove.disabled = busy || key.kind === "primary";
    remove.title = key.kind === "primary"
      ? "默认代理 Key 为系统保留，不可删除"
      : "删除代理 Key";
    edit.setAttribute("aria-label", `编辑代理 Key ${key.name || key.id}`);
    reset.setAttribute("aria-label", `重置代理 Key ${key.name || key.id} 的用量`);
    remove.setAttribute("aria-label", `删除代理 Key ${key.name || key.id}`);
    if (key.kind === "primary") {
      remove.setAttribute("aria-label", "默认代理 Key 为系统保留，不可删除");
    }
    edit.addEventListener("click", () => openProxyKeyEditor(key));
    reset.addEventListener("click", () => resetProxyKeyUsage(key));
    remove.addEventListener("click", () => deleteProxyKey(key));
    actionGroup.append(edit, reset, remove);
    action.append(actionGroup);

    row.append(identity, masked, enabled, usage, rpm, concurrency, expiry, action);
    body.append(row);
  }
}

function unixTimeToLocalInput(epochSeconds) {
  if (!epochSeconds) {
    return "";
  }
  const date = new Date(Number(epochSeconds) * 1000);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000)
    .toISOString()
    .slice(0, 16);
}

function localInputToUnixTime(value) {
  if (!value) {
    return null;
  }
  const milliseconds = new Date(value).getTime();
  return Number.isNaN(milliseconds) ? null : Math.floor(milliseconds / 1000);
}

function nullablePositiveNumber(inputId) {
  const value = byId(inputId).value.trim();
  return value ? Number(value) : null;
}

function openProxyKeyEditor(key = null) {
  state.proxyKeyEditingId = key ? key.id : null;
  byId("proxy-key-editor-title").textContent = key ? "编辑代理 Key" : "创建代理 Key";
  byId("proxy-key-editor-kind").textContent = key && key.kind === "primary"
    ? "主 Key"
    : key
      ? "托管 Key"
      : "新 Key";
  byId("save-proxy-key").textContent = key ? "保存" : "创建";
  byId("proxy-key-name").value = key ? key.name || "" : "";
  byId("proxy-key-concurrency").value = key ? key.max_concurrency : 8;
  byId("proxy-key-request-limit").value = key && key.request_limit !== null
    && key.request_limit !== undefined ? key.request_limit : "";
  byId("proxy-key-rpm").value = key && key.requests_per_minute !== null
    && key.requests_per_minute !== undefined ? key.requests_per_minute : "";
  byId("proxy-key-expires").value = key
    ? unixTimeToLocalInput(key.expires_at)
    : "";
  byId("proxy-key-enabled").checked = key ? Boolean(key.enabled) : true;
  const dialog = byId("proxy-key-editor");
  if (!dialog.open) {
    dialog.showModal();
  }
  byId("proxy-key-name").focus();
}

function closeProxyKeyEditor() {
  state.proxyKeyEditingId = null;
  const dialog = byId("proxy-key-editor");
  if (dialog.open) {
    dialog.close();
  }
}

function proxyKeyPayload() {
  return {
    name: byId("proxy-key-name").value.trim(),
    enabled: byId("proxy-key-enabled").checked,
    request_limit: nullablePositiveNumber("proxy-key-request-limit"),
    requests_per_minute: nullablePositiveNumber("proxy-key-rpm"),
    max_concurrency: Number(byId("proxy-key-concurrency").value),
    expires_at: localInputToUnixTime(byId("proxy-key-expires").value),
  };
}

function proxyKeyErrorMessage(code) {
  return {
    proxy_access_key_not_found: "代理 Key 已不存在，请刷新列表",
    primary_proxy_access_key_required: "默认代理 Key 为系统保留，不可删除",
    empty_proxy_access_key_update: "没有需要保存的修改",
    invalid_proxy_access_key: "代理 Key 配置无效，请检查限制值",
  }[code] || code;
}

function upsertProxyKey(key) {
  const index = state.proxyKeys.findIndex((candidate) => candidate.id === key.id);
  if (index < 0) {
    state.proxyKeys.push(key);
  } else {
    state.proxyKeys[index] = key;
  }
}

async function saveProxyKey() {
  const form = byId("proxy-key-form");
  if (!form.reportValidity()) {
    return;
  }
  const button = byId("save-proxy-key");
  setBusy(button, true);
  const editingId = state.proxyKeyEditingId;
  try {
    const result = await requestJson(
      editingId
        ? `/admin/v1/proxy-keys/${encodeURIComponent(editingId)}`
        : "/admin/v1/proxy-keys",
      {
        method: editingId ? "PATCH" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(proxyKeyPayload()),
      },
    );
    upsertProxyKey(result.key);
    closeProxyKeyEditor();
    renderProxyKeys();
    if (!editingId && typeof result.secret === "string" && result.secret) {
      showCreatedProxySecret(result.secret);
    } else {
      showToast(editingId ? "代理 Key 已保存" : "代理 Key 已创建");
    }
  } catch (error) {
    showToast(proxyKeyErrorMessage(error.message), true);
  } finally {
    setBusy(button, false);
  }
}

async function refreshProxyKeys() {
  const button = byId("refresh-proxy-keys");
  setBusy(button, true);
  try {
    const result = await requestJson("/admin/v1/proxy-keys");
    state.proxyKeys = Array.isArray(result.keys) ? result.keys : [];
    renderProxyKeys();
    showToast("代理 Key 已刷新");
  } catch (error) {
    showToast(proxyKeyErrorMessage(error.message), true);
  } finally {
    setBusy(button, false);
  }
}

async function updateProxyKey(key, payload, successMessage) {
  state.proxyKeyBusyIds.add(key.id);
  renderProxyKeys();
  try {
    const result = await requestJson(
      `/admin/v1/proxy-keys/${encodeURIComponent(key.id)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    );
    upsertProxyKey(result.key);
    showToast(successMessage);
  } catch (error) {
    showToast(proxyKeyErrorMessage(error.message), true);
  } finally {
    state.proxyKeyBusyIds.delete(key.id);
    renderProxyKeys();
  }
}

async function setProxyKeyEnabled(key, enabled) {
  await updateProxyKey(key, { enabled }, enabled ? "代理 Key 已启用" : "代理 Key 已停用");
}

async function resetProxyKeyUsage(key) {
  if (!window.confirm(`确认重置“${key.name || key.id}”的累计用量？`)) {
    return;
  }
  state.proxyKeyBusyIds.add(key.id);
  renderProxyKeys();
  try {
    const result = await requestJson(
      `/admin/v1/proxy-keys/${encodeURIComponent(key.id)}/reset-usage`,
      { method: "POST" },
    );
    upsertProxyKey(result.key);
    showToast("用量已重置");
  } catch (error) {
    showToast(proxyKeyErrorMessage(error.message), true);
  } finally {
    state.proxyKeyBusyIds.delete(key.id);
    renderProxyKeys();
  }
}

async function deleteProxyKey(key) {
  if (key.kind === "primary") {
    showToast("默认代理 Key 为系统保留，不可删除", true);
    return;
  }
  if (!window.confirm(`确认删除代理 Key“${key.name || key.id}”？`)) {
    return;
  }
  state.proxyKeyBusyIds.add(key.id);
  renderProxyKeys();
  try {
    await requestJson(`/admin/v1/proxy-keys/${encodeURIComponent(key.id)}`, {
      method: "DELETE",
    });
    state.proxyKeys = state.proxyKeys.filter((candidate) => candidate.id !== key.id);
    showToast("代理 Key 已删除");
  } catch (error) {
    showToast(proxyKeyErrorMessage(error.message), true);
  } finally {
    state.proxyKeyBusyIds.delete(key.id);
    renderProxyKeys();
  }
}

function showCreatedProxySecret(secret) {
  state.createdProxySecret = secret;
  byId("created-proxy-key").textContent = secret;
  const dialog = byId("proxy-key-secret-dialog");
  if (!dialog.open) {
    dialog.showModal();
  }
}

function clearCreatedProxySecret() {
  state.createdProxySecret = "";
  const display = byId("created-proxy-key");
  if (display) {
    display.textContent = "";
  }
}

function closeCreatedProxySecret() {
  const dialog = byId("proxy-key-secret-dialog");
  if (dialog.open) {
    dialog.close();
  }
  clearCreatedProxySecret();
}

async function copyCreatedProxySecret() {
  if (!state.createdProxySecret) {
    return;
  }
  const copied = await copyText(state.createdProxySecret);
  showToast(copied ? "代理 API Key 已复制" : "复制失败，请检查剪贴板权限", !copied);
}

function makeLabel(text, input) {
  const label = node("label", { text });
  label.append(input);
  return label;
}

function makeInput(type, value, onInput, options = {}) {
  const input = node("input");
  input.type = type;
  input.value = value === undefined || value === null ? "" : String(value);
  input.required = Boolean(options.required);
  input.readOnly = Boolean(options.readOnly);
  if (options.min !== undefined) {
    input.min = String(options.min);
  }
  if (options.max !== undefined) {
    input.max = String(options.max);
  }
  if (options.step !== undefined) {
    input.step = String(options.step);
  }
  if (options.maxLength !== undefined) {
    input.maxLength = Number(options.maxLength);
  }
  input.addEventListener("input", () => onInput(input.value));
  return input;
}

const CONNECTION_LANGUAGES = [
  "zh-CN,zh;q=0.9,en;q=0.8",
  "zh-CN,zh;q=0.9",
  "en-US,en;q=0.9,zh-CN;q=0.8",
];

function createConnectionProfile() {
  if (!window.crypto || typeof window.crypto.getRandomValues !== "function") {
    throw new Error("浏览器随机数功能不可用");
  }
  const bytes = new Uint8Array(16);
  window.crypto.getRandomValues(bytes);
  const profileId = [...bytes]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
  return {
    user_agent: `qveris-account-proxy/0.1.0 profile/${profileId}`,
    accept_language: CONNECTION_LANGUAGES[bytes[0] % CONNECTION_LANGUAGES.length],
  };
}

function nextAccountIdentity() {
  const accounts = state.draft && Array.isArray(state.draft.accounts)
    ? state.draft.accounts
    : [];
  const ids = new Set(accounts.map((account) => account.id));
  let index = 1;
  while (ids.has(`account-${index}`)) {
    index += 1;
  }
  return { id: `account-${index}`, name: `账号 ${accounts.length + 1}` };
}

function connectionProfileLabel(userAgent) {
  const match = /profile\/([a-f0-9]{32})$/.exec(userAgent || "");
  return match ? `标识 ${match[1].slice(-8)}` : "自定义标识";
}

function renderConfig() {
  if (!state.draft) {
    return;
  }
  const writable = Boolean(state.config.capabilities.persistent_editing);
  byId("config-mode").textContent = writable ? "持久编辑已启用" : "持久编辑未启用";
  byId("add-account").disabled = !writable || Boolean(state.deletingAccountId);
  byId("save-config").disabled = !writable || Boolean(state.deletingAccountId);
  const summary = byId("config-summary");
  summary.dataset.state = writable ? "ok" : "warning";
  summary.textContent = `${state.draft.accounts.length} 个账号 · ${state.config.routing.mode} · 默认账号 ${state.config.routing.default_account || "未设置"}`;

  const container = byId("account-editors");
  clear(container);
  state.draft.accounts.forEach((account, accountIndex) => {
    container.append(renderAccountEditor(account, accountIndex));
  });
  renderConsoleAccounts();
}

function renderAccountEditor(account, accountIndex) {
  const editor = node("section", { className: "account-editor" });
  editor.dataset.accountId = account.id;
  const header = node("div", { className: "account-editor-header" });
  const accountHeading = node("strong", {
    text: account.name || account.id || "新账号",
  });
  header.append(accountHeading);
  const remove = node("button", {
    className: "danger",
    text: "删除账号",
    type: "button",
  });
  const writable = Boolean(state.config.capabilities.persistent_editing);
  remove.dataset.accountDelete = "true";
  const deleteInProgress = Boolean(state.deletingAccountId);
  const management = account.persisted ? managementForAccount(account.id) : null;
  const fallbackDeleteReason = !writable
    ? "persistent_editing_disabled"
    : state.config.routing?.configured_default_account === account.id
      ? "default_account_locked"
      : null;
  const canDelete = !account.persisted || (management
    ? management.can_delete
    : fallbackDeleteReason === null);
  const deleteBlockedReason = canDelete
    ? null
    : accountManagementMessage(management?.delete_reason || fallbackDeleteReason);
  remove.disabled = deleteInProgress;
  remove.title = deleteBlockedReason || (account.persisted
    ? "删除账号及已保存凭据"
    : "删除未保存账号");
  remove.textContent = state.deletingAccountId === account.id
    ? "删除中"
    : "删除账号";
  remove.setAttribute(
    "aria-label",
    canDelete
      ? `删除账号 ${account.id || "新账号"}`
      : `删除账号 ${account.id}（${remove.title}）`,
  );
  remove.addEventListener("click", () => {
    if (account.persisted) {
      if (!canDelete) {
        showToast(deleteBlockedReason, true);
        return;
      }
      deleteAccount(account.id, remove);
      return;
    }
    state.draft.accounts.splice(accountIndex, 1);
    renderConfig();
  });
  header.append(remove);

  const fields = node("div", { className: "account-fields" });
  fields.append(
    makeLabel(
      "账号名称",
      makeInput("text", account.name || account.id, (value) => {
        account.name = value;
        accountHeading.textContent = value.trim() || account.id || "新账号";
      }, { required: true, maxLength: 64 }),
    ),
    makeLabel(
      "内部 ID",
      makeInput("text", account.id, () => {}, { required: true, readOnly: true }),
    ),
    makeLabel(
      "权重",
      makeInput("number", account.weight, (value) => {
        account.weight = Number(value);
      }, { min: 1, max: 100, required: true }),
    ),
    makeLabel(
      "RPM",
      makeInput("number", account.requests_per_minute, (value) => {
        account.requests_per_minute = Number(value);
      }, { min: 1, max: 10000, step: 0.1, required: true }),
    ),
    makeLabel(
      "突发",
      makeInput("number", account.burst, (value) => {
        account.burst = Number(value);
      }, { min: 1, max: 10000, required: true }),
    ),
  );

  const transportSection = node("section", {
    className: "connection-profile-section",
  });
  const transportHeading = node("div", { className: "connection-profile-heading" });
  const transportIdentity = node("div");
  const transportIdentityState = node("span", {
    text: connectionProfileLabel(account.transport.user_agent),
  });
  transportIdentity.append(
    node("strong", { text: "稳定连接标识" }),
    transportIdentityState,
  );
  const regenerateProfile = node("button", {
    className: "secondary",
    text: "重新生成",
    type: "button",
  });
  regenerateProfile.disabled = !writable;
  regenerateProfile.title = "生成并固定新的 User-Agent 和语言";
  regenerateProfile.setAttribute(
    "aria-label",
    `重新生成账号 ${account.id || "新账号"} 的稳定连接标识`,
  );
  regenerateProfile.addEventListener("click", () => {
    try {
      Object.assign(account.transport, createConnectionProfile());
      renderConfig();
      showToast("连接标识已生成，保存后生效");
    } catch (error) {
      showToast(error.message, true);
    }
  });
  transportHeading.append(transportIdentity, regenerateProfile);
  const transport = node("div", { className: "transport-fields" });
  transport.append(
    makeLabel(
      "User-Agent",
      makeInput("text", account.transport.user_agent, (value) => {
        account.transport.user_agent = value;
        transportIdentityState.textContent = connectionProfileLabel(value);
      }, { required: true }),
    ),
    makeLabel(
      "Accept-Language",
      makeInput("text", account.transport.accept_language, (value) => {
        account.transport.accept_language = value;
      }, { required: true }),
    ),
    makeLabel(
      "代理 URL 文件",
      makeInput("text", account.transport.proxy_url_file || "", (value) => {
        account.transport.proxy_url_file = value || null;
      }),
    ),
  );
  transportSection.append(transportHeading, transport);

  const credentials = node("div", { className: "credentials" });
  credentials.append(
    renderCredentials(account, "keys", "API Key", "Key"),
    renderCredentials(account, "oauth_tokens", "OAuth Token", "OAuth"),
  );
  editor.append(header, fields, transportSection, credentials);
  return editor;
}

function renderCredentials(account, field, heading, buttonLabel) {
  const section = node("section", { className: "credential-section" });
  const title = node("div", { className: "credential-heading" });
  title.append(node("h2", { text: heading }));
  const add = node("button", {
    className: "secondary",
    text: `添加 ${buttonLabel}`,
    type: "button",
  });
  add.disabled = !state.config.capabilities.persistent_editing;
  add.setAttribute(
    "aria-label",
    `为账号 ${account.id || "新账号"} 添加 ${heading}`,
  );
  add.addEventListener("click", () => {
    account[field].push({
      id: account[field].length ? `credential-${account[field].length + 1}` : "primary",
      configured: false,
      value: "",
    });
    renderConfig();
  });
  title.append(add);
  section.append(title);

  const list = node("div", { className: "credential-list" });
  if (!account[field].length) {
    list.append(node("div", { className: "empty-row", text: "未配置" }));
  }
  account[field].forEach((credential, credentialIndex) => {
    const row = node("div", { className: "credential-row" });
    row.append(
      makeLabel(
        "标识",
        makeInput("text", credential.id, (value) => {
          credential.id = value;
        }, { required: true, readOnly: credential.configured }),
      ),
      makeLabel(
        credential.configured ? "新值" : "凭据值",
        makeInput("password", credential.value || "", (value) => {
          credential.value = value;
        }, { required: !credential.configured }),
      ),
    );
    const remove = node("button", {
      className: "danger",
      text: "删除",
      type: "button",
    });
    remove.disabled = !state.config.capabilities.persistent_editing;
    remove.setAttribute(
      "aria-label",
      `删除账号 ${account.id || "新账号"} 的 ${heading} ${credential.id || credentialIndex + 1}`,
    );
    remove.addEventListener("click", () => {
      account[field].splice(credentialIndex, 1);
      renderConfig();
    });
    row.append(remove);
    list.append(row);
  });
  section.append(list);
  return section;
}

function configPayload() {
  return {
    revision: state.config?.revision ?? null,
    accounts: state.draft.accounts.map((account) => ({
      id: account.id,
      name: account.name,
      weight: account.weight,
      requests_per_minute: account.requests_per_minute,
      burst: account.burst,
      transport: {
        user_agent: account.transport.user_agent,
        accept_language: account.transport.accept_language,
        proxy_url_file: account.transport.proxy_url_file || null,
      },
      keys: account.keys.map((credential) => ({
        id: credential.id,
        value: credential.value || null,
      })),
      oauth_tokens: account.oauth_tokens.map((credential) => ({
        id: credential.id,
        value: credential.value || null,
      })),
    })),
  };
}

function draftFingerprint() {
  return state.draft ? JSON.stringify(configPayload()) : "";
}

function rememberDraftBaseline() {
  state.draftBaseline = draftFingerprint();
}

function configDraftIsDirty() {
  return Boolean(state.draft && draftFingerprint() !== state.draftBaseline);
}

async function refreshStatus() {
  const button = byId("refresh-status");
  setBusy(button, true);
  try {
    state.status = await requestJson("/admin/v1/accounts");
    renderStatus();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setBusy(button, false);
  }
}

async function reloadAccounts() {
  const button = byId("reload-accounts");
  setBusy(button, true);
  try {
    await requestJson("/admin/v1/reload-accounts", { method: "POST" });
    const [status, config] = await Promise.all([
      requestJson("/admin/v1/accounts"),
      requestJson("/admin/v1/config"),
    ]);
    state.status = status;
    state.config = config;
    state.draft = structuredClone(config);
    markPersistedAccounts();
    rememberDraftBaseline();
    renderAll();
    showToast("配置已重载");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setBusy(button, false);
  }
}

async function validateConfig() {
  if (!byId("config-form").reportValidity()) {
    return;
  }
  const button = byId("validate-config");
  setBusy(button, true);
  try {
    const result = await requestJson("/admin/v1/config/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(configPayload()),
    });
    const summary = byId("config-summary");
    summary.dataset.state = "ok";
    summary.textContent = `验证通过 · ${result.account_count} 个账号 · ${result.api_key_count} 个 Key · ${result.oauth_token_count} 个 OAuth`;
    showToast("配置验证通过");
  } catch (error) {
    byId("config-summary").dataset.state = "error";
    byId("config-summary").textContent = error.message;
    showToast(error.message, true);
  } finally {
    setBusy(button, false);
  }
}

async function saveConfig() {
  if (!byId("config-form").reportValidity()) {
    return;
  }
  const button = byId("save-config");
  setBusy(button, true);
  try {
    const result = await requestJson("/admin/v1/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(configPayload()),
    });
    state.config = await requestJson("/admin/v1/config");
    state.draft = structuredClone(state.config);
    markPersistedAccounts();
    rememberDraftBaseline();
    state.status = await requestJson("/admin/v1/accounts");
    renderAll();
    showToast(`配置已保存 · 代次 ${result.reload.generation}`);
  } catch (error) {
    showToast(
      error.message === "config_revision_conflict"
        ? "服务端账号配置已更新，请载入最新配置后重新修改"
        : error.message,
      true,
    );
  } finally {
    setBusy(button, false);
  }
}

function deleteErrorMessage(code) {
  return {
    account_not_found: "账号已不存在，请刷新状态",
    default_account_locked: "该账号是显式默认账号，请先修改 QVP_DEFAULT_ACCOUNT",
    persistent_editing_disabled: "持久编辑未启用",
    accounts_file_unavailable: "账号配置文件不可用，删除未执行",
    apply_failed: "删除未完成，原账号配置已恢复",
    apply_and_rollback_failed: "删除失败且回滚异常，请重载配置后检查",
  }[code] || code;
}

function setAccountDeleteUiBusy(busy) {
  if (!busy) {
    return;
  }
  for (const candidate of document.querySelectorAll(
    "#config-form input, #config-form button, #config-form select, [data-account-delete]",
  )) {
    candidate.disabled = true;
  }
}

function removeDeletedAccountLocally(accountId) {
  if (state.status && Array.isArray(state.status.accounts)) {
    state.status.accounts = state.status.accounts.filter(
      (account) => account.id !== accountId,
    );
  }
  if (state.config && Array.isArray(state.config.accounts)) {
    state.config.accounts = state.config.accounts.filter(
      (account) => account.id !== accountId,
    );
    if (
      state.config.routing &&
      !state.config.routing.configured_default_account
    ) {
      const hasDynamicDefault = state.config.accounts.length === 1 ||
        state.config.routing.mode === "round_robin";
      state.config.routing.default_account = hasDynamicDefault &&
        state.config.accounts.length
        ? state.config.accounts[0].id
        : null;
    }
  }
  if (state.draft && Array.isArray(state.draft.accounts)) {
    state.draft.accounts = state.draft.accounts.filter(
      (account) => !(account.persisted && account.id === accountId),
    );
  }
  state.tests.delete(accountId);
}

async function deleteAccount(accountId, button) {
  if (state.deletingAccountId) {
    showToast(`账号 ${state.deletingAccountId} 正在删除，请稍候`, true);
    return;
  }
  if (configDraftIsDirty()) {
    showToast("请先保存或载入最新配置，再删除账号", true);
    return;
  }
  const confirmed = window.confirm(
    `确认删除账号“${accountId}”及其已保存凭据？此操作立即生效。`,
  );
  if (!confirmed) {
    return;
  }

  state.deletingAccountId = accountId;
  setAccountDeleteUiBusy(true);
  setBusy(button, true);
  let deleteApplied = false;
  try {
    await requestJson(`/admin/v1/accounts/${encodeURIComponent(accountId)}`, {
      method: "DELETE",
    });
    deleteApplied = true;
    removeDeletedAccountLocally(accountId);

    const [statusResult, configResult] = await Promise.allSettled([
      requestJson("/admin/v1/accounts"),
      requestJson("/admin/v1/config"),
    ]);
    const failedRefreshes = [];
    if (statusResult.status === "fulfilled") {
      state.status = statusResult.value;
    } else {
      failedRefreshes.push("运行状态");
    }
    if (configResult.status === "fulfilled") {
      state.config = configResult.value;
      state.draft = structuredClone(configResult.value);
      markPersistedAccounts();
      rememberDraftBaseline();
    } else {
      failedRefreshes.push("账号配置");
    }
    renderAll();
    showToast(
      failedRefreshes.length
        ? `${accountId} 已删除；${failedRefreshes.join("和")}刷新失败，请手动刷新`
        : `${accountId} 已删除`,
      failedRefreshes.length > 0,
    );
  } catch (error) {
    if (deleteApplied) {
      removeDeletedAccountLocally(accountId);
      renderAll();
      showToast(`${accountId} 已删除；页面刷新失败，请手动刷新`, true);
    } else {
      showToast(deleteErrorMessage(error.message), true);
    }
  } finally {
    state.deletingAccountId = null;
    setBusy(button, false);
    renderStatus();
    renderConfig();
  }
}

function markPersistedAccounts() {
  if (!state.draft) {
    return;
  }
  for (const account of state.draft.accounts) {
    account.persisted = true;
  }
}

async function testAccount(accountId, button) {
  setBusy(button, true);
  state.tests.set(accountId, "测试中");
  button.textContent = "测试中";
  try {
    const result = await requestJson(
      `/admin/v1/accounts/${encodeURIComponent(accountId)}/test`,
      { method: "POST" },
    );
    const label = result.ok ? "通过" : "异常";
    const latency = Math.max(...result.checks.map((check) => check.latency_ms));
    state.tests.set(accountId, `${label} ${formatNumber(latency)}ms`);
    showToast(`${accountId} · ${label}`, !result.ok);
    state.status = await requestJson("/admin/v1/accounts");
    renderStatus();
  } catch (error) {
    state.tests.set(accountId, "失败");
    showToast(error.message, true);
    renderStatus();
  }
}

async function testGateway() {
  const button = byId("test-gateway");
  setBusy(button, true);
  const started = performance.now();
  try {
    const [live, meta] = await Promise.all([
      fetch("/health/live"),
      fetch("/api/v1/meta"),
    ]);
    if (!live.ok || !meta.ok) {
      throw new Error(`HTTP ${live.status}/${meta.status}`);
    }
    setGateway("ok", `${Math.round(performance.now() - started)}ms`);
    showToast("网关测试通过");
  } catch (error) {
    setGateway("error", "网关异常");
    showToast(error.message, true);
  } finally {
    setBusy(button, false);
  }
}

function renderConsole() {
  const operationSelect = byId("operation");
  clear(operationSelect);
  state.operations.forEach((operation, index) => {
    const option = node("option", {
      text: `${operation.method} /api/v1/${operation.path}`,
    });
    option.value = String(index);
    operationSelect.append(option);
  });
  renderConsoleAccounts();
  renderOperationFields();
}

function renderConsoleAccounts() {
  const select = byId("console-account");
  const selected = select.value;
  clear(select);
  const automatic = node("option", { text: "自动路由" });
  automatic.value = "";
  select.append(automatic);
  const accounts = state.config ? state.config.accounts : [];
  for (const account of accounts) {
    const option = node("option", {
      text: account.name && account.name !== account.id
        ? `${account.name} (${account.id})`
        : account.id,
    });
    option.value = account.id;
    select.append(option);
  }
  if ([...select.options].some((option) => option.value === selected)) {
    select.value = selected;
  }
}

function selectedOperation() {
  const index = Number(byId("operation").value);
  return state.operations[index] || null;
}

function renderOperationFields() {
  const operation = selectedOperation();
  const parameters = byId("path-parameters");
  clear(parameters);
  if (!operation) {
    return;
  }
  const matches = [...operation.path.matchAll(/\{([^}]+)\}/g)];
  for (const match of matches) {
    const input = makeInput("text", "", () => {}, { required: true });
    input.dataset.parameter = match[1];
    parameters.append(makeLabel(match[1], input));
  }
  const hasBody = operation.method !== "GET";
  byId("body-field").hidden = !hasBody;
  byId("request-body").disabled = !hasBody;
  byId("billing-confirm").hidden = !operation.credit_sensitive;
  byId("confirm-billing").checked = false;
}

async function sendConsoleRequest() {
  const form = byId("console-form");
  if (!form.reportValidity()) {
    return;
  }
  const operation = selectedOperation();
  if (!operation) {
    return;
  }
  if (operation.credit_sensitive && !byId("confirm-billing").checked) {
    showToast("请确认计费请求", true);
    return;
  }

  let path = operation.path;
  for (const input of byId("path-parameters").querySelectorAll("input")) {
    path = path.replace(`{${input.dataset.parameter}}`, encodeURIComponent(input.value));
  }
  const query = new URLSearchParams(byId("query-params").value);
  const queryText = query.toString();
  const url = `/api/v1/${path}${queryText ? `?${queryText}` : ""}`;
  const headers = authHeaders();
  const account = byId("console-account").value;
  if (account) {
    headers["X-QVeris-Account"] = account;
  }
  const options = { method: operation.method, headers };
  if (operation.method !== "GET") {
    try {
      JSON.parse(byId("request-body").value);
    } catch {
      showToast("JSON 请求体格式错误", true);
      return;
    }
    headers["Content-Type"] = "application/json";
    options.body = byId("request-body").value;
  }

  const button = byId("send-request");
  setBusy(button, true);
  const started = performance.now();
  try {
    const response = await fetch(url, options);
    const text = await response.text();
    const elapsed = Math.round(performance.now() - started);
    const accountHeader = response.headers.get("x-qveris-proxy-account");
    byId("response-meta").textContent = `HTTP ${response.status} · ${elapsed}ms${accountHeader ? ` · ${accountHeader}` : ""}`;
    let output = text;
    try {
      output = JSON.stringify(JSON.parse(text), null, 2);
    } catch {
      output = text;
    }
    byId("response-output").textContent = output.slice(0, 100000);
  } catch (error) {
    byId("response-meta").textContent = "请求失败";
    byId("response-output").textContent = error.message;
  } finally {
    setBusy(button, false);
  }
}

byId("auth-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const token = byId("access-token").value.trim();
  if (token) {
    connect(token);
  }
});

byId("disconnect").addEventListener("click", disconnect);
byId("copy-api-key").addEventListener("click", copyApiKey);
byId("copy-base-url").addEventListener("click", copyBaseUrl);
byId("copy-connection").addEventListener("click", copyConnection);
byId("toggle-api-key").addEventListener("click", () => {
  setApiKeyVisibility(!state.apiKeyVisible);
});
byId("toggle-manual-key").addEventListener("click", () => {
  setManualKeyVisibility(byId("access-token").type === "password");
});
byId("refresh-status").addEventListener("click", refreshStatus);
byId("reload-accounts").addEventListener("click", reloadAccounts);
byId("test-gateway").addEventListener("click", testGateway);
byId("refresh-proxy-keys").addEventListener("click", refreshProxyKeys);
byId("create-proxy-key").addEventListener("click", () => openProxyKeyEditor());
byId("close-proxy-key-editor").addEventListener("click", closeProxyKeyEditor);
byId("cancel-proxy-key-editor").addEventListener("click", closeProxyKeyEditor);
byId("proxy-key-form").addEventListener("submit", (event) => {
  event.preventDefault();
  saveProxyKey();
});
byId("proxy-key-editor").addEventListener("close", () => {
  state.proxyKeyEditingId = null;
});
byId("copy-created-proxy-key").addEventListener("click", copyCreatedProxySecret);
byId("close-proxy-key-secret").addEventListener("click", closeCreatedProxySecret);
byId("proxy-key-secret-dialog").addEventListener("close", clearCreatedProxySecret);
byId("validate-config").addEventListener("click", validateConfig);
byId("save-config").addEventListener("click", saveConfig);
byId("add-account").addEventListener("click", () => {
  let connectionProfile;
  try {
    connectionProfile = createConnectionProfile();
  } catch (error) {
    showToast(error.message, true);
    return;
  }
  const identity = nextAccountIdentity();
  state.draft.accounts.push({
    id: identity.id,
    name: identity.name,
    weight: 1,
    requests_per_minute: 10,
    burst: 10,
    transport: {
      ...connectionProfile,
      proxy_url_file: null,
    },
    keys: [{ id: "primary", configured: false, value: "" }],
    oauth_tokens: [],
    persisted: false,
  });
  renderConfig();
});
byId("operation").addEventListener("change", renderOperationFields);
byId("console-form").addEventListener("submit", (event) => {
  event.preventDefault();
  sendConsoleRequest();
});
byId("clear-response").addEventListener("click", () => {
  byId("response-output").textContent = "";
  byId("response-meta").textContent = "等待请求";
});

for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => {
    activateTab(tab.dataset.tab);
  });
}

setGateway("idle", "未连接");
bootstrap();
