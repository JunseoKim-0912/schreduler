// 화면 표시용 순수 함수 모음. DOM에 의존하지 않아 Node에서 테스트한다 (tests/frontend/format.test.mjs).
// 문구는 i18n.js 사전에서 가져오고, 날짜는 지금 화면 언어로 쓴다 ("Oct 6 (Tue)" / "10/6 (화)").

import { bydayName, monthDay, monthDayWeekday, numberLocale, t } from "./i18n.js";

// 중요도 체계 (docs/기획보고서.md 3절). null은 "없음(수면)", 6은 MAX.
export const IMPORTANCE_VALUES = [1, 2, 3, 4, 5, 6];

export function importanceOptions() {
  return IMPORTANCE_VALUES.map((value) => ({ value, label: t(`importance.${value}`) }));
}

export function importanceLabel(value) {
  if (value === null || value === undefined) return t("importance.none");
  return IMPORTANCE_VALUES.includes(value) ? t(`importance.${value}`) : String(value);
}

const FREQ_EVERY = { DAILY: "repeat.daily", WEEKLY: "repeat.weekly", MONTHLY: "repeat.monthly", YEARLY: "repeat.yearly" };

// 서버의 일시는 타임존 없는 로컬 시각 문자열("2026-09-25T18:00:00")이다. Date로 바꾸면 환경 시간대가 끼어들 수
// 있어 문자열을 직접 나눠 쓴다.
function parts(isoDateTime) {
  const match = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/.exec(isoDateTime ?? "");
  if (!match) return null;
  const [, year, month, day, hour = "00", minute = "00"] = match;
  const weekday = new Date(Number(year), Number(month) - 1, Number(day)).getDay();
  return { month: Number(month), day: Number(day), date: `${year}-${month}-${day}`, time: `${hour}:${minute}`, weekday };
}

// "Oct 6 (Tue)" / "10/6 (화)"
export function formatDate(isoDateTime) {
  const p = parts(isoDateTime);
  return p ? monthDayWeekday(p.month, p.day, p.weekday) : "";
}

// "Sep 1" / "9/1" (반복 기간처럼 짧게 보여줄 때)
export function formatShortDate(isoDate) {
  const p = parts(isoDate);
  return p ? monthDay(p.month, p.day) : "";
}

export function formatTime(isoDateTime) {
  return parts(isoDateTime)?.time ?? "";
}

export function formatDateTime(isoDateTime) {
  const p = parts(isoDateTime);
  return p ? `${monthDayWeekday(p.month, p.day, p.weekday)} ${p.time}` : "";
}

function ruleFields(rule) {
  return Object.fromEntries(
    rule
      .replace(/^RRULE:/, "")
      .split(";")
      .map((part) => part.split("="))
      .filter((pair) => pair.length === 2),
  );
}

// 반복 간격만: "Every other week" / "격주"
function recurrenceEvery(fields) {
  const interval = Number(fields.INTERVAL ?? 1);
  if (interval === 2 && fields.FREQ === "WEEKLY") return t("repeat.biweekly");
  if (interval > 1) return t("repeat.every", { n: interval, unit: t(`repeat.unit.${fields.FREQ}`) });
  return t(FREQ_EVERY[fields.FREQ]);
}

// "FREQ=WEEKLY;BYDAY=TU,TH" → "Weekly on Tue, Thu" / "매주 화·목". 모르는 규칙은 원문을 그대로 보여준다.
export function describeRecurrence(rule) {
  if (!rule) return t("repeat.none");
  const fields = ruleFields(rule);
  if (!FREQ_EVERY[fields.FREQ]) return rule;
  let text = recurrenceEvery(fields);
  if (fields.BYDAY) {
    const days = fields.BYDAY.split(",").map(bydayName).join(t("repeat.daySeparator"));
    text = t("repeat.onDays", { every: text, days });
  }
  if (fields.COUNT) text = t("repeat.count", { text, count: fields.COUNT });
  return text;
}

// 일정의 시간 부분. deadline은 end_time이 마감 일시다.
export function describeEventTime(event) {
  if (event.event_type === "deadline" || !event.start_time) return t("time.due", { when: formatDateTime(event.end_time) });
  const sameDay = parts(event.start_time)?.date === parts(event.end_time)?.date;
  const end = sameDay ? formatTime(event.end_time) : formatDateTime(event.end_time);
  return `${formatDateTime(event.start_time)}–${end}`;
}

// 목록 정렬 기준: scheduled는 시작 시각, deadline은 마감 시각
export function eventSortKey(event) {
  return event.start_time ?? event.end_time ?? "";
}

export function sortEvents(events) {
  return [...events].sort((a, b) => eventSortKey(a).localeCompare(eventSortKey(b)) || a.id - b.id);
}

