import { apiFetch, getAccount, getUserId, onApiError, onUnauthorized, setAccount } from "./api.js";
import { initCalendarPanel } from "./calendar.js";
import { initEventsPanel } from "./events.js";
import { initPersonasPanel } from "./personas.js";
import { initPointsPanel } from "./points.js";
import { initTasksPanel } from "./tasks.js";
import { getLang, onLangChange, setLang, t } from "./i18n.js";
import { clearUsage, relabelUsage } from "./usage.js";
import { demoBannerText } from "./demo.js";
import { setStatus } from "./dom.js";

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
  renderDemoBanner();
  renderVersion();
  await syncBackendLanguage();
  // 화면에 그려 둔 목록·카드도 새 언어로 다시 그린다 (지금 탭만 다시 불러오고, 나머지는 열 때 불러온다).
  hideError();
  panels.tasks.relabel();
  relabelUsage();
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

// --- 로그인 · 회원가입 ---------------------------------------------------------
// 세션은 서버가 준 HttpOnly 쿠키다. 시작할 때 GET /auth/me로 로그인 여부를 보고, 아니면 로그인 화면을 보여준다.
// 어느 요청이든 401이 오면(세션 만료, 다른 곳에서 로그아웃) 로그인 화면으로 돌아간다.

const authScreen = document.getElementById("auth-screen");
const appContent = document.getElementById("app-content");
const accountBox = document.getElementById("account");
const accountEmail = document.getElementById("account-email");
const loginForm = document.getElementById("login-form");
const signupForm = document.getElementById("signup-form");
const loginStatus = document.getElementById("login-status");
const signupStatus = document.getElementById("signup-status");
const demoEntry = document.getElementById("demo-entry");
const demoStart = document.getElementById("demo-start");
const demoStatus = document.getElementById("demo-status");
const demoBanner = document.getElementById("demo-banner");
const demoBannerLabel = document.getElementById("demo-banner-text");
let demoTimer = null;

// Demo accounts have no email; the banner (time left + Exit demo) replaces the account box. Exit is a plain log out —
// the account and its data stay until they expire, so the same browser can come back within 24 hours.
function renderDemoBanner() {
  const account = getAccount();
  const demo = Boolean(account?.is_demo);
  demoBanner.hidden = !demo;
  if (demo) demoBannerLabel.textContent = demoBannerText(account);
}

function startDemoBanner() {
  clearInterval(demoTimer);
  renderDemoBanner();
  demoTimer = getAccount()?.is_demo ? setInterval(renderDemoBanner, 60_000) : null;
}

function showAuth(form = "login") {
  authScreen.hidden = false;
  appContent.hidden = true;
  accountBox.hidden = true;
  loginForm.hidden = form !== "login";
  signupForm.hidden = form !== "signup";
  (form === "login" ? document.getElementById("login-email") : document.getElementById("signup-email")).focus();
}

async function enterApp(me) {
  setAccount(me);
  accountEmail.textContent = me.email ?? "";
  accountBox.hidden = Boolean(me.is_demo);
  startDemoBanner();
  authScreen.hidden = true;
  appContent.hidden = false;
  for (const form of [loginForm, signupForm]) form.reset();
  setStatus(loginStatus, "");
  setStatus(signupStatus, "");
  setStatus(demoStatus, "");
  await syncBackendLanguage();
  activateTab(activeTab ?? "calendar");
}

function leaveApp() {
  // 이전 사용자의 화면 상태(목록·대화·사용량)를 모두 지운다.
  setAccount(null);
  startDemoBanner();
  hideError();
  clearUsage();
  for (const panel of Object.values(panels)) panel.reset();
  showAuth("login");
}

onUnauthorized(() => {
  if (getAccount()) leaveApp();
});

async function submitAuth(event, path, form, status, body) {
  event.preventDefault();
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  setStatus(status, "");
  try {
    await enterApp(await apiFetch(path, { method: "POST", body, showError: false }));
  } catch (error) {
    setStatus(status, error.message);
  } finally {
    button.disabled = false;
  }
}

loginForm.addEventListener("submit", (event) =>
  submitAuth(event, "/auth/login", loginForm, loginStatus, {
    email: document.getElementById("login-email").value.trim(),
    password: document.getElementById("login-password").value,
  }),
);

signupForm.addEventListener("submit", (event) => {
  const password = document.getElementById("signup-password").value;
  if (password.length < 8) {
    event.preventDefault();
    setStatus(signupStatus, t("auth.passwordHint"));
    return;
  }
  const invite = document.getElementById("signup-invite").value.trim();
  submitAuth(event, "/auth/signup", signupForm, signupStatus, {
    email: document.getElementById("signup-email").value.trim(),
    password,
    ...(invite ? { invite_code: invite } : {}),
  });
});

demoStart.addEventListener("click", async () => {
  demoStart.disabled = true;
  setStatus(demoStatus, t("demo.starting"));
  try {
    await enterApp(await apiFetch("/auth/demo", { method: "POST", showError: false }));
  } catch (error) {
    setStatus(demoStatus, error.status === 404 ? t("demo.unavailable") : error.message);
  } finally {
    demoStart.disabled = false;
  }
});

// --- Prototype badge and About ----------------------------------------------------------
// GET /health says whether the demo is on (the demo button only shows then) and which version is running
// (app/core/version.py), shown as "Prototype · v0.1.0" next to the name. The badge opens the About dialog.

const aboutDialog = document.getElementById("about-dialog");
const aboutOpen = document.getElementById("about-open");
let appVersion = "";

function renderVersion() {
  aboutOpen.textContent = appVersion ? t("proto.badgeVersion", { version: appVersion }) : t("proto.badge");
  document.getElementById("about-version").textContent = appVersion ? `v${appVersion}` : "";
}

aboutOpen.addEventListener("click", () => aboutDialog.showModal());
document.getElementById("about-close").addEventListener("click", () => aboutDialog.close());
// A click on the backdrop lands on the <dialog> itself (its content is in child elements).
aboutDialog.addEventListener("click", (event) => {
  if (event.target === aboutDialog) aboutDialog.close();
});
aboutDialog.addEventListener("close", () => aboutOpen.focus());

apiFetch("/health", { showError: false })
  .then((health) => {
    demoEntry.hidden = !health?.demo_mode;
    appVersion = health?.version ?? "";
    renderVersion();
  })
  .catch(() => {
    demoEntry.hidden = true;
  });

document.getElementById("show-signup").addEventListener("click", () => showAuth("signup"));
document.getElementById("show-login").addEventListener("click", () => showAuth("login"));
async function logOut() {
  try {
    await apiFetch("/auth/logout", { method: "POST", showError: false });
  } finally {
    leaveApp();
  }
}

document.getElementById("logout").addEventListener("click", logOut);
document.getElementById("demo-exit").addEventListener("click", logOut);

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

// 페이지를 열면 로그인했는지부터 본다. 로그인했으면 캘린더 탭부터 보여준다 (백엔드 언어를 먼저 화면 언어로 맞춘다).
apiFetch("/auth/me", { showError: false })
  .then(enterApp)
  .catch((error) => {
    if (error.status === 401) showAuth("login");
    else {
      showError(error);
      showAuth("login");
    }
  });

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
