// 화면 표시용 순수 함수 모음. DOM에 의존하지 않아 Node에서 테스트한다 (tests/frontend/format.test.mjs).

// 중요도 체계 (docs/기획보고서.md 3절). null은 "없음(수면)", 6은 MAX.
export const IMPORTANCE_OPTIONS = [
  { value: 1, label: "1 · 개인 여가" },
  { value: 2, label: "2 · 타인과의 약속" },
  { value: 3, label: "3 · 출석 체크 없는 의무" },
  { value: 4, label: "4 · 공식 의무/평가" },
  { value: 5, label: "5 · 반드시 지켜야 함" },
  { value: 6, label: "MAX · 예외적으로 중요" },
];

export function importanceLabel(value) {
  if (value === null || value === undefined) return "없음";
  return IMPORTANCE_OPTIONS.find((option) => option.value === value)?.label ?? String(value);
}

const WEEKDAYS = ["일", "월", "화", "수", "목", "금", "토"];
const BYDAY_NAMES = { MO: "월", TU: "화", WE: "수", TH: "목", FR: "금", SA: "토", SU: "일" };
const FREQ = {
  DAILY: { every: "매일", unit: "일" },
  WEEKLY: { every: "매주", unit: "주" },
  MONTHLY: { every: "매월", unit: "개월" },
  YEARLY: { every: "매년", unit: "년" },
};

// 서버의 일시는 타임존 없는 로컬 시각 문자열("2026-09-25T18:00:00")이다. Date로 바꾸면 환경 시간대가 끼어들 수
// 있어 문자열을 직접 나눠 쓴다.
function parts(isoDateTime) {
  const match = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/.exec(isoDateTime ?? "");
  if (!match) return null;
  const [, year, month, day, hour = "00", minute = "00"] = match;
  const weekday = WEEKDAYS[new Date(Number(year), Number(month) - 1, Number(day)).getDay()];
  return { date: `${year}-${month}-${day}`, time: `${hour}:${minute}`, weekday };
}

export function formatDate(isoDateTime) {
  const p = parts(isoDateTime);
  return p ? `${p.date} (${p.weekday})` : "";
}

// "2026-09-01" → "9/1" (반복 기간처럼 짧게 보여줄 때)
export function formatShortDate(isoDate) {
  const p = parts(isoDate);
  return p ? `${Number(p.date.slice(5, 7))}/${Number(p.date.slice(8, 10))}` : "";
}

export function formatTime(isoDateTime) {
  return parts(isoDateTime)?.time ?? "";
}

export function formatDateTime(isoDateTime) {
  const p = parts(isoDateTime);
  return p ? `${p.date} (${p.weekday}) ${p.time}` : "";
}

// "FREQ=WEEKLY;BYDAY=TU,TH" → "매주 화·목". 모르는 규칙은 원문을 그대로 보여준다.
export function describeRecurrence(rule) {
  if (!rule) return "반복 안 함";
  const fields = Object.fromEntries(
    rule
      .replace(/^RRULE:/, "")
      .split(";")
      .map((part) => part.split("="))
      .filter((pair) => pair.length === 2),
  );
  const freq = FREQ[fields.FREQ];
  if (!freq) return rule;

  const interval = Number(fields.INTERVAL ?? 1);
  let text = freq.every;
  if (interval === 2 && fields.FREQ === "WEEKLY") text = "격주";
  else if (interval > 1) text = `${interval}${freq.unit}마다`;
  if (fields.BYDAY) {
    const days = fields.BYDAY.split(",").map((day) => BYDAY_NAMES[day] ?? day);
    text += ` ${days.join("·")}`;
  }
  if (fields.COUNT) text += ` (${fields.COUNT}회)`;
  return text;
}

// 일정의 시간 부분. deadline은 end_time이 마감 일시다.
export function describeEventTime(event) {
  if (event.event_type === "deadline" || !event.start_time) return `${formatDateTime(event.end_time)} 마감`;
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
  return `${Number(value ?? 0).toLocaleString("ko-KR", { maximumFractionDigits: 2 })}점`;
}

