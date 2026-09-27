import { apiFetch, getUserId } from "./api.js";
import { badge, el, setStatus } from "./dom.js";
import {
  candidateReply,
  describeAction,
  describeCandidate,
  describeCommandTarget,
  describeDraftRepeat,
  describeEventTime,
  describeRecurrence,
  formatDate,
  formatShortDate,
  formatTime,
  importanceLabel,
  sortEvents,
} from "./format.js";

const NO_ACTIONS = "아직 변경 기록이 없어요.";

// 이벤트 탭: 자연어 일정 관리(POST /events/parse — 추가·삭제·수정), 확인이 필요한 요청 실행(POST /events/commands/confirm),
// 되돌리기(GET /actions, POST /actions/{id}/undo), 이벤트 목록(GET /events).
// 대화 상태는 서버가 session_id로 보관하므로, 클라이언트는 session_id와 새 발화만 보낸다.
// onDataChanged: 실행·되돌리기로 데이터가 바뀌었을 때 다른 탭(할 일, 포인트)을 다시 불러오게 app.js가 넘겨준다.
export function initEventsPanel({ onDataChanged = async () => {} } = {}) {
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

  const rangeList = document.getElementById("range-list");
  const rangesStatus = document.getElementById("ranges-status");
  const rangesRefreshButton = document.getElementById("ranges-refresh");

  const actionList = document.getElementById("action-list");
  const actionsStatus = document.getElementById("actions-status");
  const actionsRefreshButton = document.getElementById("actions-refresh");

  let sessionId = null;
  let draft = null;
  let draftToken = null;
  let sending = false;

  // 409(되돌리기 순서, 확인 대기 중 대상 변경)와 410(확인 토큰 만료)은 배너 대신 화면 안에서 직접 안내한다.
  const bannerUnless = (...statuses) => (error) => !statuses.includes(error.status);

  // --- 대화창 ---------------------------------------------------------------

  function appendToLog(node) {
    chatLog.append(node);
    chatLog.hidden = false;
    chatLog.scrollTop = chatLog.scrollHeight;
    return node;
  }

  function addMessage(role, text) {
    return appendToLog(el("li", { className: `bubble bubble-${role}`, text }));
  }

  function undoButton(actionId) {
    const button = el("button", {
      className: "button button-secondary button-small",
      text: "되돌리기",
      attrs: { type: "button", "data-action-id": actionId },
    });
    button.addEventListener("click", () => undoFromChat(actionId, button));
    return button;
  }

  // 실행 결과 말풍선. 서버 문구("✔ '물리 퀴즈' 일정 삭제")를 그대로 쓰고 옆에 되돌리기 버튼을 단다.
  function addResult(message, actionId) {
    const children = [el("span", { text: message })];
    if (actionId !== null && actionId !== undefined) children.push(undoButton(actionId));
    return appendToLog(el("li", { className: "bubble bubble-assistant bubble-result" }, children));
  }

  // 되묻기. 후보가 있으면 버튼으로 보여주고, 누르면 그 후보를 답으로 보낸다(직접 입력해도 된다).
  function addQuestion(message, candidates = []) {
    const bubble = addMessage("assistant", message);
    if (!candidates.length) return;
    const buttons = candidates.map((target) => {
      const button = el("button", {
        className: "button button-secondary button-small",
        text: describeCandidate(target),
        attrs: { type: "button" },
      });
      button.addEventListener("click", () => {
        for (const other of buttons) other.disabled = true;
        send(candidateReply(target));
      });
      return button;
    });
    bubble.append(el("div", { className: "quick-replies" }, buttons));
    chatLog.scrollTop = chatLog.scrollHeight;
  }

  // 사용 중인 반복 기간을 지울 때 고르는 처리 (POST /events/commands/confirm의 option)
  const RANGE_DELETE_OPTIONS = {
    range_only: "기간만 삭제 (일정은 마지막 회차까지 유지)",
    with_events: "일정도 함께 삭제",
  };

  // 2개 이상 영향받는 삭제·수정, 사용 중인 기간 삭제: 서버는 아직 실행하지 않았다. 버튼을 눌러야 토큰으로 실행된다.
  function addConfirmCard(command) {
    const verb = command.action === "delete" ? "삭제" : "수정";
    const choices = command.options?.length ? command.options : [null];
    const runButtons = choices.map((option, index) =>
      el("button", {
        className: index === 0 ? "button" : "button button-secondary",
        text: option ? RANGE_DELETE_OPTIONS[option] : "실행",
        attrs: { type: "button" },
      }),
    );
    const cancel = el("button", { className: "button button-secondary", text: "취소", attrs: { type: "button" } });
    const buttons = el("div", { className: "actions" }, [...runButtons, cancel]);
    const status = el("p", { className: "hint", attrs: { role: "status" } });
    status.hidden = true;
    const title =
      command.target_kind === "date_range"
        ? `이 기간을 쓰는 반복 일정 ${command.affected_count}개`
        : `${verb}할 일정 ${command.affected_count}개`;
    const card = appendToLog(
      el("li", { className: "command-card" }, [
        el("p", { className: "command-card-title", text: title }),
        el("ul", { className: "command-targets" }, command.affected.map((t) => el("li", { text: describeCommandTarget(t) }))),
        buttons,
        status,
      ]),
    );

    function close(text) {
      buttons.remove();
      card.classList.add("is-closed");
      setStatus(status, text);
    }

    const setDisabled = (disabled) => {
      for (const button of [...runButtons, cancel]) button.disabled = disabled;
    };
    runButtons.forEach((runButton, index) => runButton.addEventListener("click", () => run(choices[index])));

    async function run(option) {
      setDisabled(true);
      setStatus(status, "실행하는 중…");
      const body = { user_id: Number(getUserId()), token: command.confirmation_token };
      if (option) body.option = option;
      try {
        const result = await apiFetch("/events/commands/confirm", {
          method: "POST",
          body,
          showError: bannerUnless(404, 409, 410),
        });
        close("실행했어요.");
        addResult(result.message, result.command.action_id);
        sessionId = null;
        await dataChanged();
      } catch (error) {
        if (error.status === 410 || error.status === 404) close("확인 시간이 지나 요청이 만료됐어요. 다시 말해 주세요.");
        else if (error.status === 409) close(error.detail);
        else {
          setStatus(status, "실행하지 못했어요. 위의 안내를 확인해 주세요.");
          setDisabled(false);
        }
      }
    }
    cancel.addEventListener("click", () => {
      close("취소했어요. 아무것도 바뀌지 않았어요.");
      sessionId = null;
      input.focus();
    });
    runButtons[0].focus();
  }

  async function handleCommand(message, command) {
    switch (command.status) {
      case "executed":
        addResult(message, command.action_id);
        sessionId = null;
        await dataChanged();
        return;
      case "needs_confirmation":
        if (command.target_kind === "date_range") addMessage("assistant", message);
        addConfirmCard(command);
        return;
      case "needs_clarification":
        addQuestion(message, command.candidates);
        input.placeholder = "답을 입력하거나 위의 후보를 눌러 주세요";
        return;
      default: // not_found
        addMessage("assistant", message ?? "해당 일정을 찾지 못했어요.");
        sessionId = null;
    }
  }

  function resetInput() {
    input.disabled = false;
    sendButton.disabled = false;
    input.placeholder = "예: 오늘 물리 퀴즈 6시로 옮겨줘";
  }

  // 대화 상태만 끝낸다(말풍선은 남긴다). 다음 입력은 새 요청으로 시작된다.
  function endConversation() {
    sessionId = null;
    hideDraft();
    resetInput();
  }

  function resetConversation() {
    endConversation();
    chatLog.replaceChildren();
    chatLog.hidden = true;
    setStatus(chatStatus, "");
  }


  function describeDraftRecurrence(value) {
    const text = describeDraftRepeat(value);
    return value.new_date_range ? `${text} (기간 새로 만듦)` : text;
  }

  function describeDraftLocation(value) {
    if (value.new_location) return `${value.new_location.name} (새로 등록, 이동 ${value.new_location.default_travel_minutes}분)`;
    return value.location_name ?? "없음";
  }

  // 확인 카드: 고칠 수 있는 항목을 전부 보여주고, 방금 말로 고친 항목(changes)은 강조한다.
  async function showDraft(newDraft, changes = []) {
    draft = newDraft;
    const deadline = draft.event_type === "deadline";
    const day = draft.start_time ?? draft.end_time;
    const rows = [
      ["title", "제목", draft.title],
      ["event_type", "종류", deadline ? "마감 일정" : "일반 일정"],
      ["date", draft.is_recurring ? "시작일" : "날짜", formatDate(day)],
      ["time", "시간", deadline ? `${formatTime(draft.end_time)} 마감` : `${formatTime(draft.start_time)}–${formatTime(draft.end_time)}`],
      ["importance", "중요도", importanceLabel(draft.importance)],
      ["recurrence", "반복", describeDraftRecurrence(draft)],
      ...(draft.is_recurring && draft.preview_dates?.length
        ? [["recurrence", "처음 회차", draft.preview_dates.map(formatShortDate).join(", ")]]
        : []),
      ["location", "장소", describeDraftLocation(draft)],
    ];
    draftFields.replaceChildren(
      ...rows.flatMap(([key, label, value]) => {
        const className = changes.includes(key) ? "is-changed" : undefined;
        return [el("dt", { className, text: label }), el("dd", { className, text: value })];
      }),
    );
    confirmBox.hidden = false;
    // 카드가 떠 있어도 입력창은 쓸 수 있다 — 여기서 한 말은 이 초안을 고치는 말로 처리된다.
    input.placeholder = "고칠 내용을 말해 주세요 (예: 7시로 바꿔줘, 중요도 3)";
    input.focus();
  }

  function hideDraft() {
    draft = null;
    draftToken = null;
    confirmBox.hidden = true;
  }

  async function handleParseResult(result) {
    sessionId = result.session_id;
    const command = result.command;
    if (result.is_complete && result.draft) {
      draftToken = command?.confirmation_token ?? null;
      addMessage("assistant", result.message ?? "이 내용으로 만들까요?");
      await showDraft(result.draft, result.draft_changes ?? []);
      return;
    }
    // 카드에서 "좋아"/"취소"라고 말한 경우: 버튼을 누른 것과 같다.
    if (command?.action === "create" && command.status === "executed") {
      addResult(result.message, command.action_id);
      endConversation();
      await dataChanged();
      return;
    }
    if (command?.status === "cancelled") {
      endConversation();
      addMessage("assistant", result.message);
      return;
    }
    // 초안을 고치다가 더 물어볼 게 생기면(반복 기간, 이동 시간 등) 지금 카드는 맞지 않으므로 내린다.
    if (draft && !result.is_complete && result.next_question) hideDraft();
    if (command && (command.action !== "create" || command.target_kind === "date_range")) {
      await handleCommand(result.message, command);
      return;
    }
    if (result.intent === "unknown" || result.intent === "list" || (!result.next_question && result.message)) {
      addMessage("assistant", result.message ?? "일정 추가·삭제·수정만 도와드릴 수 있어요.");
      return;
    }
    addMessage("assistant", result.next_question?.question ?? result.message ?? "조금 더 자세히 알려 주세요.");
    input.placeholder = "답을 입력하세요";
  }

  async function send(utterance) {
    if (sending || !utterance) return;
    const userId = getUserId();
    if (!userId) {
      setStatus(chatStatus, "위에서 사용자 ID를 먼저 입력해 주세요.");
      return;
    }

    sending = true;
    addMessage("user", utterance);
    sendButton.disabled = true;
    setStatus(chatStatus, "AI가 생각하는 중…");
    try {
      const body = { user_id: Number(userId), utterance };
      if (sessionId) body.session_id = sessionId;
      const result = await apiFetch("/events/parse", { method: "POST", body });
      setStatus(chatStatus, "");
      await handleParseResult(result);
    } catch (error) {
      // 서버 재시작 등으로 세션이 사라졌으면 새 대화로 다시 시작하게 한다.
      if (error.status === 404 && sessionId) {
        resetConversation();
        setStatus(chatStatus, "대화가 만료되어 새로 시작했어요. 다시 입력해 주세요.");
      } else {
        setStatus(chatStatus, "전송하지 못했어요. 위의 안내를 확인해 주세요.");
      }
    } finally {
      sending = false;
      sendButton.disabled = false;
      input.focus();
    }
  }

  function submit(event) {
    event.preventDefault();
    const utterance = input.value.trim();
    input.value = "";
    send(utterance);
  }

  async function createFromDraft() {
    createButton.disabled = true;
    cancelButton.disabled = true;
    try {
      if (draftToken) {
        // 서버가 초안을 만들고 되돌리기 기록을 남긴다.
        const result = await apiFetch("/events/commands/confirm", {
          method: "POST",
          body: { user_id: Number(getUserId()), token: draftToken },
          showError: bannerUnless(404, 410),
        });
        addResult(result.message, result.command.action_id);
      } else {
        const created = await apiFetch("/events", { method: "POST", body: draft });
        addResult(`✔ '${created.title}' 일정을 만들었어요.`, null);
      }
      endConversation();
      await dataChanged();
    } catch (error) {
      if (error.status === 410 || error.status === 404) {
        endConversation();
        addMessage("error", "확인 시간이 지나 요청이 만료됐어요. 다시 입력해 주세요.");
      } else {
        setStatus(chatStatus, "일정을 만들지 못했어요. 내용을 확인하고 다시 시도해 주세요.");
      }
    } finally {
      createButton.disabled = false;
      cancelButton.disabled = false;
    }
  }

  // --- 되돌리기 ---------------------------------------------------------------

  // 409(더 최근 변경이 있음, 이미 되돌림)는 호출한 쪽이 서버 문구를 그대로 보여준다.
  async function undo(actionId) {
    const action = await apiFetch(`/actions/${actionId}/undo`, { method: "POST", showError: bannerUnless(409) });
    for (const button of chatLog.querySelectorAll(`button[data-action-id="${actionId}"]`)) {
      button.disabled = true;
      button.textContent = "되돌림";
    }
    await dataChanged();
    return action;
  }

  async function undoFromChat(actionId, button) {
    button.disabled = true;
    try {
      const action = await undo(actionId);
      addMessage("assistant", `↩ 되돌렸어요: ${action.summary_text}`);
    } catch (error) {
      button.disabled = false;
      if (error.status === 409) addMessage("error", error.detail);
    }
  }

  function setActionsStatus(text, state = "ok") {
    setStatus(actionsStatus, text);
    actionsStatus.dataset.state = state;
  }

  function renderAction(action) {
    const main = el("div", { className: "list-main" }, [
      el("div", { className: "list-title" }, [action.undone ? badge("되돌림", "muted") : null, el("span", { text: action.summary_text })]),
      el("div", { className: "list-meta", text: describeAction(action) }),
    ]);
    const item = el("li", { className: action.undone ? "list-item is-undone" : "list-item" }, [main]);
    if (action.undone) return item;

    const button = el("button", {
      className: "button button-secondary button-small",
      text: "되돌리기",
      attrs: { type: "button", "aria-label": `${action.summary_text} 되돌리기` },
    });
    button.addEventListener("click", async () => {
      button.disabled = true;
      setActionsStatus("");
      try {
        const undone = await undo(action.id);
        setActionsStatus(`↩ 되돌렸어요: ${undone.summary_text}`);
      } catch (error) {
        button.disabled = false;
        if (error.status === 409) setActionsStatus(error.detail, "error");
      }
    });
    item.append(button);
    return item;
  }

  async function refreshActions() {
    if (!getUserId()) {
      actionList.replaceChildren();
      setActionsStatus("사용자 ID를 입력하면 최근 변경이 보여요.");
      return;
    }
    actionsRefreshButton.disabled = true;
    try {
      const actions = await apiFetch("/actions?limit=10");
      actionList.replaceChildren(...actions.map(renderAction));
      // "되돌렸어요"·409 안내는 남기고, 빈 목록 안내만 목록 상태에 맞춘다.
      if (!actions.length) setActionsStatus(NO_ACTIONS);
      else if (actionsStatus.textContent === NO_ACTIONS) setActionsStatus("");
    } catch {
      setActionsStatus("최근 변경을 불러오지 못했어요.", "error");
    } finally {
      actionsRefreshButton.disabled = false;
    }
  }

  // --- 반복 기간 -----------------------------------------------------------------

  function setRangesStatus(text, state = "ok") {
    setStatus(rangesStatus, text);
    rangesStatus.dataset.state = state;
  }

  function rangeButton(text, { primary = false } = {}) {
    return el("button", {
      className: primary ? "button button-small" : "button button-secondary button-small",
      text,
      attrs: { type: "button" },
    });
  }

  function renderRange(range) {
    const item = el("li", { className: "list-item range-item" });

    function showSummary() {
      const edit = rangeButton("수정");
      const remove = rangeButton("삭제");
      edit.addEventListener("click", showEditForm);
      remove.addEventListener("click", showDeleteConfirm);
      item.replaceChildren(
        el("div", { className: "list-main" }, [
          el("div", { className: "list-title" }, [el("span", { text: range.name })]),
          el("div", {
            className: "list-meta",
            text: `${formatShortDate(range.start_date)}~${formatShortDate(range.end_date)} · 일정 ${range.event_count}개`,
          }),
        ]),
        el("div", { className: "actions" }, [edit, remove]),
      );
    }

    function showEditForm() {
      const name = el("input", { attrs: { type: "text", value: range.name, maxlength: "100", "aria-label": "기간 이름" } });
      const start = el("input", { attrs: { type: "date", value: range.start_date, "aria-label": "시작일" } });
      const end = el("input", { attrs: { type: "date", value: range.end_date, "aria-label": "종료일" } });
      const save = rangeButton("저장", { primary: true });
      const cancel = rangeButton("취소");
      cancel.addEventListener("click", showSummary);
      save.addEventListener("click", async () => {
        save.disabled = cancel.disabled = true;
        try {
          await apiFetch(`/date-ranges/${range.id}`, {
            method: "PUT",
            body: { name: name.value.trim(), start_date: start.value, end_date: end.value },
            showError: bannerUnless(422),
          });
          setRangesStatus(`'${name.value.trim()}' 기간을 수정했어요. 반복 일정의 회차도 맞췄어요.`);
          await dataChanged();
        } catch (error) {
          save.disabled = cancel.disabled = false;
          if (error.status === 422) setRangesStatus(error.message, "error");
        }
      });
      item.replaceChildren(
        el("div", { className: "range-form" }, [
          el("label", {}, [el("span", { text: "이름" }), name]),
          el("label", {}, [el("span", { text: "시작일" }), start]),
          el("label", {}, [el("span", { text: "종료일" }), end]),
        ]),
        el("div", { className: "actions" }, [save, cancel]),
      );
      name.focus();
    }

    // 브라우저 confirm() 대신 항목 안에서 한 번 더 묻는다. 쓰는 일정이 있으면 기간만/일정도 함께를 고른다.
    async function showDeleteConfirm() {
      let users = [];
      if (range.event_count > 0) {
        const events = await apiFetch(`/events?user_id=${encodeURIComponent(getUserId())}`);
        users = events.filter((e) => e.date_range_id === range.id && e.parent_event_id === null).map((e) => e.title);
      }
      const cancel = rangeButton("취소");
      cancel.addEventListener("click", showSummary);
      const choices = users.length ? ["range_only", "with_events"] : [null];
      const buttons = choices.map((mode, index) => {
        const button = rangeButton(mode ? RANGE_DELETE_OPTIONS[mode] : "삭제", { primary: index === 0 });
        button.addEventListener("click", async () => {
          for (const b of [...buttons, cancel]) b.disabled = true;
          try {
            await apiFetch(`/date-ranges/${range.id}${mode ? `?mode=${mode}` : ""}`, { method: "DELETE" });
            setRangesStatus(`'${range.name}' 기간을 삭제했어요. 최근 변경에서 되돌릴 수 있어요.`);
            await dataChanged();
          } catch {
            for (const b of [...buttons, cancel]) b.disabled = false;
          }
        });
        return button;
      });
      const text = users.length
        ? `이 기간을 쓰는 반복 일정 ${users.length}개: ${users.join(", ")}. 어떻게 할까요?`
        : "이 기간을 삭제할까요?";
      item.replaceChildren(el("p", { className: "range-confirm", text }), el("div", { className: "actions" }, [...buttons, cancel]));
      buttons[0].focus();
    }

    showSummary();
    return item;
  }

  async function refreshRanges() {
    const userId = getUserId();
    if (!userId) {
      rangeList.replaceChildren();
      setRangesStatus("사용자 ID를 입력하면 반복 기간이 보여요.");
      return;
    }
    rangesRefreshButton.disabled = true;
    try {
      const ranges = await apiFetch(`/date-ranges?user_id=${encodeURIComponent(userId)}`);
      rangeList.replaceChildren(...ranges.map(renderRange));
      if (!ranges.length) setRangesStatus("아직 반복 기간이 없어요.");
      else if (rangesStatus.textContent === "아직 반복 기간이 없어요.") setRangesStatus("");
    } catch {
      setRangesStatus("반복 기간을 불러오지 못했어요.", "error");
    } finally {
      rangesRefreshButton.disabled = false;
    }
  }

  // --- 이벤트 목록 --------------------------------------------------------------

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

  async function refreshEvents() {
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

  async function refresh() {
    await Promise.all([refreshEvents(), refreshActions(), refreshRanges()]);
  }

  // 실행·되돌리기 뒤: 이 탭의 목록과 최근 변경, 그리고 할 일·포인트 탭까지 다시 불러온다.
  async function dataChanged() {
    await Promise.all([refresh(), onDataChanged()]);
  }

  form.addEventListener("submit", submit);
  resetButton.addEventListener("click", () => {
    resetConversation();
    input.focus();
  });
  createButton.addEventListener("click", createFromDraft);
  cancelButton.addEventListener("click", () => {
    endConversation();
    addMessage("assistant", "취소했어요. 새로 입력해 주세요.");
    input.focus();
  });
  refreshButton.addEventListener("click", refreshEvents);
  actionsRefreshButton.addEventListener("click", refreshActions);
  rangesRefreshButton.addEventListener("click", refreshRanges);

  resetConversation();
  return {
    refresh,
    // 캘린더의 빈 칸을 누르면 "9월 24일 14시에 " 같은 문구를 채워 두고 바로 이어서 입력하게 한다.
    prefill(text) {
      if (draft) endConversation();
      input.value = text;
      input.focus();
      input.setSelectionRange(text.length, text.length);
      input.scrollIntoView({ block: "center" });
    },
    // 사용자가 바뀌면 이전 사용자의 대화 세션과 목록을 지운다.
    reset() {
      resetConversation();
      list.replaceChildren();
      setStatus(listStatus, "");
      actionList.replaceChildren();
      setActionsStatus("");
      rangeList.replaceChildren();
      setRangesStatus("");
    },
  };
}
