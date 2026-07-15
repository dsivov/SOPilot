// Thin API client. Credentials live in localStorage (also settable via
// ?key=...&project=... once, e.g. for demos), requests go through the vite
// proxy at /api so the browser never needs CORS.

const KEY = "sopilot-api-key";
const PROJECT = "sopilot-project";

const params = new URLSearchParams(window.location.search);
if (params.get("key")) localStorage.setItem(KEY, params.get("key")!);
if (params.get("project")) localStorage.setItem(PROJECT, params.get("project")!);

export function getCreds(): { key: string; project: string } {
  return { key: localStorage.getItem(KEY) || "", project: localStorage.getItem(PROJECT) || "" };
}

export function setCreds(key: string, project: string): void {
  localStorage.setItem(KEY, key);
  localStorage.setItem(PROJECT, project);
}

export function clearCreds(): void {
  localStorage.removeItem(KEY);
  localStorage.removeItem(PROJECT);
}

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
    this.status = status;
    this.detail = detail;
  }
}

export async function apiUpload<T = any>(path: string, file: File, fields: Record<string, string> = {}): Promise<T> {
  const { key, project } = getCreds();
  const form = new FormData();
  form.append("file", file);
  for (const [k, v] of Object.entries(fields)) form.append(k, v);
  const res = await fetch("/api" + path, {
    method: "POST",
    headers: { Authorization: `Bearer ${key}`, "X-Project": project },
    body: form,
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (res.status === 401) window.dispatchEvent(new Event("sopilot-auth-failed"));
  if (!res.ok) throw new ApiError(res.status, data.detail ?? data);
  return data as T;
}

export async function api<T = any>(method: string, path: string, body?: unknown): Promise<T> {
  const { key, project } = getCreds();
  const res = await fetch("/api" + path, {
    method,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${key}`,
      "X-Project": project,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (res.status === 401) window.dispatchEvent(new Event("sopilot-auth-failed"));
  if (!res.ok) throw new ApiError(res.status, data.detail ?? data);
  return data as T;
}
