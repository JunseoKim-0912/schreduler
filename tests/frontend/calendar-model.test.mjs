// frontend/calendar-model.js 단위 테스트
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
  addDays,
  deadlinesOn,
  describeItemTime,
  formatRangeTitle,
  hourFromOffset,
  importanceClass,
  isOverdue,
  layoutOverlaps,
  nowMarker,
  parseLocalDateTime,
  prefillText,
  scheduledSegments,
  startOfWeek,
  toIsoDate,
  visibleDays,
} from "../../frontend/calendar-model.js";

const day = (iso) => parseLocalDateTime(iso);
const scheduled = (id, start, end, extra = {}) => ({ event_id: id, event_type: "scheduled", start_time: start, end_time: end, ...extra });

describe("날짜·주", () => {
  test("서버 일시를 UTC 변환 없이 로컬 벽시계 그대로 읽는다", () => {
    const d = parseLocalDateTime("2026-09-24T19:05:00");
    assert.deepEqual([d.getFullYear(), d.getMonth(), d.getDate(), d.getHours(), d.getMinutes()], [2026, 8, 24, 19, 5]);
    assert.equal(toIsoDate(d), "2026-09-24");
    assert.equal(parseLocalDateTime("nope"), null);
  });

  test("주는 월요일부터", () => {
    assert.equal(toIsoDate(startOfWeek(day("2026-09-26"))), "2026-09-21"); // 토
    assert.equal(toIsoDate(startOfWeek(day("2026-09-27"))), "2026-09-21"); // 일
    assert.equal(toIsoDate(startOfWeek(day("2026-09-21"))), "2026-09-21"); // 월
    assert.deepEqual(visibleDays(day("2026-09-24"), "week").map(toIsoDate), [
      "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-26", "2026-09-27",
    ]);
    assert.deepEqual(visibleDays(day("2026-09-24T15:00:00"), "day").map(toIsoDate), ["2026-09-24"]);
    assert.equal(toIsoDate(addDays(day("2026-12-31"), 1)), "2027-01-01");
  });

  test("기간 제목", () => {
    assert.equal(formatRangeTitle(visibleDays(day("2026-09-24"), "week")), "2026년 9월 21일 – 27일");
    assert.equal(formatRangeTitle(visibleDays(day("2026-10-01"), "week")), "2026년 9월 28일 – 10월 4일");
    assert.equal(formatRangeTitle(visibleDays(day("2026-12-31"), "week")), "2026년 12월 28일 – 2027년 1월 3일");
    assert.equal(formatRangeTitle([day("2026-09-24")]), "2026년 9월 24일 (목)");
  });
});

