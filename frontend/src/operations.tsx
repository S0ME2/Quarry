import { useState } from "react";
import { activeOperation, date, readable, type Operation } from "./api";
import { useAction, useResource } from "./data";
import {
  Diagnostics,
  Disclosure,
  ErrorMessage,
  Panel,
  QueryState,
  State,
} from "./ui";
const labels: Record<string, string> = {
  source_setup: "Recipe validation",
  recipe_generation: "Website investigation and recipe setup",
  collection: "Tender collection",
  automatic_matching: "Automatic matching",
  web_matching: "Manual matching",
  recipe_import: "Recipe import",
  activation: "Source activation",
};
export const operationName = (kind: string) => labels[kind] || readable(kind);
export function OperationDetails({ operation }: { operation: Operation }) {
  const resume = useAction();
  const r = operation.result;
  const monitoring =
    r.monitoring &&
    typeof r.monitoring === "object" &&
    !Array.isArray(r.monitoring)
      ? (r.monitoring as Record<string, unknown>)
      : null;
  const monitoringLabels: Record<string, string> = {
    status: "Monitoring",
    found_tenders: "Tenders found",
    relevant_tenders: "Relevant tenders",
    errors: "Monitoring errors",
    pending_evaluations: "Pending evaluations",
    unresolved_evaluations: "Evaluations needing review",
  };
  const resumable =
    (operation.status === "queued" &&
      [
        "collection",
        "recipe_validation",
        "source_setup",
        "activation",
        "web_matching",
        "automatic_matching",
      ].includes(operation.kind)) ||
    (operation.kind === "automatic_matching" && operation.status === "failed");
  const progressLabels: Record<string, string> = {
    stage: "Current step",
    records_processed: "Records checked",
    records_requested: "Sample limit",
    observations_collected: "Collected records",
    observed_count: "Records found",
    expected_count: "Expected records",
    coverage_status: "Coverage",
    validation_scope: "Validation scope",
    validation_passed: "Validation passed",
    duration_seconds: "Duration (seconds)",
    completed: "Evaluations completed",
    pending: "Evaluations pending",
    failed: "Failed evaluations",
    skipped: "Skipped",
    stopped: "Why execution stopped",
    budget_stopped: "Spending safeguard",
    message: "Details",
    error: "Error",
    detail: "Details",
  };
  const facts = Object.entries(r).filter(
    ([key, v]) => key in progressLabels && v !== null && typeof v !== "object",
  );
  return (
    <section aria-label="Operation progress" data-operation-id={operation.id}>
      <Panel title={operationName(operation.kind)}>
        <div className="actions">
          <State value={operation.status} />
          {activeOperation(operation) && (
            <span className="muted">Progress updates automatically</span>
          )}
        </div>
        {operation.kind === "recipe_generation" && (
          <p role="status">
            {operation.status === "activated"
              ? "Recipe independently verified and activated. This website is ready to collect."
              : operation.status === "failed"
                ? "Automatic setup failed. No new recipe was activated. Review the error below, then retry investigation from Sources."
                : activeOperation(operation)
                  ? "Quarry is investigating the website, generating a recipe and checking it independently. Collection becomes available only after activation."
                  : "Setup has not activated a recipe. Review the recorded checks before collecting."}
          </p>
        )}
        <dl className="fact-list">
          <div>
            <dt>Started</dt>
            <dd>{date(operation.started_at)}</dd>
          </div>
          <div>
            <dt>Finished</dt>
            <dd>{date(operation.finished_at)}</dd>
          </div>
          {facts.map(([k, v]) => (
            <div key={k}>
              <dt>{progressLabels[k]}</dt>
              <dd>
                {typeof v === "boolean"
                  ? v
                    ? "Yes"
                    : "No"
                  : k === "duration_seconds"
                    ? Number(v).toFixed(1)
                    : k === "stage"
                      ? readable(String(v))
                      : String(v)}
              </dd>
            </div>
          ))}
          {monitoring &&
            Object.entries(monitoringLabels).map(([key, label]) => {
              const value = monitoring[key];
              return (
                (typeof value === "number" || typeof value === "string") && (
                  <div key={`monitoring-${key}`}>
                    <dt>{label}</dt>
                    <dd>
                      {key === "status" ? (
                        <State value={String(value)} />
                      ) : (
                        value
                      )}
                    </dd>
                  </div>
                )
              );
            })}
        </dl>
        {operation.error_code && (
          <p role="alert" className="error-banner">
            {readable(operation.error_code)}
          </p>
        )}
        {operation.status === "failed" && r.http_status === 429 && (
          <p className="notice">
            The AI provider rate limit was reached. Wait for its quota to reset
            before retrying; repeated requests can delay recovery.
          </p>
        )}
        {operation.status === "failed" && r.http_status === 413 && (
          <p className="notice">
            The AI provider rejected the request size. Review input limits
            before retrying the same operation.
          </p>
        )}
        {operation.kind === "collection" && (
          <p className="muted">
            A successful collection can be a bounded sample. Coverage is
            established separately by source evidence.
          </p>
        )}
        {resumable && (
          <button
            className="primary"
            disabled={resume.isPending}
            onClick={() =>
              resume.mutate({
                path: `/operations/${operation.id}/resume`,
                body: {},
              })
            }
          >
            Resume operation
          </button>
        )}
        <ErrorMessage error={resume.error} />
        {r.run_id != null && <RunEvidence id={String(r.run_id)} />}
        <Diagnostics value={operation} />
      </Panel>
    </section>
  );
}
function RunEvidence({ id }: { id: string }) {
  const [open, setOpen] = useState(false);
  const run = useResource<{ data: unknown }>(open ? `/runs/${id}` : null);
  const evidence = useResource<{ data: unknown }>(
    open ? `/runs/${id}/evidence` : null,
  );
  return (
    <Disclosure title="Collection health and evidence">
      <button onClick={() => setOpen(true)}>Load collection evidence</button>
      {open && (
        <>
          <QueryState query={run} />
          <QueryState query={evidence} />
          {run.data && (
            <Diagnostics label="Collection health" value={run.data.data} />
          )}{" "}
          {evidence.data && (
            <Diagnostics label="Source evidence" value={evidence.data.data} />
          )}
        </>
      )}
    </Disclosure>
  );
}
