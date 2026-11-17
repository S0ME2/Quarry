import { useState } from "react";
import {
  IconArrowLeft,
  IconExternalLink,
  IconSearch,
} from "@tabler/icons-react";
import { date, money, safeURL } from "../api";
import {
  type AccountSource,
  type AccountTender,
  type AccountTenders,
  explanation,
  useAccountResource,
} from "../account";
import { useDebounced } from "../data";
import {
  Disclosure,
  Field,
  Pager,
  Panel,
  QueryState,
  State,
  Table,
} from "../ui";

export function AccountTenderDetails({
  id,
  campaignId,
  back,
}: {
  id: string;
  campaignId?: string;
  back: () => void;
}) {
  const query = useAccountResource<AccountTender>(
    `/account/tenders/${encodeURIComponent(id)}${campaignId ? `?campaign_id=${encodeURIComponent(campaignId)}` : ""}`,
  );
  const sources = useAccountResource<{ items: AccountSource[] }>(
    "/account/sources",
  );
  const tender = query.data;
  const sourceName =
    sources.data?.items.find((source) => source.id === tender?.source)?.name ||
    tender?.source ||
    "Not provided";
  return (
    <>
      <div className="page-head">
        <button onClick={back}>
          <IconArrowLeft size={17} />
          Back to tenders
        </button>
      </div>
      <QueryState query={query} />
      {tender && (
        <>
          <div className="page-head">
            <div>
              <h1>{tender.title || "Untitled tender"}</h1>
              <p>
                {tender.buyer || "Buyer not provided"} · {sourceName}
              </p>
            </div>
            {safeURL(tender.source_url) && (
              <a
                className="external-action"
                href={safeURL(tender.source_url)}
                target="_blank"
                rel="noreferrer"
              >
                Open original tender
                <IconExternalLink size={16} />
              </a>
            )}
          </div>
          <Panel title="Opportunity details">
            <dl className="account-facts">
              {[
                ["Value", money(tender.value, tender.currency)],
                ["Deadline", date(tender.deadline)],
                ["Published", date(tender.published_at)],
                ["Status", tender.status || "Not provided"],
                ["Source", sourceName],
              ].map(([label, value]) => (
                <div key={label}>
                  <dt>{label}</dt>
                  <dd>{value}</dd>
                </div>
              ))}
            </dl>
          </Panel>
          <Panel title="Relevance analysis">
            <p>Campaign: {tender.campaign_name}</p>
            <State value={tender.relevance} />
            {tender.analysis_stale && (
              <p role="status">
                Saved analysis uses earlier requirements or tender evidence.
                Review before acting; contact your administrator for
                reevaluation.
              </p>
            )}
            <p className="preserve-lines">{explanation(tender.reason)}</p>
          </Panel>
          <Panel title="Description">
            <p className="preserve-lines">
              {tender.description || "No description provided by this source."}
            </p>
          </Panel>
          <Panel title="Documents">
            {tender.documents?.length ? (
              <ul className="document-list">
                {tender.documents.map((document, index) => {
                  const url = safeURL(
                    String(document.url || document.source_url || ""),
                  );
                  return (
                    <li key={String(document.id || index)}>
                      {url ? (
                        <a href={url} target="_blank" rel="noreferrer">
                          {String(
                            document.name ||
                              document.filename ||
                              `Document ${index + 1}`,
                          )}
                        </a>
                      ) : (
                        <strong>
                          {String(
                            document.name ||
                              document.filename ||
                              `Document ${index + 1}`,
                          )}
                        </strong>
                      )}
                      <small>
                        {String(
                          document.status || document.processing_status || "",
                        )}
                      </small>
                      {document.error ? (
                        <small className="error-text">
                          {String(document.error)}
                        </small>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
            ) : (
              <p>No documents recorded for this tender.</p>
            )}
          </Panel>
        </>
      )}
    </>
  );
}

export default function AccountTenders({
  onOpen,
}: {
  onOpen: (id: string, campaignId: string) => void;
}) {
  const [search, setSearch] = useState("");
  const [source, setSource] = useState("");
  const [status, setStatus] = useState("");
  const [relevance, setRelevance] = useState("");
  const [dates, setDates] = useState<Record<string, string>>({});
  const [offset, setOffset] = useState(0);
  const debounced = useDebounced(search);
  const params = new URLSearchParams({
    q: debounced,
    source,
    status,
    relevance,
    offset: String(offset),
    limit: "25",
  });
  for (const [name, value] of Object.entries(dates)) {
    if (!value) continue;
    const boundary = new Date(
      `${value}T${name.endsWith("_to") ? "23:59:59.999" : "00:00:00"}`,
    );
    if (!Number.isNaN(boundary.getTime()))
      params.set(name, boundary.toISOString());
  }
  const query = useAccountResource<AccountTenders>(
    `/account/tenders?${params}`,
    10000,
  );
  const sources = useAccountResource<{ items: AccountSource[] }>(
    "/account/sources",
  );
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Tenders</h1>
          <p>Opportunities found for your monitoring campaigns.</p>
        </div>
        <a href="#/monitoring">Manage monitoring</a>
      </div>
      <div className="toolbar-container">
        <div className="toolbar-row">
          <div className="search-input">
            <IconSearch size={18} />
            <input
              type="search"
              aria-label="Search tender titles"
              placeholder="Search tender titles"
              value={search}
              maxLength={200}
              onChange={(event) => {
                setSearch(event.target.value);
                setOffset(0);
              }}
            />
          </div>
          <select
            aria-label="Source filter"
            value={source}
            onChange={(event) => {
              setSource(event.target.value);
              setOffset(0);
            }}
          >
            <option value="">All sources</option>
            {sources.data?.items.map((item) => (
              <option value={item.id} key={item.id}>
                {item.name}
              </option>
            ))}
          </select>
          <select
            aria-label="Status filter"
            value={status}
            onChange={(event) => {
              setStatus(event.target.value);
              setOffset(0);
            }}
          >
            <option value="">All statuses</option>
            {[
              "open",
              "active",
              "closed",
              "cancelled",
              "awarded",
              "unknown",
            ].map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
          <select
            aria-label="Relevance filter"
            value={relevance}
            onChange={(event) => {
              setRelevance(event.target.value);
              setOffset(0);
            }}
          >
            <option value="">All AI results</option>
            <option value="RELEVANT">Relevant</option>
            <option value="NOT_RELEVANT">Not relevant</option>
          </select>
        </div>
        <Disclosure title="Publication and deadline filters">
          <div className="form-grid">
            {[
              ["published_from", "Published from"],
              ["published_to", "Published to"],
              ["deadline_from", "Deadline from"],
              ["deadline_to", "Deadline to"],
            ].map(([name, label]) => (
              <Field key={name} label={label}>
                {(id) => (
                  <input
                    id={id}
                    type="date"
                    value={dates[name] || ""}
                    onChange={(event) => {
                      const next = { ...dates };
                      if (event.target.value) next[name] = event.target.value;
                      else delete next[name];
                      setDates(next);
                      setOffset(0);
                    }}
                  />
                )}
              </Field>
            ))}
          </div>
        </Disclosure>
      </div>
      <QueryState query={query} empty={query.data?.items.length === 0}>
        No opportunities found for these filters yet.{" "}
        <a href="#/monitoring">Review your monitoring campaigns</a> or check
        again after the next scheduled run.
      </QueryState>
      {!!query.data?.items.length && (
        <section className="table-card account-tender-table">
          <Table
            name="Your tenders"
            headers={[
              "Tender",
              "Campaign",
              "Source",
              "Buyer",
              "Price",
              "Currency",
              "Deadline",
              "Published",
              "Relevance",
              "Status",
            ]}
          >
            {query.data.items.map((tender) => (
              <tr key={`${tender.id}-${tender.campaign_id}`}>
                <td>
                  <button
                    className="record-link"
                    onClick={() => onOpen(tender.id, tender.campaign_id)}
                  >
                    {tender.title || "Untitled tender"}
                  </button>
                </td>
                <td>{tender.campaign_name}</td>
                <td>
                  {sources.data?.items.find(
                    (source) => source.id === tender.source,
                  )?.name || tender.source}
                </td>
                <td>{tender.buyer || "Not provided"}</td>
                <td>{money(tender.value)}</td>
                <td>{tender.currency || "—"}</td>
                <td>{date(tender.deadline)}</td>
                <td>{date(tender.published_at)}</td>
                <td>
                  <State value={tender.relevance} />
                  {tender.analysis_stale && (
                    <small>Saved analysis · requirements changed</small>
                  )}
                </td>
                <td>
                  <State value={tender.status} />
                </td>
              </tr>
            ))}
          </Table>
          <Pager page={query.data} offset={offset} onChange={setOffset} />
        </section>
      )}
    </>
  );
}

export function AccountNotifications() {
  const query = useAccountResource<{
    items: {
      id: string;
      status: string;
      message: string;
      last_error: string | null;
      delivered_at: string | null;
      campaign_id: string;
    }[];
  }>("/account/notifications");
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Notifications</h1>
          <p>Delivery history for your tender opportunities.</p>
        </div>
        <a href="#/monitoring">Notification settings</a>
      </div>
      <QueryState query={query} empty={query.data?.items.length === 0}>
        Notifications appear here after relevant tenders are saved. Choose email
        or Telegram in your monitoring campaign.
      </QueryState>
      {!!query.data?.items.length && (
        <section className="table-card">
          <Table
            name="Notifications"
            headers={["Message", "Delivery status", "Delivered"]}
          >
            {query.data.items.map((item) => (
              <tr key={item.id}>
                <td>
                  <span className="preserve-lines">{item.message}</span>
                  {item.last_error && (
                    <small className="error-text">{item.last_error}</small>
                  )}
                </td>
                <td>
                  <State value={item.status} />
                </td>
                <td>{date(item.delivered_at)}</td>
              </tr>
            ))}
          </Table>
        </section>
      )}
    </>
  );
}
