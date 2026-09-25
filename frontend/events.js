import { apiFetch, getUserId } from "./api.js";
import { badge, el, setStatus } from "./dom.js";
import { describeEventTime, describeRecurrence, formatDate, formatTime, importanceLabel, sortEvents } from "./format.js";

// 이벤트 탭: 자연어 일정 추가(POST /events/parse 여러 턴 → 확인 → POST /events)와 이벤트 목록(GET /events).
// 대화 상태는 서버가 session_id로 보관하므로, 클라이언트는 session_id와 새 발화만 보낸다.
export function initEventsPanel() {
  const chatLog = document.getElementById("chat-log");
  const form = document.getElementById("parse-form");
  const input = document.getElementById("parse-input");
  const sendButton = document.getElementById("parse-send");
  const resetButton = document.getElementById("parse-reset");
  const chatStatus = document.getElementById("chat-status");

  const confirmBox = document.getElementById("draft-confirm");
  const draftFields = document.getElementById("draft-fields");
  const createButton = document.getElementById("draft-create");
  const cancelButton = document.getElementById("draft-cancel");

  const list = document.getElementById("event-list");
  const listStatus = document.getElementById("events-status");
  const refreshButton = document.getElementById("events-refresh");

  let sessionId = null;
  let draft = null;
  const dateRangeNames = new Map();

  function addMessage(role, text) {
    chatLog.append(el("li", { className: `bubble bubble-${role}`, text }));
    chatLog.hidden = false;
    chatLog.scrollTop = chatLog.scrollHeight;
  }

  function resetConversation() {
    sessionId = null;
    draft = null;
    chatLog.replaceChildren();
    chatLog.hidden = true;
    confirmBox.hidden = true;
    setStatus(chatStatus, "");
    input.disabled = false;
    sendButton.disabled = false;
    input.placeholder = "예: 매주 화요일 저녁 7시에 알고리즘 스터디";
  }

  async function dateRangeName(id) {
    if (id === null || id === undefined) return "지정 안 함 (반복 회차가 생성되지 않아요)";
    if (!dateRangeNames.has(id)) {
      try {
        const range = await apiFetch(`/date-ranges/${id}`, { showError: false });
        dateRangeNames.set(id, `${range.name} (${range.start_date} ~ ${range.end_date})`);
      } catch {
        dateRangeNames.set(id, `#${id}`);
      }
    }
    return dateRangeNames.get(id);
  }

  async function showDraft(newDraft) {
    draft = newDraft;
    const rows = [
      ["제목", draft.title],
      ["시간", `${formatTime(draft.start_time)}–${formatTime(draft.end_time)}`],
      ["시작일", formatDate(draft.start_time)],
      ["반복", describeRecurrence(draft.is_recurring ? draft.recurrence_rule : null)],
      ["반복 기간", await dateRangeName(draft.date_range_id)],
      ["중요도", importanceLabel(draft.importance)],
    ];
    draftFields.replaceChildren(...rows.flatMap(([label, value]) => [el("dt", { text: label }), el("dd", { text: value })]));
    confirmBox.hidden = false;
    input.disabled = true;
    sendButton.disabled = true;
    createButton.focus();
  }

  async function send(event) {
    event.preventDefault();
    const utterance = input.value.trim();
    if (!utterance) return;
    const userId = getUserId();
    if (!userId) {
      setStatus(chatStatus, "위에서 사용자 ID를 먼저 입력해 주세요.");
      return;
    }

    addMessage("user", utterance);
    input.value = "";
    sendButton.disabled = true;
    setStatus(chatStatus, "AI가 생각하는 중…");
    try {
      const body = { user_id: Number(userId), utterance };
      if (sessionId) body.session_id = sessionId;
      const result = await apiFetch("/events/parse", { method: "POST", body });
      sessionId = result.session_id;
      setStatus(chatStatus, "");
      if (result.is_complete && result.draft) {
        addMessage("assistant", "이 내용으로 만들까요?");
        await showDraft(result.draft);
        return;
      }
      addMessage("assistant", result.next_question?.question ?? "조금 더 자세히 알려 주세요.");
      input.placeholder = "답을 입력하세요";
    } catch (error) {
      // 서버 재시작 등으로 세션이 사라졌으면 새 대화로 다시 시작하게 한다.
      if (error.status === 404 && sessionId) {
        resetConversation();
        setStatus(chatStatus, "대화가 만료되어 새로 시작했어요. 다시 입력해 주세요.");
      } else {
        setStatus(chatStatus, "전송하지 못했어요. 위의 안내를 확인해 주세요.");
      }
    } finally {
      if (!draft) sendButton.disabled = false;
      if (!draft) input.focus();
    }
  }

  async function createFromDraft() {
    createButton.disabled = true;
    cancelButton.disabled = true;
    try {
      const created = await apiFetch("/events", { method: "POST", body: draft });
      resetConversation();
      setStatus(chatStatus, `"${created.title}" 일정을 만들었어요.`);
      await refresh();
    } catch {
      setStatus(chatStatus, "일정을 만들지 못했어요. 내용을 확인하고 다시 시도해 주세요.");
    } finally {
      createButton.disabled = false;
      cancelButton.disabled = false;
    }
  }

  function renderEvent(event) {
    const badges = [
      event.event_type === "deadline" ? badge("마감", "deadline") : badge("일정", "scheduled"),
      event.child_kind === "travel" ? badge("이동", "muted") : null,
      event.child_kind === "custom" ? badge("준비", "muted") : null,
    ];
    const meta = [describeEventTime(event)];
    if (event.is_recurring) meta.push(describeRecurrence(event.recurrence_rule));
    if (event.importance !== null) meta.push(`중요도 ${importanceLabel(event.importance)}`);

    return el("li", { className: "list-item" }, [
      el("div", { className: "list-main" }, [
        el("div", { className: "list-title" }, [...badges, el("span", { text: event.title })]),
        el("div", { className: "list-meta", text: meta.join(" · ") }),
      ]),
    ]);
  }

  async function refresh() {
    const userId = getUserId();
    if (!userId) {
      list.replaceChildren();
      setStatus(listStatus, "사용자 ID를 입력하면 이벤트 목록이 보여요.");
      return;
    }
    refreshButton.disabled = true;
    setStatus(listStatus, "불러오는 중…");
    try {
      const events = await apiFetch(`/events?user_id=${encodeURIComponent(userId)}`);
      list.replaceChildren(...sortEvents(events).map(renderEvent));
      setStatus(listStatus, events.length ? "" : "아직 이벤트가 없어요.");
    } catch {
      setStatus(listStatus, "목록을 불러오지 못했어요.");
    } finally {
      refreshButton.disabled = false;
    }
  }

  form.addEventListener("submit", send);
  resetButton.addEventListener("click", () => {
    resetConversation();
    input.focus();
  });
  createButton.addEventListener("click", createFromDraft);
  cancelButton.addEventListener("click", () => {
    resetConversation();
    setStatus(chatStatus, "취소했어요. 새로 입력해 주세요.");
  });
  refreshButton.addEventListener("click", refresh);

  resetConversation();
  return {
    refresh,
    // 사용자가 바뀌면 이전 사용자의 대화 세션과 목록을 지운다.
    reset() {
      resetConversation();
      list.replaceChildren();
      setStatus(listStatus, "");
    },
  };
}
