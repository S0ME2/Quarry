import { afterAll, afterEach, beforeAll, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CampaignForm } from "../src/pages/Campaigns";
import type { Campaign } from "../src/api";

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
afterAll(() => {
  Reflect.deleteProperty(HTMLDialogElement.prototype, "showModal");
  Reflect.deleteProperty(HTMLDialogElement.prototype, "close");
});

function setup(campaign?: Campaign) {
  let submitted: Record<string, unknown> | undefined;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = String(input);
    const response = (value: unknown) =>
      new Response(JSON.stringify(value), {
        headers: { "Content-Type": "application/json" },
      });
    if (init?.method === "POST") {
      submitted = JSON.parse(String(init.body));
      return response({ id: "saved" });
    }
    if (path.endsWith("/sources"))
      return response({ items: [{ id: "iom", name: "IOM" }] });
    if (path.endsWith("/campaigns/campaign")) return response({ versions: [] });
    throw new Error(`Unexpected fixture request: ${path}`);
  });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <CampaignForm campaign={campaign} onClose={() => {}} />
    </QueryClientProvider>,
  );
  return { client, submitted: () => submitted };
}

it("scopes a new campaign to selected sources and leaves Telegram disabled", async () => {
  const fixture = setup();
  fireEvent.change(screen.getByLabelText("Campaign name"), {
    target: { value: "Office supplies" },
  });
  expect(
    screen.getByLabelText("Send Telegram notifications for relevant tenders"),
  ).not.toBeChecked();
  fireEvent.click(await screen.findByLabelText("IOM"));
  fireEvent.click(screen.getByRole("button", { name: "Save campaign" }));
  await waitFor(() =>
    expect(fixture.submitted()).toMatchObject({
      criteria: {
        source_ids: ["iom"],
        notifications: { enabled: false, channel: "telegram", chat_id: null },
      },
    }),
  );
  await waitFor(() => expect(fixture.client.isMutating()).toBe(0));
  fixture.client.clear();
});

it("preserves unavailable source selections and existing Telegram settings when editing", async () => {
  const campaign = {
    id: "campaign",
    active: true,
    current_criteria_version_id: "version",
    criteria: {
      name: "Existing",
      source_ids: ["removed"],
      notifications: { enabled: true, channel: "telegram", chat_id: "@alerts" },
    },
  } as Campaign;
  const fixture = setup(campaign);
  expect(screen.getByLabelText("Unavailable source (removed)")).toBeChecked();
  expect(
    screen.getByLabelText("Send Telegram notifications for relevant tenders"),
  ).toBeChecked();
  expect(
    screen.getByLabelText("Telegram chat ID or channel username"),
  ).toHaveValue("@alerts");
  await screen.findByLabelText("IOM");
  fireEvent.click(screen.getByRole("button", { name: "Save campaign" }));
  await waitFor(() =>
    expect(fixture.submitted()).toMatchObject({
      expected_version_id: "version",
      criteria: {
        source_ids: ["removed"],
        notifications: {
          enabled: true,
          channel: "telegram",
          chat_id: "@alerts",
        },
      },
    }),
  );
  await waitFor(() => expect(fixture.client.isMutating()).toBe(0));
  fixture.client.clear();
});

it("requires an explicit Telegram toggle and submits a destination", async () => {
  const fixture = setup();
  await screen.findByLabelText("IOM");
  fireEvent.change(screen.getByLabelText("Campaign name"), {
    target: { value: "Alerts" },
  });
  fireEvent.click(
    screen.getByLabelText("Send Telegram notifications for relevant tenders"),
  );
  fireEvent.change(
    screen.getByLabelText("Telegram chat ID or channel username"),
    {
      target: { value: " -1001234567890 " },
    },
  );
  fireEvent.click(screen.getByRole("button", { name: "Save campaign" }));
  await waitFor(() =>
    expect(fixture.submitted()).toMatchObject({
      criteria: {
        source_ids: [],
        notifications: {
          enabled: true,
          channel: "telegram",
          chat_id: "-1001234567890",
        },
      },
    }),
  );
  await waitFor(() => expect(fixture.client.isMutating()).toBe(0));
  fixture.client.clear();
});

it("preserves an existing email channel and destination in the administrator editor", async () => {
  const campaign = {
    id: "campaign",
    active: true,
    current_criteria_version_id: "version",
    criteria: {
      name: "Email campaign",
      source_ids: ["iom"],
      notifications: {
        enabled: true,
        channel: "email",
        email: "owner@example.com",
        chat_id: null,
      },
    },
  } as Campaign;
  const fixture = setup(campaign);
  expect(
    screen.getByLabelText("Send email notifications for relevant tenders"),
  ).toBeChecked();
  expect(screen.getByLabelText("Notification email")).toHaveValue(
    "owner@example.com",
  );
  expect(
    screen.queryByLabelText("Telegram chat ID or channel username"),
  ).not.toBeInTheDocument();
  await screen.findByLabelText("IOM");
  fireEvent.click(screen.getByRole("button", { name: "Save campaign" }));
  await waitFor(() =>
    expect(fixture.submitted()).toMatchObject({
      expected_version_id: "version",
      criteria: {
        notifications: {
          enabled: true,
          channel: "email",
          email: "owner@example.com",
          chat_id: null,
        },
      },
    }),
  );
  await waitFor(() => expect(fixture.client.isMutating()).toBe(0));
  fixture.client.clear();
});
