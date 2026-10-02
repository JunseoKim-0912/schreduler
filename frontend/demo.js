// Demo account (POST /auth/demo): the "resets in 23h" banner text. No DOM, so Node tests it (tests/frontend/demo.test.mjs).

import { t } from "./i18n.js";

// The server sends naive UTC ("2026-10-02T16:00:00"); without a zone the browser would read it as local time.
export function parseUtc(value) {
  if (!value) return null;
  return new Date(/(Z|[+-]\d\d:\d\d)$/.test(value) ? value : `${value}Z`);
}

export function demoTimeLeft(expiresAt, now = new Date()) {
  const ms = parseUtc(expiresAt) - now;
  if (!(ms > 0)) return t("demo.resetsSoon");
  const hours = Math.floor(ms / 3_600_000);
  if (hours >= 1) return t("demo.resetsHours", { hours });
  return t("demo.resetsMinutes", { minutes: Math.max(1, Math.ceil(ms / 60_000)) });
}

export function demoBannerText(account, now = new Date()) {
  return t("demo.banner", { left: demoTimeLeft(account?.demo_expires_at, now) });
}
