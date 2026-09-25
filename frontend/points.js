import { apiFetch, getUserId } from "./api.js";
import { el, setStatus } from "./dom.js";
import { describeStreakBonus, formatDate, formatPoints } from "./format.js";

// 포인트 탭: GET /points/summary (X-User-Id 헤더). 오늘 값은 서버가 실시간으로 계산한다.
export function initPointsPanel() {
  const cards = document.getElementById("points-cards");
  const status = document.getElementById("points-status");
  const refreshButton = document.getElementById("points-refresh");

  function card(label, value, detail, { highlight = false } = {}) {
    return el("div", { className: highlight ? "stat stat-highlight" : "stat" }, [
      el("div", { className: "stat-label", text: label }),
      el("div", { className: "stat-value", text: value }),
      el("div", { className: "stat-detail", text: detail }),
    ]);
  }

  function render(summary) {
    const today = summary.today;
    const todayDetail =
      today.total_instances === 0
        ? "오늘은 일정이 없어요"
        : `완료 ${today.done_instances}/${today.total_instances}${today.streak_multiplier > 1 ? ` · 보너스 ×${today.streak_multiplier}` : ""}`;

    cards.replaceChildren(
      card("오늘 포인트", formatPoints(today.points_earned), todayDetail, { highlight: true }),
      card("이번 주 포인트", formatPoints(summary.week_points), `${formatDate(summary.week_start)}부터`),
      card("누적 포인트", formatPoints(summary.total_points), "지금까지 모은 포인트"),
      card("연속 완료", `${summary.current_streak_days}일`, describeStreakBonus(summary.current_streak_days)),
    );
  }

  async function refresh() {
    if (!getUserId()) {
      cards.replaceChildren();
      setStatus(status, "사용자 ID를 입력하면 포인트가 보여요.");
      return;
    }
    refreshButton.disabled = true;
    setStatus(status, "불러오는 중…");
    try {
      render(await apiFetch("/points/summary"));
      setStatus(status, "");
    } catch {
      setStatus(status, "포인트를 불러오지 못했어요.");
    } finally {
      refreshButton.disabled = false;
    }
  }

  refreshButton.addEventListener("click", refresh);

  return {
    refresh,
    reset() {
      cards.replaceChildren();
      setStatus(status, "");
    },
  };
}
