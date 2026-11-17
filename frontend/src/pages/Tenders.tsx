import { useState } from "react";
import {
  IconArrowLeft,
  IconExternalLink,
  IconPlus,
  IconSearch,
  IconAdjustments,
} from "@tabler/icons-react";
import {
  date,
  money,
  safeURL,
  type Detail,
  type Match,
  type Page,
  type Source,
  type Tender,
} from "../api";
import { useAction, useDebounced, useResource } from "../data";
import {
  CampaignPicker,
  MatchSummary,
  ResultDetails,
  RunMatching,
  useMatchStates,
} from "../matches";
import {
  Diagnostics,
  Dialog,
  Disclosure,
  ErrorMessage,
  Field,
  Form,
  Pager,
  Panel,
  QueryState,
  State,
  Table,
  TextField,
} from "../ui";
export type CampaignSelection = {
  campaign: string;
  setCampaign: (id: string) => void;
};
export function TenderBrowser({
  campaign,
  setCampaign,
  onOpen,
  matching = false,
}: { onOpen: (id: string) => void; matching?: boolean } & CampaignSelection) {
  const [search, setSearch] = useState(""),
    [source, setSource] = useState(""),
    [status, setStatus] = useState(""),
    [buyer, setBuyer] = useState(""),
    [location, setLocation] = useState(""),
    [currency, setCurrency] = useState(""),
    [offset, setOffset] = useState(0),
    [limit, setLimit] = useState(25),
    [filters, setFilters] = useState(false),
    [adding, setAdding] = useState(false),
    [selected, setSelected] = useState<string[]>([]),
    [run, setRun] = useState(false),
    [inspected, setInspected] = useState<Match | null>(null);
  const q = useDebounced(search),
    b = useDebounced(buyer),
    l = useDebounced(location),
    c = useDebounced(currency);
  const params = new URLSearchParams({
    q,
    source,
    status,
    buyer: b,
    location: l,
    currency: c,
    offset: String(offset),
    limit: String(limit),
  });
  const tenders = useResource<Page<Tender>>(`/tenders?${params}`);
  const sources = useResource<{ items: Source[] }>("/sources");
  const ids = tenders.data?.items.map((t) => t.id) || [];
  const states = useMatchStates(campaign, ids);
  const reset = () => {
    setOffset(0);
  };
  return (
    <>
      <div
        className={
          matching ? "toolbar-container matching-filter" : "toolbar-container"
        }
      >
        <div className="toolbar-row">
          <div className="search-input">
            <IconSearch size={18} />
            <input
              aria-label="Search tenders"
              type="search"
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
                reset();
              }}
              placeholder="Search title, buyer or description"
              maxLength={200}
            />
          </div>
          <select
            aria-label="Source filter"
            value={source}
            onChange={(e) => {
              setSource(e.target.value);
              reset();
            }}
          >
            <option value="">Source: All</option>
            {sources.data?.items.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
          <select
            aria-label="Procurement status filter"
            value={status}
            onChange={(e) => {
              setStatus(e.target.value);
              reset();
            }}
          >
            <option value="">Procurement status: All</option>
            {[
              "open",
              "active",
              "closed",
              "cancelled",
              "awarded",
              "unknown",
            ].map((x) => (
              <option key={x} value={x}>
                {x}
              </option>
            ))}
          </select>
          <button aria-expanded={filters} onClick={() => setFilters(!filters)}>
            <IconAdjustments size={17} />
            More filters
          </button>
          {!matching && (
            <button onClick={() => setAdding(true)}>
              <IconPlus size={17} />
              Add tender
            </button>
          )}
        </div>
        {filters && (
          <div className="form-grid filters-grid">
            {[
              ["Buyer filter", buyer, setBuyer],
              ["Location filter", location, setLocation],
              ["Currency filter", currency, setCurrency],
            ].map(([label, value, update]) => (
              <Field key={String(label)} label={String(label)}>
                {(id) => (
                  <input
                    id={id}
                    value={value as string}
                    onChange={(e) => {
                      (update as (s: string) => void)(e.target.value);
                      reset();
                    }}
                  />
                )}
              </Field>
            ))}
          </div>
        )}
        <CampaignPicker
          value={campaign}
          onChange={(id) => {
            setCampaign(id);
            setSelected([]);
          }}
        />
      </div>
      <ErrorMessage error={sources.error} />
      {campaign && <ErrorMessage error={states.error} />}{" "}
      {selected.length > 0 && (
        <div className="selection-bar">
          <strong>{selected.length} selected</strong>
          <button
            className="primary"
            disabled={!campaign}
            onClick={() => setRun(true)}
          >
            Run matching
          </button>
          <button onClick={() => setSelected([])}>Clear selection</button>
          {!campaign && <span>Choose a campaign to run matching.</span>}
        </div>
      )}
      <QueryState query={tenders} empty={tenders.data?.items.length === 0}>
        No tenders match these filters. Try a broader search, collect a source,
        or add a tender.
      </QueryState>
      {tenders.data && tenders.data.items.length > 0 && (
        <section className="table-card" aria-busy={tenders.isFetching}>
          <Table
            name={matching ? "Matching selection" : "Tenders"}
            headers={[
              <input
                aria-label="Select all tenders on page"
                type="checkbox"
                checked={
                  ids.length > 0 && ids.every((id) => selected.includes(id))
                }
                onChange={(e) =>
                  setSelected(
                    e.target.checked
                      ? [...new Set([...selected, ...ids])].slice(0, 500)
                      : selected.filter((id) => !ids.includes(id)),
                  )
                }
              />,
              "Tender",
              <span className="wide-column">Buyer</span>,
              <span className="wide-column">Deadline</span>,
              "Value",
              "Procurement",
              "Campaign match",
            ]}
          >
            {tenders.data.items.map((t) => (
              <tr key={t.id}>
                <td>
                  <input
                    type="checkbox"
                    aria-label={`Select ${t.title || "Untitled tender"}`}
                    checked={selected.includes(t.id)}
                    disabled={
                      !selected.includes(t.id) && selected.length >= 500
                    }
                    onChange={(e) =>
                      setSelected(
                        e.target.checked
                          ? [...selected, t.id]
                          : selected.filter((id) => id !== t.id),
                      )
                    }
                  />
                </td>
                <td className="tender-cell">
                  <button className="record-link" onClick={() => onOpen(t.id)}>
                    {t.title || "Untitled tender"}
                  </button>
                  <small className="compact-fact line-clamp">
                    {t.buyer || "Buyer not provided"}
                  </small>
                  <small className="compact-fact">
                    Deadline: {date(t.deadline)}
                  </small>
                  <small className="source-caption">
                    {sources.data?.items.find((s) => s.id === t.source)?.name ||
                      (t.source === "manual"
                        ? "Manual entry"
                        : "Collected source")}
                  </small>
                </td>
                <td className="wide-column buyer-cell">
                  <span className="line-clamp">
                    {t.buyer || "Not provided"}
                  </span>
                </td>
                <td className="wide-column">{date(t.deadline)}</td>
                <td className="value-cell">{money(t.value, t.currency)}</td>
                <td>
                  <State value={t.status} />
                  {!t.valid && <small>Not eligible for matching</small>}
                </td>
                <td className="match-cell">
                  {campaign ? (
                    <MatchSummary
                      state={states.data?.find((s) => s.tender_id === t.id)}
                      onInspect={setInspected}
                    />
                  ) : (
                    <span className="muted">Choose a campaign</span>
                  )}
                </td>
              </tr>
            ))}
          </Table>
          <div className="pagination-row">
            <Pager
              page={tenders.data}
              offset={offset}
              onChange={setOffset}
              limit={limit}
            />
            <label>
              Rows per page{" "}
              <select
                aria-label="Rows per page"
                value={limit}
                onChange={(e) => {
                  setLimit(Number(e.target.value));
                  setOffset(0);
                }}
              >
                {[25, 50, 100].map((n) => (
                  <option key={n}>{n}</option>
                ))}
              </select>
            </label>
          </div>
        </section>
      )}
      {adding && (
        <ManualTender
          onClose={() => setAdding(false)}
          onSaved={(id) => {
            setAdding(false);
            onOpen(id);
          }}
        />
      )}
      {run && (
        <RunMatching
          campaign={campaign}
          ids={selected}
          onClose={() => setRun(false)}
        />
      )}{" "}
      {inspected && (
        <Dialog title="Matching result" onClose={() => setInspected(null)}>
          <ResultDetails result={inspected} />
        </Dialog>
      )}
    </>
  );
}
export default function Tenders(
  props: CampaignSelection & { onOpen: (id: string) => void },
) {
  return (
    <>
      <div className="page-head">
        <h1>Tenders</h1>
      </div>
      <TenderBrowser {...props} />
    </>
  );
}
function ManualTender({
  onClose,
  onSaved,
}: {
  onClose: () => void;
  onSaved: (id: string) => void;
}) {
  const save = useAction<Tender>((r) => onSaved(r.id));
  const [requestId] = useState(() => crypto.randomUUID());
  const [items, setItems] = useState<{ name: string; quantity: string }[]>([]);
  return (
    <Dialog title="Add tender" onClose={onClose} wide>
      <p>Create a record with manual provenance. Only a title is required.</p>
      <Form
        onSubmit={(f) =>
          save.mutate({
            path: "/tenders",
            body: {
              request_id: requestId,
              ...Object.fromEntries(
                [...f.entries()].map(([k, v]) => [k, v || null]),
              ),
              deadline: f.get("deadline")
                ? new Date(String(f.get("deadline"))).toISOString()
                : null,
              product_items: items.map((item) => ({
                ...item,
                quantity: item.quantity || null,
              })),
            },
          })
        }
      >
        <TextField label="Tender title" name="title" required />
        <Field label="Description">
          {(id) => (
            <textarea id={id} name="description" rows={3} maxLength={20000} />
          )}
        </Field>
        <div className="form-grid">
          <TextField label="Buyer" name="buyer" />
          <TextField label="Location" name="location" />
          <TextField
            label="Value"
            name="value"
            type="number"
            min={0}
            step="any"
          />
          <TextField label="Currency" name="currency" placeholder="UZS, USD…" />
          <TextField label="Deadline" name="deadline" type="datetime-local" />
          <TextField
            label="Procurement status"
            name="status"
            value="open"
            required
          />
          <TextField label="Original source URL" name="source_url" type="url" />
        </div>
        <Disclosure title="Product items">
          {items.map((item, index) => (
            <div key={index} className="product-form">
              <Field label={`Product ${index + 1} name`}>
                {(id) => (
                  <input
                    id={id}
                    value={item.name}
                    required
                    onChange={(e) =>
                      setItems(
                        items.map((v, i) =>
                          i === index ? { ...v, name: e.target.value } : v,
                        ),
                      )
                    }
                  />
                )}
              </Field>
              <Field label={`Product ${index + 1} quantity`}>
                {(id) => (
                  <input
                    id={id}
                    value={item.quantity}
                    onChange={(e) =>
                      setItems(
                        items.map((v, i) =>
                          i === index ? { ...v, quantity: e.target.value } : v,
                        ),
                      )
                    }
                  />
                )}
              </Field>
              <button
                type="button"
                onClick={() => setItems(items.filter((_, i) => i !== index))}
              >
                Remove item {index + 1}
              </button>
            </div>
          ))}
          <button
            type="button"
            onClick={() => setItems([...items, { name: "", quantity: "" }])}
          >
            Add product item
          </button>
        </Disclosure>
        <ErrorMessage error={save.error} />
        <div className="form-footer">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button className="primary" disabled={save.isPending}>
            Save manual tender
          </button>
        </div>
      </Form>
    </Dialog>
  );
}
const display = (v: unknown): string =>
  v == null
    ? "Not provided"
    : typeof v === "object"
      ? Array.isArray(v)
        ? v.map(display).join(", ")
        : Object.entries(v)
            .map(([k, value]) => `${k.replaceAll("_", " ")}: ${display(value)}`)
            .join("; ")
      : String(v);
