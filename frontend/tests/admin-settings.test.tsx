import { afterEach, beforeAll, expect, it, vi } from "vitest";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import AdminSettings from "../src/pages/AdminSettings";

const response = (value: unknown) =>
  new Response(JSON.stringify(value), {
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

it("requires an administrator to resolve a possible legacy website duplicate", async () => {
  const writes: { path: string; body: unknown }[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = String(input);
    if (init?.method) {
      writes.push({ path, body: JSON.parse(String(init.body)) });
      return response({});
    }
    if (path.endsWith("/admin/source-requests"))
      return response({
        items: [
          {
            id: "new",
            name: "Tenders",
            url: "https://new.example/",
            decision: "pending",
            technical_verified: false,
            enabled: true,
            manual_only: false,
            identity_candidates: [{ id: "legacy", name: "Tenders" }],
            latest_operation: null,
          },
        ],
      });
    if (path.endsWith("/admin/ai"))
      return response({ providers: {}, tasks: {} });
    if (path.endsWith("/admin/smtp")) return response({ configured: false });
    throw new Error(`Unexpected URL ${path}`);
  });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <AdminSettings />
    </QueryClientProvider>,
  );
  const same = await screen.findByRole("button", {
    name: "Same website: Tenders (legacy)",
  });
  expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
  fireEvent.click(same);
  await waitFor(() =>
    expect(writes).toContainEqual({
      path: "/api/v1/admin/source-requests/new/resolve",
      body: { existing_source_id: "legacy" },
    }),
  );
  client.clear();
});

it("supports provider settings, source decisions and SMTP test with truthful delivery status", async () => {
  const writes: { path: string; method: string; body: unknown }[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = String(input);
    if (init?.method) {
      writes.push({
        path,
        method: init.method,
        body: init.body ? JSON.parse(String(init.body)) : null,
      });
      return response(
        path.endsWith("/test")
          ? path.includes("smtp")
            ? { status: "accepted" }
            : { ok: true }
          : {},
      );
    }
    if (path.endsWith("/admin/ai"))
      return response({
        providers: {
          p1: {
            name: "Compatible",
            base_url: "https://ai.example/v1",
            enabled: true,
            has_api_key: true,
          },
        },
        tasks: {},
      });
    if (path.endsWith("/admin/smtp"))
      return response({
        configured: true,
        source: "settings",
        host: "smtp.example",
        port: 587,
        security: "starttls",
        sender: "sender@example.com",
        password_configured: true,
      });
    if (path.endsWith("/admin/source-requests"))
      return response({
        items: [
          {
            id: "s1",
            name: "Tender site",
            url: "https://tender.example",
            decision: "pending",
            technical_verified: false,
            enabled: true,
            manual_only: true,
            latest_operation: { status: "failed", error_code: "unverified" },
          },
        ],
      });
    throw new Error(`Unexpected URL ${path}`);
  });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <AdminSettings />
    </QueryClientProvider>,
  );
  await screen.findByRole("button", { name: "Edit" });
  expect(await screen.findByText(/Verification: not passed/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Approve" }));
  await waitFor(() =>
    expect(writes).toContainEqual({
      path: "/api/v1/admin/source-requests/s1",
      method: "PATCH",
      body: { decision: "approved" },
    }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Test connection" }));
  expect(await screen.findByText("Connection succeeded.")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Add provider" }));
  const addDialog = screen.getByRole("dialog");
  fireEvent.change(within(addDialog).getByLabelText("Provider name"), {
    target: { value: "New provider" },
  });
  fireEvent.change(within(addDialog).getByLabelText("API base URL"), {
    target: { value: "https://api.example/v1" },
  });
  fireEvent.change(within(addDialog).getByLabelText("API key"), {
    target: { value: "test-secret" },
  });
  fireEvent.click(
    within(addDialog).getByRole("button", { name: "Save provider" }),
  );
  await waitFor(() =>
    expect(writes).toContainEqual({
      path: "/api/v1/admin/ai/providers",
      method: "POST",
      body: {
        name: "New provider",
        base_url: "https://api.example/v1",
        protocol: "openai-compatible",
        enabled: true,
        api_key: "test-secret",
      },
    }),
  );
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Edit" }));
  const dialog = screen.getByRole("dialog");
  expect(within(dialog).getByLabelText(/New API key/)).toHaveValue("");
  fireEvent.click(within(dialog).getByLabelText("Enabled"));
  fireEvent.click(
    within(dialog).getByRole("button", { name: "Save provider" }),
  );
  await waitFor(() =>
    expect(writes).toContainEqual({
      path: "/api/v1/admin/ai/providers/p1",
      method: "PUT",
      body: {
        name: "Compatible",
        base_url: "https://ai.example/v1",
        protocol: "openai-compatible",
        enabled: false,
      },
    }),
  );
  const recipe = screen.getByText("Website recipe generation").closest("form")!;
  fireEvent.change(within(recipe).getByLabelText("Provider"), {
    target: { value: "p1" },
  });
  fireEvent.change(within(recipe).getByLabelText("Model name"), {
    target: { value: "model-x" },
  });
  fireEvent.click(within(recipe).getByRole("button", { name: "Save model" }));
  await waitFor(() =>
    expect(
      writes.some(
        (w) =>
          w.path.endsWith("/tasks/recipe_generation") &&
          (w.body as { model: string }).model === "model-x",
      ),
    ).toBe(true),
  );
  expect(screen.getByLabelText(/New password/)).toHaveValue("");
  fireEvent.click(screen.getByRole("button", { name: "Save email settings" }));
  await waitFor(() =>
    expect(
      writes.some(
        (w) =>
          w.path.endsWith("/admin/smtp") && !("password" in (w.body as object)),
      ),
    ).toBe(true),
  );
  fireEvent.change(screen.getByLabelText("Recipient email address"), {
    target: { value: "recipient@example.com" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send test email" }));
  expect(
    await screen.findByText(/Delivery to the recipient is not confirmed/),
  ).toBeVisible();
  client.clear();
});
