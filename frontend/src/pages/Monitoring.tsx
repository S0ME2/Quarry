import { useState } from "react";
import { IconPlus } from "@tabler/icons-react";
import { date, readable } from "../api";
import {
  type AccountCampaign,
  type AccountSource,
  type User,
  useAccountAction,
  useAccountResource,
} from "../account";
import {
  Check,
  Dialog,
  ErrorMessage,
  Field,
  Form,
  QueryState,
  State,
  Table,
  TextField,
} from "../ui";

const terms = (value: FormDataEntryValue | null) =>
  String(value || "")
    .split(/[,\n]/)
    .map((term) => term.trim())
    .filter(Boolean);

export function MonitoringForm({
  campaign,
  user,
  onClose,
}: {
  campaign?: AccountCampaign;
  user: User;
  onClose: () => void;
}) {
  const criteria = campaign?.criteria;
  const sources = useAccountResource<{ items: AccountSource[] }>(
    "/account/sources",
  );
  const [selected, setSelected] = useState(criteria?.source_ids || []);
  const [channel, setChannel] = useState(
    criteria?.notifications.channel || "email",
  );
  const [enabled, setEnabled] = useState(
    criteria?.notifications.enabled ?? true,
  );
  const [inheritBudget, setInheritBudget] = useState(false);
  const save = useAccountAction(onClose);
  return (
    <Dialog
      title={campaign ? "Edit monitoring" : "Create monitoring campaign"}
      onClose={onClose}
      wide
    >
      <p>
        Choose websites and requirements. Quarry checks for new opportunities on
        your schedule.
      </p>
      <Form
        onSubmit={(form) =>
          save.mutate({
            path: campaign
              ? `/account/campaigns/${campaign.id}`
              : "/account/campaigns",
            method: campaign ? "PUT" : "POST",
            body: {
              active: campaign?.active ?? true,
              expected_version_id: campaign?.current_criteria_version_id,
              interval_seconds: Number(form.get("interval_seconds")),
              criteria: {
                ...criteria,
                name: String(form.get("name")).trim(),
                description: String(form.get("description") || "").trim(),
                source_ids: selected,
                include_keywords: terms(form.get("keywords")),
                exclude_keywords: terms(form.get("stop_words")),
                custom_notes: String(form.get("notes") || "").trim(),
                automatic_matching: true,
                automatic_mode: criteria?.automatic_mode || "rules",
                automatic_ai_authorized:
                  criteria?.automatic_ai_authorized || false,
                ai_request_limit: inheritBudget
                  ? null
                  : (criteria?.ai_request_limit ?? null),
                ai_budget_usd: inheritBudget
                  ? null
                  : (criteria?.ai_budget_usd ?? null),
                notifications: {
                  enabled,
                  channel,
                  chat_id:
                    channel === "telegram"
                      ? String(form.get("chat_id") || "").trim()
                      : null,
                  email:
                    channel === "email"
                      ? String(form.get("notification_email") || "").trim()
                      : null,
                },
              },
            },
          })
        }
      >
        {criteria &&
          (criteria.ai_budget_usd != null ||
            criteria.ai_request_limit != null) && (
            <label className="check">
              <input
                type="checkbox"
                checked={inheritBudget}
                onChange={(event) => setInheritBudget(event.target.checked)}
              />
              <span>
                Replace this campaign's saved AI limits with current
                administrator limits
              </span>
            </label>
          )}
        <TextField
          label="Campaign name"
          name="name"
          value={criteria?.name}
          required
          placeholder="For example, construction materials"
        />
        <fieldset>
          <legend>Websites to monitor</legend>
          <p>
            Select at least one approved website.{" "}
            <a href="#/sources" onClick={onClose}>
              Manage sources
            </a>
          </p>
          <QueryState query={sources} />
          <div className="source-choices">
            {sources.data?.items.map((source) => (
              <label className="check" key={source.id}>
                <input
                  type="checkbox"
                  checked={selected.includes(source.id)}
                  disabled={
                    source.status !== "APPROVED" &&
                    !selected.includes(source.id)
                  }
                  onChange={(event) =>
                    setSelected(
                      event.target.checked
                        ? [...selected, source.id]
                        : selected.filter((id) => id !== source.id),
                    )
                  }
                />
                <span>
                  {source.name} <State value={source.status} />
                </span>
              </label>
            ))}
          </div>
          {selected.some(
            (id) => !sources.data?.items.some((source) => source.id === id),
          ) && (
            <p className="notice">
              Some saved sources are unavailable. Review your selection before
              saving.
            </p>
          )}
        </fieldset>
        <div className="form-grid">
          <TextField
            label="Keywords"
            name="keywords"
            value={criteria?.include_keywords.join(", ")}
            placeholder="Separate words or phrases with commas"
          />
          <TextField
            label="Stop words"
            name="stop_words"
            value={criteria?.exclude_keywords.join(", ")}
            placeholder="Terms to exclude, separated by commas"
          />
        </div>
        <Field label="Business requirements">
          {(id) => (
            <textarea
              id={id}
              name="description"
              rows={3}
              defaultValue={criteria?.description}
              placeholder="Describe the opportunities your business needs."
            />
          )}
        </Field>
        <Field label="Additional criteria (optional)">
          {(id) => (
            <textarea
              id={id}
              name="notes"
              rows={2}
              defaultValue={criteria?.custom_notes}
              placeholder="For example, delivery must be available in Tashkent."
            />
          )}
        </Field>
        <Field label="Check for new tenders">
          {(id) => (
            <select
              id={id}
              name="interval_seconds"
              defaultValue={campaign?.interval_seconds || 3600}
            >
              {[3600, 10800, 21600, 86400].map((seconds) => (
                <option key={seconds} value={seconds}>
                  {seconds === 86400
                    ? "Every day"
                    : `Every ${seconds / 3600} hour${seconds > 3600 ? "s" : ""}`}
                </option>
              ))}
              {campaign?.interval_seconds &&
                ![3600, 10800, 21600, 86400].includes(
                  campaign.interval_seconds,
                ) && (
                  <option value={campaign.interval_seconds}>
                    Every {campaign.interval_seconds / 60} minutes
                  </option>
                )}
            </select>
          )}
        </Field>
        <fieldset>
          <legend>Notifications</legend>
          <Check
            label="Notify me about relevant tenders"
            checked={enabled}
            onChange={setEnabled}
          />
          <Field label="Notification channel">
            {(id) => (
              <select
                id={id}
                value={channel}
                onChange={(event) =>
                  setChannel(event.target.value as "email" | "telegram")
                }
              >
                <option value="email">Email</option>
                <option value="telegram">Telegram</option>
              </select>
            )}
          </Field>
          {channel === "email" ? (
            <Field
              label="Notification email"
              hint="Notifications go to your account email."
            >
              {(id) => (
                <input
                  id={id}
                  name="notification_email"
                  type="email"
                  value={user.email}
                  readOnly
                />
              )}
            </Field>
          ) : (
            <TextField
              label="Telegram chat ID or channel username"
              name="chat_id"
              value={criteria?.notifications.chat_id}
              required={enabled}
              placeholder="-1001234567890 or @channel"
            />
          )}
          {channel === "telegram" && (
            <p className="muted">
              Start the configured Quarry bot in Telegram first, or add it to
              your channel. Your destination belongs to this campaign.
            </p>
          )}
        </fieldset>
        <ErrorMessage error={save.error} />
        <div className="form-footer">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button
            className="primary"
            disabled={save.isPending || selected.length === 0}
          >
            {save.isPending ? "Saving…" : "Save monitoring"}
          </button>
        </div>
      </Form>
    </Dialog>
  );
}