export function ProductInformation({
  item,
}: {
  item: Record<string, unknown>;
}) {
  const category =
    typeof item.category === "object" && item.category !== null
      ? (item.category as Record<string, unknown>)
      : null;
  const fields: [string, unknown][] = [
    ["Description", item.description],
    [
      "Category",
      category?.title ||
        category?.name ||
        (typeof item.category === "string" ? item.category : null),
    ],
    ["Classification", category?.code || item.category_code],
    ["Unit", item.unit],
  ];
  return (
    <>
      {fields
        .filter(([, value]) => value != null && value !== "")
        .map(([label, value]) => (
          <p key={label}>
            <span className="muted">{label}: </span>
            {display(value)}
          </p>
        ))}
      <Diagnostics label="More product information" value={item} />
    </>
  );
}
export function TenderDetails({
  id,
  back,
  campaign,
  setCampaign,
}: { id: string; back: () => void } & CampaignSelection) {
  const query = useResource<Detail>(`/tenders/${id}`);
  const states = useMatchStates(campaign, [id]);
  const [run, setRun] = useState(false);
  const t = query.data?.tender;
  return (
    <>
      <button className="back-link" onClick={back}>
        <IconArrowLeft size={17} />
        Back to Tenders
      </button>
      <QueryState query={query} />
      {t && query.data && (
        <>
          <header className="tender-header-card">
            <div className="actions">
              <span className="muted">Procurement</span>
              <State value={t.status} />
              <span className="provenance-label">
                {query.data.provenance === "manual"
                  ? "Manual entry"
                  : "Collected record"}
              </span>
            </div>
            <h1 className="tender-headline">{t.title || "Untitled tender"}</h1>
            <p className="buyer-full-line">
              <strong>Buyer</strong>
              {t.buyer || "Not provided"}
            </p>
            <div className="header-facts-grid">
              <div className="fact-item">
                <span>Tender value</span>
                <strong className="fact-value">
                  {money(t.value, t.currency)}
                </strong>
              </div>
              <div className="fact-item">
                <span>Deadline</span>
                <strong>{date(t.deadline)}</strong>
              </div>
              <div className="fact-item">
                <span>Location</span>
                <strong>{t.location || "Not provided"}</strong>
              </div>
            </div>
          </header>
          <div className="detail-grid-container">
            <div className="detail-match">
              <Panel title="Campaign match">
                <CampaignPicker value={campaign} onChange={setCampaign} />
                {campaign ? (
                  <>
                    <QueryState query={states} />
                    <MatchSummary state={states.data?.[0]} />
                    {states.data?.[0]?.state === "current" &&
                      states.data[0].result && (
                        <Disclosure title="Decision, AI explanation and evidence">
                          <ResultDetails result={states.data[0].result} />
                        </Disclosure>
                      )}
                  </>
                ) : (
                  <p>
                    Choose a campaign to see relevance to your requirements.
                  </p>
                )}
                <button
                  disabled={!campaign || !t.valid}
                  onClick={() => setRun(true)}
                >
                  Run matching
                </button>
                {!t.valid && (
                  <Disclosure title="Why matching is unavailable">
                    {t.validity_reasons.map((r, i) => (
                      <p key={i}>{r.detail}</p>
                    ))}
                  </Disclosure>
                )}
              </Panel>
            </div>
            <div className="detail-main">
              <Panel title="Products and scope">
                {query.data.product_items?.length ? (
                  <Table
                    name="Product items"
                    headers={[
                      "Product information",
                      "Quantity",
                      "Source price",
                    ]}
                  >
                    {query.data.product_items.map((item, i) => (
                      <tr key={i}>
                        <td>
                          <strong>
                            {display(
                              item.name || item.title || `Product ${i + 1}`,
                            )}
                          </strong>
                          <ProductInformation item={item} />
                        </td>
                        <td>{display(item.quantity)}</td>
                        <td className="value-cell">
                          {item.price != null &&
                          Number.isFinite(Number(item.price))
                            ? money(
                                String(item.price),
                                typeof item.currency === "string"
                                  ? item.currency
                                  : null,
                              )
                            : display(item.price)}
                        </td>
                      </tr>
                    ))}
                  </Table>
                ) : (
                  <p>Product information was not provided by the source.</p>
                )}
                <Disclosure title="Source description">
                  <p className="preserve-lines">
                    {t.description || "No description provided."}
                  </p>
                </Disclosure>
                {safeURL(t.source_url) && (
                  <a
                    className="external-link"
                    href={safeURL(t.source_url)}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Open original source
                    <IconExternalLink size={16} />
                  </a>
                )}
              </Panel>
              <Disclosure title="History and provenance">
                <p>
                  {query.data.provenance === "manual"
                    ? "Entered manually"
                    : "Collected from a website"}
                  . Last observed {date(t.observed_at)}.
                </p>
                {query.data.history.map((v, i) => (
                  <div className="history-entry" key={v.id}>
                    <h3>
                      {date(v.observed_at)} ·{" "}
                      {v.previous_version_id ? "Updated" : "First recorded"}
                    </h3>
                    <dl className="fact-list">
                      {Object.entries(v.changed_fields).map(([k, value]) => (
                        <div key={k}>
                          <dt>{k.replaceAll("_", " ")}</dt>
                          <dd>{display(value)}</dd>
                        </div>
                      ))}
                    </dl>
                    {i === 0 && Object.keys(v.changed_fields).length === 0 && (
                      <p>No field changes recorded.</p>
                    )}
                  </div>
                ))}
                <Diagnostics
                  value={query.data.metadata}
                  label="Source metadata"
                />
                <Diagnostics
                  value={query.data.history}
                  label="History diagnostics"
                />
              </Disclosure>
            </div>
          </div>
        </>
      )}
      {run && (
        <RunMatching
          campaign={campaign}
          ids={[id]}
          onClose={() => setRun(false)}
        />
      )}
    </>
  );
}
