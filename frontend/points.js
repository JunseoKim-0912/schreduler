import { apiFetch, getUserId } from "./api.js";
import { el, setStatus } from "./dom.js";
import { describeStreakBonus, formatDate, formatPoints } from "./format.js";
import { t } from "./i18n.js";

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
        ? t("points.noEventsToday")
        : t("points.todayDone", { done: today.done_instances, total: today.total_instances }) +
          (today.streak_multiplier > 1 ? t("points.bonus", { multiplier: today.streak_multiplier }) : "");

    cards.replaceChildren(
      card(t("points.today"), formatPoints(today.points_earned), todayDetail, { highlight: true }),
      card(t("points.week"), formatPoints(summary.week_points), t("points.since", { date: formatDate(summary.week_start) })),
      card(t("points.total"), formatPoints(summary.total_points), t("points.allTime")),
      card(t("points.streak"), t("points.days", { count: summary.current_streak_days }), describeStreakBonus(summary.current_streak_days)),
    );
  }

  async function refresh() {
    if (!getUserId()) {
      cards.replaceChildren();
      setStatus(status, t("points.needUser"));
      return;
    }
    refreshButton.disabled = true;
    setStatus(status, t("common.loading"));
    try {
      render(await apiFetch("/points/summary"));
      setStatus(status, "");
    } catch {
      setStatus(status, t("points.loadFailed"));
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
