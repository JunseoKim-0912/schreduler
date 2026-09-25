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
  let text = interval > 1 ? `${interval}${freq.unit}마다` : freq.every;
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
