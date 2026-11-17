import { afterEach, beforeAll, beforeEach, expect, it, vi } from "vitest";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "../src/App";
import { AccountGate, SignIn } from "../src/pages/Account";
import Monitoring, { MonitoringForm } from "../src/pages/Monitoring";
import Administration from "../src/pages/Administration";
import AccountTenders, {
  AccountTenderDetails,
} from "../src/pages/AccountTenders";
import {
  accountRequest,
  setAccountSession,
  type AccountCampaign,
  type User,
} from "../src/account";

const user: User = {
  id: "user-1",
  email: "owner@example.com",
  role: "user",
  active: true,
};
const admin: User = {
  id: "admin-1",
  email: "admin@example.com",
  role: "admin",
  active: true,
};
const response = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
afterEach(() => vi.restoreAllMocks());
beforeAll(() => {
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
    configurable: true,
    value(this: HTMLDialogElement) {
      this.setAttribute("open", "");
    },
  });
  Object.defineProperty(HTMLDialogElement.prototype, "close", {
    configurable: true,
    value() {},
  });
});
beforeEach(() => {
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
});
function mount(
  node: React.ReactNode,
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
) {
  render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
  return client;
}

it("registers, signs in with cookies, and opens first monitoring setup", async () => {
  const requests: {
    path: string;
    body: unknown;
    credentials: RequestCredentials | undefined;
  }[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    requests.push({
      path: String(input),
      body: JSON.parse(String(init?.body)),
      credentials: init?.credentials,
    });
    return response(user, String(input).endsWith("register") ? 201 : 200);
  });
  const signedIn = vi.fn();
  mount(<SignIn onAuthenticated={signedIn} />);
  fireEvent.click(screen.getByRole("button", { name: "Create an account" }));
  fireEvent.change(screen.getByLabelText("Email address"), {
    target: { value: "owner@example.com" },
  });
  fireEvent.change(screen.getByLabelText("Password"), {
    target: { value: "fixture-password-123" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Create account" }));
  await waitFor(() => expect(signedIn).toHaveBeenCalledWith(user));
  expect(requests.map((request) => request.path)).toEqual([
    "/api/v1/auth/register",
    "/api/v1/auth/login",
  ]);
  expect(
    requests.every((request) => request.credentials === "same-origin"),
  ).toBe(true);
  expect(window.location.hash).toBe("#/monitoring");
});

it("keeps backend failures visible instead of treating them as signed-out access", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    response({ detail: "Service unavailable" }, 503),
  );
  const client = mount(
    <AccountGate>{() => <p>Private workspace</p>}</AccountGate>,
  );
  expect(
    await screen.findByRole("heading", { name: "Workspace unavailable" }),
  ).toBeVisible();
  expect(screen.getByRole("alert")).toHaveTextContent("Service unavailable");
  expect(screen.queryByText("Private workspace")).not.toBeInTheDocument();
  client.clear();
});

it("expires a session and removes cached private results before another user signs in", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    response({ detail: "Expired" }, 401),
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  client.setQueryData(["session"], user);
  client.setQueryData(["account", "/account/tenders"], {
    items: [{ title: "Private tender" }],
  });
  mount(
    <AccountGate>
      {(account) => <p>Private workspace for {account.email}</p>}
    </AccountGate>,
    client,
  );
  expect(screen.getByText(`Private workspace for ${user.email}`)).toBeVisible();
  await act(async () => {
    await expect(accountRequest("/account/tenders")).rejects.toThrow("Expired");
  });
  expect(
    await screen.findByRole("heading", { name: "Sign in to Quarry" }),
  ).toBeVisible();
  expect(client.getQueryData(["account", "/account/tenders"])).toBeUndefined();
  expect(
    screen.queryByText(`Private workspace for ${user.email}`),
  ).not.toBeInTheDocument();
  act(() =>
    setAccountSession(client, {
      ...user,
      id: "another",
      email: "another@example.com",
    }),
  );
  expect(
    await screen.findByText("Private workspace for another@example.com"),
  ).toBeVisible();
  expect(client.getQueryData(["account", "/account/tenders"])).toBeUndefined();
  client.clear();
});

it("signs out and clears all account results without leaking operator navigation", async () => {
  window.location.hash = "/tenders";
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    if (String(input).endsWith("/auth/logout"))
      return new Response(null, { status: 204 });
    return response({ items: [], total: 0, offset: 0, limit: 25 });
  });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  client.setQueryData(["session"], user);
  client.setQueryData(["private-other-query"], "Sensitive value");
  mount(<App />, client);
  expect(
    screen.queryByRole("link", { name: "Administration" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("link", { name: "Matching" }),
  ).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
  expect(
    await screen.findByRole("heading", { name: "Sign in to Quarry" }),
  ).toBeVisible();
  expect(client.getQueryData(["private-other-query"])).toBeUndefined();
  client.clear();
});

