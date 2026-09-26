import { apiFetch, getUserId } from "./api.js";
import { el, setStatus } from "./dom.js";
import {
  HOUR_HEIGHT,
  addDays,
  deadlinesOn,
  describeItemTime,
  formatRangeTitle,
  hourFromOffset,
  importanceClass,
  isOverdue,
  nowMarker,
  prefillText,
  scheduledSegments,
  startOfDay,
  startOfWeek,
  statusLabel,
  stepDays,
  timeOf,
  toIsoDate,
  visibleDays,
  weekdayLabel,
} from "./calendar-model.js";
import { describeRecurrence, importanceLabel } from "./format.js";

const NARROW_QUERY = "(max-width: 699px)";
const SCROLL_TO_HOUR = 7;
const CHILD_KIND_LABELS = { travel: "이동시간", custom: "준비" };

// 캘린더 탭: GET /event-instances로 주(좁은 화면은 하루) 단위 회차를 그린다.
// onDataChanged: 완료·삭제·되돌리기 뒤 다른 탭을 다시 불러오게 app.js가 넘겨준다.
// onCreateAt: 빈 칸을 누르면 이벤트 탭의 자연어 입력창으로 보낼 문구를 넘긴다.
export function initCalendarPanel({ onDataChanged = async () => {}, onCreateAt = () => {} } = {}) {
  const panel = document.getElementById("panel-calendar");
  const prevButton = document.getElementById("cal-prev");
  const todayButton = document.getElementById("cal-today");
  const nextButton = document.getElementById("cal-next");
  const rangeTitle = document.getElementById("cal-range");
  const status = document.getElementById("calendar-status");
  const toast = document.getElementById("calendar-toast");
  const scroller = document.getElementById("calendar-scroll");
  const grid = document.getElementById("calendar-grid");
  const popover = document.getElementById("cal-popover");

  const narrow = window.matchMedia(NARROW_QUERY);
  let mode = narrow.matches ? "day" : "week";
  let anchor = startOfDay(new Date());
  let items = [];
  let loadSeq = 0;
  let scrolled = false;
  let popoverAnchor = null;

  const days = () => visibleDays(anchor, mode);

  // --- 그리기 ------------------------------------------------------------------

  function chip(item, now) {
    const classes = ["cal-chip", importanceClass(item.importance)];
    if (item.status === "done") classes.push("is-done");
    if (item.status === "missed") classes.push("is-missed");
    if (isOverdue(item, now)) classes.push("is-overdue");
    const button = el("button", {
      className: classes.join(" "),
      attrs: { type: "button", "aria-label": `${item.title}, ${describeItemTime(item)}, ${statusLabel(item.status)}` },
    }, [el("span", { className: "cal-chip-time", text: timeOf(item.end_time) }), el("span", { className: "cal-chip-title", text: item.title })]);
    button.addEventListener("click", () => openPopover(item, button));
    return button;
  }

  function block(segment) {
    const { item, startMin, endMin, col, cols } = segment;
    const classes = ["cal-block", importanceClass(item.importance)];
    if (item.status === "done") classes.push("is-done");
    if (item.status === "missed") classes.push("is-missed");
    if (item.child_kind) classes.push("is-child");
    const button = el(
      "button",
      {
        className: classes.join(" "),
        attrs: { type: "button", "aria-label": `${item.title}, ${describeItemTime(item)}, ${statusLabel(item.status)}` },
      },
      [
        el("span", { className: "cal-block-title", text: item.title }),
        el("span", { className: "cal-block-time", text: `${timeOf(item.start_time)}–${timeOf(item.end_time)}` }),
      ],
    );
    button.style.top = `${(startMin / 60) * HOUR_HEIGHT}px`;
    button.style.height = `${((endMin - startMin) / 60) * HOUR_HEIGHT}px`;
    button.style.left = `calc(${(col / cols) * 100}% + 2px)`;
    button.style.width = `calc(${100 / cols}% - 4px)`;
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      openPopover(item, button);
    });
    return button;
  }

  function dayColumn(day, isToday) {
    const column = el("div", {
      className: isToday ? "cal-col is-today" : "cal-col",
      attrs: { "data-date": toIsoDate(day), title: "빈 칸을 누르면 이 시간에 일정을 추가해요" },
    }, scheduledSegments(items, day).map(block));
    column.addEventListener("click", (event) => {
      if (event.target !== column) return;
      onCreateAt(prefillText(day, hourFromOffset(event.offsetY)));
    });
    return column;
  }

  function render() {
    const now = new Date();
    const ds = days();
    const todayIso = toIsoDate(now);
    rangeTitle.textContent = formatRangeTitle(ds);
    prevButton.textContent = mode === "day" ? "◀ 이전 날" : "◀ 이전 주";
    nextButton.textContent = mode === "day" ? "다음 날 ▶" : "다음 주 ▶";
    grid.style.setProperty("--days", ds.length);

    const heads = ds.map((day) => {
      const isToday = toIsoDate(day) === todayIso;
      return el("div", { className: isToday ? "cal-dayhead is-today" : "cal-dayhead" }, [
        el("div", { className: "cal-daylabel", attrs: { "aria-current": isToday ? "date" : undefined } }, [
          el("span", { className: "cal-weekday", text: weekdayLabel(day) }),
          el("span", { className: "cal-date", text: String(day.getDate()) }),
        ]),
        el("div", { className: "cal-deadlines" }, deadlinesOn(items, day).map((item) => chip(item, now))),
      ]);
    });
    const hours = Array.from({ length: 24 }, (_, h) =>
      el("span", { className: "cal-hour", text: h === 0 ? "" : `${String(h).padStart(2, "0")}:00` }),
    );
    for (const [h, label] of hours.entries()) label.style.top = `${h * HOUR_HEIGHT}px`;

    grid.replaceChildren(
      el("div", { className: "cal-corner" }, [el("span", { text: "마감" })]),
      ...heads,
      el("div", { className: "cal-gutter", attrs: { "aria-hidden": "true" } }, hours),
      ...ds.map((day) => dayColumn(day, toIsoDate(day) === todayIso)),
    );
    drawNowLine();
  }

  function drawNowLine() {
    grid.querySelector(".cal-now")?.remove();
    const marker = nowMarker(new Date(), days());
    if (!marker) return;
    const line = el("div", { className: "cal-now", attrs: { "aria-hidden": "true" } });
    line.style.top = `${(marker.minutes / 60) * HOUR_HEIGHT}px`;
    grid.querySelectorAll(".cal-col")[marker.dayIndex]?.append(line);
  }

  // 1분마다 현재 시각 선을 옮긴다. 날짜가 바뀌었으면(자정) 오늘 강조까지 다시 그린다.
  let renderedDay = toIsoDate(new Date());
  setInterval(() => {
    const today = toIsoDate(new Date());
    if (today !== renderedDay) {
      renderedDay = today;
      render();
    } else {
      drawNowLine();
    }
  }, 60_000);

  // --- 불러오기 ---------------------------------------------------------------

  async function load() {
    if (!getUserId()) {
      items = [];
      render();
      setStatus(status, "사용자 ID를 입력하면 캘린더가 보여요.");
      return;
    }
    const seq = ++loadSeq;
    const ds = days();
    grid.setAttribute("aria-busy", "true");
    try {
      const data = await apiFetch(`/event-instances?start=${toIsoDate(ds[0])}&end=${toIsoDate(ds.at(-1))}`);
      if (seq !== loadSeq) return; // 그사이 다른 주로 이동했으면 늦게 온 응답은 버린다
      items = data;
      render();
      setStatus(status, "");
    } catch {
      if (seq === loadSeq) setStatus(status, "캘린더를 불러오지 못했어요.");
    } finally {
      if (seq === loadSeq) grid.removeAttribute("aria-busy");
    }
  }

  async function refresh() {
    await load();
    // 처음 보일 때 오전 7시쯤이 맨 위에 오게 한다 (숨은 탭에서는 스크롤 위치를 잡을 수 없다).
    if (!scrolled && !panel.hidden) {
      scroller.scrollTop = SCROLL_TO_HOUR * HOUR_HEIGHT - 12; // 07:00 라벨이 고정 머리칸에 반쯤 가리지 않게 살짝 위로
      scrolled = true;
    }
  }

  function navigate(newAnchor) {
    closePopover();
    anchor = startOfDay(newAnchor);
    items = [];
    render();
    load();
  }

  // --- 알림(토스트) ------------------------------------------------------------

  function showToast(text, { actionId = null, state = "ok" } = {}) {
    const children = [el("span", { text })];
    if (actionId) {
      const undoButton = el("button", { className: "button button-secondary button-small", text: "되돌리기", attrs: { type: "button" } });
      undoButton.addEventListener("click", async () => {
        undoButton.disabled = true;
        try {
          const action = await apiFetch(`/actions/${actionId}/undo`, { method: "POST", showError: (e) => e.status !== 409 });
          showToast(`↩ 되돌렸어요: ${action.summary_text}`);
          await changed();
        } catch (error) {
          if (error.status === 409) showToast(error.detail, { state: "error" });
          else undoButton.disabled = false;
        }
      });
      children.push(undoButton);
    }
    toast.replaceChildren(...children);
    toast.dataset.state = state;
    toast.hidden = false;
  }

  async function changed() {
    await Promise.all([load(), onDataChanged()]);
  }

  // --- 상세 패널 --------------------------------------------------------------

  function closePopover() {
    if (popover.hidden) return;
    popover.hidden = true;
    popover.replaceChildren();
    popoverAnchor?.focus?.({ preventScroll: true });
    popoverAnchor = null;
  }

  function position(anchorEl) {
    if (narrow.matches) {
      popover.style.left = popover.style.top = "";
      return;
    }
    const rect = anchorEl.getBoundingClientRect();
    const { offsetWidth: width, offsetHeight: height } = popover;
    let left = rect.right + 8;
    if (left + width > window.innerWidth - 8) left = Math.max(8, rect.left - width - 8);
    const top = Math.min(Math.max(8, rect.top), window.innerHeight - height - 8);
    popover.style.left = `${left}px`;
    popover.style.top = `${Math.max(8, top)}px`;
  }

  function openPopover(item, anchorEl) {
    closePopover();
    popoverAnchor = anchorEl;
    const rows = [
      ["날짜·시간", describeItemTime(item)],
      ["중요도", importanceLabel(item.importance)],
      ["반복", item.is_recurring ? describeRecurrence(item.recurrence_rule) : "반복 안 함"],
      ["상태", statusLabel(item.status) + (isOverdue(item, new Date()) ? " (마감 지남)" : "")],
    ];
    if (item.child_kind) rows.push(["종류", `${CHILD_KIND_LABELS[item.child_kind] ?? item.child_kind} (하위 일정)`]);
    if (item.time_overridden) rows.push(["참고", "이 회차만 시간이 바뀌었어요"]);

    const statusLine = el("p", { className: "hint", attrs: { role: "status" } });
    statusLine.hidden = true;
    const actions = el("div", { className: "actions" });
    const closeButton = el("button", { className: "cal-popover-close", text: "×", attrs: { type: "button", "aria-label": "닫기" } });
    closeButton.addEventListener("click", closePopover);

    popover.replaceChildren(
      el("div", { className: "cal-popover-head" }, [
        el("h3", { className: "cal-popover-title", text: item.title, attrs: { id: "cal-popover-title" } }),
        closeButton,
      ]),
      el("dl", { className: "draft-fields cal-popover-fields" }, rows.flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })])),
      actions,
      statusLine,
    );
    showMainActions(item, actions, statusLine);
    popover.hidden = false;
    position(anchorEl);
    actions.querySelector("button:not(:disabled)")?.focus();
  }

  function smallButton(text, { primary = false, danger = false, disabled = false, title } = {}) {
    const classes = ["button", "button-small"];
    if (!primary) classes.push("button-secondary");
    if (danger) classes.push("button-danger");
    return el("button", { className: classes.join(" "), text, attrs: { type: "button", disabled, title } });
  }

  function showMainActions(item, actions, statusLine) {
    const done = item.status === "done";
    const completeButton = smallButton(done ? "완료됨" : "완료", { primary: true, disabled: done });
    const deleteButton = smallButton("삭제", { danger: true });
    completeButton.addEventListener("click", () => complete(item, completeButton, statusLine));
    deleteButton.addEventListener("click", () => showDeleteConfirm(item, actions, statusLine));
    // 회차가 없는 건 회차 생성 이전에 만든 지난 단발 일정뿐이다 (백필 대상 아님). 완료할 회차가 없어 삭제만 둔다.
    actions.replaceChildren(...(item.event_instance_id === null ? [deleteButton] : [completeButton, deleteButton]));
    setStatus(statusLine, "");
  }

  // 브라우저 confirm() 대신 패널 안에서 한 번 더 확인한다. 반복 일정은 이 회차만/반복 전체를 고른다.
  function showDeleteConfirm(item, actions, statusLine) {
    const cancel = smallButton("취소");
    cancel.addEventListener("click", () => showMainActions(item, actions, statusLine));
    if (item.is_recurring && item.event_instance_id !== null) {
      const onlyThis = smallButton("이 회차만 삭제", { primary: true, danger: true });
      const series = smallButton("반복 전체 삭제", { danger: true });
      onlyThis.addEventListener("click", () => remove(item, "instance", statusLine, [onlyThis, series, cancel]));
      series.addEventListener("click", () => remove(item, "series", statusLine, [onlyThis, series, cancel]));
      actions.replaceChildren(onlyThis, series, cancel);
      setStatus(statusLine, "어떻게 삭제할까요? 삭제한 뒤에도 되돌릴 수 있어요.");
      onlyThis.focus();
    } else {
      const confirmButton = smallButton("삭제", { primary: true, danger: true });
      confirmButton.addEventListener("click", () => remove(item, "series", statusLine, [confirmButton, cancel]));
      actions.replaceChildren(confirmButton, cancel);
      setStatus(statusLine, "이 일정을 삭제할까요? 삭제한 뒤에도 되돌릴 수 있어요.");
      confirmButton.focus();
    }
  }

  async function complete(item, button, statusLine) {
    button.disabled = true;
    // 할 일(deadline)은 기존 할 일 완료 API, 일반 일정은 회차 완료 API. 둘 다 같은 완료 처리 로직이다.
    const path = item.event_type === "deadline" ? `/tasks/${item.event_instance_id}/complete` : `/event-instances/${item.event_instance_id}/complete`;
    try {
      await apiFetch(path, { method: "PUT" });
      closePopover();
      showToast(`✔ '${item.title}' 완료했어요.`);
      await changed();
    } catch {
      button.disabled = false;
      setStatus(statusLine, "완료 처리하지 못했어요. 위의 안내를 확인해 주세요.");
    }
  }

  async function remove(item, scope, statusLine, buttons) {
    for (const b of buttons) b.disabled = true;
    const path = scope === "instance" ? `/event-instances/${item.event_instance_id}` : `/events/${item.event_id}`;
    let actionId = null;
    try {
      await apiFetch(path, { method: "DELETE", onHeaders: (headers) => (actionId = headers.get("X-Action-Id")) });
      closePopover();
      showToast(scope === "instance" ? `'${item.title}' 이 회차를 삭제했어요.` : `'${item.title}' 일정을 삭제했어요.`, { actionId });
      await changed();
    } catch {
      for (const b of buttons) b.disabled = false;
      setStatus(statusLine, "삭제하지 못했어요. 위의 안내를 확인해 주세요.");
    }
  }

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closePopover();
  });
  document.addEventListener("pointerdown", (event) => {
    if (!popover.hidden && !popover.contains(event.target) && !popoverAnchor?.contains(event.target)) closePopover();
  });
  scroller.addEventListener("scroll", () => {
    if (!narrow.matches) closePopover(); // 붙어 있던 블록이 움직이면 패널 위치가 어긋나므로 닫는다
  });

  // --- 이동 --------------------------------------------------------------------

  prevButton.addEventListener("click", () => navigate(addDays(anchor, -stepDays(mode))));
  nextButton.addEventListener("click", () => navigate(addDays(anchor, stepDays(mode))));
  todayButton.addEventListener("click", () => navigate(new Date()));
  narrow.addEventListener("change", (event) => {
    const today = startOfDay(new Date());
    mode = event.matches ? "day" : "week";
    // 주 → 하루: 보고 있던 주에 오늘이 있으면 오늘, 아니면 그 주 월요일
    if (mode === "day") {
      const monday = startOfWeek(anchor);
      anchor = today >= monday && today < addDays(monday, 7) ? today : monday;
    }
    navigate(anchor);
  });

  render();
  return {
    refresh,
    reset() {
      closePopover();
      toast.hidden = true;
      items = [];
      render();
      setStatus(status, "");
    },
  };
}
