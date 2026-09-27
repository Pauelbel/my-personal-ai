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
  list: () => request("/sessions"),
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
  read: (skillId) => request(`/skills/${encodeURIComponent(skillId)}`),
  save: (skillId, instructions) => request(
    `/skills/${encodeURIComponent(skillId)}`,
    { method: "PUT", body: JSON.stringify({ instructions }) },
  ),
};

export const agentsApi = {
  prompt: (agentId) => request(`/agents/${encodeURIComponent(agentId)}/prompt`),
  savePrompt: (agentId, systemPrompt) => request(
    `/agents/${encodeURIComponent(agentId)}/prompt`,
    { method: "PUT", body: JSON.stringify({ system_prompt: systemPrompt }) },
  ),
};

export const turnsApi = {
  // Ответ приходит потоком событий SSE; onEvent вызывается на каждое событие.
  stream: async (sessionId, content, signal, onEvent) => {
    const response = await fetch(`/api${sessionPath(sessionId)}/turns/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
      signal,
    });
    if (!response.ok) throw await responseError(response);
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += value;
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) !== -1) {
        const chunk = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const data = chunk.split("\n").filter((line) => line.startsWith("data: ")).map((line) => line.slice(6)).join("\n");
        if (data) onEvent(JSON.parse(data));
      }
    }
  },
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
