import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  activeOperation,
  date,
  readable,
  send,
  usd,
  type Campaign,
  type Match,
  type MatchState,
  type Operation,
  type Page,
} from "./api";
import {
  useAction,
  useCompletedOperation,
  useDebounced,
  useResource,
} from "./data";
import {
  Check,
  Diagnostics,
  Dialog,
  Disclosure,
  ErrorMessage,
  Field,
  Panel,
  QueryState,
  State,
} from "./ui";
import { OperationDetails } from "./operations";
export function CampaignPicker({
  value,
  onChange,
}: {
  value: string;
  onChange: (id: string) => void;
}) {
  const [search, setSearch] = useState(""),
    [offset, setOffset] = useState(0),
    [searching, setSearching] = useState(false);
  const q = useDebounced(search);
  const choices = useResource<
    Page<{ id: string; name: string; criteria_number: number }>
  >(`/campaign-options?q=${encodeURIComponent(q)}&offset=${offset}`);
  const detail = useResource<{ campaign: Campaign }>(
    value ? `/campaigns/${value}` : null,
  );
  const items = choices.data?.items || [];
  return (
    <div className="campaign-context">
      <Field label="Campaign">
        {(id) => (
          <select
            id={id}
            value={value}
            onChange={(e) => onChange(e.target.value)}
          >
            <option value="">Choose a campaign</option>
            {value && !items.some((x) => x.id === value) && (
              <option value={value}>
                {detail.data?.campaign.criteria.name || "Selected campaign"}
              </option>
            )}
            {items.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name} · v{c.criteria_number}
              </option>
            ))}
          </select>
        )}
      </Field>
      {detail.data && (
        <span className="muted">
          {!detail.data.campaign.active
            ? "Campaign paused"
            : !detail.data.campaign.criteria.automatic_matching
              ? "Automatic matching off"
              : detail.data.campaign.criteria.automatic_mode === "semantic" &&
                  !detail.data.campaign.criteria.automatic_ai_authorized
                ? "Automatic AI not authorized"
                : `Automatic ${detail.data.campaign.criteria.automatic_mode === "semantic" ? "AI" : "rules"} matching on`}
        </span>
      )}
      <button className="text-button" onClick={() => setSearching(!searching)}>
        Find campaign
      </button>
      {searching && (
        <>
          <Field label="Search campaigns">
            {(id) => (
              <input
                id={id}
                type="search"
                value={search}
                onChange={(e) => {
                  setSearch(e.target.value);
                  setOffset(0);
                }}
              />
            )}
          </Field>
          <div className="actions">
            <button
              disabled={offset === 0}
              onClick={() => setOffset(offset - 50)}
            >
              Previous campaigns
            </button>
            <button
              disabled={!choices.data || offset + 50 >= choices.data.total}
              onClick={() => setOffset(offset + 50)}
            >
              More campaigns
            </button>
          </div>
        </>
      )}
      <ErrorMessage error={choices.error} />
      <ErrorMessage error={detail.error} />
    </div>
  );
}
export function useMatchStates(campaign: string, ids: string[]) {
  return useQuery({
    queryKey: ["matching-status", campaign, ids.join(",")],
    enabled: !!campaign && ids.length > 0,
    queryFn: () =>
      send<MatchState[]>("/matching/status", {
        campaign_id: campaign,
        tender_ids: ids,
      }),
    staleTime: 15000,
    retry: false,
    refetchInterval: (q) =>
      q.state.data?.some((s) => s.state === "pending" || s.state === "running")
        ? 1200
        : false,
  });
}
export function evaluationLabel(outcome: Match["outcome"]): string {
  return outcome.relevance === "RELEVANT"
    ? "Relevant"
    : outcome.relevance === "NOT_RELEVANT"
      ? "Not Relevant"
      : outcome.processing_status === "failed"
        ? "Technical error"
        : "Needs review";
}
export function MatchSummary({
  state,
  onInspect,
}: {
  state: MatchState | undefined;
  onInspect?: (result: Match) => void;
}) {
  if (!state) return <span className="muted">Not loaded</span>;
  if (state.state !== "current")
    return (
      <>
        <State
          value={
            state.state === "not_evaluated" ? "Not evaluated" : state.state
          }
        />
        {state.state === "stale" && (
          <small>Requirements or tender changed</small>
        )}
        {state.error_code && <small>{readable(state.error_code)}</small>}
      </>
    );
  const r = state.result;
  if (!r) return <span>Result unavailable</span>;
  return (
    <>
      <span
        className={`state ${r.outcome.relevance === "RELEVANT" ? "positive" : r.outcome.relevance == null ? "warning" : ""}`}
      >
        {evaluationLabel(r.outcome)}
      </span>
      <small className="line-clamp">
        {(r.outcome.evidence_verification?.status === "invalid"
          ? r.outcome.evidence_verification.detail ||
            "Source evidence could not be verified"
          : null) ||
          (r.outcome.result === "unsure"
            ? r.outcome.uncertainties[0]?.detail
            : null) ||
          r.outcome.reasons[0]?.detail ||
          "View recorded decision"}
      </small>
      {onInspect && (
        <button className="text-button" onClick={() => onInspect(r)}>
          Inspect result
        </button>
      )}
    </>
  );
}
export function ResultDetails({ result }: { result: Match }) {
  const o = result.outcome;
  const reported =
    o.model_calls.length && o.model_calls.every((c) => c.cost != null)
      ? o.model_calls.reduce((sum, c) => sum + (c.cost || 0), 0)
      : null;
  return (
    <>
      <div className="result-title">
        <span
          className={`state ${o.relevance === "RELEVANT" ? "positive" : o.relevance == null ? "warning" : ""}`}
        >
          {evaluationLabel(o)}
        </span>
        <span className="muted">
          {o.decision_stage === "rules" ? "Rules evaluation" : "AI evaluation"}{" "}
          · {date(result.created_at)}
        </span>
      </div>
      {o.evidence_verification?.status === "invalid" && (
        <p className="notice">
          Source evidence could not be verified. The model opinion is not a
          verified decision.
        </p>
      )}
      <h3>Why this decision</h3>
      {o.reasons.map((r, i) => (
        <p key={i}>{r.detail}</p>
      ))}
      {!o.reasons.length && <p>No explanation was recorded.</p>}
      {!!o.uncertainties.length && (
        <div className="notice">
          <h3>What needs attention</h3>
          {o.uncertainties.map((u, i) => (
            <p key={i}>{u.detail}</p>
          ))}
        </div>
      )}
      <Disclosure title="AI opinion and source evidence">
        <h3>Model opinion</h3>
        {o.model_opinion ? (
          <>
            <State value={o.model_opinion.decision} />
            <p>{o.model_opinion.explanation}</p>
          </>
        ) : (
          <p>No model opinion recorded. Rules do not require AI.</p>
        )}
        <h3>Evidence verification</h3>
        <State value={o.evidence_verification?.status || "not_provided"} />
        <p>
          {o.evidence_verification?.detail ||
            "No verified source evidence recorded."}
        </p>
        {o.evidence.map((e, i) => (
          <blockquote key={i}>
            <p>{e.quote}</p>
            <cite>Source field: {readable(e.field)}</cite>
          </blockquote>
        ))}
      </Disclosure>
      <Disclosure title="Execution and provider charges">
        <dl className="fact-list">
          <div>
            <dt>Provider-reported cost</dt>
            <dd>{usd(reported)}</dd>
          </div>
          <div>
            <dt>Matcher</dt>
            <dd>
              {readable(result.matcher_implementation)} {result.matcher_version}
            </dd>
          </div>
          {o.model_calls.map((call, i) => (
            <div key={i}>
              <dt>
                {call.provider} · {call.model}
              </dt>
              <dd>
                {call.input_tokens ?? "Unknown"} input /{" "}
                {call.output_tokens ?? "Unknown"} output tokens ·{" "}
                {usd(call.cost)}
                {call.failure_detail && <p>{call.failure_detail}</p>}
              </dd>
            </div>
          ))}
        </dl>
        <small>
          Provider-reported cost is separate from configured estimates and
          budget reservations.
        </small>
      </Disclosure>
      <Diagnostics value={result} />
    </>
  );
}
export function RunMatching({
  campaign,
  ids,
  onClose,
}: {
  campaign: string;
  ids: string[];
  onClose: () => void;
}) {
  const [mode, setMode] = useState<"rules" | "semantic">("rules"),
    [force, setForce] = useState(false),
    [preview, setPreview] = useState<Operation | null>(null),
    [operation, setOperation] = useState("");
  const prepare = useAction<Operation>(setPreview),
    confirm = useAction<Operation>((r) => {
      setOperation(r.id);
      setPreview(null);
    });
  const status = useResource<Operation>(
    operation ? `/operations/${operation}` : null,
    activeOperation,
  );
  useCompletedOperation(status.data);
  return (
    <Dialog title="Run matching" onClose={onClose}>
      <p>
        Evaluate {ids.length} selected {ids.length === 1 ? "tender" : "tenders"}{" "}
        against the chosen campaign.
      </p>
      {!operation && !preview && (
        <>
          <Field label="Matcher">
            {(id) => (
              <select
                id={id}
                value={mode}
                onChange={(e) =>
                  setMode(e.target.value as "rules" | "semantic")
                }
              >
                <option value="rules">Rules · no AI charges</option>
                <option value="semantic">
                  AI · provider charges may apply
                </option>
              </select>
            )}
          </Field>
          <Check
            label="Create a new evaluation for these versions"
            checked={force}
            onChange={setForce}
          />
          <small>
            By default, existing evaluations are reused. A new evaluation may
            incur new AI charges.
          </small>
          <ErrorMessage error={prepare.error} />
          <div className="form-footer">
            <button onClick={onClose}>Cancel</button>
            <button
              className="primary"
              disabled={prepare.isPending}
              onClick={() =>
                prepare.mutate({
                  path: "/matching/preview",
                  body: {
                    campaign_id: campaign,
                    tender_ids: ids,
                    mode,
                    force_new: force,
                  },
                })
              }
            >
              Review {mode === "rules" ? "rules" : "AI"} run
            </button>
          </div>
        </>
      )}
      {preview && (
        <Panel
          title={
            mode === "semantic"
              ? "Confirm AI evaluation"
              : "Confirm rules evaluation"
          }
        >
          <dl className="fact-list">
            <div>
              <dt>Selected tenders</dt>
              <dd>{String(preview.result.selected_count)}</dd>
            </div>
            <div>
              <dt>Maximum AI requests</dt>
              <dd>{String(preview.result.maximum_model_requests)}</dd>
            </div>
            {mode === "semantic" && (
              <>
                <div>
                  <dt>Model</dt>
                  <dd>{String(preview.result.model)}</dd>
                </div>
                <div>
                  <dt>Cost estimate</dt>
                  <dd>Unavailable before provider usage</dd>
                </div>
              </>
            )}
          </dl>
          {mode === "semantic" && (
            <p className="notice">
              {String(preview.result.spending_protection)} No automatic paid
              retries.
            </p>
          )}
          <ErrorMessage error={confirm.error} />
          <div className="actions">
            <button
              disabled={confirm.isPending}
              onClick={() => setPreview(null)}
            >
              Back
            </button>
            <button
              className="primary"
              disabled={confirm.isPending}
              onClick={() =>
                confirm.mutate({
                  path: `/matching/${preview.id}/confirm`,
                  body: {},
                })
              }
            >
              {mode === "semantic" ? "Confirm AI run" : "Confirm rules run"}
            </button>
          </div>
        </Panel>
      )}
      {operation && (
        <>
          <QueryState query={status} />
          {status.data && <OperationDetails operation={status.data} />}
        </>
      )}
    </Dialog>
  );
}
