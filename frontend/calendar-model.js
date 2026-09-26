// 캘린더 계산(주·날짜, 블록 위치, 겹침 배치, 라벨). DOM에 의존하지 않아 Node에서 테스트한다.
// 서버 일시는 타임존 없는 로컬 시각("2026-09-24T19:00:00")이다. new Date(문자열)이나 toISOString()처럼
// UTC를 거치는 변환을 쓰지 않고, 연·월·일·시·분을 그대로 브라우저 로컬 시각으로 다룬다.

export const HOUR_HEIGHT = 48; // px, CSS의 --hour와 같은 값
export const MIN_BLOCK_MINUTES = 20; // 아주 짧은 일정도 글자가 보이게 하는 최소 높이(겹침 계산에도 같은 값을 써야 서로 가리지 않는다)
const WEEKDAYS = ["일", "월", "화", "수", "목", "금", "토"];
const pad = (n) => String(n).padStart(2, "0");

export function parseLocalDateTime(value) {
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?/.exec(value ?? "");
  if (!m) return null;
  const [, y, mo, d, h = "0", mi = "0", s = "0"] = m;
  return new Date(Number(y), Number(mo) - 1, Number(d), Number(h), Number(mi), Number(s));
}

export function toIsoDate(date) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

export function startOfDay(date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

// 연·월·일로 계산해 서머타임이 있는 시간대에서도 하루가 23/25시간이 되지 않는다.
export function addDays(date, days) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate() + days);
}

export function startOfWeek(date) {
  return addDays(startOfDay(date), -((date.getDay() + 6) % 7)); // 월요일 시작
}

export function visibleDays(anchor, mode) {
  if (mode === "day") return [startOfDay(anchor)];
  const monday = startOfWeek(anchor);
  return Array.from({ length: 7 }, (_, i) => addDays(monday, i));
}

export function stepDays(mode) {
  return mode === "day" ? 1 : 7;
}

export function weekdayLabel(date) {
  return WEEKDAYS[date.getDay()];
}

export function formatRangeTitle(days) {
  const first = days[0];
  const last = days.at(-1);
  const [y1, m1, d1] = [first.getFullYear(), first.getMonth() + 1, first.getDate()];
  const [y2, m2, d2] = [last.getFullYear(), last.getMonth() + 1, last.getDate()];
  if (days.length === 1) return `${y1}년 ${m1}월 ${d1}일 (${weekdayLabel(first)})`;
  if (y1 !== y2) return `${y1}년 ${m1}월 ${d1}일 – ${y2}년 ${m2}월 ${d2}일`;
  if (m1 !== m2) return `${y1}년 ${m1}월 ${d1}일 – ${m2}월 ${d2}일`;
  return `${y1}년 ${m1}월 ${d1}일 – ${d2}일`;
}

function dayNumber(date) {
  return Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()) / 86_400_000;
}

// day 00:00 기준 벽시계 분. 전날이면 음수, 다음 날이면 1440 이상.
export function wallMinutes(dateTime, day) {
  return (dayNumber(dateTime) - dayNumber(day)) * 1440 + dateTime.getHours() * 60 + dateTime.getMinutes();
}

export function timeOf(value) {
  return /T(\d{2}:\d{2})/.exec(value ?? "")?.[1] ?? "";
}

// 그 날짜 열에 그릴 scheduled 블록들. 자정을 넘는 일정은 날짜마다 잘라서 그린다.
export function scheduledSegments(items, day) {
  const segments = [];
  for (const item of items) {
    if (item.event_type !== "scheduled" || !item.start_time) continue;
    const start = wallMinutes(parseLocalDateTime(item.start_time), day);
    const end = Math.max(wallMinutes(parseLocalDateTime(item.end_time), day), start + 1);
    if (end <= 0 || start >= 1440) continue;
    const startMin = Math.max(0, start);
    segments.push({
      item,
      startMin,
      endMin: Math.min(1440, Math.max(end, startMin + MIN_BLOCK_MINUTES)),
      continuesBefore: start < 0,
      continuesAfter: end > 1440,
    });
  }
  return layoutOverlaps(segments);
}

// 겹치는 블록끼리 열 안에서 폭을 나눈다. 서로 이어서 겹치는 묶음(cluster)마다 필요한 칸 수를 구하고,
// 각 블록은 비어 있는 가장 왼쪽 칸에 놓는다.
export function layoutOverlaps(segments) {
  const sorted = [...segments].sort((a, b) => a.startMin - b.startMin || b.endMin - a.endMin);
  const result = [];
  let cluster = [];
  let columns = [];
  let clusterEnd = -Infinity;

  const flush = () => {
    for (const seg of cluster) result.push({ ...seg, cols: columns.length });
    cluster = [];
    columns = [];
  };

  for (const seg of sorted) {
    if (seg.startMin >= clusterEnd) flush();
    let col = columns.findIndex((end) => end <= seg.startMin);
    if (col === -1) {
      col = columns.length;
      columns.push(seg.endMin);
    } else {
      columns[col] = seg.endMin;
    }
    cluster.push({ ...seg, col });
    clusterEnd = cluster.length === 1 ? seg.endMin : Math.max(clusterEnd, seg.endMin);
  }
  flush();
  return result;
}

export function deadlinesOn(items, day) {
  const iso = toIsoDate(day);
  return items
    .filter((item) => item.event_type === "deadline" && item.end_time?.startsWith(iso))
    .sort((a, b) => a.end_time.localeCompare(b.end_time));
}

export function isOverdue(item, now) {
  return item.event_type === "deadline" && item.status !== "done" && parseLocalDateTime(item.end_time) < now;
}

export function importanceClass(importance) {
  if (importance === null || importance === undefined) return "imp-none";
  return Number(importance) >= 6 ? "imp-max" : `imp-${importance}`;
}

const STATUS_LABELS = { pending: "예정", done: "완료", missed: "놓침", cancelled: "취소됨" };

export function statusLabel(status) {
  return STATUS_LABELS[status] ?? status;
}

// 이번 날짜들 중 오늘이 있으면 현재 시각 선의 위치
export function nowMarker(now, days) {
  const dayIndex = days.findIndex((day) => toIsoDate(day) === toIsoDate(now));
  return dayIndex === -1 ? null : { dayIndex, minutes: now.getHours() * 60 + now.getMinutes() };
}

export function hourFromOffset(offsetY, hourHeight = HOUR_HEIGHT) {
  return Math.min(23, Math.max(0, Math.floor(offsetY / hourHeight)));
}

// 빈 칸을 눌렀을 때 자연어 입력창에 미리 채울 문구
export function prefillText(day, hour) {
  return `${day.getMonth() + 1}월 ${day.getDate()}일 ${hour}시에 `;
}

function formatDay(value) {
  const d = parseLocalDateTime(value);
  return `${d.getMonth() + 1}월 ${d.getDate()}일 (${weekdayLabel(d)})`;
}

export function describeItemTime(item) {
  if (item.event_type === "deadline" || !item.start_time) return `${formatDay(item.end_time)} ${timeOf(item.end_time)} 마감`;
  const sameDay = item.start_time.slice(0, 10) === item.end_time.slice(0, 10);
  const end = sameDay ? timeOf(item.end_time) : `${formatDay(item.end_time)} ${timeOf(item.end_time)}`;
  return `${formatDay(item.start_time)} ${timeOf(item.start_time)}–${end}`;
}
