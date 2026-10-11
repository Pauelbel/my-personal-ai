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
  // С дочерними сессиями саб-агентов: их разговоры открываются с холста и из полосы агентов.
  sessions: () => request("/sessions?include_hidden=true"),
  session: (id) => request(`/sessions/${id}`),
  createSession: (projectId) => request("/sessions", json("POST", projectId ? { project_id: projectId } : {})),
  renameSession: (id, title) => request(`/sessions/${id}/title`, json("PUT", { title })),
  deleteSession: (id) => request(`/sessions/${id}`, { method: "DELETE" }),
  configure: (id, provider, model) => request(`/sessions/${id}/config`, json("PUT", { provider, model })),
  messages: (id) => request(`/sessions/${id}/messages`),
  send: (id, content, signal, onEvent, retry = false) => stream(`/sessions/${id}/turns/stream`, { content, retry }, signal, onEvent),
  team: (id) => request(`/sessions/${id}/team`),
  link: (agentId, target) => request(`/agents/${encodeURIComponent(agentId)}/links/${encodeURIComponent(target)}`, { method: "PUT" }),
  unlink: (agentId, target) => request(`/agents/${encodeURIComponent(agentId)}/links/${encodeURIComponent(target)}`, { method: "DELETE" }),
  teamChat: (id, agentId) => request(`/sessions/${id}/team/${encodeURIComponent(agentId)}/chat`, { method: "POST" }),
  resume: (id, decision, signal, onEvent) => stream(`/sessions/${id}/resume`, decision, signal, onEvent),
  // Ход идёт на сервере независимо от вкладки: к нему можно подключиться заново и остановить его.
  turn: (id) => request(`/sessions/${id}/turn`),
  follow: (id, start, signal, onEvent) => stream(`/sessions/${id}/turn/stream?${q({ start })}`, null, signal, onEvent),
  stop: (id) => request(`/sessions/${id}/turn/stop`, { method: "POST" }),
  enqueue: (id, content) => request(`/sessions/${id}/queue`, json("POST", { content })),
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
  models: (provider) => request(`/models?${q({ provider })}`),

  attention: (sessionId) => request(`/attention${sessionId ? `?${q({ session_id: sessionId })}` : ""}`),

  capabilities: () => request("/capabilities"),
  capabilitySource: (kind, id) => request(`/capabilities/${kind}/${encodeURIComponent(id)}`),
  archive: (kind, id) => request(`/capabilities/${kind}/${encodeURIComponent(id)}/archive`, { method: "POST" }),
  restore: (kind, archiveId) => request(`/capabilities/${kind}/restore/${encodeURIComponent(archiveId)}`, { method: "POST" }),
  reloadCapabilities: () => request("/capabilities/reload", { method: "POST" }),

  wiki: () => request("/wiki"),
  wikiSearch: (query) => request(`/wiki/search?${q({ q: query })}`),
  wikiPage: (path) => request(`/wiki/page?${q({ path })}`),

  memory: () => request("/memory"),
  memoryDocument: (name) => request(`/memory/${encodeURIComponent(name)}`),
  saveMemory: (name, content) => request(`/memory/${encodeURIComponent(name)}`, json("PUT", { content })),
  updateMemory: (sessionId) => request(`/sessions/${sessionId}/memory/update`, { method: "POST" }),
};