describe("블록 배치", () => {
  test("시작~종료 시각이 세로 위치·길이가 된다", () => {
    const [seg] = scheduledSegments([scheduled(1, "2026-09-24T09:30:00", "2026-09-24T11:00:00")], day("2026-09-24"));
    assert.deepEqual([seg.startMin, seg.endMin, seg.col, seg.cols], [570, 660, 0, 1]);
  });

  test("다른 날짜와 deadline은 그리지 않는다", () => {
    const items = [
      scheduled(1, "2026-09-25T09:00:00", "2026-09-25T10:00:00"),
      { event_id: 2, event_type: "deadline", start_time: null, end_time: "2026-09-24T23:59:00" },
    ];
    assert.deepEqual(scheduledSegments(items, day("2026-09-24")), []);
  });

  test("자정을 넘는 일정은 날짜마다 잘린다", () => {
    const item = scheduled(1, "2026-09-24T23:00:00", "2026-09-25T01:00:00");
    const [first] = scheduledSegments([item], day("2026-09-24"));
    const [second] = scheduledSegments([item], day("2026-09-25"));
    assert.deepEqual([first.startMin, first.endMin, first.continuesAfter], [1380, 1440, true]);
    assert.deepEqual([second.startMin, second.endMin, second.continuesBefore], [0, 60, true]);
  });

  test("아주 짧은 일정도 최소 높이를 가진다", () => {
    const [seg] = scheduledSegments([scheduled(1, "2026-09-24T09:00:00", "2026-09-24T09:05:00")], day("2026-09-24"));
    assert.equal(seg.endMin - seg.startMin, 20);
  });

  test("겹치는 일정은 폭을 나누고, 이어 붙은 일정은 겹치지 않는다", () => {
    const segs = scheduledSegments(
      [
        scheduled(1, "2026-09-24T09:00:00", "2026-09-24T11:00:00"),
        scheduled(2, "2026-09-24T10:00:00", "2026-09-24T12:00:00"),
        scheduled(3, "2026-09-24T11:00:00", "2026-09-24T11:30:00"), // 1이 끝난 칸을 다시 쓴다
        scheduled(4, "2026-09-24T13:00:00", "2026-09-24T14:00:00"), // 따로 떨어진 묶음
      ],
      day("2026-09-24"),
    );
    const byId = Object.fromEntries(segs.map((s) => [s.item.event_id, [s.col, s.cols]]));
    assert.deepEqual(byId, { 1: [0, 2], 2: [1, 2], 3: [0, 2], 4: [0, 1] });
  });

  test("세 개가 한꺼번에 겹치면 세 칸", () => {
    const segs = layoutOverlaps([
      { item: { event_id: 1 }, startMin: 600, endMin: 700 },
      { item: { event_id: 2 }, startMin: 610, endMin: 650 },
      { item: { event_id: 3 }, startMin: 620, endMin: 640 },
    ]);
    assert.deepEqual(segs.map((s) => [s.col, s.cols]), [[0, 3], [1, 3], [2, 3]]);
  });
});

describe("마감·색·기타", () => {
  const task = { event_type: "deadline", start_time: null, end_time: "2026-09-24T18:00:00", status: "pending" };

  test("마감 칩은 그 날짜 것만, 시각 순서", () => {
    const later = { ...task, end_time: "2026-09-24T23:59:00" };
    const other = { ...task, end_time: "2026-09-25T09:00:00" };
    assert.deepEqual(deadlinesOn([later, other, task], day("2026-09-24")), [task, later]);
  });

  test("overdue는 마감이 지났고 완료하지 않은 경우만", () => {
    const now = day("2026-09-24T18:01:00");
    assert.equal(isOverdue(task, now), true);
    assert.equal(isOverdue({ ...task, status: "done" }, now), false);
    assert.equal(isOverdue(task, day("2026-09-24T17:59:00")), false);
  });

  test("중요도별 색 클래스", () => {
    assert.deepEqual([null, 1, 3, 5, 6].map(importanceClass), ["imp-none", "imp-1", "imp-3", "imp-5", "imp-max"]);
  });

  test("현재 시각 선은 보고 있는 날짜에 오늘이 있을 때만", () => {
    const week = visibleDays(day("2026-09-24"), "week");
    assert.deepEqual(nowMarker(day("2026-09-26T14:30:00"), week), { dayIndex: 5, minutes: 870 });
    assert.equal(nowMarker(day("2026-10-01T14:30:00"), week), null);
  });

  test("빈 칸 클릭: 시간 계산과 미리 채울 문구", () => {
    assert.equal(hourFromOffset(14 * 48 + 20), 14);
    assert.equal(hourFromOffset(-5), 0);
    assert.equal(hourFromOffset(24 * 48), 23);
    assert.equal(prefillText(day("2026-09-24"), 14), "9월 24일 14시에 ");
  });

  test("상세 패널의 날짜·시간 문구", () => {
    assert.equal(describeItemTime(scheduled(1, "2026-09-24T09:00:00", "2026-09-24T10:30:00")), "9월 24일 (목) 09:00–10:30");
    assert.equal(describeItemTime(scheduled(1, "2026-09-24T23:00:00", "2026-09-25T01:00:00")), "9월 24일 (목) 23:00–9월 25일 (금) 01:00");
    assert.equal(describeItemTime(task), "9월 24일 (목) 18:00 마감");
  });
});
