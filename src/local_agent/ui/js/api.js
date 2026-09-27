// Этот модуль держит все HTTP-запросы UI к backend API в одном месте.
async function request(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const detail = data.detail;
    const message = typeof detail === "string"
      ? detail
      : (detail?.message || `Ошибка сервера (${response.status})`);
    const error = new Error(message);
    error.messageSaved = detail?.user_message_saved === true;
    throw error;
  }
  if (response.status === 204) return null;
  return response.json();
}

export const sessionsApi = {
  list: () => request("/sessions"),
  create: (model) => request("/sessions", { method: "POST", body: JSON.stringify({ model }) }),
  configure: (sessionId, config) => request(
    `/sessions/${encodeURIComponent(sessionId)}/config`,
    { method: "PUT", body: JSON.stringify(config) },
  ),
  rename: (sessionId, title) => request(
    `/sessions/${encodeURIComponent(sessionId)}/title`,
    { method: "PUT", body: JSON.stringify({ title }) },
  ),
  delete: (sessionId) => request(
    `/sessions/${encodeURIComponent(sessionId)}`,
    { method: "DELETE" },
  ),
};

export const messagesApi = {
  list: (sessionId) => request(`/sessions/${encodeURIComponent(sessionId)}/messages`),
};

export const modelsApi = {
  list: () => request("/models"),
};

export const turnsApi = {
  create: (sessionId, content) => request(
    `/sessions/${encodeURIComponent(sessionId)}/turns`,
    { method: "POST", body: JSON.stringify({ content }) },
  ),
};

export const toolsApi = {
  list: () => request("/tools"),
  configure: (toolId, enabled) => request(
    `/tools/${encodeURIComponent(toolId)}`,
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
  update: (sessionId) => request(
    `/sessions/${encodeURIComponent(sessionId)}/memory/update`,
    { method: "POST" },
  ),
};
