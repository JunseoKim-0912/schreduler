import { apiFetch, getUserId } from "./api.js";
import { badge, el, setStatus } from "./dom.js";
import { describeRecurrence, formatDateTime, importanceLabel, importanceOptions, toApiDateTime } from "./format.js";
import { t } from "./i18n.js";

// 할 일 탭: 새 할 일(POST /tasks), 목록(GET /tasks — 서버가 마감 오름차순으로 준다), 완료(PUT /tasks/{event_instance_id}/complete).
export function initTasksPanel({ onDataChanged = async () => {} } = {}) {
  const form = document.getElementById("task-form");
  const titleInput = document.getElementById("task-title");
  const dueInput = document.getElementById("task-due");
  const importanceSelect = document.getElementById("task-importance");
  const submitButton = document.getElementById("task-submit");
  const formStatus = document.getElementById("task-form-status");

  const list = document.getElementById("task-list");
  const listStatus = document.getElementById("tasks-status");
  const refreshButton = document.getElementById("tasks-refresh");

  // 중요도 선택지는 화면 언어가 바뀌면 다시 그린다 (고른 값은 유지).
  function renderImportanceOptions() {
    const selected = importanceSelect.value;
    importanceSelect.replaceChildren(
      el("option", { text: t("tasks.importanceNone"), attrs: { value: "" } }),
      ...importanceOptions().map((option) => el("option", { text: option.label, attrs: { value: option.value } })),
    );
    importanceSelect.value = selected;
  }
  renderImportanceOptions();

  async function create(event) {
    event.preventDefault();
    if (!getUserId()) {
      setStatus(formStatus, t("user.required"));
      return;
    }
    const body = { title: titleInput.value.trim(), end_time: toApiDateTime(dueInput.value) };
    if (importanceSelect.value) body.importance = Number(importanceSelect.value);

    submitButton.disabled = true;
    try {
      const task = await apiFetch("/tasks", { method: "POST", body });
      form.reset();
      setStatus(formStatus, t("tasks.added", { title: task.title }));
      await Promise.all([refresh(), onDataChanged()]);
    } catch {
      setStatus(formStatus, t("tasks.addFailed"));
    } finally {
      submitButton.disabled = false;
    }
  }

  async function complete(task, button) {
    button.disabled = true;
    try {
      await apiFetch(`/tasks/${task.event_instance_id}/complete`, { method: "PUT" });
      await Promise.all([refresh(), onDataChanged()]);
    } catch {
      button.disabled = false;
    }
  }

  function renderTask(task) {
    const badges = [
      task.completed ? badge(t("tasks.badge.done"), "done") : null,
      task.overdue ? badge(t("tasks.badge.overdue"), "overdue") : null,
      task.is_recurring ? badge(describeRecurrence(task.recurrence_rule), "muted") : null,
    ];
    const meta = [t("tasks.dueMeta", { when: formatDateTime(task.due_at) })];
    if (task.importance !== null) meta.push(t("tasks.importanceMeta", { label: importanceLabel(task.importance) }));

    const button = el("button", {
      className: "button button-small",
      text: task.completed ? t("tasks.completed") : t("tasks.complete"),
      attrs: {
        type: "button",
        disabled: task.completed || task.event_instance_id === null,
        title: task.event_instance_id === null ? t("tasks.noInstance") : undefined,
        "aria-label": t("tasks.completeLabel", { title: task.title }),
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
      setStatus(listStatus, t("tasks.needUser"));
      return;
    }
    refreshButton.disabled = true;
    setStatus(listStatus, t("common.loading"));
    try {
      const tasks = await apiFetch("/tasks");
      list.replaceChildren(...tasks.map(renderTask));
      setStatus(listStatus, tasks.length ? "" : t("tasks.empty"));
    } catch {
      setStatus(listStatus, t("tasks.loadFailed"));
    } finally {
      refreshButton.disabled = false;
    }
  }

  form.addEventListener("submit", create);
  refreshButton.addEventListener("click", refresh);

  return {
    refresh,
    relabel: renderImportanceOptions,
    reset() {
      list.replaceChildren();
      setStatus(listStatus, "");
      setStatus(formStatus, "");
    },
  };
}
