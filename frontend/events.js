import { apiFetch, getUserId } from "./api.js";
import { initAssistantChat } from "./assistant.js";
import { badge, el, setStatus } from "./dom.js";
import { describeAction, describeEventTime, describeRecurrence, formatShortDate, importanceLabel, sortEvents } from "./format.js";
import { t } from "./i18n.js";


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
      button.textContent = t("common.undone");
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
      el("div", { className: "list-title" }, [action.undone ? badge(t("common.undone"), "muted") : null, el("span", { text: action.summary_text })]),
      el("div", { className: "list-meta", text: describeAction(action) }),
    ]);
    const item = el("li", { className: action.undone ? "list-item is-undone" : "list-item" }, [main]);
    if (action.undone) return item;

    const button = el("button", {
      className: "button button-secondary button-small",
      text: t("common.undo"),
      attrs: { type: "button", "aria-label": t("actions.undoLabel", { summary: action.summary_text }) },
    });
    button.addEventListener("click", async () => {
      button.disabled = true;
      setActionsStatus("");
      try {
        const undone = await undo(action.id);
        setActionsStatus(t("common.undoneMessage", { summary: undone.summary_text }));
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
      setActionsStatus(t("actions.needUser"));
      return;
    }
    actionsRefreshButton.disabled = true;
    try {
      const actions = await apiFetch("/actions?limit=10");
      actionList.replaceChildren(...actions.map(renderAction));
      // "되돌렸어요"·409 안내는 남기고, 빈 목록 안내만 목록 상태에 맞춘다.
      if (!actions.length) setActionsStatus(t("actions.empty"));
      else if (actionsStatus.textContent === t("actions.empty")) setActionsStatus("");
    } catch {
      setActionsStatus(t("actions.loadFailed"), "error");
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
      const edit = rangeButton(t("common.edit"));
      const remove = rangeButton(t("common.delete"));
      edit.addEventListener("click", showEditForm);
      remove.addEventListener("click", showDeleteConfirm);
      item.replaceChildren(
        el("div", { className: "list-main" }, [
          el("div", { className: "list-title" }, [el("span", { text: range.name })]),
          el("div", {
            className: "list-meta",
            text: t("ranges.meta", { span: `${formatShortDate(range.start_date)}~${formatShortDate(range.end_date)}`, count: range.event_count }),
          }),
        ]),
        el("div", { className: "actions" }, [edit, remove]),
      );
    }

    function showEditForm() {
      const name = el("input", { attrs: { type: "text", value: range.name, maxlength: "100", "aria-label": t("ranges.nameLabel") } });
      const start = el("input", { attrs: { type: "date", value: range.start_date, "aria-label": t("ranges.start") } });
      const end = el("input", { attrs: { type: "date", value: range.end_date, "aria-label": t("ranges.end") } });
      const save = rangeButton(t("common.save"), { primary: true });
      const cancel = rangeButton(t("common.cancel"));
      cancel.addEventListener("click", showSummary);
      save.addEventListener("click", async () => {
        save.disabled = cancel.disabled = true;
        try {
          await apiFetch(`/date-ranges/${range.id}`, {
            method: "PUT",
            body: { name: name.value.trim(), start_date: start.value, end_date: end.value },
            showError: bannerUnless(422),
          });
          setRangesStatus(t("ranges.updated", { name: name.value.trim() }));
          await dataChanged();
        } catch (error) {
          save.disabled = cancel.disabled = false;
          if (error.status === 422) setRangesStatus(error.message, "error");
        }
      });
      item.replaceChildren(
        el("div", { className: "range-form" }, [
          el("label", {}, [el("span", { text: t("common.name") }), name]),
          el("label", {}, [el("span", { text: t("ranges.start") }), start]),
          el("label", {}, [el("span", { text: t("ranges.end") }), end]),
        ]),
        el("div", { className: "actions" }, [save, cancel]),
      );
      name.focus();
    }

    // 브라우저 confirm() 대신 항목 안에서 한 번 더 묻는다. 쓰는 일정이 있으면 기간만/일정도 함께를 고른다.
    async function showDeleteConfirm() {
      let users = [];
      if (range.event_count > 0) {
        const events = await apiFetch("/events");
        users = events.filter((e) => e.date_range_id === range.id && e.parent_event_id === null).map((e) => e.title);
      }
      const cancel = rangeButton(t("common.cancel"));
      cancel.addEventListener("click", showSummary);
      const choices = users.length ? ["range_only", "with_events"] : [null];
      const buttons = choices.map((mode, index) => {
        const button = rangeButton(mode ? t(`ranges.mode.${mode}`) : t("common.delete"), { primary: index === 0 });
        button.addEventListener("click", async () => {
          for (const b of [...buttons, cancel]) b.disabled = true;
          try {
            await apiFetch(`/date-ranges/${range.id}${mode ? `?mode=${mode}` : ""}`, { method: "DELETE" });
            setRangesStatus(t("ranges.deleted", { name: range.name }));
            await dataChanged();
          } catch {
            for (const b of [...buttons, cancel]) b.disabled = false;
          }
        });
        return button;
      });
      const text = users.length
        ? t("ranges.deleteInUse", { count: users.length, titles: users.join(", ") })
        : t("ranges.deleteAsk");
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
      setRangesStatus(t("ranges.needUser"));
      return;
    }
    rangesRefreshButton.disabled = true;
    try {
      const ranges = await apiFetch("/date-ranges");
      rangeList.replaceChildren(...ranges.map(renderRange));
      if (!ranges.length) setRangesStatus(t("ranges.empty"));
      else if (rangesStatus.textContent === t("ranges.empty")) setRangesStatus("");
    } catch {
      setRangesStatus(t("ranges.loadFailed"), "error");
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
      const edit = rangeButton(t("common.edit"));
      const remove = rangeButton(t("common.delete"));
      edit.addEventListener("click", showEditForm);
      remove.addEventListener("click", showDeleteConfirm);
      item.replaceChildren(
        el("div", { className: "list-main" }, [
          el("div", { className: "list-title" }, [el("span", { text: location.name })]),
          el("div", { className: "list-meta", text: t("locations.travel", { minutes: location.default_travel_minutes }) }),
        ]),
        el("div", { className: "actions" }, [edit, remove]),
      );
    }

    function showEditForm() {
      const name = el("input", { attrs: { type: "text", value: location.name, maxlength: "100", "aria-label": t("locations.nameLabel") } });
      const minutes = el("input", {
        attrs: { type: "number", min: "0", value: String(location.default_travel_minutes), "aria-label": t("locations.minutesLabel") },
      });
      const save = rangeButton(t("common.save"), { primary: true });
      const cancel = rangeButton(t("common.cancel"));
      cancel.addEventListener("click", showSummary);
      save.addEventListener("click", async () => {
        save.disabled = cancel.disabled = true;
        try {
          await apiFetch(`/locations/${location.id}`, {
            method: "PUT",
            body: { name: name.value.trim(), default_travel_minutes: Number(minutes.value) },
            showError: bannerUnless(422),
          });
          setLocationsStatus(t("locations.updated", { name: name.value.trim() }));
          await dataChanged();
        } catch (error) {
          save.disabled = cancel.disabled = false;
          if (error.status === 422) setLocationsStatus(error.message, "error");
        }
      });
      item.replaceChildren(
        el("div", { className: "range-form" }, [
          el("label", {}, [el("span", { text: t("common.name") }), name]),
          el("label", {}, [el("span", { text: t("locations.minutesLabel") }), minutes]),
        ]),
        el("div", { className: "actions" }, [save, cancel]),
      );
      name.focus();
    }

    function showDeleteConfirm() {
      const confirmButton = rangeButton(t("common.delete"), { primary: true });
      const cancel = rangeButton(t("common.cancel"));
      cancel.addEventListener("click", showSummary);
      confirmButton.addEventListener("click", async () => {
        confirmButton.disabled = cancel.disabled = true;
        try {
          await apiFetch(`/locations/${location.id}`, { method: "DELETE", showError: bannerUnless(409) });
          setLocationsStatus(t("locations.deleted", { name: location.name }));
          await refreshLocations();
        } catch (error) {
          confirmButton.disabled = cancel.disabled = false;
          // 이 장소를 쓰는 일정이 있으면 409 — 서버 문구를 그대로 보여준다.
          if (error.status === 409) setLocationsStatus(t("locations.inUse", { detail: error.detail }), "error");
        }
      });
      item.replaceChildren(el("p", { className: "range-confirm", text: t("locations.deleteAsk") }), el("div", { className: "actions" }, [confirmButton, cancel]));
      confirmButton.focus();
    }

    showSummary();
    return item;
  }

  async function refreshLocations() {
    const userId = getUserId();
    if (!userId) {
      locationList.replaceChildren();
      setLocationsStatus(t("locations.needUser"));
      return;
    }
    locationsRefreshButton.disabled = true;
    try {
      const locations = await apiFetch("/locations");
      locationList.replaceChildren(...locations.map(renderLocation));
      if (!locations.length) setLocationsStatus(t("locations.empty"));
      else if (locationsStatus.textContent === t("locations.empty")) setLocationsStatus("");
    } catch {
      setLocationsStatus(t("locations.loadFailed"), "error");
    } finally {
      locationsRefreshButton.disabled = false;
    }
  }

  // --- 이벤트 목록 --------------------------------------------------------------

  function renderEvent(event) {
    const badges = [
      event.event_type === "deadline" ? badge(t("events.badge.deadline"), "deadline") : badge(t("events.badge.scheduled"), "scheduled"),
      event.child_kind === "travel" ? badge(t("events.badge.travel"), "muted") : null,
      event.child_kind === "custom" ? badge(t("events.badge.custom"), "muted") : null,
    ];
    const meta = [describeEventTime(event)];
    if (event.is_recurring) meta.push(describeRecurrence(event.recurrence_rule));
    if (event.importance !== null) meta.push(t("events.importance", { label: importanceLabel(event.importance) }));

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
      setStatus(listStatus, t("events.needUser"));
      return;
    }
    refreshButton.disabled = true;
    setStatus(listStatus, t("common.loading"));
    try {
      const events = await apiFetch("/events");
      list.replaceChildren(...sortEvents(events).map(renderEvent));
      setStatus(listStatus, events.length ? "" : t("events.empty"));
    } catch {
      setStatus(listStatus, t("events.loadFailed"));
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
