// 모든 frontend 모듈의 이름 있는 import가 실제로 export돼 있는지 (node --check는 이걸 잡지 못한다).
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { test } from "node:test";

const dir = new URL("../../frontend/", import.meta.url);
const files = readdirSync(dir).filter((name) => name.endsWith(".js"));
const source = Object.fromEntries(files.map((name) => [name, readFileSync(new URL(name, dir), "utf-8")]));

function exportsOf(name) {
  const text = source[name];
  return new Set([...text.matchAll(/export\s+(?:async\s+)?(?:function|const|let|class)\s+(\w+)/g)].map((m) => m[1]));
}

test("이름 있는 import는 모두 그 모듈의 export다", () => {
  const missing = [];
  for (const [name, text] of Object.entries(source)) {
    for (const match of text.matchAll(/import\s*\{([^}]+)\}\s*from\s*"\.\/([\w.-]+\.js)"/g)) {
      const exported = exportsOf(match[2]);
      for (const imported of match[1].split(",").map((part) => part.trim().split(/\s+as\s+/)[0]).filter(Boolean)) {
        if (!exported.has(imported)) missing.push(`${name}: ${imported} from ${match[2]}`);
      }
    }
  }
  assert.deepEqual(missing, []);
});