// <input type="datetime-local">의 값("2026-09-25T18:00")을 API 형식("2026-09-25T18:00:00")으로
export function toApiDateTime(localValue) {
  if (!localValue) return "";
  return localValue.length === 16 ? `${localValue}:00` : localValue;
}

// 포인트는 소수 둘째 자리까지 나올 수 있다 (5 × 1.1 = 5.5). 불필요한 0은 빼고 천 단위 구분.
export function formatPoints(value) {
  return t("points.value", { value: Number(value ?? 0).toLocaleString(numberLocale(), { maximumFractionDigits: 2 }) });
}

// FR-10 연속 100% 완료 보너스 (app/services/points.py STREAK_MULTIPLIERS와 같은 값)
export const STREAK_TIERS = [
  { days: 3, multiplier: 1.1 },
  { days: 7, multiplier: 1.25 },
  { days: 14, multiplier: 1.5 },
];

export function describeStreakBonus(streakDays) {
  const next = STREAK_TIERS.find((tier) => tier.days > streakDays);
  if (!next) return t("streak.max", { multiplier: STREAK_TIERS.at(-1).multiplier });
  const current = [...STREAK_TIERS].reverse().find((tier) => tier.days <= streakDays);
  const nextText = t("streak.next", { days: next.days - streakDays, multiplier: next.multiplier });
  return current ? t("streak.current", { multiplier: current.multiplier, next: nextText }) : nextText;
}

// --- 최근 변경 (GET /actions) ---

export function describeAction(action) {
  const source = ["nl", "ui"].includes(action.source) ? t(`actions.source.${action.source}`) : action.source;
  return [source, formatDateTime(action.created_at)].filter(Boolean).join(" · ");
}

// --- 어시스턴트 확인 카드 (POST /assistant/chat의 proposal.items) ---

// 카드 행 → 그 행에 "추정" 배지를 붙게 하는 inferred_fields 이름. "location.travel_minutes"처럼 하위 항목도 부모 행에 붙인다.
const INFERRED_ROWS = {
  title: ["title"],
  event_type: ["event_type"],
  date: ["date"],
  time: ["start_time", "end_time"],
  importance: ["importance"],
  recurrence: ["recurrence", "by_day", "interval", "frequency"],
  date_range: ["date_range"],
  location: ["location", "changes.location"],
  mode: ["mode"],
};

function isInferred(inferred, key) {
  const names = INFERRED_ROWS[key] ?? [key];
  return inferred.some((field) => names.some((name) => field === name || field.startsWith(`${name}.`)));
}

function describeLocation(location) {
  if (!location) return t("common.none");
  const minutes = location.travel_minutes ?? location.default_travel_minutes;
  const details = [minutes !== undefined && minutes !== null ? t("card.travel", { minutes }) : "", location.is_new ? t("card.newLocation") : ""];
  const extra = details.filter(Boolean).join(", ");
  return extra ? `${location.name} (${extra})` : location.name;
}

// 수정·삭제 대상 한 줄: "Physics quiz · Sep 29 (Tue) · 6:00 PM – 7:00 PM (1h) → 7:00 PM – …"
export function describeAssistantTarget(target, scope) {
  const when = scope === "series" && target.recurring ? t("card.allRepeats") : [formatDate(target.date), target.time_display].filter(Boolean).join(" · ");
  const text = [target.title, when].filter(Boolean).join(" · ");
  if (!target.new_time_display) return text;
  const after = [target.new_date && target.new_date !== target.date ? formatDate(target.new_date) : "", target.new_time_display];
  return `${text} → ${after.filter(Boolean).join(" ")}`;
}

function rangeModeLabel(mode) {
  return ["range_only", "with_events"].includes(mode) ? t(`ranges.mode.${mode}`) : mode;
}

/**
 * 확인 카드 초안 하나를 화면용으로 풀어 쓴다.
 * 돌려주는 값: { heading, rows: [{key, label, value, inferred}], notes, targets, warnings }
 * 시간은 백엔드가 준 time_display를 그대로 쓴다 (오전/오후·다음 날 표기를 한 곳에서만 만든다). 경고 문구도 백엔드가
 * 사용자 언어로 준 것을 그대로 쓴다.
 */
