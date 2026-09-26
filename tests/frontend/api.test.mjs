// frontend/api.js 단위 테스트 (Node 내장 테스트 러너). pytest의 tests/test_frontend.py가 `node --test`로 실행한다.
import assert from "node:assert/strict";
import { beforeEach, describe, test } from "node:test";

import { ApiError, apiFetch, describeError, getUserId, isLlmEndpoint, onApiError, setUserId } from "../../frontend/api.js";

class MemoryStorage {
  #data = new Map();
  getItem(key) {
    return this.#data.has(key) ? this.#data.get(key) : null;
  }
  setItem(key, value) {
    this.#data.set(key, String(value));
  }
  removeItem(key) {
    this.#data.delete(key);
  }
}

let requests;

function mockFetch(respond) {
  requests = [];
  globalThis.fetch = async (url, init) => {
    requests.push({ url, init });
    return respond(url, init);
  };
}

function jsonResponse(status, body) {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  globalThis.localStorage = new MemoryStorage();
});

describe("사용자 ID 저장", () => {
  test("저장하고 다시 읽는다, 빈 값이면 지운다", () => {
    assert.equal(getUserId(), "");
    setUserId("7");
    assert.equal(getUserId(), "7");
    setUserId("");
    assert.equal(getUserId(), "");
  });

  test("localStorage가 막혀 있어도 예외 없이 동작한다", () => {
    globalThis.localStorage = {
      getItem() {
        throw new Error("blocked");
      },
      setItem() {
        throw new Error("blocked");
      },
    };
    assert.equal(getUserId(), "");
    assert.equal(setUserId("1"), false);
  });
});

describe("apiFetch 요청", () => {
  test("X-User-Id와 Content-Type을 자동으로 붙이고 객체 body를 JSON으로 바꾼다", async () => {
    setUserId("3");
    mockFetch(() => jsonResponse(201, { id: 1 }));

    const data = await apiFetch("/tasks", { method: "POST", body: { title: "과제" } });

    assert.deepEqual(data, { id: 1 });
    const { url, init } = requests[0];
    assert.equal(url, "/tasks");
    assert.equal(init.method, "POST");
    assert.equal(init.headers.get("X-User-Id"), "3");
    assert.equal(init.headers.get("Content-Type"), "application/json");
    assert.equal(init.body, JSON.stringify({ title: "과제" }));
  });

  test("사용자 ID가 없으면 X-User-Id를 붙이지 않는다", async () => {
    mockFetch(() => jsonResponse(200, { status: "ok" }));

    await apiFetch("/health");

    assert.equal(requests[0].init.headers.has("X-User-Id"), false);
    assert.equal(requests[0].init.body, undefined);
  });

  test("204는 null을 돌려준다", async () => {
    mockFetch(() => new Response(null, { status: 204 }));

    assert.equal(await apiFetch("/events/1", { method: "DELETE" }), null);
  });
});

describe("apiFetch 에러", () => {
  test("실패하면 ApiError를 던지고 등록된 핸들러에 알린다", async () => {
    mockFetch(() => jsonResponse(502, { detail: "LLM API 호출에 실패했습니다: timeout" }));
    const seen = [];
    const off = onApiError((error) => seen.push(error));

    await assert.rejects(
      apiFetch("/events/parse", { method: "POST", body: { user_id: 1, utterance: "스터디" } }),
      (error) => error instanceof ApiError && error.status === 502,
    );
    off();

    assert.equal(seen.length, 1);
    assert.equal(seen[0].message, "AI 서버와 연결이 원활하지 않아요. 잠시 후 다시 시도해 주세요.");
    assert.equal(seen[0].detail, "LLM API 호출에 실패했습니다: timeout");
  });

  test("showError: false면 핸들러에 알리지 않는다", async () => {
    mockFetch(() => jsonResponse(404, { detail: "Event not found" }));
    const seen = [];
    const off = onApiError((error) => seen.push(error));

    await assert.rejects(apiFetch("/events/9", { showError: false }), ApiError);
    off();

    assert.equal(seen.length, 0);
  });

  test("showError에 함수를 넘기면 true인 에러만 알린다", async () => {
    const seen = [];
    const off = onApiError((error) => seen.push(error));
    const notConflict = (error) => error.status !== 409;

    mockFetch(() => jsonResponse(409, { detail: "더 최근 변경을 먼저 되돌려야 해요." }));
    const conflict = await apiFetch("/actions/1/undo", { method: "POST", showError: notConflict }).catch((e) => e);
    mockFetch(() => jsonResponse(404, { detail: "action 9 does not exist" }));
    await assert.rejects(apiFetch("/actions/9/undo", { method: "POST", showError: notConflict }), ApiError);
    off();

    assert.equal(conflict.detail, "더 최근 변경을 먼저 되돌려야 해요.");
    assert.deepEqual(seen.map((e) => e.status), [404]);
  });

  test("onHeaders로 성공 응답의 헤더를 받는다 (204 포함)", async () => {
    mockFetch(() => new Response(null, { status: 204, headers: { "X-Action-Id": "12" } }));
    let actionId = null;

    const result = await apiFetch("/event-instances/3", { method: "DELETE", onHeaders: (h) => (actionId = h.get("X-Action-Id")) });

    assert.equal(result, null);
    assert.equal(actionId, "12");
  });

  test("네트워크 오류는 status 0으로 알린다", async () => {
    globalThis.fetch = async () => {
      throw new TypeError("Failed to fetch");
    };

    await assert.rejects(apiFetch("/health"), (error) => error.status === 0 && error.message.includes("서버에 연결할 수 없어요"));
  });

  test("JSON이 아닌 에러 본문도 처리한다", async () => {
    mockFetch(() => new Response("Internal Server Error", { status: 500 }));

    await assert.rejects(apiFetch("/points/summary"), (error) => error.message === "서버에서 문제가 생겼어요. 잠시 후 다시 시도해 주세요.");
  });
});

