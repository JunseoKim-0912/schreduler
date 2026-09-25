import { apiFetch, getUserId } from "./api.js";
import { badge, el, setStatus } from "./dom.js";
import { IMPORTANCE_OPTIONS, describeRecurrence, formatDateTime, importanceLabel, toApiDateTime } from "./format.js";

// 할 일 탭: 새 할 일(POST /tasks), 목록(GET /tasks — 서버가 마감 오름차순으로 준다), 완료(PUT /tasks/{event_instance_id}/complete).
export function initTasksPanel() {
  const form = document.getElementById("task-form");
  const titleInput = document.getElementById("task-title");
  const dueInput = document.getElementById("task-due");
  const importanceSelect = document.getElementById("task-importance");
  const submitButton = document.getElementById("task-submit");
  const formStatus = document.getElementById("task-form-status");

  const list = document.getElementById("task-list");
  const listStatus = document.getElementById("tasks-status");
  const refreshButton = document.getElementById("tasks-refresh");

  importanceSelect.append(
    ...IMPORTANCE_OPTIONS.map((option) => el("option", { text: option.label, attrs: { value: option.value } })),
  );

  async function create(event) {
    event.preventDefault();
    if (!getUserId()) {
      setStatus(formStatus, "위에서 사용자 ID를 먼저 입력해 주세요.");
      return;
    }
    const body = { title: titleInput.value.trim(), end_time: toApiDateTime(dueInput.value) };
    if (importanceSelect.value) body.importance = Number(importanceSelect.value);

    submitButton.disabled = true;
    try {
      const task = await apiFetch("/tasks", { method: "POST", body });
      form.reset();
      setStatus(formStatus, `"${task.title}"을(를) 추가했어요.`);
      await refresh();
    } catch {
      setStatus(formStatus, "추가하지 못했어요. 위의 안내를 확인해 주세요.");
    } finally {
      submitButton.disabled = false;
    }
  }

  async function complete(task, button) {
    button.disabled = true;
    try {
      await apiFetch(`/tasks/${task.event_instance_id}/complete`, { method: "PUT" });
      await refresh();
    } catch {
      button.disabled = false;
    }
  }

  function renderTask(task) {
    const badges = [
      task.completed ? badge("완료", "done") : null,
      task.overdue ? badge("마감 지남", "overdue") : null,
      task.is_recurring ? badge(describeRecurrence(task.recurrence_rule), "muted") : null,
    ];
    const meta = [`마감 ${formatDateTime(task.due_at)}`];
    if (task.importance !== null) meta.push(`중요도 ${importanceLabel(task.importance)}`);

    const button = el("button", {
      className: "button button-small",
      text: task.completed ? "완료됨" : "완료",
      attrs: {
        type: "button",
        disabled: task.completed || task.event_instance_id === null,
        title: task.event_instance_id === null ? "완료 처리할 회차가 없는 할 일이에요" : undefined,
        "aria-label": `${task.title} 완료`,
      },
    });
    button.addEventListener("click", () => complete(task, button));

    const classes = ["list-item", task.overdue ? "is-overdue" : "", task.completed ? "is-done" : ""].filter(Boolean);
    return el("li", { className: classes.join(" ") }, [
      el("div", { className: "list-main" }, [
        el("div", { className: "list-title" }, [...badges, el("span", { text: task.title })]),
        el("div", { className: "list-meta", text: meta.join(" · ") }),
      ]),
      button,
    ]);
  }

  async function refresh() {
    if (!getUserId()) {
      list.replaceChildren();
      setStatus(listStatus, "사용자 ID를 입력하면 할 일 목록이 보여요.");
      return;
    }
    refreshButton.disabled = true;
    setStatus(listStatus, "불러오는 중…");
    try {
      const tasks = await apiFetch("/tasks");
      list.replaceChildren(...tasks.map(renderTask));
      setStatus(listStatus, tasks.length ? "" : "할 일이 없어요.");
    } catch {
      setStatus(listStatus, "목록을 불러오지 못했어요.");
    } finally {
      refreshButton.disabled = false;
    }
  }

  form.addEventListener("submit", create);
  refreshButton.addEventListener("click", refresh);

  return {
    refresh,
    reset() {
      list.replaceChildren();
      setStatus(listStatus, "");
      setStatus(formStatus, "");
    },
  };
}
