// Этот модуль держит все HTTP-запросы UI к backend API в одном месте.
async function request(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  if (!response.ok) throw await responseError(response);
  if (response.status === 204) return null;
  return response.json();
}

async function responseError(response) {
  const data = await response.json().catch(() => ({}));
  const detail = data.detail;
  const message = typeof detail === "string"
    ? detail
    : (detail?.message || `Ошибка сервера (${response.status})`);
  const error = new Error(message);
  error.messageSaved = detail?.user_message_saved === true;
  return error;
}

const sessionPath = (sessionId) => `/sessions/${encodeURIComponent(sessionId)}`;

export const sessionsApi = {
  // Служебные сессии запуска тоже нужны: их открывают из ленты, а сайдбар их прячет.
  list: () => request("/sessions?include_hidden=true"),
  create: (model, projectId = null) => request(
    "/sessions",
    { method: "POST", body: JSON.stringify({ model, project_id: projectId }) },
  ),
  configure: (sessionId, config) => request(
    `${sessionPath(sessionId)}/config`,
    { method: "PUT", body: JSON.stringify(config) },
  ),
  rename: (sessionId, title) => request(
    `${sessionPath(sessionId)}/title`,
    { method: "PUT", body: JSON.stringify({ title }) },
  ),
  delete: (sessionId) => request(sessionPath(sessionId), { method: "DELETE" }),
  // Холст сохраняется целиком; в ответе — ошибки, из-за которых команда пока не запустится.
  // Переписка агента с холста: создаётся, если ему ещё ничего не поручали.
  openNode: (sessionId, nodeId) => request(
    `${sessionPath(sessionId)}/canvas/${encodeURIComponent(nodeId)}/session`, { method: "POST" },
  ),
  saveCanvas: (sessionId, canvas) => request(
    `${sessionPath(sessionId)}/canvas`, { method: "PUT", body: JSON.stringify(canvas) },
  ),
};

export const projectsApi = {
  list: () => request("/projects"),
  create: (project) => request("/projects", { method: "POST", body: JSON.stringify(project) }),
  update: (projectId, project) => request(
    `/projects/${encodeURIComponent(projectId)}`,
    { method: "PUT", body: JSON.stringify(project) },
  ),
  delete: (projectId) => request(`/projects/${encodeURIComponent(projectId)}`, { method: "DELETE" }),
};

export const foldersApi = {
  list: (path) => request(path ? `/folders?path=${encodeURIComponent(path)}` : "/folders"),
};

export const messagesApi = {
  list: (sessionId) => request(`${sessionPath(sessionId)}/messages`),
};

export const catalogApi = {
  models: (provider) => request(`/models?provider=${encodeURIComponent(provider)}`),
  providers: () => request("/providers"),
  agents: () => request("/agents"),
};

export const skillsApi = {
  list: () => request("/skills"),
  create: (skill) => request("/skills", { method: "POST", body: JSON.stringify(skill) }),
  delete: (skillId) => request(`/skills/${encodeURIComponent(skillId)}`, { method: "DELETE" }),
  archive: () => request("/skills/archive"),
  clearArchive: () => request("/skills/archive", { method: "DELETE" }),
  restore: (archiveId) => request(`/skills/archive/${encodeURIComponent(archiveId)}/restore`, { method: "POST" }),
  read: (skillId) => request(`/skills/${encodeURIComponent(skillId)}`),
  save: (skillId, instructions) => request(
    `/skills/${encodeURIComponent(skillId)}`,
    { method: "PUT", body: JSON.stringify({ instructions }) },
  ),
};

export const agentsApi = {
  options: () => request("/agents/options"),
  create: (agent) => request("/agents", { method: "POST", body: JSON.stringify(agent) }),
  update: (agentId, agent) => request(
    `/agents/${encodeURIComponent(agentId)}`, { method: "PUT", body: JSON.stringify(agent) },
  ),
  delete: (agentId) => request(`/agents/${encodeURIComponent(agentId)}`, { method: "DELETE" }),
  prompt: (agentId) => request(`/agents/${encodeURIComponent(agentId)}/prompt`),
  savePrompt: (agentId, systemPrompt) => request(
    `/agents/${encodeURIComponent(agentId)}/prompt`,
    { method: "PUT", body: JSON.stringify({ system_prompt: systemPrompt }) },
  ),
};

// Поток событий SSE: onEvent вызывается на каждое событие. Без события finalType поток считается оборванным.
async function streamEvents(path, body, signal, onEvent, finalType) {
  const response = await fetch(`/api${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) throw await responseError(response);
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  let completed = false;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const chunk = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const data = chunk.split("\n").filter((line) => line.startsWith("data: ")).map((line) => line.slice(6)).join("\n");
      if (data) {
        const event = JSON.parse(data);
        completed ||= event.type === finalType;
        onEvent(event);
      }
    }
  }
  if (!completed) throw new Error("Соединение закрыто до завершения ответа.");
}

export const turnsApi = {
  stream: (sessionId, content, signal, onEvent) => streamEvents(
    `${sessionPath(sessionId)}/turns/stream`, { content }, signal, onEvent, "done",
  ),
  decide: (sessionId, callId, approved) => request(
    `${sessionPath(sessionId)}/approvals/${encodeURIComponent(callId)}`,
    { method: "POST", body: JSON.stringify({ approved }) },
  ),
};

export const toolsApi = {
  list: (sessionId) => request(`${sessionPath(sessionId)}/tools`),
  configure: (sessionId, toolId, enabled) => request(
    `${sessionPath(sessionId)}/tools/${encodeURIComponent(toolId)}`,
    { method: "PUT", body: JSON.stringify({ enabled }) },
  ),
};

export const memoryApi = {
  list: () => request("/memory"),
  read: (name) => request(`/memory/${encodeURIComponent(name)}`),
  save: (name, content) => request(
    `/memory/${encodeURIComponent(name)}`,
    { method: "PUT", body: JSON.stringify({ content }) },
  ),
  update: (sessionId) => request(`${sessionPath(sessionId)}/memory/update`, { method: "POST" }),
};