export function describeAssistantItem(item) {
  const inferred = (item.inferred_fields ?? []).map(String);
  const rows = [];
  const notes = [];
  let targets = [];
  let heading = "";
  const row = (key, label, value) => rows.push({ key, label, value, inferred: isInferred(inferred, key) });

  switch (item.kind) {
    case "create_event": {
      heading = t("card.newEvent");
      row("title", t("card.title"), item.title);
      row("event_type", t("card.type"), t(`card.type.${item.event_type === "deadline" ? "deadline" : "scheduled"}`));
      row("date", item.recurring ? t("card.firstOccurrence") : t("card.date"), formatDate(item.date));
      row("time", item.event_type === "deadline" ? t("card.dueTime") : t("card.time"), item.time_display);
      row("importance", t("card.importance"), importanceLabel(item.importance));
      if (item.recurring) {
        row("recurrence", t("card.repeat"), describeRecurrence(item.recurrence_rule));
        if (item.preview_dates?.length) row("preview", t("card.nextOccurrences"), item.preview_dates.map(formatShortDate).join(", "));
        // 기준일은 격주 이상에서만 리듬을 정한다 (매주면 어느 주에서 세든 같다).
        const fields = ruleFields(item.recurrence_rule ?? "");
        if (Number(fields.INTERVAL ?? 1) > 1 && item.recurrence_start && item.recurrence_start !== item.date) {
          notes.push(t("card.anchor", { date: formatShortDate(item.recurrence_start), every: recurrenceEvery(fields) }));
        }
        const range = item.date_range;
        if (range) {
          const span = `${formatShortDate(range.start_date)}~${formatShortDate(range.end_date)}`;
          row("date_range", t("card.period"), `${range.name} (${span}${range.is_new ? t("card.newSuffix") : ""})`);
        }
      } else {
        row("recurrence", t("card.repeat"), t("card.noRepeat"));
      }
      row("location", t("card.location"), describeLocation(item.location));
      break;
    }
    case "update_event":
    case "delete_event": {
      const count = item.targets?.length ?? 0;
      heading = t(item.kind === "delete_event" ? "card.delete" : "card.update", { count }) + (item.scope === "instance" ? t("card.onlyThis") : "");
      targets = (item.targets ?? []).map((target) => describeAssistantTarget(target, item.scope));
      const changes = item.changes ?? {};
      if (changes.title) row("title", t("card.newTitle"), changes.title);
      if (changes.date) row("date", t("card.newDate"), formatDate(changes.date));
      if (changes.start_time || changes.end_time) {
        row("time", t("card.newTime"), [changes.start_time, changes.end_time].map((v) => v ?? t("card.unchanged")).join(" – "));
      }
      if (changes.importance !== null && changes.importance !== undefined) {
        row("importance", t("card.newImportance"), importanceLabel(changes.importance));
      }
      if (changes.location) {
        row("location", t("card.location"), changes.location.action === "remove" ? t("card.removeLocation") : describeLocation(changes.location));
      }
      if (changes.event_type) row("event_type", t("card.newType"), t(`card.type.${changes.event_type}`));
      if (item.scope) row("scope", t("card.scope"), describeScope(item));
      if (item.detaches) notes.push(t("card.detaches"));
      break;
    }
    case "create_range":
      heading = t("card.newRange");
      row("title", t("card.name"), item.name);
      row("date_range", t("card.period"), `${formatShortDate(item.start_date)}~${formatShortDate(item.end_date)}`);
      break;
    case "update_range":
      heading = t("card.updateRange");
      row("title", t("card.name"), item.new_name ? `${item.name} → ${item.new_name}` : item.name);
      row(
        "date_range",
        t("card.period"),
        `${formatShortDate(item.before?.start_date)}~${formatShortDate(item.before?.end_date)} → ${formatShortDate(item.start_date)}~${formatShortDate(item.end_date)}`,
      );
      if (item.events_using) row("events_using", t("card.eventsUsing"), t("card.eventsUsingResync", { count: item.events_using }));
      break;
    case "delete_range":
      heading = t("card.deleteRange");
      row("title", t("card.name"), item.name);
      if (item.events_using) {
        row("events_using", t("card.eventsUsing"), t("card.eventsUsingCount", { count: item.events_using }));
        row("mode", t("card.handling"), rangeModeLabel(item.mode));
      }
      break;
    default:
      heading = item.kind ?? t("card.proposal");
  }
  const warnings = (item.warnings ?? []).map((warning) => `⚠ ${warning.message}`);
  return { heading, rows, notes, targets, warnings };
}

// "This occurrence only · only 1 occurrence changes" / "이 회차만 · 1개 회차만 바뀌어요"
export function describeScope(item) {
  const scope = ["instance", "series"].includes(item.scope) ? t(`card.scope.${item.scope}`) : item.scope;
  const count = item.affected_count;
  if (count === undefined || count === null) return scope;
  const suffix = item.kind === "delete_event" ? "Delete" : "";
  return count === 1 ? t(`card.affectedOne${suffix}`, { scope }) : t(`card.affectedMany${suffix}`, { scope, count });
}

// 카드의 실행 버튼 이름: 전부 새로 만드는 제안이면 "Create/만들기", 수정·삭제가 섞이면 "Apply/실행".
export function assistantConfirmLabel(items) {
  return items.every((item) => item.kind?.startsWith("create_")) ? t("assistant.create") : t("assistant.run");
}
