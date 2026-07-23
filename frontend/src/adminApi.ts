// Platform-admin API client. Talks to /admin/* with the deployment admin token
// (X-Admin-Token) — a different credential from tenant API keys, held only by
// whoever operates the SOPilot instance. The token lives in localStorage and is
// entered on the admin console's login.
import { ApiError } from "./api";

const ADMIN_TOKEN = "sopilot-admin-token";

export const getAdminToken = (): string => localStorage.getItem(ADMIN_TOKEN) || "";
export const setAdminToken = (t: string): void => localStorage.setItem(ADMIN_TOKEN, t);
export const clearAdminToken = (): void => localStorage.removeItem(ADMIN_TOKEN);

export async function adminApi<T = any>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch("/api" + path, {
    method,
    headers: { "Content-Type": "application/json", "X-Admin-Token": getAdminToken() },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) throw new ApiError(res.status, data.detail ?? data);
  return data as T;
}

export interface AdminTenant { tenant_id: string; slug: string; name: string; created_at: string; projects: number; active_keys: number }
export interface AdminKey { id: string; label: string; role: string; hash_prefix: string; created_at: string; revoked: boolean; revoked_at: string | null }
