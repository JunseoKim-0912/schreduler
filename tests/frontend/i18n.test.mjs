// frontend/i18n.js — 영어 기본, Eng/Kor 전환, 두 사전의 키 일치, index.html의 키가 사전에 있는지
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import { describeAssistantItem, describeRecurrence, formatDate, formatPoints, importanceLabel } from "../../frontend/format.js";
import { formatRangeTitle, prefillText, visibleDays } from "../../frontend/calendar-model.js";
import { getLang, hasSavedLang, messageKeys, onLangChange, setLang, t } from "../../frontend/i18n.js";

describe("기본 언어와 전환", () => {
  test("저장된 선택이 없으면 영어", () => {
    assert.equal(hasSavedLang(), false);
    assert.equal(getLang(), "en");
    assert.equal(t("tab.calendar"), "Calendar");
  });

  test("전환하면 문구가 바뀌고 알림을 받는다", () => {
    const seen = [];
    const off = onLangChange((lang) => seen.push(lang));
    setLang("ko");
    assert.equal(t("tab.calendar"), "캘린더");
    setLang("en");
    off();
    assert.deepEqual(seen, ["ko", "en"]);
    assert.equal(setLang("fr"), false, "지원하지 않는 언어는 무시");
  });

  test("자리 표시자를 채운다", () => {
    assert.equal(t("tasks.added", { title: "Essay" }), "Added “Essay”.");
  });
});

describe("영어 화면 표기", () => {
  test("날짜·반복·중요도·포인트", () => {
    setLang("en");
    assert.equal(formatDate("2026-10-06T10:00:00"), "Oct 6 (Tue)");
    assert.equal(describeRecurrence("FREQ=WEEKLY;INTERVAL=2;BYDAY=TU,TH"), "Every other week on Tue, Thu");
    assert.equal(importanceLabel(6), "MAX · Exceptionally important");
    assert.equal(formatPoints(1234.5), "1,234.5 pts");
    assert.equal(formatRangeTitle(visibleDays(new Date(2026, 8, 30), "week")), "Sep 28 – Oct 4, 2026");
    assert.equal(prefillText(new Date(2026, 9, 6), 14), "Oct 6 at 14:00 ");
  });

  test("확인 카드도 영어", () => {
    setLang("en");
    const view = describeAssistantItem({
      kind: "update_event", scope: "instance", affected_count: 1, detaches: true, warnings: [], inferred_fields: [],
      targets: [{ title: "ECE355 Tutorial", date: "2026-10-07", recurring: true, time_display: "11:00 AM – 1:00 PM (2h)" }],
      changes: { title: "ECE355 Quiz 2" },
    });
    assert.equal(view.heading, "Change 1 event(s) (this one only)");
    assert.equal(view.rows.find((row) => row.key === "scope").value, "This occurrence only · only 1 occurrence changes");
    assert.deepEqual(view.targets, ["ECE355 Tutorial · Oct 7 (Wed) · 11:00 AM – 1:00 PM (2h)"]);
  });
});

describe("사전", () => {
  test("en과 ko의 키가 같다", () => {
    assert.deepEqual([...messageKeys("ko")].sort(), [...messageKeys("en")].sort());
  });

  test("index.html의 data-i18n 키는 모두 사전에 있고, 태그에 한국어가 박혀 있지 않다", () => {
    const html = readFileSync(new URL("../../frontend/index.html", import.meta.url), "utf-8");
    const keys = [...html.matchAll(/data-i18n(?:-placeholder|-aria-label)?="([^"]+)"/g)].map((m) => m[1]);
    assert.ok(keys.length > 30);
    const known = new Set(messageKeys("en"));
    assert.deepEqual(keys.filter((key) => !known.has(key)), []);
    const withoutComments = html.replace(/<!--[\s\S]*?-->/g, "");
    assert.equal(/[가-힣]/.test(withoutComments), false);
  });
});
