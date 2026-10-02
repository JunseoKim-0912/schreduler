import { apiFetch, getUserId } from "./api.js";
import { badge, el, setStatus } from "./dom.js";
import { getLang, t } from "./i18n.js";
import { createUsageMeter, refreshUsage } from "./usage.js";

// 페르소나 대화 탭
// - 목록: GET /personas, 현재 선택: GET /users/me/persona, 선택: PUT /users/me/persona (X-User-Id 헤더)
// - 대화: POST /daily-actual-logs/checkin (저녁 체크인). 로그인한 사용자의 대화이고,
//   응답의 conversation_id를 다음 요청에 보내면 같은 대화로 이어진다. 선택한 페르소나 말투로 답한다.
// - 탭에 들어오거나 페르소나를 바꾸면 GET /personas/{name}/conversations/current로 그 페르소나의 오늘 대화를
//   불러와 이어 보여준다 (A→B→A로 돌아와도, 새로고침해도 유지). [새 대화]는 POST /personas/{name}/conversations.
export function initPersonasPanel() {
  const personaList = document.getElementById("persona-list");
  const personaStatus = document.getElementById("personas-status");

  const chatTitle = document.getElementById("checkin-title");
  const summaryBox = document.getElementById("checkin-summary");
  const chatLog = document.getElementById("checkin-log");
  const form = document.getElementById("checkin-form");
  const input = document.getElementById("checkin-input");
  const sendButton = document.getElementById("checkin-send");
  const newChatButton = document.getElementById("checkin-new");
  const chatStatus = document.getElementById("checkin-status");

  let personas = [];
  let selectedName = null;
  let conversationId = null;
  let loadedFor = null; // 지금 대화창에 불러온 페르소나 (다시 불러오지 않게)
  let sending = false;
  const usage = createUsageMeter({ node: document.getElementById("checkin-usage"), input, button: sendButton, isBusy: () => sending });

  // 앱 UI가 한국어라 페르소나 이름·설명도 ko를 우선 보여준다 (없으면 en).
  const localized = (value) => value?.[getLang()] || value?.en || value?.ko || "";

  function selectedPersona() {
    return personas.find((persona) => persona.name === selectedName) ?? null;
  }

  function resetChat() {
    conversationId = null;
    loadedFor = null;
    chatLog.replaceChildren();
    chatLog.hidden = true;
    summaryBox.hidden = true;
    setStatus(chatStatus, "");
    const persona = selectedPersona();
    chatTitle.textContent = persona ? t("checkin.headingWith", { name: localized(persona.display_name) }) : t("checkin.heading");
    input.placeholder = persona ? t("checkin.placeholder") : t("checkin.placeholderNoPersona");
  }

  function addBubble(role, text, speaker) {
    const children = speaker ? [el("span", { className: "bubble-speaker", text: speaker }), el("span", { text })] : [];
    chatLog.append(el("li", { className: `bubble bubble-${role}`, text: speaker ? undefined : text }, children));
    chatLog.hidden = false;
    chatLog.scrollTop = chatLog.scrollHeight;
  }

  function renderPersonas() {
    personaList.replaceChildren(
      ...personas.map((persona) => {
        const selected = persona.name === selectedName;
        const button = el(
          "button",
          { className: selected ? "persona-card is-selected" : "persona-card", attrs: { type: "button", "aria-pressed": String(selected) } },
          [
            el("span", { className: "persona-name" }, [
              el("span", { text: localized(persona.display_name) }),
              selected ? badge(t("persona.selected"), "done") : null,
            ]),
            el("span", { className: "persona-description", text: localized(persona.description) }),
          ],
        );
        button.addEventListener("click", () => select(persona.name));
        return el("li", {}, [button]);
      }),
    );
  }

  function showConversation(conversation) {
    const persona = selectedPersona();
    const speaker = persona ? localized(persona.display_name) : t("persona.coach");
    for (const message of conversation?.messages ?? []) {
      if (message.role === "user") addBubble("user", message.content);
      else addBubble("assistant", message.content, speaker);
    }
    conversationId = conversation?.id ?? null;
  }

  // 선택한 페르소나의 오늘 대화를 불러와 이어 보여준다.
  async function loadCurrentConversation() {
    resetChat();
    if (!selectedName) return;
    const name = selectedName;
    try {
      const conversation = await apiFetch(`/personas/${encodeURIComponent(name)}/conversations/current?context=checkin`);
      if (name !== selectedName) return; // 그사이 다른 페르소나를 골랐으면 버린다
      showConversation(conversation);
      loadedFor = name;
    } catch {
      setStatus(chatStatus, t("checkin.loadFailed"));
    }
  }

  async function select(name) {
    if (name === selectedName) return;
    for (const button of personaList.querySelectorAll("button")) button.disabled = true;
    try {
      const result = await apiFetch("/users/me/persona", { method: "PUT", body: { persona_name: name } });
      selectedName = result.selected_persona?.name ?? null;
      renderPersonas();
      await loadCurrentConversation();
    } catch {
      renderPersonas();
    }
  }

  async function send(event) {
    event.preventDefault();
    const utterance = input.value.trim();
    if (!utterance || sending || usage.blocked) return;
    const userId = getUserId();
    if (!userId) {
      setStatus(chatStatus, t("user.required"));
      return;
    }

    addBubble("user", utterance);
    input.value = "";
    sending = true;
    sendButton.disabled = true;
    setStatus(chatStatus, t("checkin.waiting"));
    try {
      const body = { utterance };
      if (conversationId) body.conversation_id = conversationId;
      const result = await apiFetch("/daily-actual-logs/checkin", { method: "POST", body, showError: (error) => error.status !== 429 });
      conversationId = result.conversation_id;
      summaryBox.textContent = result.summary;
      summaryBox.hidden = false;
      const persona = selectedPersona();
      addBubble("assistant", result.reply, persona ? localized(persona.display_name) : t("persona.coach"));
      setStatus(chatStatus, "");
    } catch (error) {
      // 저장된 대화를 못 찾으면(다른 사용자·서버 데이터 초기화 등) 새 대화로 시작한다.
      if (error.status === 404 && conversationId) {
        conversationId = null;
        setStatus(chatStatus, t("checkin.sessionGone"));
      } else if (error.status === 429) {
        setStatus(chatStatus, error.message);
      } else {
        setStatus(chatStatus, t("checkin.sendFailed"));
      }
    } finally {
      sending = false;
      usage.apply();
      input.focus();
      refreshUsage();
    }
  }

  async function refresh() {
    refreshUsage();
    if (!getUserId()) {
      personaList.replaceChildren();
      setStatus(personaStatus, t("persona.needUser"));
      return;
    }
    setStatus(personaStatus, t("common.loading"));
    try {
      const [list, mine] = await Promise.all([apiFetch("/personas"), apiFetch("/users/me/persona")]);
      personas = list;
      const previous = selectedName;
      selectedName = mine.selected_persona?.name ?? null;
      renderPersonas();
      setStatus(personaStatus, personas.length ? "" : t("persona.empty"));
      if (previous !== selectedName || loadedFor !== selectedName) await loadCurrentConversation();
    } catch {
      setStatus(personaStatus, t("persona.loadFailed"));
    }
  }

  form.addEventListener("submit", send);
  // [새 대화]: 오늘 새 대화를 시작한다. 이전 대화는 서버에 그대로 남는다.
  newChatButton.addEventListener("click", async () => {
    const name = selectedName;
    resetChat();
    if (name) {
      try {
        const conversation = await apiFetch(`/personas/${encodeURIComponent(name)}/conversations?context=checkin`, { method: "POST" });
        conversationId = conversation.id;
        loadedFor = name;
        setStatus(chatStatus, t("checkin.newDone"));
      } catch {
        setStatus(chatStatus, t("checkin.newFailed"));
      }
    }
    input.focus();
  });

  resetChat();
  return {
    refresh,
    reset() {
      personas = [];
      selectedName = null;
      personaList.replaceChildren();
      setStatus(personaStatus, "");
      resetChat();
    },
  };
}
