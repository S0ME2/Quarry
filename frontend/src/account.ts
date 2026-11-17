import type { Campaign, Page, Tender } from "./api";
import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";

export type User = {
  id: string;
  email: string;
  role: "user" | "admin";
  active: boolean;
  operator?: boolean;
};
export type AccountSource = {
  id: string;
  name: string;
  url: string;
  status: "UNDER_REVIEW" | "APPROVED" | "REJECTED" | "NEEDS_ATTENTION";
  custom: boolean;
  error: string | null;
};
export type AccountCampaign = Omit<Campaign, "criteria"> & {
  interval_seconds: number;
  monitoring?: {
    status:
      | "idle"
      | "scheduled"
      | "running"
      | "blocked"
      | "failed"
      | "completed"
      | "partial"
      | "coverage_unknown";
    coverage_status?: "complete" | "incomplete" | "unknown";
    sources?: {
      source_id: string;
      max_tenders: number | null;
      scope_approved: boolean;
      coverage_status: string;
    }[];
    last_error: string | null;
    last_scheduled_at: string | null;
    last_run_at: string | null;
    next_run_at: string | null;
    last_operation_status: string | null;
    scheduler_running: boolean;
    pending: number;
    failed: number;
  };
  criteria: Omit<Campaign["criteria"], "notifications"> & {
    notifications: {
      enabled: boolean;
      channel: "telegram" | "email";
      chat_id: string | null;
      email?: string | null;
    };
  };
};
export type AccountTender = Tender & {
  relevance: string;
  reason: string | Record<string, unknown> | null;
  campaign_id: string;
  campaign_name: string;
  analysis_stale: boolean;
  documents?: Record<string, unknown>[];
  analysis?: unknown;
};
export type AccountTenders = Page<AccountTender>;

export function setAccountSession(client: QueryClient, user: User | null) {
  void client.cancelQueries();
  client.removeQueries({
    predicate: (query) => query.queryKey[0] !== "session",
  });
  client.getMutationCache().clear();
  client.setQueryData(["session"], user);
}

export class AccountError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

export async function accountRequest<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch("/api/v1" + path, {
    ...options,
    credentials: "same-origin",
  });
  const value: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const body = value as {
      error?: { message?: string };
      detail?: string | { msg: string; loc?: string[] }[];
    } | null;
    const detail = body?.detail;
    const message =
      body?.error?.message ||
      (typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail.map((item) => item.msg).join(". ")
          : null) ||
      (response.status === 401
        ? "Your session has expired. Sign in to continue."
        : `Request failed (${response.status}). Try again.`);
    if (response.status === 401 && !path.startsWith("/auth/"))
      window.dispatchEvent(new Event("quarry-session-expired"));
    throw new AccountError(message, response.status);
  }
  return value as T;
}

export function accountSend<T>(path: string, body?: unknown, method = "POST") {
  return accountRequest<T>(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export function explanation(value: AccountTender["reason"]): string {
  if (!value) return "No explanation available.";
  if (typeof value === "string") return value;
  return String(
    value.detail ||
      value.message ||
      value.reason ||
      "Analysis available in details.",
  );
}

export function useAccountResource<T>(
  path: string | null,
  poll: boolean | number = false,
) {
  return useQuery({
    queryKey: ["account", path],
    enabled: path !== null,
    queryFn: ({ signal }) => accountRequest<T>(path!, { signal }),
    retry: false,
    refetchInterval: poll === true ? 3000 : poll || false,
  });
}

export function useAccountAction<T = unknown>(after?: (result: T) => void) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      path,
      body,
      method,
    }: {
      path: string;
      body?: unknown;
      method?: string;
    }) => accountSend<T>(path, body, method),
    onSuccess: async (result) => {
      after?.(result);
      await client.invalidateQueries({ queryKey: ["account"] });
    },
  });
}