// FR-10 연속 100% 완료 보너스 (app/services/points.py STREAK_MULTIPLIERS와 같은 값)
export const STREAK_TIERS = [
  { days: 3, multiplier: 1.1 },
  { days: 7, multiplier: 1.25 },
  { days: 14, multiplier: 1.5 },
];

export function describeStreakBonus(streakDays) {
  const next = STREAK_TIERS.find((tier) => tier.days > streakDays);
  if (!next) return `최고 보너스 ×${STREAK_TIERS.at(-1).multiplier} 적용 중`;
  const current = [...STREAK_TIERS].reverse().find((tier) => tier.days <= streakDays);
  const nextText = `${next.days - streakDays}일 더 이어가면 ×${next.multiplier}`;
  return current ? `×${current.multiplier} 적용 중 · ${nextText}` : nextText;
}

// --- 최근 변경 (GET /actions) ---

const ACTION_SOURCE_LABELS = { nl: "자연어", ui: "목록에서 삭제" };

export function describeAction(action) {
  return [ACTION_SOURCE_LABELS[action.source] ?? action.source, formatDateTime(action.created_at)].filter(Boolean).join(" · ");
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

// "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU" → "격주" (요일 없이 반복 간격만)
function recurrenceEvery(rule) {
  return describeRecurrence(rule).replace(/ [월화수목금토일](?:·[월화수목금토일])*$/, "");
}

function describeLocation(location) {
  if (!location) return "없음";
  const minutes = location.travel_minutes ?? location.default_travel_minutes;
  const details = [minutes !== undefined && minutes !== null ? `이동 ${minutes}분` : "", location.is_new ? "새로 등록" : ""];
  const extra = details.filter(Boolean).join(", ");
  return extra ? `${location.name} (${extra})` : location.name;
}

// 수정·삭제 대상 한 줄: "물리 퀴즈 · 2026-09-29 (화) · 오후 6:00 – 오후 7:00 (1시간) → 오후 7:00 – …"
export function describeAssistantTarget(target, scope) {
  const when = scope === "series" && target.recurring ? "반복 전체" : [formatDate(target.date), target.time_display].filter(Boolean).join(" · ");
  const text = [target.title, when].filter(Boolean).join(" · ");
  if (!target.new_time_display) return text;
  const after = [target.new_date && target.new_date !== target.date ? formatDate(target.new_date) : "", target.new_time_display];
  return `${text} → ${after.filter(Boolean).join(" ")}`;
}

const RANGE_MODES = { range_only: "기간만 삭제 (일정은 마지막 회차까지 유지)", with_events: "일정도 함께 삭제" };
const SCOPES = { instance: "이 회차만", series: "반복 전체" };

/**
 * 확인 카드 초안 하나를 화면용으로 풀어 쓴다.
 * 돌려주는 값: { heading, rows: [{key, label, value, inferred}], notes, targets, warnings }
 * 시간은 백엔드가 준 time_display를 그대로 쓴다 (오전/오후·다음 날 표기를 한 곳에서만 만든다).
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
      heading = "새 일정";
      row("title", "제목", item.title);
      row("event_type", "종류", item.event_type === "deadline" ? "마감" : "일반");
      row("date", item.recurring ? "첫 회차" : "날짜", formatDate(item.date));
      row("time", item.event_type === "deadline" ? "마감 시각" : "시간", item.time_display);
      row("importance", "중요도", importanceLabel(item.importance));
      if (item.recurring) {
        row("recurrence", "반복", describeRecurrence(item.recurrence_rule).replace(/ ([월화수목금토일](?:·[월화수목금토일])*)$/, " $1요일"));
        if (item.preview_dates?.length) row("preview", "다음 회차", item.preview_dates.map(formatShortDate).join(", "));
        // 기준일은 격주 이상에서만 리듬을 정한다 (매주면 어느 주에서 세든 같다).
        const interval = Number(/INTERVAL=(\d+)/.exec(item.recurrence_rule ?? "")?.[1] ?? 1);
        if (interval > 1 && item.recurrence_start && item.recurrence_start !== item.date) {
          notes.push(`${formatShortDate(item.recurrence_start)} 기준 ${recurrenceEvery(item.recurrence_rule)}`);
        }
        const range = item.date_range;
        if (range) {
          const span = `${formatShortDate(range.start_date)}~${formatShortDate(range.end_date)}`;
          row("date_range", "기간", `${range.name} (${span}${range.is_new ? ", 새로 만듦" : ""})`);
        }
      } else {
        row("recurrence", "반복", "반복 안 함");
      }
      row("location", "장소", describeLocation(item.location));
      break;
    }
    case "update_event":
    case "delete_event": {
      const verb = item.kind === "delete_event" ? "삭제" : "수정";
      const count = item.targets?.length ?? 0;
      heading = `${verb}할 일정 ${count}개${item.scope === "instance" ? " (이 회차만)" : ""}`;
      targets = (item.targets ?? []).map((target) => describeAssistantTarget(target, item.scope));
      const changes = item.changes ?? {};
      if (changes.title) row("title", "새 제목", changes.title);
      if (changes.date) row("date", "새 날짜", formatDate(changes.date));
      if (changes.start_time || changes.end_time) {
        row("time", "새 시간", [changes.start_time, changes.end_time].map((t) => t ?? "그대로").join(" – "));
      }
      if (changes.importance !== null && changes.importance !== undefined) row("importance", "새 중요도", importanceLabel(changes.importance));
      if (changes.location) {
        row("location", "장소", changes.location.action === "remove" ? "빼기" : describeLocation(changes.location));
      }
      if (changes.event_type) row("event_type", "새 종류", changes.event_type === "deadline" ? "마감" : "일반");
      if (item.scope) row("scope", "범위", describeScope(item));
      if (item.detaches) notes.push("이 회차만 따로 떼어 단발 일정으로 바꿔요. 다른 회차는 그대로예요.");
      break;
    }
    case "create_range":
      heading = "새 반복 기간";
      row("title", "이름", item.name);
      row("date_range", "기간", `${formatShortDate(item.start_date)}~${formatShortDate(item.end_date)}`);
      break;
    case "update_range":
      heading = "반복 기간 수정";
      row("title", "이름", item.new_name ? `${item.name} → ${item.new_name}` : item.name);
      row(
        "date_range",
        "기간",
        `${formatShortDate(item.before?.start_date)}~${formatShortDate(item.before?.end_date)} → ${formatShortDate(item.start_date)}~${formatShortDate(item.end_date)}`,
      );
      if (item.events_using) row("events_using", "쓰는 일정", `${item.events_using}개 (회차를 다시 맞춰요)`);
      break;
    case "delete_range":
      heading = "반복 기간 삭제";
      row("title", "이름", item.name);
      if (item.events_using) {
        row("events_using", "쓰는 일정", `${item.events_using}개`);
        row("mode", "처리", RANGE_MODES[item.mode] ?? item.mode);
      }
      break;
    default:
      heading = item.kind ?? "제안";
  }
  const warnings = (item.warnings ?? []).map((warning) => `⚠ ${warning.message}`);
  return { heading, rows, notes, targets, warnings };
}

// "이 회차만 · 1개 회차만 바뀌어요" / "반복 전체 · 12개 회차가 바뀌어요"
export function describeScope(item) {
  const scope = SCOPES[item.scope] ?? item.scope;
  const count = item.affected_count;
  if (count === undefined || count === null) return scope;
  const verb = item.kind === "delete_event" ? "취소돼요" : "바뀌어요";
  return count === 1 ? `${scope} · 1개 회차만 ${verb}` : `${scope} · ${count}개 회차가 ${verb}`;
}

// 카드의 실행 버튼 이름: 전부 새로 만드는 제안이면 "만들기", 수정·삭제가 섞이면 "실행".
export function assistantConfirmLabel(items) {
  return items.every((item) => item.kind?.startsWith("create_")) ? "만들기" : "실행";
}
