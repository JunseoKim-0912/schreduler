import { apiFetch, getUserId, onApiError, setUserId } from "./api.js";
import { initCalendarPanel } from "./calendar.js";
import { initEventsPanel } from "./events.js";
import { initPersonasPanel } from "./personas.js";
import { initPointsPanel } from "./points.js";
import { initTasksPanel } from "./tasks.js";

// --- 에러 배너 ---------------------------------------------------------------

const banner = document.getElementById("error-banner");
const bannerMessage = document.getElementById("error-message");
const bannerStatus = document.getElementById("error-status");

function showError(error) {
  bannerMessage.textContent = error.message;
  bannerStatus.textContent = error.status ? `HTTP ${error.status} · ${error.method} ${error.path}` : `${error.method} ${error.path}`;
  banner.hidden = false;
}

function hideError() {
  banner.hidden = true;
}

document.getElementById("error-close").addEventListener("click", hideError);
onApiError(showError);

// --- 사용자 ID (X-User-Id) ----------------------------------------------------

const userIdInput = document.getElementById("user-id");
const userIdStatus = document.getElementById("user-id-status");

function saveUserId() {
  const value = userIdInput.value.trim();
  if (value && !/^[1-9]\d*$/.test(value)) {
    userIdStatus.textContent = "숫자(1 이상)만 입력할 수 있어요.";
    userIdStatus.dataset.state = "error";
    return;
  }
  const changed = value !== getUserId();
  const saved = setUserId(value);
  userIdStatus.dataset.state = saved ? "ok" : "error";
  if (!saved) userIdStatus.textContent = "브라우저 저장소를 쓸 수 없어 새로고침하면 사라져요.";
  else userIdStatus.textContent = value ? "저장됨" : "사용자 ID를 지웠어요.";
  if (changed && saved) {
    // 모든 탭의 이전 사용자 상태는 지우고, 다시 불러오는 건 지금 보이는 탭만 한다
    // (숨은 탭의 요청 에러가 배너에 뜨지 않게). 다른 탭은 열 때 activateTab이 불러온다.
    hideError();
    for (const panel of Object.values(panels)) panel.reset();
    panels[activeTab]?.refresh();
  }
}

userIdInput.value = getUserId();
userIdInput.addEventListener("change", saveUserId);
userIdInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") saveUserId();
});

// --- 탭별 기능 ---------------------------------------------------------------

// 탭을 열 때마다 해당 탭의 데이터를 새로 불러온다. 키는 탭 버튼의 data-tab 값이다.
// 한 탭에서 일정을 바꾸면(자연어 실행·되돌리기, 캘린더 완료·삭제, 할 일 완료) 다른 탭의 캘린더·목록·포인트도
// 달라지므로 나머지 탭을 함께 다시 불러온다. 페르소나 탭은 일정과 무관해서 뺀다.
const DATA_TABS = ["calendar", "events", "tasks", "points"];
const refreshOtherTabs = (source) =>
  Promise.all(DATA_TABS.filter((name) => name !== source).map((name) => panels[name].refresh()));

const panels = {
  calendar: initCalendarPanel({
    onDataChanged: () => refreshOtherTabs("calendar"),
    onCreateAt: (text) => {
      activateTab("events");
      panels.events.prefill(text);
    },
  }),
  events: initEventsPanel({ onDataChanged: () => refreshOtherTabs("events") }),
  tasks: initTasksPanel({ onDataChanged: () => refreshOtherTabs("tasks") }),
  points: initPointsPanel(),
  chat: initPersonasPanel(),
};

// --- 탭 --------------------------------------------------------------------

const tabs = [...document.querySelectorAll('[role="tab"]')];
let activeTab = null;

function activateTab(name, { focus = false } = {}) {
  const target = tabs.find((tab) => tab.dataset.tab === name) ?? tabs[0];
  for (const tab of tabs) {
    const selected = tab === target;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    document.getElementById(tab.getAttribute("aria-controls")).hidden = !selected;
  }
  if (focus) target.focus();
  activeTab = target.dataset.tab;
  panels[activeTab]?.refresh();
}

for (const tab of tabs) {
  tab.addEventListener("click", () => activateTab(tab.dataset.tab));
  tab.addEventListener("keydown", (event) => {
    const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
    if (!step) return;
    const next = tabs[(tabs.indexOf(tab) + step + tabs.length) % tabs.length];
    activateTab(next.dataset.tab, { focus: true });
  });
}

// 페이지를 열면 항상 캘린더 탭부터 보여준다.
activateTab("calendar");

// --- 서버 연결 확인 (공통 fetch 동작 확인용) ----------------------------------

const healthButton = document.getElementById("health-check");
const healthStatus = document.getElementById("health-status");

healthButton.addEventListener("click", async () => {
  healthButton.disabled = true;
  healthStatus.textContent = "확인 중…";
  try {
    const data = await apiFetch("/health");
    healthStatus.textContent = data?.status === "ok" ? "서버 연결 정상" : "응답이 예상과 달라요";
    hideError();
  } catch {
    healthStatus.textContent = "연결 실패";
  } finally {
    healthButton.disabled = false;
  }
});
