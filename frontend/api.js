// 모든 API 호출이 거치는 공통 레이어. DOM에 의존하지 않아 Node에서도 테스트할 수 있다.
// 화면에 에러를 띄우는 일은 onApiError로 등록한 핸들러(app.js)가 맡는다.

import { t } from "./i18n.js";

// 같은 서버(FastAPI)가 /app에서 이 파일을 서빙하므로 API는 같은 origin이다 — CORS 설정이 필요 없다.
export const API_BASE = "";

const USER_ID_KEY = "schreduler.userId";

// LLM을 호출해 422(응답 형식 오류) / 429(오늘 사용 한도) / 500(키 미설정) / 502(호출 실패)가 올 수 있는 엔드포인트
export const LLM_ENDPOINTS = [
  { method: "POST", path: "/assistant/chat" },
  { method: "POST", path: "/daily-actual-logs/checkin" },
  { method: "POST", path: "/compliance-reports" },
];

export class ApiError extends Error {
  constructor(message, { status, detail = null, method, path, cause } = {}) {
    super(message, { cause });
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.method = method;
    this.path = path;
  }
}

// localStorage는 사생활 보호 모드 등에서 막힐 수 있으므로 실패해도 앱이 동작하게 한다.
export function getUserId() {
  try {
    return globalThis.localStorage?.getItem(USER_ID_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setUserId(value) {
  try {
    if (value) globalThis.localStorage?.setItem(USER_ID_KEY, value);
    else globalThis.localStorage?.removeItem(USER_ID_KEY);
    return true;
  } catch {
    return false;
  }
}

const errorHandlers = new Set();

export function onApiError(handler) {
  errorHandlers.add(handler);
  return () => errorHandlers.delete(handler);
}

function report(error) {
  for (const handler of errorHandlers) handler(error);
  return error;
}

export function isLlmEndpoint(method, path) {
  const bare = path.split("?")[0];
  return LLM_ENDPOINTS.some((e) => e.method === method.toUpperCase() && e.path === bare);
}

// FastAPI 요청 검증 오류: [{loc: ["body", "title"], msg: "...", type: "..."}]
function formatValidationErrors(items) {
  return items
    .map((item) => {
      const field = (item.loc ?? []).filter((part) => part !== "body").join(".");
      const message = String(item.msg ?? "").replace(/^Value error, /, "");
      return field ? `• ${field}: ${message}` : `• ${message}`;
    })
    .join("\n");
}

// 상태 코드와 서버 detail을 사용자에게 보여줄 문장으로 바꾼다.
// detail은 문자열(도메인 오류)이거나 배열(요청 형식 검증 오류)일 수 있다 — docs/api_overview.md 1.4절.
export function describeError({ status, detail, method = "GET", path = "" }) {
  const llm = isLlmEndpoint(method, path);

  if (status === 0) return t("api.offline");
  if (status === 401) return t("api.needUser");
  if (status === 422 && Array.isArray(detail)) return `${t("api.checkInput")}\n${formatValidationErrors(detail)}`;

  const serverMessage = typeof detail === "string" && detail ? detail : "";
  if (llm) {
    // 429: today's AI usage limit. The server's message says which limit and when it reopens.
    if (status === 429) return serverMessage || t("api.llm429");
    if (status === 422) return t("api.llm422");
    if (status === 500) return t("api.llm500");
    if (status === 502) return t("api.llm502");
  }

  if (status === 404) return serverMessage ? t("api.notFoundDetail", { detail: serverMessage }) : t("api.notFound");
  if (status === 409) return serverMessage ? t("api.conflictDetail", { detail: serverMessage }) : t("api.conflict");
  if (status === 422) return serverMessage ? t("api.checkInputDetail", { detail: serverMessage }) : t("api.checkInput");
  if (status >= 500) return t("api.server");
  return serverMessage || t("api.other", { status });
}

async function readBody(response) {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

/**
 * 모든 API 호출의 공통 진입점.
 * - X-User-Id(저장된 사용자 ID)와 Content-Type: application/json을 자동으로 붙인다.
 * - body에 객체를 넘기면 JSON 문자열로 바꾼다.
 * - 2xx면 파싱한 JSON(204면 null)을 돌려주고, 실패하면 ApiError를 던지면서 등록된 에러 핸들러에 알린다.
 *   화면에 띄우지 않고 직접 처리하려면 { showError: false }. 함수를 넘기면 그 함수가 true를 돌려준 에러만 알린다
 *   (예: 409는 화면 안에서 직접 보여주고 나머지는 배너로).
 * - 응답 헤더가 필요하면(예: 삭제의 X-Action-Id) onHeaders(headers)로 받는다. 성공 응답에서만 불린다.
 */
export async function apiFetch(path, { method = "GET", body, headers, showError = true, onHeaders, ...rest } = {}) {
  const requestHeaders = new Headers(headers);
  requestHeaders.set("Content-Type", "application/json");
  requestHeaders.set("Accept", "application/json");
  const userId = getUserId();
  if (userId) requestHeaders.set("X-User-Id", userId);

  const fail = (error) => {
    if (typeof showError === "function" ? showError(error) : showError) report(error);
    return error;
  };

  let response;
  try {
    response = await fetch(API_BASE + path, {
      ...rest,
      method,
      headers: requestHeaders,
      body: body === undefined || typeof body === "string" ? body : JSON.stringify(body),
    });
  } catch (cause) {
    throw fail(new ApiError(describeError({ status: 0, method, path }), { status: 0, method, path, cause }));
  }

  if (response.ok) onHeaders?.(response.headers);
  if (response.status === 204) return null;
  const data = await readBody(response);
  if (!response.ok) {
    const detail = data && typeof data === "object" && "detail" in data ? data.detail : data;
    const message = describeError({ status: response.status, detail, method, path });
    throw fail(new ApiError(message, { status: response.status, detail, method, path }));
  }
  return data;
}