describe("describeError 문구", () => {
  const validation = [
    { loc: ["body", "title"], msg: "String should have at least 1 character", type: "string_too_short" },
    { loc: ["body", "recurrence_rule"], msg: "Value error, invalid recurrence_rule 'X'", type: "value_error" },
  ];

  const cases = [
    [{ status: 401 }, "사용자 ID를 먼저 입력해 주세요."],
    [{ status: 422, detail: validation, method: "POST", path: "/events" }, "입력값을 확인해 주세요.\n• title: String should have at least 1 character\n• recurrence_rule: invalid recurrence_rule 'X'"],
    [{ status: 422, detail: validation, method: "POST", path: "/events/parse" }, "입력값을 확인해 주세요.\n• title: String should have at least 1 character\n• recurrence_rule: invalid recurrence_rule 'X'"],
    [{ status: 422, detail: "LLM 응답이 유효한 JSON이 아닙니다", method: "POST", path: "/events/parse" }, "AI가 요청을 제대로 이해하지 못했어요. 표현을 조금 바꿔서 다시 시도해 주세요."],
    [{ status: 500, detail: "LLM_API_KEY가 설정되지 않았습니다", method: "POST", path: "/daily-actual-logs/checkin" }, "AI 기능이 아직 서버에 설정되지 않았어요. 관리자에게 문의해 주세요."],
    [{ status: 502, detail: "x", method: "POST", path: "/compliance-reports" }, "AI 서버와 연결이 원활하지 않아요. 잠시 후 다시 시도해 주세요."],
    [{ status: 500, detail: "서버 내부 오류가 발생했습니다", method: "GET", path: "/tasks" }, "서버에서 문제가 생겼어요. 잠시 후 다시 시도해 주세요."],
    [{ status: 404, detail: "Event not found", method: "GET", path: "/events/9" }, "요청한 항목을 찾을 수 없어요. (Event not found)"],
    [{ status: 409, detail: "location 1 is used by 2 events", method: "DELETE", path: "/locations/1" }, "지금은 처리할 수 없어요: location 1 is used by 2 events"],
    [{ status: 422, detail: "end_date must not be before start_date", method: "PUT", path: "/date-ranges/1" }, "입력값을 확인해 주세요: end_date must not be before start_date"],
    [{ status: 418 }, "요청을 처리하지 못했어요. (HTTP 418)"],
  ];

  for (const [input, expected] of cases) {
    test(`${input.status} ${input.method ?? ""} ${input.path ?? ""}`, () => {
      assert.equal(describeError(input), expected);
    });
  }

  test("LLM 엔드포인트는 메서드와 쿼리를 구분해서 판별한다", () => {
    assert.equal(isLlmEndpoint("post", "/events/parse"), true);
    assert.equal(isLlmEndpoint("GET", "/compliance-reports/stats?days=30"), false);
    assert.equal(isLlmEndpoint("GET", "/compliance-reports"), false);
  });
});