it("saves selected ready sources, schedule, terms, and the account email", async () => {
  let submitted: Record<string, unknown> | undefined;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    if (init?.method) {
      submitted = JSON.parse(String(init.body));
      return response({ id: "campaign-1" }, 201);
    }
    if (String(input).endsWith("/account/sources"))
      return response({
        items: [
          { id: "catalog", name: "Catalog site", status: "APPROVED" },
          { id: "broken", name: "Broken site", status: "NEEDS_ATTENTION" },
        ],
      });
    throw new Error(`Unexpected request ${String(input)}`);
  });
  const closed = vi.fn();
  const client = mount(<MonitoringForm user={user} onClose={closed} />);
  fireEvent.change(screen.getByLabelText("Campaign name"), {
    target: { value: "Office supplies" },
  });
  expect(
    screen.getByRole("button", { name: "Save monitoring" }),
  ).toBeDisabled();
  fireEvent.click(await screen.findByLabelText(/Catalog site/));
  expect(screen.getByLabelText(/Broken site/)).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Keywords"), {
    target: { value: "paper, pens" },
  });
  fireEvent.change(screen.getByLabelText("Stop words"), {
    target: { value: "used" },
  });
  fireEvent.change(screen.getByLabelText("Check for new tenders"), {
    target: { value: "10800" },
  });
  expect(screen.getByLabelText("Notification email")).toHaveValue(user.email);
  expect(screen.getByLabelText("Notification email")).toHaveAttribute(
    "readonly",
  );
  fireEvent.click(screen.getByRole("button", { name: "Save monitoring" }));
  await waitFor(() =>
    expect(submitted).toMatchObject({
      interval_seconds: 10800,
      active: true,
      criteria: {
        source_ids: ["catalog"],
        include_keywords: ["paper", "pens"],
        exclude_keywords: ["used"],
        automatic_mode: "rules",
        notifications: {
          enabled: true,
          channel: "email",
          email: user.email,
          chat_id: null,
        },
      },
    }),
  );
  await waitFor(() => expect(closed).toHaveBeenCalled());
  client.clear();
});

it("preserves the expected campaign version when editing monitoring", async () => {
  let submitted: Record<string, unknown> | undefined;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (_input, init) => {
    if (init?.method) {
      submitted = JSON.parse(String(init.body));
      return response({});
    }
    return response({
      items: [{ id: "catalog", name: "Catalog site", status: "APPROVED" }],
    });
  });
  const campaign = {
    id: "campaign",
    current_criteria_version_id: "expected-version",
    active: true,
    interval_seconds: 3600,
    criteria: {
      name: "Existing",
      source_ids: ["catalog"],
      include_keywords: [],
      exclude_keywords: [],
      automatic_mode: "semantic",
      automatic_ai_authorized: true,
      ai_budget_usd: 5,
      ai_request_limit: 50,
      notifications: {
        enabled: true,
        channel: "email",
        email: user.email,
        chat_id: null,
      },
    },
  } as unknown as AccountCampaign;
  const client = mount(
    <MonitoringForm campaign={campaign} user={user} onClose={() => {}} />,
  );
  await screen.findByLabelText(/Catalog site/);
  fireEvent.click(screen.getByRole("button", { name: "Save monitoring" }));
  await waitFor(() =>
    expect(submitted).toMatchObject({
      expected_version_id: "expected-version",
      criteria: {
        automatic_mode: "semantic",
        automatic_ai_authorized: true,
        ai_budget_usd: 5,
        ai_request_limit: 50,
      },
    }),
  );
  await waitFor(() => expect(client.isMutating()).toBe(0));
  client.clear();
});

it("shows a recoverable blocked AI state in the user's monitoring workspace", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    response({
      items: [
        {
          id: "campaign",
          active: true,
          interval_seconds: 3600,
          processing: 1,
          failed: 0,
          criteria: {
            name: "Blocked campaign",
            include_keywords: ["network"],
            source_ids: ["catalog"],
            automatic_mode: "semantic",
            notifications: { enabled: true, channel: "email" },
          },
          monitoring: {
            status: "blocked",
            last_error: "matching_budget_stopped",
            last_scheduled_at: null,
            pending: 1,
            failed: 0,
          },
        },
      ],
    }),
  );
  const client = mount(<Monitoring user={user} />);
  expect(await screen.findByText("blocked", { exact: true })).toBeVisible();
  expect(screen.getByText(/Analysis is temporarily unavailable/)).toBeVisible();
  expect(screen.getByText(/Automatic checking was not detected/)).toBeVisible();
  client.clear();
});

