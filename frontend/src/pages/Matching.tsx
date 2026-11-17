import { useState } from "react";
import { date, type Match, type Page, type Result } from "../api";
import { useResource } from "../data";
import { evaluationLabel, ResultDetails } from "../matches";
import { Dialog, Disclosure, Pager, QueryState, State, Table } from "../ui";
import { TenderBrowser, type CampaignSelection } from "./Tenders";
function Results({
  campaign,
  onOpen,
  current = false,
}: {
  campaign: string;
  onOpen: (id: string) => void;
  current?: boolean;
}) {
  const [offset, setOffset] = useState(0),
    [filter, setFilter] = useState("all"),
    [selected, setSelected] = useState<Match | null>(null);
  const query = useResource<Page<Result>>(
    `/results?campaign_id=${campaign}&offset=${offset}&limit=25&state=${filter}&view=${current ? "current" : "history"}`,
  );
  return (
    <>
      {!current && (
        <>
          <p>
            All recorded evaluations for the selected campaign, including
            earlier results.
          </p>
          <label>
            Evaluation outcome{" "}
            <select
              aria-label="Historical outcome filter"
              value={filter}
              onChange={(e) => {
                setFilter(e.target.value);
                setOffset(0);
              }}
            >
              {[
                "all",
                "match",
                "no_match",
                "unsure",
                "technical_error",
                "unverified_evidence",
              ].map((s) => (
                <option key={s} value={s}>
                  {s.replaceAll("_", " ")}
                </option>
              ))}
            </select>
          </label>
        </>
      )}
      <QueryState query={query} empty={query.data?.items.length === 0}>
        {current
          ? "No current relevant tenders for this campaign."
          : "No recorded evaluations for this campaign."}
      </QueryState>
      {query.data && query.data.items.length > 0 && (
        <>
          <Table
            name={current ? "Relevant tenders" : "Historical matching results"}
            headers={["Tender", "Recorded decision", "Evaluated", "Details"]}
          >
            {query.data.items.map((r) => (
              <tr key={r.id}>
                <td>
                  <button
                    className="record-link"
                    onClick={() => onOpen(r.tender_id)}
                  >
                    {r.tender_title || "Untitled tender"}
                  </button>
                </td>
                <td>
                  <State
                    value={
                      r.outcome.model_calls.some((call) => call.error_code)
                        ? "Technical error"
                        : r.outcome.evidence_verification?.status === "invalid"
                          ? "Unverified evidence"
                          : evaluationLabel(r.outcome)
                    }
                  />
                </td>
                <td>{date(r.created_at)}</td>
                <td>
                  <button onClick={() => setSelected(r)}>Inspect result</button>
                </td>
              </tr>
            ))}
          </Table>
          <Pager page={query.data} offset={offset} onChange={setOffset} />
        </>
      )}
      {selected && (
        <Dialog
          title={current ? "Relevance evaluation" : "Historical evaluation"}
          onClose={() => setSelected(null)}
        >
          {!current && (
            <p className="notice">
              Historical record. Check current status before acting on this
              decision.
            </p>
          )}
          <ResultDetails result={selected} />
        </Dialog>
      )}
    </>
  );
}
export default function Matching(
  props: CampaignSelection & { onOpen: (id: string) => void },
) {
  const [history, setHistory] = useState(false);
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Matching</h1>
          <p>
            Current decisions, grounded evidence, and manual evaluation when you
            need it.
          </p>
        </div>
      </div>
      {props.campaign && (
        <section aria-label="Relevant tenders">
          <h2>Relevant tenders</h2>
          <p>Current finalized relevant results for the selected campaign.</p>
          <Results
            key={props.campaign}
            campaign={props.campaign}
            onOpen={props.onOpen}
            current
          />
        </section>
      )}
      <h2>Evaluation workspace</h2>
      <p>Inspect all collected candidates and recover evaluations here.</p>
      <TenderBrowser {...props} matching />
      {props.campaign && (
        <details
          className="disclosure"
          onToggle={(e) => setHistory(e.currentTarget.open)}
        >
          <summary>Historical evaluations</summary>
          <div className="disclosure-body">
            {history && (
              <Results
                key={props.campaign}
                campaign={props.campaign}
                onOpen={props.onOpen}
              />
            )}
          </div>
        </details>
      )}
      <Disclosure title="Understanding your results">
        <p>
          Procurement status describes the tender. Campaign match describes
          relevance to your requirements. AI opinion is separate from verified
          source evidence.
        </p>
        <p>
          Current results match both the tender version and current
          requirements. Pending, failed, and older evaluations are identified
          separately.
        </p>
      </Disclosure>
    </>
  );
}
