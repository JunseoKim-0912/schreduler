// 작은 DOM 헬퍼. 사용자·서버가 준 문자열은 항상 textContent로 넣는다 (innerHTML을 쓰지 않아 XSS 걱정이 없다).

export function el(tag, { className, text, attrs } = {}, children = []) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  for (const [name, value] of Object.entries(attrs ?? {})) {
    if (value !== undefined && value !== null && value !== false) node.setAttribute(name, value === true ? "" : value);
  }
  for (const child of children) if (child) node.append(child);
  return node;
}

export function badge(text, variant) {
  return el("span", { className: `badge badge-${variant}`, text });
}

export function setStatus(node, text) {
  node.textContent = text;
  node.hidden = !text;
}
