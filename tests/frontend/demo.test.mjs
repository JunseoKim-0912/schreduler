// frontend/demo.js: the demo banner's time left.
import assert from "node:assert/strict";
import { afterEach, describe, test } from "node:test";

import { demoBannerText, demoTimeLeft, parseUtc } from "../../frontend/demo.js";
import { setLang } from "../../frontend/i18n.js";

const NOW = new Date("2026-10-01T16:00:00Z");

afterEach(() => setLang("en"));

describe("demo banner", () => {
  test("reads the server's naive time as UTC", () => {
    assert.equal(parseUtc("2026-10-02T16:00:00").toISOString(), "2026-10-02T16:00:00.000Z");
    assert.equal(parseUtc("2026-10-02T16:00:00Z").toISOString(), "2026-10-02T16:00:00.000Z");
  });

  test("right after starting it says 23h, then minutes in the last hour", () => {
    assert.equal(demoBannerText({ demo_expires_at: "2026-10-02T15:59:30" }, NOW), "Demo account · Prototype · resets in 23h");
    assert.equal(demoTimeLeft("2026-10-01T16:40:00", NOW), "resets in 40m");
    assert.equal(demoTimeLeft("2026-10-01T16:00:10", NOW), "resets in 1m");
    assert.equal(demoTimeLeft("2026-10-01T15:00:00", NOW), "resets soon");
  });

  test("Korean", () => {
    setLang("ko");
    assert.equal(demoBannerText({ demo_expires_at: "2026-10-02T14:59:00" }, NOW), "데모 계정 · 프로토타입 · 22시간 후 초기화");
  });
});
