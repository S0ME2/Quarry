import { afterEach, expect, it, vi } from "vitest";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import Sources from "../src/pages/Sources";

afterEach(() => vi.restoreAllMocks());

it("protects edits during saved-recipe loading and follows the requested operation", async () => {
  let finishLoad!: (response: Response) => void;
  const saved = new Promise<Response>((resolve) => {
    finishLoad = resolve;
  });
  const reply = (body: unknown) =>
    new Response(JSON.stringify(body), {
      headers: { "Content-Type": "application/json" },
    });
  const operation = {
    id: "old",
    kind: "source_setup",
    source_id: "source",
    status: "failed",
    created_at: "2026-11-05T10:00:00Z",
    result: {},
  };
  const fetch = vi
    .spyOn(globalThis, "fetch")
    .mockImplementation(async (input, init) => {
      const path = String(input);
      if (path.endsWith("/sources"))
        return reply({
          items: [
            {
              id: "source",
              name: "Source",
              enabled: true,
              recipe_version: "1",
            },
          ],
        });
      if (path.endsWith("/workspace"))
        return reply({ operations: [operation] });
      if (path.includes("/recipe?")) return saved;
      if (path.endsWith("/recipe") && init?.method === "POST")
        return reply({ ...operation, id: "new", status: "queued" });
      if (path.endsWith("/operations/new"))
        return reply({ ...operation, id: "new", status: "running" });
      throw new Error(`Unexpected fixture request: ${path}`);
    });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <Sources />
    </QueryClientProvider>,
  );
  fireEvent.click(await screen.findByRole("button", { name: "Open source" }));
  fireEvent.click(
    await screen.findByText("Collection recipe", { exact: true }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Load saved recipe" }));
  const editor = screen.getByLabelText("Recipe JSON", { exact: true });
  expect(editor).toBeDisabled();
  finishLoad(reply({ text: '{"saved":true}' }));
  await waitFor(() => expect(editor).toBeEnabled());
  expect(editor).toHaveValue('{"saved":true}');
  fireEvent.change(editor, { target: { value: '{"edited":true}' } });
  fireEvent.click(
    screen.getByRole("button", { name: "Save and validate recipe" }),
  );
  await waitFor(() =>
    expect(
      fetch.mock.calls.some(([input]) =>
        String(input).endsWith("/operations/new"),
      ),
    ).toBe(true),
  );
  expect(
    await within(
      screen.getByRole("region", { name: "Operation progress" }),
    ).findByText("running", { exact: true }),
  ).toBeVisible();
  await waitFor(() => expect(client.isMutating()).toBe(0));
  expect(screen.getByRole("button", { name: "Scrape tenders" })).toBeDisabled();
  client.clear();
});

it("requires confirmation for source scheduling and keeps global pause visible", async () => {
  let patch: unknown;
  const confirmed = vi.spyOn(window, "confirm").mockReturnValue(false);
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    if (init?.method === "PATCH") {
      patch = JSON.parse(String(init.body));
      return new Response(JSON.stringify({}));
    }
    return new Response(
      JSON.stringify(
        String(input).endsWith("/sources")
          ? {
              items: [
                {
                  id: "source",
                  name: "Source",
                  enabled: true,
                  recipe_version: "1",
                  manual_only: true,
                  source_manual_only: true,
                  global_manual_only: true,
                },
              ],
            }
          : { operations: [] },
      ),
    );
  });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <Sources />
    </QueryClientProvider>,
  );
  fireEvent.click(await screen.findByRole("button", { name: "Open source" }));
  expect(
    screen.getByText(/Global scheduled monitoring is paused/),
  ).toBeVisible();
  fireEvent.click(
    screen.getByRole("button", { name: "Allow scheduled monitoring" }),
  );
  expect(patch).toBeUndefined();
  confirmed.mockReturnValue(true);
  fireEvent.click(
    screen.getByRole("button", { name: "Allow scheduled monitoring" }),
  );
  await waitFor(() => expect(patch).toEqual({ manual_only: false }));
  await waitFor(() => expect(client.isMutating()).toBe(0));
  client.clear();
});