it("serializes inclusive local date boundaries and removes cleared date filters", async () => {
  const paths: string[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    paths.push(String(input));
    return response({ items: [], total: 0, offset: 0, limit: 25 });
  });
  const client = mount(<AccountTenders onOpen={() => {}} />);
  await screen.findByRole("heading", { name: "No records yet" });
  fireEvent.change(screen.getByLabelText("Published from"), {
    target: { value: "2026-11-01" },
  });
  fireEvent.change(screen.getByLabelText("Published to"), {
    target: { value: "2026-11-06" },
  });
  await waitFor(() =>
    expect(
      new URL(
        paths.filter((path) => path.includes("/account/tenders?")).at(-1)!,
        "http://localhost",
      ).searchParams.get("published_to"),
    ).toBe(new Date("2026-11-06T23:59:59.999").toISOString()),
  );
  const latest = new URL(
    paths.filter((path) => path.includes("/account/tenders?")).at(-1)!,
    "http://localhost",
  );
  expect(latest.searchParams.get("published_from")).toBe(
    new Date("2026-11-01T00:00:00").toISOString(),
  );
  fireEvent.change(screen.getByLabelText("Published to"), {
    target: { value: "" },
  });
  await waitFor(() =>
    expect(
      new URL(
        paths.filter((path) => path.includes("/account/tenders?")).at(-1)!,
        "http://localhost",
      ).searchParams.has("published_to"),
    ).toBe(false),
  );
  expect(paths.some((path) => /published_to=(?:&|$)/.test(path))).toBe(false);
  client.clear();
});

it("requires confirmation before changing another user's role and protects own controls", async () => {
  let patch: unknown;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = String(input);
    if (init?.method === "PATCH") {
      patch = JSON.parse(String(init.body));
      return response({ ...user, role: "admin" });
    }
    if (path.endsWith("/admin/ai"))
      return response({ providers: {}, tasks: {} });
    if (path.endsWith("/admin/smtp")) return response({ configured: false });
    if (path.endsWith("/admin/source-requests")) return response({ items: [] });
    expect(path).not.toContain("active=");
    expect(path).not.toContain("role=");
    return response({ items: [admin, user] });
  });
  const client = mount(<Administration currentUser={admin} />);
  await screen.findByText(admin.email);
  expect(screen.getByRole("button", { name: "Remove admin" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Make admin" }));
  expect(patch).toBeUndefined();
  const dialog = screen.getByRole("dialog");
  expect(dialog).toHaveTextContent("global access");
  fireEvent.click(
    within(dialog).getByRole("button", { name: "Confirm change" }),
  );
  await waitFor(() => expect(patch).toEqual({ role: "admin" }));
  await waitFor(() => expect(client.isMutating()).toBe(0));
  client.clear();
});

it("opens a tender with its row's campaign and fetches that exact analysis", async () => {
  const paths: string[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const path = String(input);
    paths.push(path);
    if (path.includes("/sources")) return response({ items: [] });
    if (path.includes("/tenders?"))
      return response({
        items: [
          {
            id: "tender",
            title: "Router",
            campaign_id: "campaign-b",
            campaign_name: "Concrete",
            relevance: "NOT_RELEVANT",
          },
        ],
        total: 1,
      });
    return response({
      id: "tender",
      title: "Router",
      campaign_id: "campaign-b",
      campaign_name: "Concrete",
      relevance: "NOT_RELEVANT",
      analysis_stale: true,
    });
  });
  const opened = vi.fn();
  const client = mount(<AccountTenders onOpen={opened} />);
  fireEvent.click(await screen.findByRole("button", { name: "Router" }));
  expect(opened).toHaveBeenCalledWith("tender", "campaign-b");
  expect(screen.getByText("Concrete")).toBeVisible();
  client.clear();
});

it("requests the authorized campaign detail and labels saved analysis", async () => {
  const paths: string[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const path = String(input);
    paths.push(path);
    if (path.includes("/sources")) return response({ items: [] });
    return response({
      id: "tender",
      title: "Router",
      campaign_id: "campaign-b",
      campaign_name: "Concrete",
      relevance: "NOT_RELEVANT",
      analysis_stale: true,
    });
  });
  const client = mount(
    <AccountTenderDetails
      id="tender"
      campaignId="campaign-b"
      back={() => {}}
    />,
  );
  expect(await screen.findByText("Campaign: Concrete")).toBeVisible();
  expect(screen.getByRole("status")).toHaveTextContent(
    "Saved analysis uses earlier requirements",
  );
  expect(paths).toContain(
    "/api/v1/account/tenders/tender?campaign_id=campaign-b",
  );
  client.clear();
});

it("shows incomplete collection coverage and approved sample limits", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    response({
      items: [
        {
          id: "campaign",
          active: true,
          interval_seconds: 3600,
          processing: 0,
          failed: 0,
          criteria: {
            name: "Sampling",
            include_keywords: [],
            source_ids: ["catalog"],
            notifications: { enabled: false },
          },
          monitoring: {
            status: "partial",
            coverage_status: "incomplete",
            sources: [
              { source_id: "catalog", max_tenders: 2, scope_approved: true },
            ],
            scheduler_running: true,
            pending: 0,
            failed: 0,
          },
        },
      ],
    }),
  );
  const client = mount(<Monitoring user={user} />);
  expect(
    await screen.findByText(
      "Partial website coverage. Some tenders may be missing.",
    ),
  ).toBeVisible();
  expect(
    screen.getByText(/approved sample limit 2 tenders per check/),
  ).toBeVisible();
  expect(
    screen.getByText(
      /Full website monitoring requires separate administrator approval/,
    ),
  ).toBeVisible();
  client.clear();
});
