import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ResultDetails, MatchSummary } from "../src/matches";
import { safeURL, type Match } from "../src/api";
const result: Match = {
  id: "result",
  job_id: "job",
  tender_id: "tender",
  tender_version_id: "tender-v1",
  campaign_id: "campaign",
  criteria_version_id: "criteria-v1",
  matcher_version: "v3",
  matcher_implementation: "intelligent",
  settings_hash: "",
  created_at: "2026-10-16T10:00:00Z",
  completion_token: null,
  outcome: {
    processing_status: "needs_review",
    relevance: null,
    schema_version: 1,
    result: "unsure",
    score: 0,
    confidence: null,
    reasons: [
      {
        code: "evidence_invalid",
        detail: "Source evidence could not be verified",
        field: null,
      },
    ],
    uncertainties: [],
    matcher_implementation: "intelligent",
    matcher_version: "v3",
    tender_version_id: "tender-v1",
    criteria_version_id: "criteria-v1",
    decision_stage: "semantic",
    evidence: [],
    model_calls: [
      {
        stage: "semantic",
        provider: "groq",
        model: "model",
        model_version: null,
        input_tokens: 100,
        output_tokens: 40,
        cost: null,
        error_code: null,
        failure_stage: null,
        failure_detail: null,
        http_status: null,
        retry_after_seconds: null,
        attempts: 1,
        attempt_errors: [],
      },
    ],
    model_opinion: {
      decision: "match",
      confidence: 0.95,
      explanation: "Model says this fits",
    },
    evidence_verification: {
      status: "invalid",
      error_code: "not_grounded",
      detail: "Quote was absent from source",
    },
  },
};
describe("decision integrity", () => {
  it("keeps model opinion separate from verified decision and unknown charges", () => {
    render(<ResultDetails result={result} />);
    expect(screen.getByText("Needs review")).toBeVisible();
    expect(screen.getByText("Model says this fits")).toBeInTheDocument();
    expect(
      screen.getByText("Quote was absent from source"),
    ).toBeInTheDocument();
    expect(screen.getAllByText("Not reported").length).toBeGreaterThan(0);
    expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
  });
  it("identifies stale evaluations without promoting historical outcomes", () => {
    render(
      <MatchSummary
        state={{
          tender_id: "tender",
          state: "stale",
          result,
          error_code: null,
        }}
      />,
    );
    expect(screen.getByText("stale")).toBeVisible();
    expect(screen.queryByText("Relevant")).not.toBeInTheDocument();
  });
  it("does not infer unevaluated when projection is unavailable", () => {
    render(<MatchSummary state={undefined} />);
    expect(screen.getByText("Not loaded")).toBeVisible();
    expect(screen.queryByText("Not evaluated")).not.toBeInTheDocument();
  });
  it("explains unverified evidence before the model rationale", () => {
    render(
      <MatchSummary
        state={{
          tender_id: "tender",
          state: "current",
          result,
          error_code: null,
        }}
      />,
    );
    expect(screen.getByText("Quote was absent from source")).toBeVisible();
    expect(screen.queryByText("Model says this fits")).not.toBeInTheDocument();
  });
  it("rejects unsafe source links", () => {
    expect(safeURL("javascript:alert(1)")).toBeUndefined();
    expect(safeURL("https://example.com/lot")).toBe("https://example.com/lot");
  });
  it("shows a failed evaluation without presenting the diagnostic as relevance", () => {
    render(
      <ResultDetails
        result={{
          ...result,
          outcome: {
            ...result.outcome,
            result: "no_match",
            processing_status: "failed",
            relevance: null,
          },
        }}
      />,
    );
    expect(screen.getByText("Technical error")).toBeVisible();
    expect(screen.queryByText("Not Relevant")).not.toBeInTheDocument();
  });
});
