// frontend/format.js 단위 테스트
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
  describeEventTime,
  describeRecurrence,
  describeStreakBonus,
  formatDate,
  formatDateTime,
  formatPoints,
  formatTime,
  importanceLabel,
  sortEvents,
  toApiDateTime,
} from "../../frontend/format.js";

describe("날짜·시간", () => {
  test("타임존 없는 서버 일시를 요일과 함께 보여준다", () => {
    assert.equal(formatDateTime("2026-09-25T18:00:00"), "2026-09-25 (금) 18:00");
    assert.equal(formatDate("2026-09-01T19:00:00"), "2026-09-01 (화)");
    assert.equal(formatTime("2026-09-01T07:05:00"), "07:05");
  });

  test("비어 있거나 형식이 다르면 빈 문자열", () => {
    assert.equal(formatDateTime(null), "");
    assert.equal(formatDateTime("not a date"), "");
  });

  test("datetime-local 값을 API 형식으로 바꾼다", () => {
    assert.equal(toApiDateTime("2026-09-25T18:00"), "2026-09-25T18:00:00");
    assert.equal(toApiDateTime("2026-09-25T18:00:30"), "2026-09-25T18:00:30");
    assert.equal(toApiDateTime(""), "");
  });
});

describe("반복 규칙", () => {
  const cases = [
    [null, "반복 안 함"],
    ["FREQ=DAILY", "매일"],
    ["FREQ=WEEKLY;BYDAY=TU", "매주 화"],
    ["FREQ=WEEKLY;BYDAY=TU,TH", "매주 화·목"],
    ["RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=FR", "2주마다 금"],
    ["FREQ=MONTHLY;COUNT=3", "매월 (3회)"],
    ["FREQ=HOURLY", "FREQ=HOURLY"],
  ];
  for (const [rule, expected] of cases) {
    test(String(rule), () => assert.equal(describeRecurrence(rule), expected));
  }
});

describe("이벤트 표시", () => {
  test("scheduled는 시작–종료, deadline은 마감 시각", () => {
    assert.equal(
      describeEventTime({ event_type: "scheduled", start_time: "2026-09-01T19:00:00", end_time: "2026-09-01T21:00:00" }),
      "2026-09-01 (화) 19:00–21:00",
    );
    assert.equal(
      describeEventTime({ event_type: "deadline", start_time: null, end_time: "2026-09-25T23:59:00" }),
      "2026-09-25 (금) 23:59 마감",
    );
  });

  test("날짜를 넘기는 일정은 종료 날짜도 보여준다", () => {
    assert.equal(
      describeEventTime({ event_type: "scheduled", start_time: "2026-09-01T23:00:00", end_time: "2026-09-02T07:00:00" }),
      "2026-09-01 (화) 23:00–2026-09-02 (수) 07:00",
    );
  });

  test("시작(마감) 시각 순으로 정렬하고 원본은 바꾸지 않는다", () => {
    const events = [
      { id: 1, event_type: "scheduled", start_time: "2026-09-03T09:00:00", end_time: "2026-09-03T10:00:00" },
      { id: 2, event_type: "deadline", start_time: null, end_time: "2026-09-02T18:00:00" },
      { id: 3, event_type: "scheduled", start_time: "2026-09-01T09:00:00", end_time: "2026-09-01T10:00:00" },
    ];

    assert.deepEqual(sortEvents(events).map((e) => e.id), [3, 2, 1]);
    assert.deepEqual(events.map((e) => e.id), [1, 2, 3]);
  });

  test("중요도 라벨", () => {
    assert.equal(importanceLabel(null), "없음");
    assert.equal(importanceLabel(3), "3 · 출석 체크 없는 의무");
    assert.equal(importanceLabel(6), "MAX · 예외적으로 중요");
  });
});

describe("포인트", () => {
  test("점수 표기", () => {
    assert.equal(formatPoints(5), "5점");
    assert.equal(formatPoints(5.5), "5.5점");
    assert.equal(formatPoints(1234.567), "1,234.57점");
    assert.equal(formatPoints(null), "0점");
  });

  const streakCases = [
    [0, "3일 더 이어가면 ×1.1"],
    [2, "1일 더 이어가면 ×1.1"],
    [3, "×1.1 적용 중 · 4일 더 이어가면 ×1.25"],
    [7, "×1.25 적용 중 · 7일 더 이어가면 ×1.5"],
    [14, "최고 보너스 ×1.5 적용 중"],
    [30, "최고 보너스 ×1.5 적용 중"],
  ];
  for (const [days, expected] of streakCases) {
    test(`streak ${days}일`, () => assert.equal(describeStreakBonus(days), expected));
  }
});
