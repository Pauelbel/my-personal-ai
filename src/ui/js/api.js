// HTTP API приложения. Ошибки сервера превращаются в Error с понятным текстом.

async function request(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
  });
  if (!response.ok) throw await responseError(response);
  return response.status === 204 ? null : response.json();
}

async function responseError(response) {
  let message = `Ошибка ${response.status}`;
  try {
    const data = await response.json();
    const detail = data.detail;
    message = typeof detail === "string" ? detail : detail?.message || (Array.isArray(detail) ? detail.map((item) => item.msg).join("; ") : message);
  } catch {
    // Ответ без JSON: оставляем код ошибки.
  }
  return new Error(message);
}

const json = (method, body) => ({ method, body: JSON.stringify(body) });
const q = (params) => new URLSearchParams(params).toString();

// Поток событий хода. Заканчивается событием done (ответ), interrupt (ход ждёт решения), stopped или error.
// body === null — подключение к уже идущему ходу (GET).
async function stream(path, body, signal, onEvent) {
  const response = await fetch(`/api${path}`, body === null ? { signal } : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal,
  });
  if (!response.ok) throw await responseError(response);
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  let finished = false;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const chunk = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const data = chunk.split("\n").filter((line) => line.startsWith("data: ")).map((line) => line.slice(6)).join("\n");
      if (!data) continue;
      const event = JSON.parse(data);
      finished ||= ["done", "interrupt", "error", "stopped"].includes(event.type);
      onEvent(event);
    }
  }
  if (!finished) throw new Error("Соединение закрыто до конца ответа.");
}

