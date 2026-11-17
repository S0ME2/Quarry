import { useState } from "react";
import { IconPlus, IconSettings } from "@tabler/icons-react";
import type { Budget, Campaign, Criteria, Page, Source } from "../api";
import { date, usd } from "../api";
import { useAction, useResource } from "../data";
import {
  Check,
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
const arrayFields = [
  ["include_keywords", "Included keywords"],
  ["exclude_keywords", "Excluded keywords"],
  ["currencies", "Currencies"],
  ["buyers", "Buyers"],
  ["exclude_buyers", "Excluded buyers"],
  ["locations", "Locations"],
  ["status_rules", "Procurement statuses"],
] as const;
const split = (v: FormDataEntryValue | null) =>
  String(v || "")
    .split(/[,\n]/)
    .map((x) => x.trim())
    .filter(Boolean);
const numeric = (f: FormData, k: string) =>
  f.get(k) ? Number(f.get(k)) : null;
export function CampaignForm({
  campaign,
  onClose,
}: {
  campaign?: Campaign;
  onClose: () => void;
}) {
  const c = campaign?.criteria;
  const notificationChannel = c?.notifications?.channel || "telegram";
  const save = useAction<Campaign>(onClose);
  const sources = useResource<{ items: Source[] }>("/sources");
  const [sourceIds, setSourceIds] = useState(c?.source_ids || []),
    [notifications, setNotifications] = useState(
      c?.notifications?.enabled ?? Boolean(campaign),
    );
  const [automatic, setAutomatic] = useState(c?.automatic_matching || false),
    [mode, setMode] = useState(c?.automatic_mode || "rules");
  const history = useResource<{
    versions: {
      id: string;
      number: number;
      created_at: string;
      criteria: Criteria;
    }[];
  }>(campaign ? `/campaigns/${campaign.id}` : null);
  function submit(f: FormData) {
    const criteria = {
      ...c,
      name: String(f.get("name")).trim(),
      description: String(f.get("description") || ""),
      custom_notes: String(f.get("custom_notes") || ""),
      source_ids: sourceIds,
      notifications: {
        ...c?.notifications,
        enabled: notifications,
        channel: notificationChannel,
        chat_id:
          notificationChannel === "telegram"
            ? String(f.get("chat_id") || "").trim() || null
            : (c?.notifications?.chat_id ?? null),
        ...(notificationChannel === "email"
          ? { email: String(f.get("notification_email") || "").trim() || null }
          : {}),
      },
      matching_mode: f.get("matching_mode"),
      automatic_matching: automatic,
      automatic_mode: mode,
      automatic_ai_authorized: f.has("authorize"),
      ai_budget_usd: Number(f.get("budget") || 0),
      ai_request_limit: Number(f.get("requests") || 0),
      min_value: numeric(f, "min_value"),
      max_value: numeric(f, "max_value"),
      deadline_rules: {
        min_days_remaining: numeric(f, "min_days"),
        max_days_remaining: numeric(f, "max_days"),
      },
      ...Object.fromEntries(
        arrayFields.map(([key]) => [key, split(f.get(key))]),
      ),
    };
    save.mutate({
      path: campaign ? `/campaigns/${campaign.id}/criteria` : "/campaigns",
      body: {
        criteria,
        active: campaign?.active ?? true,
        expected_version_id: campaign?.current_criteria_version_id,
      },
    });
  }
  return (
    <Dialog
      title={campaign ? "Edit campaign" : "Create campaign"}
      onClose={onClose}
      wide
    >
      <Form onSubmit={submit}>
        <TextField label="Campaign name" name="name" value={c?.name} required />
        <fieldset>
          <legend>Tender sources</legend>
          <p>
            Select websites for this campaign. No selection includes all
            sources, including websites added later.
          </p>
          <QueryState query={sources} />
          {Array.from(
            new Set([
              ...(sources.data?.items.map((source) => source.id) || []),
              ...sourceIds,
            ]),
          ).map((id) => {
            const source = sources.data?.items.find((item) => item.id === id);
            return (
              <Check
                key={id}
                label={source?.name || `Unavailable source (${id})`}
                checked={sourceIds.includes(id)}
                onChange={(checked) =>
                  setSourceIds(
                    checked
                      ? [...sourceIds, id]
                      : sourceIds.filter((value) => value !== id),
                  )
                }
              />
            );
          })}
        </fieldset>
        <Field label="What are you looking for?">
          {(id) => (
            <textarea
              id={id}
              name="description"
              rows={3}
              defaultValue={c?.description}
              placeholder="Describe your business requirements in any language"
            />
          )}
        </Field>
        <div className="form-grid">
          {arrayFields.slice(0, 2).map(([key, label]) => (
            <TextField
              key={key}
              label={label}
              name={key}
              value={c?.[key]?.join(", ")}
              placeholder="Separate terms with commas"
            />
          ))}
          <Field label="Matching preference">
            {(id) => (
              <select
                id={id}
                name="matching_mode"
                defaultValue={c?.matching_mode || "balanced"}
              >
                <option value="broad">
                  Broad · explore more opportunities
                </option>
                <option value="balanced">Balanced · recommended</option>
                <option value="strict">Strict · close fits only</option>
              </select>
            )}
          </Field>
        </div>
        <Disclosure title="Value, buyer, location and deadline filters">
          <div className="form-grid">
            <TextField
              label="Minimum value"
              name="min_value"
              type="number"
              min={0}
              step="any"
              value={c?.min_value}
            />
            <TextField
              label="Maximum value"
              name="max_value"
              type="number"
              min={0}
              step="any"
              value={c?.max_value}
            />
            {arrayFields.slice(2).map(([key, label]) => (
              <TextField
                key={key}
                label={label}
                name={key}
                value={c?.[key]?.join(", ")}
                placeholder="Comma-separated values"
              />
            ))}
            <TextField
              label="Minimum days remaining"
              name="min_days"
              type="number"
              min={0}
              value={c?.deadline_rules?.min_days_remaining}
            />
            <TextField
              label="Maximum days remaining"
              name="max_days"
              type="number"
              min={0}
              value={c?.deadline_rules?.max_days_remaining}
            />
          </div>
          <Field label="Additional matching instructions">
            {(id) => (
              <textarea
                id={id}
                name="custom_notes"
                defaultValue={c?.custom_notes}
                rows={3}
              />
            )}
          </Field>
        </Disclosure>
        <div className="setting-section">
          <h3>Automatic matching</h3>
          <p>
            Evaluate published tender changes against this campaign. AI requires
            authorization and spending limits.
          </p>
          <div className="automatic-setting">
            <Check
              label="Automatically match new and changed tenders"
              checked={automatic}
              onChange={setAutomatic}
            />
          </div>
          <Field label="Automatic matcher">
            {(id) => (
              <select
                id={id}
                value={mode}
                onChange={(e) =>
                  setMode(e.target.value as "rules" | "semantic")
                }
              >
                <option value="rules">Rules · no AI charges</option>
                <option value="semantic">AI · uses configured provider</option>
              </select>
            )}
          </Field>
          <Check
            name="authorize"
            label="I authorize automatic AI requests for this campaign"
            checked={c?.automatic_ai_authorized}
          />
          {automatic && mode === "semantic" && (
            <p className="notice">
              Automatic AI runs only while authorization, campaign limits and
              global spending safeguards permit it.
            </p>
          )}
          <div className="form-grid">
            <TextField
              label="Campaign AI limit (USD)"
              name="budget"
              type="number"
              min={0}
              step="any"
              value={c?.ai_budget_usd ?? 0}
              required
            />
            <TextField
              label="Unpriced AI request ceiling"
              name="requests"
              type="number"
              min={0}
              max={100000}
              value={c?.ai_request_limit ?? 0}
              required
            />
          </div>
          <small>
            Lifetime limits. Zero blocks AI unless an authorized alternative
            budget applies. Global limits also apply.
          </small>
        </div>
        <div className="setting-section">
          <h3>Notifications</h3>
          <Check
            label={`Send ${notificationChannel === "email" ? "email" : "Telegram"} notifications for relevant tenders`}
            checked={notifications}
            onChange={setNotifications}
          />
          {notificationChannel === "email" ? (
            <TextField
              label="Notification email"
              name="notification_email"
              type="email"
              value={c?.notifications?.email}
              required={notifications}
            />
          ) : (
            <>
              <TextField
                label="Telegram chat ID or channel username"
                name="chat_id"
                value={c?.notifications?.chat_id ?? ""}
                placeholder="-1001234567890 or @channel"
              />
              <p className="muted">
                Leave the destination blank to use the server's default chat. A
                bot token must be configured on the server. Notifications are
                sent only after a relevant result is saved; saving this form
                sends no message.
              </p>
            </>
          )}
          {notificationChannel === "email" && (
            <p className="muted">
              Email notifications are sent after a relevant result is saved.
              Saving this form sends no message.
            </p>
          )}
        </div>
        <ErrorMessage error={save.error} />
        {campaign && (
          <Disclosure title="Requirements history">
            <p>
              Saving creates a new version. Existing evaluations stay in
              history.
            </p>
            {history.data?.versions.map((v) => (
              <details key={v.id}>
                <summary>
                  Version {v.number} · {date(v.created_at)}
                </summary>
                <p>{v.criteria.name}</p>
                <p>{v.criteria.description || "No description"}</p>
                <dl className="fact-list">
                  {Object.entries(v.criteria)
                    .filter(
                      ([k]) =>
                        !["schema_version", "name", "description"].includes(k),
                    )
                    .map(([k, value]) => (
                      <div key={k}>
                        <dt>{k.replaceAll("_", " ")}</dt>
                        <dd>
                          {typeof value === "object"
                            ? JSON.stringify(value)
                            : String(value)}
                        </dd>
                      </div>
                    ))}
                </dl>
              </details>
            ))}
            <ErrorMessage error={history.error} />
          </Disclosure>
        )}
        <div className="form-footer">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button className="primary" disabled={save.isPending}>
            {save.isPending ? "Saving…" : "Save campaign"}
          </button>
        </div>
      </Form>
    </Dialog>
  );
}
export function BudgetDialog({ onClose }: { onClose: () => void }) {
  const query = useResource<Budget>("/ai-budget");
  const save = useAction();
  return (
    <Dialog title="AI spending controls" onClose={onClose} wide>
      <QueryState query={query} />
      {query.data && (
        <>
          <Panel title="Usage and reservations">
            <div className="facts">
              <div>
                <span>Accounted / reserved</span>
                <strong>{usd(query.data.accounted_cost_usd)}</strong>
              </div>
              <div>
                <span>Estimated costs</span>
                <strong>
                  {query.data.estimated_cost_usd === null
                    ? "Unavailable"
                    : usd(query.data.estimated_cost_usd)}
                </strong>
              </div>
              <div>
                <span>Provider-reported charges</span>
                <strong>{usd(query.data.provider_reported_cost_usd)}</strong>
              </div>
            </div>
            <p>
              {query.data.requests} requests · {query.data.uncertain_requests}{" "}
              uncertain or reserved. Unknown charges retain reservations.
            </p>
          </Panel>
          <Form
            onSubmit={(f) =>
              save.mutate({
                path: "/ai-budget",
                method: "PUT",
                body: {
                  ...query.data!.settings,
                  provider: query.data!.active_model.provider,
                  model: query.data!.active_model.model,
                  ...Object.fromEntries(
                    [
                      "global_limit_usd",
                      "input_usd_per_million",
                      "output_usd_per_million",
                      "unpriced_request_limit",
                      "max_requests_per_run",
                      "max_tokens_per_run",
                      "max_input_tokens",
                    ].map((k) => [
                      k,
                      f.get(k) === "" ? null : Number(f.get(k)),
                    ]),
                  ),
                },
              })
            }
          >
            <p>
              Active model: <strong>{query.data.active_model.model}</strong> ·{" "}
              {query.data.active_model.provider}
            </p>
            {!query.data.pricing_matches_model && (
              <p className="notice">
                Saved pricing does not match the active model. Set verified
                rates or use an explicit request ceiling.
              </p>
            )}
            <div className="form-grid">
              <TextField
                label="Global AI limit (USD)"
                name="global_limit_usd"
                type="number"
                min={0}
                step="any"
                required
                value={query.data.settings.global_limit_usd}
              />
              <TextField
                label="Unpriced lifetime request ceiling"
                name="unpriced_request_limit"
                type="number"
                min={0}
                max={100000}
                required
                value={query.data.settings.unpriced_request_limit}
              />
              <TextField
                label="Input price (USD / million tokens)"
                name="input_usd_per_million"
                type="number"
                min={0}
                step="any"
                value={query.data.settings.input_usd_per_million}
              />
              <TextField
                label="Output price (USD / million tokens)"
                name="output_usd_per_million"
                type="number"
                min={0}
                step="any"
                value={query.data.settings.output_usd_per_million}
              />
            </div>
            <Disclosure title="Per-run safeguards">
              <div className="form-grid">
                <TextField
                  label="Maximum AI requests per run"
                  name="max_requests_per_run"
                  type="number"
                  min={1}
                  max={100}
                  required
                  value={query.data.settings.max_requests_per_run}
                />
                <TextField
                  label="Maximum tokens per run"
                  name="max_tokens_per_run"
                  type="number"
                  min={1}
                  max={1000000}
                  required
                  value={query.data.settings.max_tokens_per_run}
                />
                <TextField
                  label="Maximum input tokens per request"
                  name="max_input_tokens"
                  type="number"
                  min={1}
                  max={100000}
                  required
                  value={query.data.settings.max_input_tokens}
                />
              </div>
            </Disclosure>
            <small>
              Rates are supplied by you and bound to the active model when
              saved. They produce estimates; provider charges may differ.
            </small>
            <ErrorMessage error={save.error} />
            {save.isSuccess && <p role="status">AI spending controls saved.</p>}
            <div className="form-footer">
              <button type="button" onClick={onClose}>
                Close
              </button>
              <button className="primary" disabled={save.isPending}>
                Save spending controls
              </button>
            </div>
          </Form>
        </>
      )}
    </Dialog>
  );
}
export default function Campaigns() {
  const [offset, setOffset] = useState(0),
    [edit, setEdit] = useState<Campaign | "new" | null>(null),
    [budget, setBudget] = useState(false);
  const query = useResource<Page<Campaign>>(
    `/campaigns?offset=${offset}&limit=25`,
  );
  const toggle = useAction();
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Campaigns</h1>
          <p>
            Define a good opportunity. Let your requirements guide every match.
          </p>
        </div>
        <div className="actions">
          <button onClick={() => setBudget(true)}>
            <IconSettings size={17} />
            AI spending
          </button>
          <button className="primary" onClick={() => setEdit("new")}>
            <IconPlus size={17} />
            Create campaign
          </button>
        </div>
      </div>
      <ErrorMessage error={toggle.error} />
      <QueryState query={query} empty={query.data?.items.length === 0}>
        Create a campaign to define your requirements and start matching
        tenders.
      </QueryState>
      {!!query.data?.items.length && (
        <section className="table-card">
          <Table
            name="Campaigns"
            headers={[
              "Campaign / requirements",
              "State",
              "Automatic matching",
              "Work in progress",
              "Actions",
            ]}
          >
            {query.data.items.map((c) => (
              <tr key={c.id}>
                <td>
                  <button className="record-link" onClick={() => setEdit(c)}>
                    {c.criteria.name}
                  </button>
                  <small>
                    {c.criteria.description ||
                      c.criteria.include_keywords?.join(", ") ||
                      "No keyword restrictions"}
                  </small>
                  <small>Requirements version {c.criteria_number}</small>
                </td>
                <td>
                  <State value={c.active ? "active" : "paused"} />
                </td>
                <td>
                  {c.criteria.automatic_matching
                    ? c.criteria.automatic_mode === "semantic"
                      ? "AI"
                      : "Rules"
                    : "Off"}
                  {c.criteria.automatic_matching &&
                    c.criteria.automatic_mode === "semantic" && (
                      <small>
                        {c.criteria.automatic_ai_authorized
                          ? "AI authorized"
                          : "AI not authorized"}
                      </small>
                    )}
                </td>
                <td>
                  {c.processing} pending
                  {c.failed > 0 && (
                    <small className="error-text">{c.failed} failed</small>
                  )}
                </td>
                <td>
                  <div className="actions">
                    <button onClick={() => setEdit(c)}>Edit campaign</button>
                    <button
                      disabled={toggle.isPending}
                      onClick={() =>
                        toggle.mutate({
                          path: `/campaigns/${c.id}/state`,
                          method: "PATCH",
                          body: { active: !c.active },
                        })
                      }
                    >
                      {c.active ? "Pause" : "Activate"}
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </Table>
          <Pager page={query.data} offset={offset} onChange={setOffset} />
        </section>
      )}
      {edit && (
        <CampaignForm
          campaign={edit === "new" ? undefined : edit}
          onClose={() => setEdit(null)}
        />
      )}{" "}
      {budget && <BudgetDialog onClose={() => setBudget(false)} />}
    </>
  );
}
