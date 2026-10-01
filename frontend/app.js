import { apiFetch, getUserId, onApiError, setUserId } from "./api.js";
import { initCalendarPanel } from "./calendar.js";
import { initEventsPanel } from "./events.js";
import { initPersonasPanel } from "./personas.js";
import { initPointsPanel } from "./points.js";
import { initTasksPanel } from "./tasks.js";
import { getLang, onLangChange, setLang, t } from "./i18n.js";

// --- 화면 언어 (Eng | Kor) ------------------------------------------------------
// 처음 방문하면 영어. 고른 언어는 localStorage에 남고, 백엔드 User.preferred_language도 같이 바꿔서 알림·경고·
// 시간 표시·카테고리 라벨이 같은 언어로 나오게 한다.

const langButtons = [...document.querySelectorAll(".lang-switch [data-lang]")];

function applyStaticText() {
  document.documentElement.lang = getLang();
  for (const node of document.querySelectorAll("[data-i18n]")) node.textContent = t(node.dataset.i18n);
  for (const node of document.querySelectorAll("[data-i18n-placeholder]")) node.placeholder = t(node.dataset.i18nPlaceholder);
  for (const node of document.querySelectorAll("[data-i18n-aria-label]")) node.setAttribute("aria-label", t(node.dataset.i18nAriaLabel));
  for (const button of langButtons) {
    const selected = button.dataset.lang === getLang();
    button.setAttribute("aria-checked", String(selected));
    button.tabIndex = selected ? 0 : -1;
  }
}

async function syncBackendLanguage() {
  if (!getUserId()) return;
  try {
    await apiFetch("/users/me/language", { method: "PUT", body: { language: getLang() }, showError: false });
  } catch {
    // 서버가 꺼져 있어도 화면 언어는 바뀐다. 다음에 언어를 고르거나 사용자 ID를 저장할 때 다시 맞춘다.
  }
}

for (const button of langButtons) {
  button.addEventListener("click", () => setLang(button.dataset.lang));
  button.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    const other = langButtons.find((b) => b.dataset.lang !== getLang());
    setLang(other.dataset.lang);
    other.focus();
  });
}

onLangChange(async () => {
  applyStaticText();
  await syncBackendLanguage();
  // 화면에 그려 둔 목록·카드도 새 언어로 다시 그린다 (지금 탭만 다시 불러오고, 나머지는 열 때 불러온다).
  hideError();
  panels.tasks.relabel();
  for (const panel of Object.values(panels)) panel.reset();
  panels[activeTab]?.refresh();
});

applyStaticText();

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
    userIdStatus.textContent = t("user.invalid");
    userIdStatus.dataset.state = "error";
    return;
  }
  const changed = value !== getUserId();
  const saved = setUserId(value);
  userIdStatus.dataset.state = saved ? "ok" : "error";
  if (!saved) userIdStatus.textContent = t("user.storageBlocked");
  else userIdStatus.textContent = value ? t("user.saved") : t("user.cleared");
  if (changed && saved) {
    // 모든 탭의 이전 사용자 상태는 지우고, 다시 불러오는 건 지금 보이는 탭만 한다
    // (숨은 탭의 요청 에러가 배너에 뜨지 않게). 다른 탭은 열 때 activateTab이 불러온다.
    hideError();
    for (const panel of Object.values(panels)) panel.reset();
    syncBackendLanguage().then(() => panels[activeTab]?.refresh());
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

// 페이지를 열면 항상 캘린더 탭부터 보여준다. 백엔드 언어를 먼저 화면 언어로 맞춘다 (처음 방문이면 영어).
syncBackendLanguage().then(() => activateTab("calendar"));

// --- 서버 연결 확인 (공통 fetch 동작 확인용) ----------------------------------

const healthButton = document.getElementById("health-check");
const healthStatus = document.getElementById("health-status");

healthButton.addEventListener("click", async () => {
  healthButton.disabled = true;
  healthStatus.textContent = t("health.checking");
  try {
    const data = await apiFetch("/health");
    healthStatus.textContent = data?.status === "ok" ? t("health.ok") : t("health.unexpected");
    hideError();
  } catch {
    healthStatus.textContent = t("health.failed");
  } finally {
    healthButton.disabled = false;
  }
});