export const api = {
  sessions: () => request("/sessions"),
  createSession: (projectId) => request("/sessions", json("POST", projectId ? { project_id: projectId } : {})),
  renameSession: (id, title) => request(`/sessions/${id}/title`, json("PUT", { title })),
  deleteSession: (id) => request(`/sessions/${id}`, { method: "DELETE" }),
  // Пустую сессию убирает сервер и только если в ней правда ничего нет; иначе отвечает 409.
  discardSession: (id) => request(`/sessions/${id}/discard`, { method: "POST" }),
  configure: (id, provider, model) => request(`/sessions/${id}/config`, json("PUT", { provider, model })),
  messages: (id) => request(`/sessions/${id}/messages`),
  send: (id, content, signal, onEvent, retry = false, attachments = []) =>
    stream(`/sessions/${id}/turns/stream`, { content, retry, attachments }, signal, onEvent),
  // Картинка как data URL → её имя для сообщения.
  attach: (data) => request("/attachments", json("POST", { data })),
  resume: (id, decision, signal, onEvent) => stream(`/sessions/${id}/resume`, decision, signal, onEvent),
  // Ход идёт на сервере независимо от вкладки: к нему можно подключиться заново и остановить его.
  turn: (id) => request(`/sessions/${id}/turn`),
  follow: (id, start, signal, onEvent) => stream(`/sessions/${id}/turn/stream?${q({ start })}`, null, signal, onEvent),
  stop: (id) => request(`/sessions/${id}/turn/stop`, { method: "POST" }),
  enqueue: (id, content, attachments = []) => request(`/sessions/${id}/queue`, json("POST", { content, attachments })),
  dequeue: (id, itemId) => request(`/sessions/${id}/queue/${itemId}`, { method: "DELETE" }),

  projects: () => request("/projects"),
  createProject: (name, workspace) => request("/projects", json("POST", { name, workspace })),
  updateProject: (id, name, workspace, mode) => request(`/projects/${id}`, json("PUT", { name, workspace, mode })),
  files: (projectId, path) => request(`/projects/${projectId}/files?${q({ path })}`),
  file: (projectId, path) => request(`/projects/${projectId}/files/content?${q({ path })}`),
  saveFile: (projectId, path, content) => request(`/projects/${projectId}/files/content?${q({ path })}`, json("PUT", { content })),
  moveFile: (projectId, path, newPath) => request(`/projects/${projectId}/files/move?${q({ path })}`, json("POST", { new_path: newPath })),
  deleteFile: (projectId, path) => request(`/projects/${projectId}/files?${q({ path })}`, { method: "DELETE" }),
  deleteProject: (id) => request(`/projects/${id}`, { method: "DELETE" }),
  folders: (path) => request(`/folders${path ? `?${q({ path })}` : ""}`),

  providers: () => request("/providers"),
  providerConfig: () => request("/providers/config"),
  probeProvider: (fields) => request("/providers/probe", json("POST", fields)),
  createProvider: (fields) => request("/providers", json("POST", fields)),
  updateProvider: (id, fields) => request(`/providers/${id}`, json("PUT", fields)),
  deleteProvider: (id) => request(`/providers/${id}`, { method: "DELETE" }),
  setDefaultModel: (provider, model) => request("/providers/default", json("PUT", { provider, model })),
  models: (provider) => request(`/models?${q({ provider })}`),
  // Правила основного агента и его встроенный промпт (только для просмотра).
  instructions: () => request("/instructions"),
  saveInstructions: (text) => request("/instructions", json("PUT", { text })),

  attention: (sessionId) => request(`/attention${sessionId ? `?${q({ session_id: sessionId })}` : ""}`),

  capabilities: () => request("/capabilities"),
  capabilitySource: (kind, id) => request(`/capabilities/${kind}/${encodeURIComponent(id)}`),
  archive: (kind, id) => request(`/capabilities/${kind}/${encodeURIComponent(id)}/archive`, { method: "POST" }),
  enable: (kind, id) => request(`/capabilities/library/${kind}/${encodeURIComponent(id)}`, { method: "POST" }),
  restore: (kind, archiveId) => request(`/capabilities/${kind}/restore/${encodeURIComponent(archiveId)}`, { method: "POST" }),
  createCapability: (kind, id, text) => request(`/capabilities/${kind}`, { method: "POST", body: JSON.stringify({ id, text }) }),
  saveSource: (kind, id, files) => request(`/capabilities/${kind}/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify({ files }) }),
  purge: (kind, archiveId) => request(`/capabilities/${kind}/archive/${encodeURIComponent(archiveId)}`, { method: "DELETE" }),
  reloadCapabilities: () => request("/capabilities/reload", { method: "POST" }),
  setSecret: (name, value) => request(`/capabilities/secrets/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify({ value }) }),
  deleteSecret: (name) => request(`/capabilities/secrets/${encodeURIComponent(name)}`, { method: "DELETE" }),
  checkConnector: (id) => request(`/capabilities/connectors/${encodeURIComponent(id)}/check`, { method: "POST" }),
  tryConnector: (id, args) => request(`/capabilities/connectors/${encodeURIComponent(id)}/try`, json("POST", { arguments: args })),
  installPackages: (requirements) => request("/capabilities/packages", json("POST", { requirements })),
  uninstallPackage: (name) => request(`/capabilities/packages/${encodeURIComponent(name)}`, { method: "DELETE" }),
  // Переключатели MCP: состояние сохраняется в data/mcp.json и действует сразу.
  switchServer: (name, enabled) => request(`/capabilities/mcp/${encodeURIComponent(name)}`, json("PUT", { enabled })),
  switchTool: (name, tool, enabled) =>
    request(`/capabilities/mcp/${encodeURIComponent(name)}/tools/${encodeURIComponent(tool)}`, json("PUT", { enabled })),

  wiki: () => request("/wiki"),
  wikiSearch: (query) => request(`/wiki/search?${q({ q: query })}`),
  wikiPage: (path) => request(`/wiki/page?${q({ path })}`),
  wikiGraph: () => request("/wiki/graph"),
  // Чат вики — служебная сессия вне проектов: её сообщения, ход и очередь — обычными запросами сессии.
  wikiChat: () => request("/wiki/chat"),
  ensureWikiChat: (provider, model) => request("/wiki/chat", json("POST", { provider, model })),
  clearWikiChat: () => request("/wiki/chat", { method: "DELETE" }),
  // Меню «Действия ▾» чата вики: data/wiki-actions.md.
  wikiActions: () => request("/wiki/actions"),
  saveWikiActions: (actions) => request("/wiki/actions", json("PUT", { actions })),

  memory: () => request("/memory"),
  memoryDocument: (name) => request(`/memory/${encodeURIComponent(name)}`),
  saveMemory: (name, content) => request(`/memory/${encodeURIComponent(name)}`, json("PUT", { content })),
  updateMemory: (sessionId) => request(`/sessions/${sessionId}/memory/update`, { method: "POST" }),
};
