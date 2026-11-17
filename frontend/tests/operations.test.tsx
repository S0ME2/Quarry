import { expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { OperationDetails } from "../src/operations";
import type { Operation } from "../src/api";

it("shows blocked matching alongside completed collection", () => {
  const operation: Operation = {
    id: "collection",
    kind: "collection",
    source_id: "iom",
    status: "completed",
    created_at: "2026-11-05T10:00:00Z",
    started_at: null,
    finished_at: null,
    error_code: null,
    result: {
      monitoring: {
        status: "blocked",
        found_tenders: 2,
        relevant_tenders: 0,
        errors: 0,
        pending_evaluations: 2,
        unresolved_evaluations: 0,
      },
    },
  };
  render(
    <QueryClientProvider client={new QueryClient()}>
      <OperationDetails operation={operation} />
    </QueryClientProvider>,
  );
  expect(screen.getByText("completed")).toBeVisible();
  expect(screen.getByText("blocked")).toBeVisible();
  expect(screen.getByText("blocked")).toHaveClass("warning");
  expect(screen.getByText("Pending evaluations")).toBeVisible();
  expect(screen.getByText("Relevant tenders")).toBeVisible();
});

it.each([
  ["running", "checking it independently"],
  ["failed", "No new recipe was activated"],
  ["activated", "independently verified and activated"],
])("explains %s automatic onboarding", (status, message) => {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <OperationDetails
        operation={{
          id: "generation",
          kind: "recipe_generation",
          source_id: "source",
          status,
          created_at: "2026-11-05T10:00:00Z",
          started_at: null,
          finished_at: null,
          error_code: status === "failed" ? "provider_rate_limit" : null,
          result: status === "failed" ? { http_status: 429 } : {},
        }}
      />
    </QueryClientProvider>,
  );
  expect(screen.getByText(new RegExp(message))).toBeVisible();
  if (status === "failed")
    expect(screen.getByText(/Wait for its quota to reset/)).toBeVisible();
});
