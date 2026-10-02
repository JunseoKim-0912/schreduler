import { apiFetch, getUserId } from "./api.js";
import { hasMessage, t } from "./i18n.js";

// "Today's AI usage $0.12 / $1.00" next to the assistant and persona chat inputs (GET /usage/today).
// Yellow from 80% of the limit; at the limit (or when the app-wide limit is hit) the input is disabled with a notice.
// Only these two inputs call the LLM, so nothing else on the page is affected.

export const WARN_RATIO = 0.8;

export function zoneCity(zone) {
  if (!zone) return "";
  const key = `usage.zone.${zone}`;
  return hasMessage(key) ? t(key) : zone.split("/").pop().replaceAll("_", " ");
}

// Cents normally; amounts under a cent (a tiny test limit, the first call of the day) get 4 decimals so they don't read as $0.00.
// Spend is rounded down so "$1.00 / $1.00" never shows while the input is still open.
export function usd(value, { roundDown = false } = {}) {
  const amount = Number(value ?? 0);
  const digits = amount > 0 && amount < 0.01 ? 4 : 2;
  const shown = roundDown ? Math.floor(amount * 10 ** digits) / 10 ** digits : amount;
  return `$${shown.toFixed(digits)}`;
}

// Pure: /usage/today response → what the meter shows. null when there is nothing to show.
export function usageView(data) {
  if (!data) return null;
  const blocked = Boolean(data.total_blocked) || data.spent_usd >= data.limit_usd;
  if (blocked) {
    const text = data.is_demo ? t("usage.demoBlocked") : t("usage.blocked", { city: zoneCity(data.timezone) });
    return { level: "blocked", blocked, text };
  }
  const level = data.spent_usd >= data.limit_usd * WARN_RATIO ? "warn" : "ok";
  return { level, blocked, text: t("usage.meter", { spent: usd(data.spent_usd, { roundDown: true }), limit: usd(data.limit_usd) }) };
}

let latest = null;
const meters = new Set();

function render() {
  for (const meter of meters) meter.apply();
}

// node: the <p> under the form. isBusy: while a message is being sent, the send button stays disabled.
export function createUsageMeter({ node, input, button, isBusy = () => false }) {
  const meter = {
    apply() {
      const view = usageView(latest);
      node.hidden = !view;
      node.textContent = view?.text ?? "";
      node.dataset.level = view?.level ?? "ok";
      const blocked = Boolean(view?.blocked);
      input.disabled = blocked;
      button.disabled = blocked || isBusy();
    },
    get blocked() {
      return Boolean(usageView(latest)?.blocked);
    },
  };
  meters.add(meter);
  meter.apply();
  return meter;
}

export async function refreshUsage() {
  if (!getUserId()) {
    latest = null;
    render();
    return;
  }
  const userId = getUserId();
  try {
    const data = await apiFetch("/usage/today", { showError: false });
    if (getUserId() === userId) latest = data;
  } catch {
    // Leave the last known state; the server still refuses over-limit requests with 429.
  }
  render();
}

export function clearUsage() {
  latest = null;
  render();
}

// Re-render the text in the new screen language without another request.
export const relabelUsage = render;
