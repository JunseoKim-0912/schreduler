import { apiFetch, getUserId } from "./api.js";
import { badge, el, setStatus } from "./dom.js";
import { assistantConfirmLabel, describeAssistantItem } from "./format.js";
import { t } from "./i18n.js";
import { createUsageMeter, refreshUsage } from "./usage.js";

// 이벤트 탭의 새 어시스턴트 (docs/assistant_design.md §1, §5, §11).
// 대화 기록과 대기 중인 제안은 서버(assistant_sessions)에 있다. 탭에 들어오면 GET /assistant/sessions/current로 이어 그린다.
// undo·dataChanged는 events.js의 것을 그대로 받아 쓴다 (되돌리기 버튼, 캘린더·할 일·포인트·목록 다시 불러오기).
export function initAssistantChat({ undo, dataChanged }) {
  const log = document.getElementById("assistant-log");
  const form = document.getElementById("assistant-form");
  const input = document.getElementById("assistant-input");
  const sendButton = document.getElementById("assistant-send");
  const resetButton = document.getElementById("assistant-reset");
  const status = document.getElementById("assistant-status");

  let sessionId = null;
  let loadedFor = null; // 서버에서 대화를 불러온 사용자 ID. 같은 사용자·같은 세션이면 다시 그리지 않는다(되돌리기 버튼 유지).
  let activeCard = null; // { token, close(text), supersede(), setBusy(busy) }
  let sending = false;
  const usage = createUsageMeter({ node: document.getElementById("assistant-usage"), input, button: sendButton, isBusy: () => sending });

  // --- 말풍선 -------------------------------------------------------------------

  function append(node) {
    log.append(node);
    log.hidden = false;
    log.scrollTop = log.scrollHeight;
    return node;
  }

  // 모델이 가끔 **굵게**를 쓴다. 다른 마크다운은 글자 그대로 둔다.
  function richText(text) {
    return String(text ?? "")
      .split(/\*\*(.+?)\*\*/g)
      .map((part, index) => (index % 2 ? el("strong", { text: part }) : document.createTextNode(part)));
  }

  const addMessage = (role, text) =>
    append(el("li", { className: `bubble bubble-${role}` }, role === "assistant" ? richText(text) : [document.createTextNode(text)]));

  function addResult(summary, actionId) {
    const button = el("button", {
      className: "button button-secondary button-small",
      text: t("common.undo"),
      attrs: { type: "button", "data-action-id": actionId },
    });
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const action = await undo(actionId);
        addMessage("assistant", t("common.undoneMessage", { summary: action.summary_text }));
      } catch (error) {
        button.disabled = false;
        if (error.status === 409) addMessage("error", error.detail);
      }
    });
    append(el("li", { className: "bubble bubble-assistant bubble-result" }, [el("span", { text: `✔ ${summary}` }), button]));
  }

  function showThinking() {
    const node = append(el("li", { className: "bubble bubble-assistant bubble-thinking", text: t("assistant.thinking"), attrs: { "aria-busy": "true" } }));
    return () => node.remove();
  }

  // --- 확인 카드 ------------------------------------------------------------------

  function renderItem(item) {
    const view = describeAssistantItem(item);
    const fields = el(
      "dl",
      { className: "assistant-fields" },
      view.rows.flatMap((row) => [
        el("dt", { text: row.label }),
        el("dd", {}, [el("span", { text: row.value ?? "" }), row.inferred ? badge(t("assistant.inferred"), "inferred") : null]),
      ]),
    );
    return el("section", { className: "assistant-item" }, [
      el("p", { className: "assistant-item-heading", text: view.heading }),
      view.targets.length ? el("ul", { className: "command-targets" }, view.targets.map((text) => el("li", { text }))) : null,
      view.rows.length ? fields : null,
      ...view.notes.map((text) => el("p", { className: "assistant-note", text })),
      ...view.warnings.map((text) => el("p", { className: "assistant-warning", text })),
    ]);
  }

  function renderProposal(proposal) {
    activeCard?.supersede();
    const items = proposal.items ?? [];
    const run = el("button", { className: "button", text: assistantConfirmLabel(items), attrs: { type: "button" } });
    const cancel = el("button", { className: "button button-secondary", text: t("common.cancel"), attrs: { type: "button" } });
    const buttons = el("div", { className: "actions" }, [run, cancel]);
    const cardStatus = el("p", { className: "hint", attrs: { role: "status" } });
    cardStatus.hidden = true;
    const title = el("p", { className: "command-card-title", text: items.length > 1 ? t("assistant.cardTitleMany", { count: items.length }) : t("assistant.cardTitle") });
    const node = append(el("li", { className: "command-card assistant-card" }, [title, ...items.map(renderItem), buttons, cardStatus]));

    const card = {
      token: proposal.token,
      setBusy(busy) {
        run.disabled = cancel.disabled = busy;
      },
      close(text) {
        buttons.remove();
        node.classList.add("is-closed");
        setStatus(cardStatus, text);
        if (activeCard === card) activeCard = null;
      },
      supersede() {
        card.setBusy(true);
        buttons.remove();
        node.classList.add("is-superseded");
        title.prepend(badge(t("assistant.changed"), "muted"), " ");
        if (activeCard === card) activeCard = null;
      },
    };
    activeCard = card;

    // 409·410·404는 카드 안에서 직접 안내하고, 나머지(서버 연결 등)는 카드 상태 줄에 서버 문구를 보여준다.
    async function answer(path, onDone) {
      card.setBusy(true);
      setStatus(cardStatus, t("assistant.working"));
      try {
        const result = await apiFetch(path, { method: "POST", body: { session_id: sessionId, token: card.token }, showError: false });
        await onDone(result);
      } catch (error) {
        if (error.status === 410) card.close(t("assistant.expired"));
        else if (error.status === 409 || error.status === 404) card.close(t("assistant.stale"));
        else {
          setStatus(cardStatus, error.message);
          card.setBusy(false);
        }
      }
    }

    run.addEventListener("click", () =>
      answer("/assistant/confirm", async (result) => {
        card.close(t("assistant.done"));
        await showExecuted(result.executed);
      }),
    );
    cancel.addEventListener("click", () =>
      answer("/assistant/cancel", async (result) => {
        card.close(t("assistant.cancelled"));
        addMessage("assistant", result.reply);
        input.focus();
      }),
    );
    if (sending) card.setBusy(true);
  }

  async function showExecuted(executed) {
    for (const action of executed) addResult(action.summary, action.action_id);
    if (executed.length) await dataChanged();
  }

  // --- 대화 --------------------------------------------------------------------

  // 서버의 실행 답("✔ …")은 되돌리기 버튼이 달린 결과 말풍선으로 대신 보여준다.
  const isExecutedEcho = (reply, executed) => executed.length && reply.trim() === executed.map((a) => `✔ ${a.summary}`).join(" ");

  async function handleReply(result) {
    const executed = result.executed ?? [];
    if (result.reply && !isExecutedEcho(result.reply, executed)) addMessage("assistant", result.reply);
    if (executed.length) activeCard?.close(t("assistant.doneByChat"));
    if (result.proposal) renderProposal(result.proposal);
    await showExecuted(executed);
  }

  async function send(message) {
    if (sending || !message || usage.blocked) return;
    if (!getUserId()) {
      setStatus(status, t("user.required"));
      return;
    }
    setStatus(status, "");
    sending = true;
    sendButton.disabled = true;
    activeCard?.setBusy(true);
    addMessage("user", message);
    const hideThinking = showThinking();
    try {
      const body = { message };
      if (sessionId) body.session_id = sessionId;
      const result = await apiFetch("/assistant/chat", { method: "POST", body, showError: false });
      hideThinking();
      sessionId = result.session_id;
      loadedFor = getUserId();
      await handleReply(result);
    } catch (error) {
      hideThinking();
      if (error.status === 404 && sessionId) {
        sessionId = null;
        activeCard?.close(t("assistant.sessionGoneCard"));
        addMessage("error", t("assistant.sessionGone"));
      } else {
        addMessage("error", error.message);
      }
    } finally {
      sending = false;
      usage.apply();
      activeCard?.setBusy(false);
      input.focus();
      refreshUsage();
    }
  }

  function renderSession(data) {
    log.replaceChildren();
    log.hidden = true;
    activeCard = null;
    sessionId = data.session_id;
    for (const message of data.messages ?? []) addMessage(message.role, message.text);
    if (data.proposal) renderProposal(data.proposal);
  }

  // 탭에 들어올 때·데이터가 바뀔 때 부른다. 보고 있는 대화와 서버의 "오늘 대화"가 같으면 그대로 둔다.
  async function sync() {
    const userId = getUserId();
    if (sending) return;
    refreshUsage();
    if (!userId) {
      reset();
      setStatus(status, t("assistant.needUser"));
      status.dataset.kind = "notice";
      return;
    }
    try {
      const data = await apiFetch("/assistant/sessions/current", { showError: false });
      if (sending || getUserId() !== userId) return;
      if (status.dataset.kind === "notice") {
        setStatus(status, "");
        delete status.dataset.kind;
      }
      if (loadedFor === userId && data.session_id === sessionId) {
        if (activeCard && data.proposal?.token !== activeCard.token) activeCard.close(t("assistant.expired"));
        return;
      }
      loadedFor = userId;
      renderSession(data);
    } catch (error) {
      setStatus(status, t("assistant.loadFailed", { error: error.message }));
      status.dataset.kind = "notice";
    }
  }

  async function startNewSession() {
    if (sending) return;
    if (!getUserId()) {
      setStatus(status, t("user.required"));
      return;
    }
    resetButton.disabled = true;
    try {
      const data = await apiFetch("/assistant/sessions", { method: "POST", showError: false });
      loadedFor = getUserId();
      renderSession(data);
      setStatus(status, "");
      input.focus();
    } catch (error) {
      setStatus(status, t("assistant.newFailed", { error: error.message }));
    } finally {
      resetButton.disabled = false;
    }
  }

  function reset() {
    sessionId = null;
    loadedFor = null;
    activeCard = null;
    log.replaceChildren();
    log.hidden = true;
    setStatus(status, "");
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (sending) return;
    const message = input.value.trim();
    input.value = "";
    send(message);
  });
  resetButton.addEventListener("click", startNewSession);

  return {
    sync,
    reset,
    prefill(text) {
      input.value = text;
      input.focus();
      input.setSelectionRange(text.length, text.length);
      input.scrollIntoView({ block: "center" });
    },
  };
}
