"use strict";

const TOKEN_HEADER = "X-Claude-Usage-Token";
const POLL_MS = 30000;
const state = { token: "", payload: null, receivedAt: 0, refreshSerial: 0,
  previous: new Map(), saves: new Map() };

function text(node, value) { node.textContent = value == null ? "" : String(value); }
function element(name, className) {
  const node = document.createElement(name);
  if (className) node.className = className;
  return node;
}
function boundedPercent(value) {
  return Number.isFinite(value) ? Math.max(0, Math.min(100, Math.round(value))) : null;
}
function tokenFromFragment() {
  const params = new URLSearchParams(location.hash.slice(1));
  const token = params.get("token") || "";
  history.replaceState(null, "", location.pathname + location.search);
  return token;
}
async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set(TOKEN_HEADER, state.token);
  const response = await fetch(path, { ...options, headers, cache: "no-store" });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}
function formatReset(raw, expired) {
  if (!raw) return expired ? "Window ended" : "Reset time unavailable";
  const date = new Date(raw);
  if (!Number.isFinite(date.getTime())) return expired ? "Window ended" : "Reset time unavailable";
  return `${expired ? "Ended" : "Resets"} ${date.toLocaleString()}`;
}
function parseThresholds(raw) {
  if (!raw.trim()) return [];
  const values = raw.split(",").map(value => Number(value.trim()));
  if (values.some(value => !Number.isInteger(value) || value < 1 || value > 100)) {
    throw new Error("Thresholds must be whole percentages from 1 to 100");
  }
  return [...new Set(values)].sort((a, b) => a - b);
}
function showNotice(message, error = false) {
  const notice = document.getElementById("notice");
  text(notice, message);
  notice.className = error ? "notice error" : "notice";
  notice.hidden = !message;
}
function thresholdEditor(key, thresholds) {
  const wrap = element("div");
  const label = element("label");
  text(label, "Alert at percentages (comma-separated)");
  const edit = element("div", "edit");
  const input = element("input");
  input.type = "text";
  input.inputMode = "numeric";
  input.value = (thresholds || []).join(", ");
  input.setAttribute("aria-label", `Alert thresholds for ${key || "this window"}`);
  const button = element("button");
  button.type = "button";
  text(button, "Save");
  button.addEventListener("click", () => queueSave(key, input, button));
  edit.append(input, button);
  wrap.append(label, edit);
  return wrap;
}
function liveCard(window) {
  const card = element("article", "card");
  const head = element("div", "card-head");
  const title = element("h3");
  text(title, window.label || "Limit");
  const percent = boundedPercent(window.percent);
  const value = element("span", "percent");
  text(value, percent == null ? "—" : `${percent}%`);
  head.append(title, value);
  const gauge = element("progress");
  gauge.max = 100;
  gauge.value = percent == null ? 0 : percent;
  const meta = element("p", "meta");
  text(meta, formatReset(window.resets_at, Boolean(window.expired)));
  card.append(head, gauge, meta);
  if (window.key) card.append(thresholdEditor(window.key, window.thresholds));
  return card;
}
function orphanCard(key, thresholds) {
  const card = element("article", "card");
  const title = element("h3");
  text(title, "Configured window");
  const status = element("p", "meta");
  text(status, "Not present in the current Claude Code reading.");
  const shownKey = element("p", "key");
  text(shownKey, key);
  card.append(title, status, shownKey, thresholdEditor(key, thresholds));
  return card;
}
function reportCrossings(payload) {
  const messages = [];
  for (const window of payload.windows || []) {
    if (!window.key || window.expired || window.orphaned) continue;
    const now = boundedPercent(window.percent);
    if (now == null) continue;
    // Settings use a stable key, but crossings belong to one reset window.
    // Match the dashboard/account minute rounding so reset-time jitter cannot
    // announce the same threshold twice. Keep only the latest window per key.
    const resetAt = Date.parse(window.resets_at || '');
    const reset = Number.isFinite(resetAt)
      ? Math.round(resetAt / 60000) : String(window.resets_at || '');
    const before = state.previous.get(window.key);
    const baseline = before && before.reset === reset ? before.percent : 0;
    state.previous.set(window.key, {reset, percent: Math.max(baseline, now)});
    if (!before) continue;
    const crossed = (window.thresholds || []).filter(value => baseline < value && now >= value);
    if (crossed.length) messages.push(`${window.label || "Limit"} crossed ${crossed.join("%, ")}%`);
  }
  if (messages.length) showNotice(messages.join(" · "));
}
function render(payload) {
  reportCrossings(payload);
  state.payload = payload;
  state.receivedAt = Date.now();
  const windows = document.getElementById("windows");
  const orphans = document.getElementById("orphans");
  windows.replaceChildren(...(payload.windows || []).map(liveCard));
  const absent = Object.entries(payload.orphaned || {});
  orphans.replaceChildren(...absent.map(([key, thresholds]) => orphanCard(key, thresholds)));
  document.getElementById("absent").hidden = absent.length === 0;
  document.getElementById("empty").hidden = Boolean((payload.windows || []).length || absent.length);
  updateReading();
}
function updateReading() {
  if (!state.payload) return;
  const base = state.payload.age_seconds;
  const elapsed = Math.floor((Date.now() - state.receivedAt) / 1000);
  const age = Number.isFinite(base) && base >= 0 ? Math.floor(base) + elapsed : null;
  const source = state.payload.reading === "live" ? "Live reading" : "Claude Code cache";
  text(document.getElementById("reading"), age == null ? source : `${source} · ${age}s old`);
}
async function refresh() {
  const serial = ++state.refreshSerial;
  try {
    const payload = await api("/api/limits");
    if (serial === state.refreshSerial) render(payload);
  } catch (error) {
    // Polling and post-save refreshes can overlap. An obsolete request must
    // neither replace a newer reading nor show an error after recovery.
    if (serial === state.refreshSerial) throw error;
  }
}
function queueSave(key, input, button) {
  if (!key) return;
  let thresholds;
  try { thresholds = parseThresholds(input.value); }
  catch (error) { showNotice(error.message, true); return; }
  button.disabled = true;
  const previous = state.saves.get(key) || Promise.resolve();
  const save = previous.catch(() => {}).then(async () => {
    await api("/api/limits/thresholds", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ thresholds: { [key]: thresholds } }),
    });
    await refresh();
    showNotice("Alert thresholds saved.");
  }).catch(async error => {
    showNotice(error.message || "Could not save thresholds", true);
    try { await refresh(); } catch (_) { /* the visible error is already useful */ }
  }).finally(() => {
    if (state.saves.get(key) === save) state.saves.delete(key);
    button.disabled = false;
  });
  state.saves.set(key, save);
}
async function poll() {
  try { await refresh(); }
  catch (error) { showNotice(error.message || "Could not load quota", true); }
  window.setTimeout(poll, POLL_MS);
}

state.token = tokenFromFragment();
if (!state.token) {
  showNotice("This page needs the token from the URL printed by limits_server.py.", true);
} else {
  poll();
  window.setInterval(updateReading, 1000);
}
