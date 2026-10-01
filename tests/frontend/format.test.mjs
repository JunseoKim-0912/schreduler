// frontend/format.js 단위 테스트
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
  assistantConfirmLabel,
  describeAction,
  describeAssistantItem,
  describeEventTime,
  describeRecurrence,
  describeStreakBonus,
  formatDate,
  formatDateTime,
  formatShortDate,
  formatPoints,
  formatTime,
  importanceLabel,
  sortEvents,
  toApiDateTime,
} from "../../frontend/format.js";
import { setLang } from "../../frontend/i18n.js";

// 아래 기대값은 한국어 화면 기준이다. 영어 기본값은 i18n.test.mjs에서 본다.
setLang("ko");

describe("날짜·시간", () => {
  test("타임존 없는 서버 일시를 요일과 함께 보여준다", () => {
    assert.equal(formatDateTime("2026-09-25T18:00:00"), "9/25 (금) 18:00");
    assert.equal(formatDate("2026-09-01T19:00:00"), "9/1 (화)");
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
    ["RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=FR", "격주 금"],
    ["FREQ=WEEKLY;INTERVAL=3;BYDAY=TU", "3주마다 화"],
    ["FREQ=DAILY;INTERVAL=2", "2일마다"],
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
      "9/1 (화) 19:00–21:00",
    );
    assert.equal(
      describeEventTime({ event_type: "deadline", start_time: null, end_time: "2026-09-25T23:59:00" }),
      "9/25 (금) 23:59 마감",
    );
  });

  test("날짜를 넘기는 일정은 종료 날짜도 보여준다", () => {
    assert.equal(
      describeEventTime({ event_type: "scheduled", start_time: "2026-09-01T23:00:00", end_time: "2026-09-02T07:00:00" }),
      "9/1 (화) 23:00–9/2 (수) 07:00",
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

describe("최근 변경", () => {
  test("변경 기록은 출처와 시각을 보여준다", () => {
    assert.equal(describeAction({ source: "ui", created_at: "2026-09-26T09:05:12.345678" }), "목록에서 삭제 · 9/26 (토) 09:05");
    assert.equal(describeAction({ source: "nl", created_at: "2026-09-26T21:00:00" }), "자연어 · 9/26 (토) 21:00");
  });
});

describe("반복 기간", () => {
  test("짧은 날짜는 월/일", () => {
    assert.equal(formatShortDate("2026-09-01"), "9/1");
    assert.equal(formatShortDate("2026-12-20"), "12/20");
    assert.equal(formatShortDate(null), "");
  });
});

describe("어시스턴트 확인 카드", () => {
  const biweekly = {
    draft_id: "d1", kind: "create_event", title: "ECE360 Lab", event_type: "scheduled",
    date: "2026-10-06", start_time: "2026-10-06T09:00:00", end_time: "2026-10-06T12:00:00",
    time_display: "오전 9:00 – 오후 12:00 (3시간)", importance: 3, recurring: true,
    recurrence_start: "2026-09-22", recurrence_rule: "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU",
    date_range: { id: 1, name: "Lecture Period", start_date: "2026-09-03", end_date: "2026-12-08", is_new: false },
    location: null, preview_dates: ["2026-10-06", "2026-10-20", "2026-11-03"],
    warnings: [], inferred_fields: ["importance", "date_range"],
  };
  const value = (view, key) => view.rows.find((row) => row.key === key);

  test("반복 일정: 시간 문자열 그대로, 미리보기 3회차, 기준일 보조 문구, 추정 배지", () => {
    const view = describeAssistantItem(biweekly);
    assert.equal(value(view, "time").value, "오전 9:00 – 오후 12:00 (3시간)");
    assert.equal(value(view, "recurrence").value, "격주 화");
    assert.equal(value(view, "preview").value, "10/6, 10/20, 11/3");
    assert.equal(value(view, "date_range").value, "Lecture Period (9/3~12/8)");
    assert.deepEqual(view.notes, ["9/22 기준 격주"]);
    assert.equal(value(view, "importance").inferred, true);
    assert.equal(value(view, "date_range").inferred, true);
    assert.equal(value(view, "time").inferred, false);
  });

  test("기준일이 첫 회차와 같거나 매주 반복이면 보조 문구가 없다", () => {
    assert.deepEqual(describeAssistantItem({ ...biweekly, recurrence_start: "2026-10-06" }).notes, []);
    assert.deepEqual(describeAssistantItem({ ...biweekly, recurrence_rule: "FREQ=WEEKLY;BYDAY=TU" }).notes, []);
  });

  test("마감 일정과 경고, 하위 항목 추정(location.travel_minutes)", () => {
    const view = describeAssistantItem({
      kind: "create_event", title: "MAT389 과제", event_type: "deadline", date: "2026-09-29",
      start_time: null, end_time: "2026-09-29T23:30:00", time_display: "오후 11:30 마감", importance: 4,
      recurring: false, location: { id: null, name: "Bahen", travel_minutes: 15, is_new: true },
      warnings: [{ code: "past_date", message: "이미 지난 날짜예요" }],
      inferred_fields: ["location.travel_minutes"],
    });
    assert.equal(value(view, "event_type").value, "마감");
    assert.equal(value(view, "time").label, "마감 시각");
    assert.equal(value(view, "recurrence").value, "반복 안 함");
    assert.equal(value(view, "location").value, "Bahen (이동 15분, 새로 등록)");
    assert.equal(value(view, "location").inferred, true);
    assert.deepEqual(view.warnings, ["⚠ 이미 지난 날짜예요"]);
  });

  test("삭제 초안은 대상 목록과 개수", () => {
    const view = describeAssistantItem({
      kind: "delete_event", scope: "series", warnings: [], inferred_fields: [],
      targets: [
        { title: "물리 퀴즈", date: "2026-09-29", recurring: false, time_display: "오후 6:00 – 오후 7:00 (1시간)" },
        { title: "물리 강의", date: "2026-09-28", recurring: true, time_display: "오전 9:00 – 오전 10:00 (1시간)" },
      ],
    });
    assert.equal(view.heading, "삭제할 일정 2개");
    assert.deepEqual(view.targets, ["물리 퀴즈 · 9/29 (화) · 오후 6:00 – 오후 7:00 (1시간)", "물리 강의 · 반복 전체"]);
  });

  test("수정 초안은 바뀐 시간을 화살표로", () => {
    const view = describeAssistantItem({
      kind: "update_event", scope: "instance", warnings: [], inferred_fields: [],
      targets: [{ title: "물리 퀴즈", date: "2026-09-29", recurring: true, time_display: "오후 6:00 – 오후 7:00 (1시간)", new_time_display: "오후 7:00 – 오후 8:00 (1시간)" }],
      changes: { start_time: "19:00", end_time: null, title: null, date: null, importance: null, location: null },
    });
    assert.equal(view.targets[0], "물리 퀴즈 · 9/29 (화) · 오후 6:00 – 오후 7:00 (1시간) → 오후 7:00 – 오후 8:00 (1시간)");
    assert.equal(value(view, "time").value, "19:00 – 그대로");
  });

  test("실행 버튼 이름", () => {
    assert.equal(assistantConfirmLabel([{ kind: "create_range" }, { kind: "create_event" }]), "만들기");
    assert.equal(assistantConfirmLabel([{ kind: "delete_event" }]), "실행");
  });

  test("범위와 바뀌는 회차 수, 떼어내기 안내", () => {
    const view = describeAssistantItem({
      kind: "update_event", scope: "instance", affected_count: 1, detaches: true, warnings: [], inferred_fields: [],
      targets: [{ title: "ECE355 Tutorial", date: "2026-10-07", recurring: true, time_display: "오전 11:00 – 오후 1:00 (2시간)" }],
      changes: { title: "ECE355 Quiz 2", start_time: null, end_time: null, importance: null, location: null, event_type: null },
    });
    assert.equal(value(view, "scope").value, "이 회차만 · 1개 회차만 바뀌어요");
    assert.deepEqual(view.notes, ["이 회차만 따로 떼어 단발 일정으로 바꿔요. 다른 회차는 그대로예요."]);
    assert.equal(describeAssistantItem({ kind: "delete_event", scope: "series", affected_count: 12, targets: [], warnings: [] }).rows[0].value,
      "반복 전체 · 12개 회차가 취소돼요");
  });
});
