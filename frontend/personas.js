import { apiFetch, getUserId } from "./api.js";
import { badge, el, setStatus } from "./dom.js";

// 페르소나 대화 탭
// - 목록: GET /personas, 현재 선택: GET /users/me/persona, 선택: PUT /users/me/persona (X-User-Id 헤더)
// - 대화: POST /daily-actual-logs/checkin (저녁 체크인). 본문의 user_id로 사용자를 구분하고,
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

  // 앱 UI가 한국어라 페르소나 이름·설명도 ko를 우선 보여준다 (없으면 en).
  const localized = (value) => value?.ko || value?.en || "";

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
    chatTitle.textContent = persona ? `${localized(persona.display_name)}와(과) 저녁 체크인` : "저녁 체크인";
    input.placeholder = persona
      ? "오늘 하루 어땠는지 이야기해 보세요"
      : "페르소나를 선택하지 않으면 기본 코치가 답하고, 대화는 저장되지 않아요";
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
              selected ? badge("선택됨", "done") : null,
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
    const speaker = persona ? localized(persona.display_name) : "코치";
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
      setStatus(chatStatus, "오늘 대화를 불러오지 못했어요.");
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
    if (!utterance) return;
    const userId = getUserId();
    if (!userId) {
      setStatus(chatStatus, "위에서 사용자 ID를 먼저 입력해 주세요.");
      return;
    }

    addBubble("user", utterance);
    input.value = "";
    sendButton.disabled = true;
    setStatus(chatStatus, "답장을 기다리는 중…");
    try {
      const body = { user_id: Number(userId), utterance };
      if (conversationId) body.conversation_id = conversationId;
      const result = await apiFetch("/daily-actual-logs/checkin", { method: "POST", body });
      conversationId = result.conversation_id;
      summaryBox.textContent = result.summary;
      summaryBox.hidden = false;
      const persona = selectedPersona();
      addBubble("assistant", result.reply, persona ? localized(persona.display_name) : "코치");
      setStatus(chatStatus, "");
    } catch (error) {
      // 저장된 대화를 못 찾으면(다른 사용자·서버 데이터 초기화 등) 새 대화로 시작한다.
      if (error.status === 404 && conversationId) {
        conversationId = null;
        setStatus(chatStatus, "이전 대화를 찾을 수 없어 새 대화로 시작해요. 다시 보내 주세요.");
      } else {
        setStatus(chatStatus, "보내지 못했어요. 위의 안내를 확인해 주세요.");
      }
    } finally {
      sendButton.disabled = false;
      input.focus();
    }
  }

  async function refresh() {
    if (!getUserId()) {
      personaList.replaceChildren();
      setStatus(personaStatus, "사용자 ID를 입력하면 페르소나를 고를 수 있어요.");
      return;
    }
    setStatus(personaStatus, "불러오는 중…");
    try {
      const [list, mine] = await Promise.all([apiFetch("/personas"), apiFetch("/users/me/persona")]);
      personas = list;
      const previous = selectedName;
      selectedName = mine.selected_persona?.name ?? null;
      renderPersonas();
      setStatus(personaStatus, personas.length ? "" : "등록된 페르소나가 없어요. (python -m app.scripts.seed_personas)");
      if (previous !== selectedName || loadedFor !== selectedName) await loadCurrentConversation();
    } catch {
      setStatus(personaStatus, "페르소나를 불러오지 못했어요.");
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
        setStatus(chatStatus, "새 대화를 시작했어요. 이전 대화는 기록에 남아 있어요.");
      } catch {
        setStatus(chatStatus, "새 대화를 시작하지 못했어요.");
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
