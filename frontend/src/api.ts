import type { components } from "./schema";
export type Tender = components["schemas"]["TenderView"];
export type Campaign = components["schemas"]["CampaignView"];
export type Criteria = Campaign["criteria"];
export type Operation = components["schemas"]["OperationView"];
export type Match = components["schemas"]["StoredMatchResult"];
export type MatchState = components["schemas"]["TenderMatchingStatus"];
export type Result = components["schemas"]["WebMatchResult"];
export type Page<T> = {
  items: T[];
  total: number;
  offset: number;
  limit: number;
};
export type Source = {
  id: string;
  name: string;
  url?: string;
  enabled: boolean;
  health: string;
  recipe_version: string | null;
  tender_count: number;
  collection_status?: string;
  coverage_status?: string;
  last_success?: string;
  manual_only?: boolean;
  source_manual_only?: boolean;
  global_manual_only?: boolean;
  warnings?: unknown[];
};
export type SourceWorkspace = {
  recipe_import: Operation | null;
  operations: Operation[];
  operation_timeout_seconds: number;
};
export type Detail = {
  tender: Tender;
  product_items: Record<string, unknown>[] | null;
  metadata: unknown;
  provenance: string;
  history: {
    id: string;
    observed_at: string;
    previous_version_id: string | null;
    changed_fields: Record<string, unknown>;
  }[];
};
export type BudgetSettings = components["schemas"]["MatchingBudget"];
export type Budget = {
  settings: BudgetSettings;
  active_model: { provider: string; model: string };
  pricing_matches_model: boolean;
  accounted_cost_usd: number;
  estimated_cost_usd: number | null;
  provider_reported_cost_usd: number | null;
  requests: number;
  uncertain_requests: number;
};
export async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch("/api/v1/web" + path, options);
  const value = await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401)
      window.dispatchEvent(new Event("quarry-session-expired"));
    throw new Error(
      value?.error?.message ||
        `Request failed (${response.status}). Check your backend connection.`,
    );
  }
  return value as T;
}
export function send<T>(path: string, body: unknown, method = "POST") {
  return request<T>(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
export const activeOperation = (op: Operation) =>
  op.status === "queued" || op.status === "running";
export const readable = (value: string) => value.replaceAll("_", " ");
export const date = (value?: string | null) =>
  value
    ? new Intl.DateTimeFormat(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(new Date(value))
    : "Not provided";
export const money = (
  value?: string | number | null,
  currency?: string | null,
) =>
  value == null
    ? "Not provided"
    : `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(Number(value))}${currency ? " " + currency : ""}`;
export const usd = (value: number | null | undefined) =>
  value == null
    ? "Not reported"
    : new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
        maximumFractionDigits: 6,
      }).format(value);
export const safeURL = (value: string | null | undefined) =>
  value && /^https?:\/\//i.test(value) ? value : undefined;
