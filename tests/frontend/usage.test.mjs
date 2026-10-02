// frontend/usage.js: what the AI usage meter shows for a GET /usage/today response.
import assert from "node:assert/strict";
import { afterEach, describe, test } from "node:test";

import { describeError } from "../../frontend/api.js";
import { setLang } from "../../frontend/i18n.js";
import { usageView, usd, zoneCity } from "../../frontend/usage.js";

const today = (spent, limit = 1, totalBlocked = false) => ({
  spent_usd: spent,
  limit_usd: limit,
  total_blocked: totalBlocked,
  resets_at: "2026-10-02T00:00:00-04:00",
  timezone: "America/Toronto",
});

afterEach(() => setLang("en"));

describe("usageView", () => {
  test("a demo account at its limit gets the demo notice", () => {
    assert.equal(usageView({ ...today(0.05, 0.05), is_demo: true }).text, "You've reached the demo's AI limit. You can keep trying everything else.");
    setLang("ko");
    assert.equal(usageView({ ...today(0.01, 0.05, true), is_demo: true }).text, "데모 AI 한도에 도달했어요. 다른 기능은 계속 써볼 수 있어요.");
  });

  test("shows spend over limit in dollars", () => {
    setLang("en");
    assert.deepEqual(usageView(today(0.12)), { level: "ok", blocked: false, text: "Today's AI usage $0.12 / $1.00" });
    setLang("ko");
    assert.equal(usageView(today(0.12)).text, "오늘 AI 사용량 $0.12 / $1.00");
  });

  test("turns yellow from 80% of the limit", () => {
    assert.equal(usageView(today(0.79)).level, "ok");
    assert.equal(usageView(today(0.8)).level, "warn");
    assert.equal(usageView(today(0.99)).blocked, false);
  });

  test("blocks at the limit or when the app-wide limit is hit", () => {
    setLang("ko");
    const view = usageView(today(1));
    assert.equal(view.blocked, true);
    assert.equal(view.level, "blocked");
    assert.equal(view.text, "오늘 AI 사용 한도에 도달했어요. 자정(토론토 시간)에 다시 열려요.");
    setLang("en");
    assert.equal(usageView(today(0.01, 1, true)).text, "You've reached today's AI usage limit. It reopens at midnight (Toronto time).");
  });

  test("amounts under a cent keep four decimals", () => {
    assert.equal(usd(0), "$0.00");
    assert.equal(usd(0.0004), "$0.0004");
    assert.equal(usd(1), "$1.00");
    assert.equal(usageView(today(0.000966, 0.001)).text, "Today's AI usage $0.0009 / $0.0010");
    assert.equal(usageView(today(0.996)).text, "Today's AI usage $0.99 / $1.00");
  });

  test("nothing to show without data", () => {
    assert.equal(usageView(null), null);
  });

  test("unknown time zones fall back to the city part", () => {
    assert.equal(zoneCity("America/Los_Angeles"), "Los Angeles");
  });
});

test("a 429 from an LLM endpoint shows the server's reason", () => {
  const detail = "오늘 AI 사용 한도에 도달했어요. 자정(토론토 시간)에 다시 열려요.";
  assert.equal(describeError({ status: 429, detail, method: "POST", path: "/assistant/chat" }), detail);
  setLang("en");
  assert.equal(
    describeError({ status: 429, detail: null, method: "POST", path: "/daily-actual-logs/checkin" }),
    "You've reached today's AI usage limit. It reopens at midnight.",
  );
});
