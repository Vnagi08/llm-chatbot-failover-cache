// Backend base URL resolution order:
//   1. window.__APP_CONFIG__.API_BASE_URL  (runtime, injected by the Docker entrypoint)
//   2. import.meta.env.VITE_API_BASE_URL   (build time, for static hosting)
//   3. http://localhost:8000                (local development default)
const runtime = (typeof window !== "undefined" && window.__APP_CONFIG__) || {};
export const API_BASE_URL = String(
  runtime.API_BASE_URL || import.meta.env.VITE_API_BASE_URL || "http://localhost:8000",
).replace(/\/+$/, "");

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

function describeDetail(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => {
        const field = Array.isArray(d.loc) ? d.loc.filter((p) => p !== "body").join(".") : "";
        return field ? `${field}: ${d.msg}` : d.msg;
      })
      .join("; ");
  }
  return "Unexpected error";
}

export async function sendChat({ sessionId, message, systemPrompt, simulateFailure, signal }) {
  const body = { session_id: sessionId, message };
  if (systemPrompt && systemPrompt.trim()) body.system_prompt = systemPrompt.trim();
  const headers = { "Content-Type": "application/json" };
  if (simulateFailure && simulateFailure.trim()) headers["X-Simulate-Failure"] = simulateFailure.trim();

  let response;
  try {
    response = await fetch(`${API_BASE_URL}/chat`, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      signal,
    });
  } catch (err) {
    if (err.name === "AbortError") throw err;
    throw new ApiError(`Cannot reach the backend at ${API_BASE_URL}.`, 0);
  }

  let data = null;
  try {
    data = await response.json();
  } catch {
    /* non-JSON body */
  }
  if (!response.ok) {
    const detail = data && data.detail !== undefined ? describeDetail(data.detail) : response.statusText;
    throw new ApiError(`${response.status} — ${detail}`, response.status);
  }
  return data;
}
