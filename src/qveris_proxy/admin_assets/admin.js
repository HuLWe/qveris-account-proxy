"use strict";

const SESSION_TOKEN_KEY = "qveris-proxy.admin-token.v1";
const BOOTSTRAP_TICKET_FRAGMENT_KEY = "bootstrap_ticket";

const state = {
  token: "",
  status: null,
  config: null,
  draft: null,
  operations: [],
  tests: new Map(),
  apiKeyVisible: false,
  deletingAccountId: null,
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
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
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
    const [status, config, catalog] = await Promise.all([
      requestJson("/admin/v1/accounts"),
      requestJson("/admin/v1/config"),
      requestJson("/admin/v1/operations"),
    ]);
    state.status = status;
    state.config = config;
    state.draft = structuredClone(config);
    state.operations = catalog.operations;
    markPersistedAccounts();
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
  state.operations = [];
  state.tests.clear();
  state.deletingAccountId = null;
  byId("workspace").hidden = true;
  byId("locked-state").hidden = false;
  byId("topbar-actions").hidden = true;
  byId("disconnect").hidden = true;
  byId("access-token").value = "";
  setManualKeyVisibility(false);
  setApiKeyVisibility(false);
}

function disconnect() {
  clearSessionToken();
  state.token = "";
  resetWorkspace();
  setGateway("idle", "未连接");
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
  renderConfig();
  renderConsole();
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
  for (const account of accounts) {
    const row = node("tr");
    const identity = node("td");
    identity.append(
      node("strong", { text: account.id }),
      node("span", { text: `权重 ${account.weight}` }),
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
    const testButton = node("button", {
      className: "secondary",
      text: state.tests.get(account.id) || "测试",
      type: "button",
    });
    testButton.setAttribute("aria-label", `测试账号 ${account.id}`);
    testButton.addEventListener("click", () => testAccount(account.id, testButton));
    const deleteButton = node("button", {
      className: "danger",
      text: "删除",
      type: "button",
    });
    const writable = Boolean(
      state.config && state.config.capabilities.persistent_editing,
    );
    deleteButton.dataset.accountDelete = "true";
    const deleteInProgress = Boolean(state.deletingAccountId);
    const explicitDefault = Boolean(
      state.config &&
      state.config.routing &&
      state.config.routing.configured_default_account === account.id,
    );
    const deleteBlockedReason = !writable
      ? "持久编辑未启用"
      : accounts.length <= 1
        ? "至少保留一个账号"
        : explicitDefault
          ? "显式默认账号，请先修改 QVP_DEFAULT_ACCOUNT"
          : "删除账号及已保存凭据";
    deleteButton.disabled =
      deleteInProgress || deleteBlockedReason !== "删除账号及已保存凭据";
    deleteButton.title = deleteBlockedReason;
    deleteButton.textContent = !writable
      ? "只读"
      : accounts.length <= 1
        ? "保留"
        : explicitDefault
          ? "默认账号"
          : state.deletingAccountId === account.id
            ? "删除中"
            : "删除";
    deleteButton.setAttribute(
      "aria-label",
      deleteButton.disabled && !deleteInProgress
        ? `${account.id}：${deleteButton.title}`
        : `删除账号 ${account.id}`,
    );
    deleteButton.addEventListener("click", () =>
      deleteAccount(account.id, deleteButton));
    actionGroup.append(testButton, deleteButton);
    action.append(actionGroup);

    row.append(identity, credentials, quota, rate, cooldown, network, action);
    body.append(row);
  }
  byId("status-updated").textContent = `刷新于 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
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
  const header = node("div", { className: "account-editor-header" });
  header.append(node("strong", { text: account.id || "新账号" }));
  const remove = node("button", {
    className: "danger",
    text: "删除账号",
    type: "button",
  });
  const writable = Boolean(state.config.capabilities.persistent_editing);
  remove.dataset.accountDelete = "true";
  const deleteInProgress = Boolean(state.deletingAccountId);
  const explicitDefault = Boolean(
    account.persisted &&
    state.config.routing &&
    state.config.routing.configured_default_account === account.id,
  );
  const deleteBlockedReason = !writable
    ? "持久编辑未启用"
    : account.persisted && state.config.accounts.length <= 1
      ? "至少保留一个账号"
      : explicitDefault
        ? "显式默认账号，请先修改 QVP_DEFAULT_ACCOUNT"
        : "删除账号及已保存凭据";
  remove.disabled = account.persisted
    ? deleteInProgress || deleteBlockedReason !== "删除账号及已保存凭据"
    : deleteInProgress;
  remove.title = deleteBlockedReason;
  remove.textContent = account.persisted && !writable
    ? "只读"
    : account.persisted && state.config.accounts.length <= 1
      ? "保留账号"
      : explicitDefault
        ? "默认账号"
        : state.deletingAccountId === account.id
          ? "删除中"
          : "删除账号";
  remove.setAttribute(
    "aria-label",
    remove.disabled && !deleteInProgress
      ? `${account.id || "新账号"}：${remove.title}`
      : `删除账号 ${account.id || "新账号"}`,
  );
  remove.addEventListener("click", () => {
    if (account.persisted) {
      deleteAccount(account.id, remove);
      return;
    }
    state.draft.accounts.splice(accountIndex, 1);
    renderConfig();
  });
  header.append(remove);

  const fields = node("div", { className: "account-fields" });
  const existingAccount = Boolean(account.persisted);
  fields.append(
    makeLabel(
      "账号 ID",
      makeInput("text", account.id, (value) => {
        account.id = value;
      }, { required: true, readOnly: existingAccount }),
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
    accounts: state.draft.accounts.map((account) => ({
      id: account.id,
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
    state.status = await requestJson("/admin/v1/accounts");
    renderAll();
    showToast(`配置已保存 · 代次 ${result.reload.generation}`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setBusy(button, false);
  }
}

function deleteErrorMessage(code) {
  return {
    account_not_found: "账号已不存在，请刷新状态",
    default_account_locked: "该账号是显式默认账号，请先修改 QVP_DEFAULT_ACCOUNT",
    last_account_required: "至少保留一个账号",
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
  for (const candidate of document.querySelectorAll("[data-account-delete]")) {
    candidate.disabled = true;
  }
  byId("add-account").disabled = true;
  byId("save-config").disabled = true;
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
      state.config.routing.mode === "round_robin" &&
      state.config.routing.default_account === accountId
    ) {
      state.config.routing.default_account = state.config.accounts.length
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

function mergeDraftWithConfig(config) {
  const currentAccounts = state.draft && Array.isArray(state.draft.accounts)
    ? state.draft.accounts
    : [];
  const currentPersisted = new Map(
    currentAccounts
      .filter((account) => account.persisted)
      .map((account) => [account.id, account]),
  );
  const persistedAccounts = config.accounts.map((account) => {
    const current = currentPersisted.get(account.id);
    if (current) {
      return current;
    }
    const added = structuredClone(account);
    added.persisted = true;
    return added;
  });
  const unsavedAccounts = currentAccounts.filter((account) => !account.persisted);
  state.draft = structuredClone(config);
  state.draft.accounts = [...persistedAccounts, ...unsavedAccounts];
}

async function deleteAccount(accountId, button) {
  if (state.deletingAccountId) {
    showToast(`账号 ${state.deletingAccountId} 正在删除，请稍候`, true);
    return;
  }
  if (!state.config || state.config.accounts.length <= 1) {
    showToast("至少保留一个账号", true);
    return;
  }
  if (state.config.routing?.configured_default_account === accountId) {
    showToast("该账号是显式默认账号，请先修改 QVP_DEFAULT_ACCOUNT", true);
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
    renderAll();

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
      mergeDraftWithConfig(configResult.value);
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
    const option = node("option", { text: account.id });
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
  state.draft.accounts.push({
    id: "",
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
    for (const candidate of document.querySelectorAll(".tab")) {
      candidate.classList.toggle("active", candidate === tab);
    }
    for (const panel of document.querySelectorAll(".panel")) {
      panel.classList.toggle("active", panel.dataset.panel === tab.dataset.tab);
    }
  });
}

setGateway("idle", "未连接");
bootstrap();