export default function Monitoring({ user }: { user: User }) {
  const query = useAccountResource<{ items: AccountCampaign[] }>(
    "/account/campaigns",
  );
  const [editing, setEditing] = useState<AccountCampaign | "new" | null>(null);
  const toggle = useAccountAction();
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Monitoring</h1>
          <p>
            Your websites, business requirements, schedule, and notifications.
          </p>
        </div>
        <button className="primary" onClick={() => setEditing("new")}>
          <IconPlus size={18} />
          Create campaign
        </button>
      </div>
      <ErrorMessage error={toggle.error} />
      <QueryState query={query} empty={query.data?.items.length === 0}>
        Create your first campaign to choose websites, set requirements, and
        receive relevant opportunities automatically.
      </QueryState>
      {!!query.data?.items.length && (
        <section className="table-card">
          <Table
            name="Monitoring campaigns"
            headers={[
              "Campaign",
              "State",
              "Schedule",
              "Notifications",
              "Actions",
            ]}
          >
            {query.data.items.map((campaign) => (
              <tr key={campaign.id}>
                <td>
                  <button
                    className="record-link"
                    onClick={() => setEditing(campaign)}
                  >
                    {campaign.criteria.name}
                  </button>
                  <small>
                    {campaign.criteria.include_keywords.join(", ") ||
                      campaign.criteria.description ||
                      "All opportunities from selected websites"}
                  </small>
                  <small>
                    {campaign.criteria.source_ids.length} website
                    {campaign.criteria.source_ids.length === 1 ? "" : "s"}
                  </small>
                </td>
                <td>
                  <State value={campaign.active ? "active" : "paused"} />
                  {campaign.monitoring && (
                    <>
                      <small>
                        <State value={campaign.monitoring.status} />
                      </small>
                      {campaign.active &&
                        !campaign.monitoring.scheduler_running && (
                          <small className="error-text">
                            Automatic checking was not detected. Contact your
                            administrator.
                          </small>
                        )}
                      {campaign.monitoring.coverage_status === "incomplete" && (
                        <small className="error-text">
                          Partial website coverage. Some tenders may be missing.
                        </small>
                      )}
                      {campaign.monitoring.coverage_status === "unknown" && (
                        <small>Website coverage has not been verified.</small>
                      )}
                      {campaign.monitoring.sources?.map((scope) => (
                        <small key={scope.source_id}>
                          {scope.source_id}:{" "}
                          {scope.max_tenders != null
                            ? `approved sample limit ${scope.max_tenders} tenders per check`
                            : scope.scope_approved
                              ? "approved collection limits"
                              : "collection scope needs administrator approval"}
                          .
                          {scope.max_tenders != null &&
                            " Full website monitoring requires separate administrator approval."}
                        </small>
                      ))}
                      <small>
                        Last check:{" "}
                        {campaign.monitoring.last_run_at
                          ? date(campaign.monitoring.last_run_at)
                          : "Not yet checked"}
                      </small>
                      {campaign.active &&
                        campaign.monitoring.scheduler_running &&
                        campaign.monitoring.next_run_at && (
                          <small>
                            Next check: {date(campaign.monitoring.next_run_at)}
                          </small>
                        )}
                      {campaign.monitoring.last_operation_status && (
                        <small>
                          Last result:{" "}
                          {readable(campaign.monitoring.last_operation_status)}
                        </small>
                      )}
                    </>
                  )}
                  {campaign.processing > 0 && (
                    <small>{campaign.processing} evaluations in progress</small>
                  )}
                  {campaign.failed > 0 && (
                    <small className="error-text">
                      Some results could not be evaluated. Contact your
                      administrator.
                    </small>
                  )}
                  {campaign.monitoring?.last_error && (
                    <small className="error-text">
                      {campaign.monitoring.last_error ===
                      "matching_budget_stopped"
                        ? "Analysis is temporarily unavailable. Contact your administrator."
                        : readable(campaign.monitoring.last_error)}
                    </small>
                  )}
                </td>
                <td>
                  Every {campaign.interval_seconds / 3600} hour
                  {campaign.interval_seconds === 3600 ? "" : "s"}
                </td>
                <td>
                  {campaign.criteria.notifications.enabled
                    ? campaign.criteria.notifications.channel === "email"
                      ? "Email"
                      : "Telegram"
                    : "Off"}
                </td>
                <td>
                  <div className="actions">
                    <button onClick={() => setEditing(campaign)}>Edit</button>
                    <button
                      disabled={toggle.isPending}
                      onClick={() =>
                        toggle.mutate({
                          path: `/account/campaigns/${campaign.id}`,
                          method: "PUT",
                          body: {
                            criteria: campaign.criteria,
                            interval_seconds: campaign.interval_seconds,
                            active: !campaign.active,
                            expected_version_id:
                              campaign.current_criteria_version_id,
                          },
                        })
                      }
                    >
                      {campaign.active ? "Pause" : "Resume"}
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </Table>
        </section>
      )}
      {editing && (
        <MonitoringForm
          campaign={editing === "new" ? undefined : editing}
          user={user}
          onClose={() => setEditing(null)}
        />
      )}
    </>
  );
}
