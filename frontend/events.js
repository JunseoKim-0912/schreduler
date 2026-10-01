import { apiFetch, getUserId } from "./api.js";
import { initAssistantChat } from "./assistant.js";
import { badge, el, setStatus } from "./dom.js";
import { describeAction, describeEventTime, describeRecurrence, formatShortDate, importanceLabel, sortEvents } from "./format.js";

const NO_ACTIONS = "아직 변경 기록이 없어요.";

// 사용 중인 반복 기간을 지울 때 고르는 처리 (DELETE /date-ranges/{id}?mode=)
const RANGE_DELETE_OPTIONS = {
  range_only: "기간만 삭제 (일정은 마지막 회차까지 유지)",
  with_events: "일정도 함께 삭제",
};

// 예전 [새 어시스턴트 | 기존 방식] 스위치가 저장하던 값. 스위치가 없어졌으므로 남은 값을 지운다.
try {
  globalThis.localStorage?.removeItem("schreduler.eventsMode");
} catch {
  // 저장소를 못 쓰면 지울 것도 없다.
}

// 이벤트 탭: 자연어 일정 관리는 어시스턴트(assistant.js, POST /assistant/chat)가 맡고, 여기서는 되돌리기
// (GET /actions, POST /actions/{id}/undo), 반복 기간·장소 목록, 이벤트 목록(GET /events)을 다룬다.
// onDataChanged: 실행·되돌리기로 데이터가 바뀌었을 때 다른 탭(할 일, 포인트)을 다시 불러오게 app.js가 넘겨준다.
export function initEventsPanel({ onDataChanged = async () => {} } = {}) {
  const list = document.getElementById("event-list");
  const listStatus = document.getElementById("events-status");
  const refreshButton = document.getElementById("events-refresh");

  const rangeList = document.getElementById("range-list");
  const rangesStatus = document.getElementById("ranges-status");
  const rangesRefreshButton = document.getElementById("ranges-refresh");

  const locationList = document.getElementById("location-list");
  const locationsStatus = document.getElementById("locations-status");
  const locationsRefreshButton = document.getElementById("locations-refresh");

  const actionList = document.getElementById("action-list");
  const actionsStatus = document.getElementById("actions-status");
  const actionsRefreshButton = document.getElementById("actions-refresh");

  const panel = document.getElementById("panel-events");

  // 409(되돌리기 순서, 사용 중인 장소)와 422(입력값)는 배너 대신 화면 안에서 직접 안내한다.
  const bannerUnless = (...statuses) => (error) => !statuses.includes(error.status);

  // --- 되돌리기 ---------------------------------------------------------------

  // 409(더 최근 변경이 있음, 이미 되돌림)는 호출한 쪽이 서버 문구를 그대로 보여준다.
  async function undo(actionId) {
    const action = await apiFetch(`/actions/${actionId}/undo`, { method: "POST", showError: bannerUnless(409) });
    for (const button of panel.querySelectorAll(`button[data-action-id="${actionId}"]`)) {
      button.disabled = true;
      button.textContent = "되돌림";
    }
    await dataChanged();
    return action;
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

  // --- 장소 ---------------------------------------------------------------------

  function setLocationsStatus(text, state = "ok") {
    setStatus(locationsStatus, text);
    locationsStatus.dataset.state = state;
  }

  function renderLocation(location) {
    const item = el("li", { className: "list-item range-item" });

    function showSummary() {
      const edit = rangeButton("수정");
      const remove = rangeButton("삭제");
      edit.addEventListener("click", showEditForm);
      remove.addEventListener("click", showDeleteConfirm);
      item.replaceChildren(
        el("div", { className: "list-main" }, [
          el("div", { className: "list-title" }, [el("span", { text: location.name })]),
          el("div", { className: "list-meta", text: `이동 ${location.default_travel_minutes}분` }),
        ]),
        el("div", { className: "actions" }, [edit, remove]),
      );
    }

    function showEditForm() {
      const name = el("input", { attrs: { type: "text", value: location.name, maxlength: "100", "aria-label": "장소 이름" } });
      const minutes = el("input", {
        attrs: { type: "number", min: "0", value: String(location.default_travel_minutes), "aria-label": "이동 시간(분)" },
      });
      const save = rangeButton("저장", { primary: true });
      const cancel = rangeButton("취소");
      cancel.addEventListener("click", showSummary);
      save.addEventListener("click", async () => {
        save.disabled = cancel.disabled = true;
        try {
          await apiFetch(`/locations/${location.id}`, {
            method: "PUT",
            body: { name: name.value.trim(), default_travel_minutes: Number(minutes.value) },
            showError: bannerUnless(422),
          });
          setLocationsStatus(`'${name.value.trim()}' 장소를 수정했어요.`);
          await dataChanged();
        } catch (error) {
          save.disabled = cancel.disabled = false;
          if (error.status === 422) setLocationsStatus(error.message, "error");
        }
      });
      item.replaceChildren(
        el("div", { className: "range-form" }, [
          el("label", {}, [el("span", { text: "이름" }), name]),
          el("label", {}, [el("span", { text: "이동 시간(분)" }), minutes]),
        ]),
        el("div", { className: "actions" }, [save, cancel]),
      );
      name.focus();
    }

    function showDeleteConfirm() {
      const confirmButton = rangeButton("삭제", { primary: true });
      const cancel = rangeButton("취소");
      cancel.addEventListener("click", showSummary);
      confirmButton.addEventListener("click", async () => {
        confirmButton.disabled = cancel.disabled = true;
        try {
          await apiFetch(`/locations/${location.id}`, { method: "DELETE", showError: bannerUnless(409) });
          setLocationsStatus(`'${location.name}' 장소를 삭제했어요.`);
          await refreshLocations();
        } catch (error) {
          confirmButton.disabled = cancel.disabled = false;
          // 이 장소를 쓰는 일정이 있으면 409 — 서버 문구를 그대로 보여준다.
          if (error.status === 409) setLocationsStatus(`${error.detail} — 일정에서 장소를 먼저 빼 주세요.`, "error");
        }
      });
      item.replaceChildren(el("p", { className: "range-confirm", text: "이 장소를 삭제할까요?" }), el("div", { className: "actions" }, [confirmButton, cancel]));
      confirmButton.focus();
    }

    showSummary();
    return item;
  }

  async function refreshLocations() {
    const userId = getUserId();
    if (!userId) {
      locationList.replaceChildren();
      setLocationsStatus("사용자 ID를 입력하면 장소가 보여요.");
      return;
    }
    locationsRefreshButton.disabled = true;
    try {
      const locations = await apiFetch(`/locations?user_id=${encodeURIComponent(userId)}`);
      locationList.replaceChildren(...locations.map(renderLocation));
      if (!locations.length) setLocationsStatus("아직 등록된 장소가 없어요.");
      else if (locationsStatus.textContent === "아직 등록된 장소가 없어요.") setLocationsStatus("");
    } catch {
      setLocationsStatus("장소를 불러오지 못했어요.", "error");
    } finally {
      locationsRefreshButton.disabled = false;
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
    await Promise.all([refreshEvents(), refreshActions(), refreshRanges(), refreshLocations(), assistant.sync()]);
  }

  // 실행·되돌리기 뒤: 이 탭의 목록과 최근 변경, 그리고 할 일·포인트 탭까지 다시 불러온다.
  async function dataChanged() {
    await Promise.all([refresh(), onDataChanged()]);
  }

  const assistant = initAssistantChat({ undo, dataChanged });

  refreshButton.addEventListener("click", refreshEvents);
  actionsRefreshButton.addEventListener("click", refreshActions);
  rangesRefreshButton.addEventListener("click", refreshRanges);
  locationsRefreshButton.addEventListener("click", refreshLocations);

  return {
    refresh,
    // 캘린더의 빈 칸을 누르면 "9월 24일 14시에 " 같은 문구를 채워 두고 바로 이어서 입력하게 한다.
    prefill: assistant.prefill,
    // 사용자가 바뀌면 이전 사용자의 대화 세션과 목록을 지운다.
    reset() {
      assistant.reset();
      list.replaceChildren();
      setStatus(listStatus, "");
      actionList.replaceChildren();
      setActionsStatus("");
      rangeList.replaceChildren();
      setRangesStatus("");
      locationList.replaceChildren();
      setLocationsStatus("");
    },
  };
}
