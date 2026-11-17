import { useState } from "react";
import { useAccountAction, useAccountResource } from "../account";
import { Dialog, ErrorMessage, Form, QueryState, TextField } from "../ui";

type Provider = {
  name: string;
  base_url: string;
  enabled: boolean;
  has_api_key: boolean;
};
type Assignment = {
  provider_id: string;
  model: string;
  max_output_tokens: number;
  max_input_bytes: number;
  context_window_tokens: number;
  timeout_seconds: number;
};
type AISettings = {
  providers: Record<string, Provider>;
  tasks: Record<string, Assignment>;
};
type SMTPSettings = {
  configured: boolean;
  source: string;
  host?: string;
  port?: number;
  security?: string;
  username?: string;
  sender?: string;
  password_configured?: boolean;
};
type SourceRequest = {
  id: string;
  name: string;
  url: string;
  decision: string;
  technical_verified: boolean;
  active_recipe_version: string | null;
  enabled: boolean;
  manual_only: boolean;
  identity_candidates: { id: string; name: string }[];
  latest_operation: {
    status: string;
    error_code: string | null;
    result?: unknown;
  } | null;
};

function ProviderEditor({
  id,
  provider,
  onClose,
}: {
  id?: string;
  provider?: Provider;
  onClose: () => void;
}) {
  const save = useAccountAction(onClose);
  return (
    <Dialog
      title={provider ? "Edit AI provider" : "Add AI provider"}
      onClose={onClose}
    >
      <p>Use the HTTPS base URL of an OpenAI-compatible API.</p>
      <Form
        onSubmit={(f) => {
          const key = String(f.get("api_key") || "");
          save.mutate({
            path: id
              ? `/admin/ai/providers/${encodeURIComponent(id)}`
              : "/admin/ai/providers",
            method: id ? "PUT" : "POST",
            body: {
              name: String(f.get("name") || "").trim(),
              base_url: String(f.get("base_url") || "").trim(),
              protocol: "openai-compatible",
              enabled: f.get("enabled") === "on",
              ...(key ? { api_key: key } : {}),
            },
          });
        }}
      >
        <div className="form-grid">
          <TextField
            label="Provider name"
            name="name"
            value={provider?.name}
            required
          />
          <TextField
            label="API base URL"
            name="base_url"
            type="url"
            value={provider?.base_url}
            placeholder="https://api.example.com/v1"
            required
          />
          <TextField
            label={
              provider
                ? "New API key (leave blank to keep current key)"
                : "API key"
            }
            name="api_key"
            type="password"
            required={!provider}
          />
        </div>
        <label>
          <input
            name="enabled"
            type="checkbox"
            defaultChecked={provider?.enabled ?? true}
          />{" "}
          Enabled
        </label>
        <ErrorMessage error={save.error} />
        <div className="form-footer">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button className="primary" disabled={save.isPending}>
            {save.isPending ? "Saving…" : "Save provider"}
          </button>
        </div>
      </Form>
    </Dialog>
  );
}

function AssignmentForm({
  role,
  label,
  settings,
}: {
  role: string;
  label: string;
  settings: AISettings;
}) {
  const save = useAccountAction();
  const current = settings.tasks[role];
  const providers = Object.entries(settings.providers).filter(
    ([, p]) => p.enabled,
  );
  return (
    <Form
      key={`${role}-${current?.provider_id || ""}-${current?.model || ""}`}
      onSubmit={(f) =>
        save.mutate({
          path: `/admin/ai/tasks/${role}`,
          method: "PUT",
          body: {
            max_output_tokens: current?.max_output_tokens ?? 4096,
            max_input_bytes: current?.max_input_bytes ?? 11000,
            context_window_tokens: current?.context_window_tokens ?? 32768,
            timeout_seconds: current?.timeout_seconds ?? 90,
            provider_id: String(f.get("provider_id")),
            model: String(f.get("model") || "").trim(),
          },
        })
      }
    >
      <h4>{label}</h4>
      <div className="form-grid">
        <div className="field">
          <label htmlFor={`${role}-provider`}>Provider</label>
          <select
            id={`${role}-provider`}
            name="provider_id"
            defaultValue={current?.provider_id || ""}
            required
          >
            <option value="">Select a provider</option>
            {providers.map(([id, p]) => (
              <option key={id} value={id}>
                {p.name}
              </option>
            ))}
          </select>
        </div>
        <TextField
          label="Model name"
          name="model"
          value={current?.model}
          required
          placeholder="Enter provider model ID"
        />
      </div>
      <ErrorMessage error={save.error} />
      {save.isSuccess && <p role="status">Model assignment saved.</p>}
      <button disabled={!providers.length || save.isPending}>
        {save.isPending ? "Saving…" : "Save model"}
      </button>
    </Form>
  );
}

function AIAdministration() {
  const query = useAccountResource<AISettings>("/admin/ai");
  const [editor, setEditor] = useState<{
    id?: string;
    provider?: Provider;
  } | null>(null);
  const [tested, setTested] = useState<{ id: string; ok: boolean } | null>(
    null,
  );
  const test = useAccountAction<{ ok: boolean }>();
  return (
    <section className="table-card">
      <h2>AI providers</h2>
      <p>
        Configure an OpenAI-compatible provider and choose models for recipes
        and tender matching.
      </p>
      <QueryState query={query} />
      {query.data?.providers && query.data.tasks && (
        <>
          {Object.entries(query.data.providers).length === 0 && (
            <p>No providers configured.</p>
          )}
          {Object.entries(query.data.providers).map(([id, p]) => (
            <div className="user-campaign" key={id}>
              <strong>{p.name}</strong>
              <p>
                {p.base_url} · {p.enabled ? "Enabled" : "Disabled"} ·{" "}
                {p.has_api_key ? "Key saved" : "Key missing"}
              </p>
              <div className="actions">
                <button onClick={() => setEditor({ id, provider: p })}>
                  Edit
                </button>
                <button
                  disabled={!p.enabled || test.isPending}
                  onClick={() => {
                    setTested(null);
                    test.mutate(
                      {
                        path: `/admin/ai/providers/${encodeURIComponent(id)}/test`,
                      },
                      {
                        onSuccess: (result) => setTested({ id, ok: result.ok }),
                      },
                    );
                  }}
                >
                  Test connection
                </button>
              </div>
              {tested?.id === id && (
                <p role="status">
                  {tested.ok
                    ? "Connection succeeded."
                    : "Connection failed. Check the URL and key."}
                </p>
              )}
            </div>
          ))}
          <ErrorMessage error={test.error} />
          <button onClick={() => setEditor({})}>Add provider</button>
          <AssignmentForm
            role="recipe_generation"
            label="Website recipe generation"
            settings={query.data}
          />
          <AssignmentForm
            role="tender_matching"
            label="Tender matching"
            settings={query.data}
          />
        </>
      )}
      {editor && <ProviderEditor {...editor} onClose={() => setEditor(null)} />}
    </section>
  );
}

function SMTPAdministration() {
  const query = useAccountResource<SMTPSettings>("/admin/smtp");
  const save = useAccountAction();
  const test = useAccountAction<{ status: string }>();
  return (
    <section className="table-card">
      <h2>Email delivery</h2>
      <p>Configure the mail server used for notifications.</p>
      <QueryState query={query} />
      {query.data && (
        <>
          <p>
            {query.data.configured
              ? `Configured using ${query.data.source === "settings" ? "saved settings" : "environment settings"}.`
              : "Email is not configured."}
          </p>
          <Form
            key={JSON.stringify(query.data)}
            onSubmit={(f) => {
              const password = String(f.get("password") || "");
              save.mutate({
                path: "/admin/smtp",
                method: "PUT",
                body: {
                  host: String(f.get("host") || "").trim(),
                  port: Number(f.get("port")),
                  security: String(f.get("security")),
                  username: String(f.get("username") || "").trim(),
                  sender: String(f.get("sender") || "").trim(),
                  ...(password ? { password } : {}),
                },
              });
            }}
          >
            <div className="form-grid">
              <TextField
                label="SMTP host"
                name="host"
                value={query.data.host}
                required
              />
              <TextField
                label="Port"
                name="port"
                type="number"
                value={query.data.port ?? 587}
                min={1}
                max={65535}
                required
              />
              <div className="field">
                <label htmlFor="smtp-security">Connection security</label>
                <select
                  id="smtp-security"
                  name="security"
                  defaultValue={query.data.security ?? "starttls"}
                >
                  <option value="starttls">STARTTLS</option>
                  <option value="tls">TLS</option>
                </select>
              </div>
              <TextField
                label="Username (optional)"
                name="username"
                value={query.data.username}
              />
              <TextField
                label={
                  query.data.password_configured
                    ? "New password (leave blank to keep current password)"
                    : "Password (optional)"
                }
                name="password"
                type="password"
              />
              <TextField
                label="Sender email address"
                name="sender"
                type="email"
                value={query.data.sender}
                required
              />
            </div>
            <ErrorMessage error={save.error} />
            {save.isSuccess && <p role="status">Email settings saved.</p>}
            <button className="primary" disabled={save.isPending}>
              {save.isPending ? "Saving…" : "Save email settings"}
            </button>
          </Form>
          <Form
            onSubmit={(f) =>
              test.mutate({
                path: "/admin/smtp/test",
                body: {
                  destination: String(f.get("destination") || "").trim(),
                },
              })
            }
          >
            <h3>Send a test email</h3>
            <TextField
              label="Recipient email address"
              name="destination"
              type="email"
              required
            />
            <ErrorMessage error={test.error} />
            {test.isSuccess && (
              <p role="status">
                The mail server accepted the message. Delivery to the recipient
                is not confirmed.
              </p>
            )}
            <button disabled={test.isPending}>
              {test.isPending ? "Sending…" : "Send test email"}
            </button>
          </Form>
        </>
      )}
    </section>
  );
}

function SourceApprovals() {
  const query = useAccountResource<{ items: SourceRequest[] }>(
    "/admin/source-requests",
  );
  const update = useAccountAction();
  return (
    <section className="table-card">
      <h2>Website requests</h2>
      <p>
        Monitoring requires both approval and independent technical
        verification. Approval does not replace verification.
      </p>
      <QueryState query={query} empty={query.data?.items.length === 0}>
        No website requests.
      </QueryState>
      <ErrorMessage error={update.error} />
      {query.data?.items?.map((item) => (
        <div className="user-campaign" key={item.id}>
          <strong>{item.name}</strong>
          <p>{item.url}</p>
          {(item.identity_candidates?.length ?? 0) > 0 && (
            <div>
              <p>
                Possible existing website. Confirm its identity before review or
                verification.
              </p>
              {item.identity_candidates.map((candidate) => (
                <button
                  key={candidate.id}
                  disabled={update.isPending}
                  onClick={() =>
                    update.mutate({
                      path: `/admin/source-requests/${encodeURIComponent(item.id)}/resolve`,
                      method: "POST",
                      body: { existing_source_id: candidate.id },
                    })
                  }
                >
                  Same website: {candidate.name} ({candidate.id})
                </button>
              ))}
              <button
                disabled={update.isPending}
                onClick={() =>
                  update.mutate({
                    path: `/admin/source-requests/${encodeURIComponent(item.id)}/resolve`,
                    method: "POST",
                    body: { existing_source_id: null },
                  })
                }
              >
                Different website
              </button>
            </div>
          )}
          <p>
            Decision: {item.decision} · Verification:{" "}
            {item.technical_verified ? "passed" : "not passed"} · Source:{" "}
            {item.enabled ? "enabled" : "disabled"}
            {item.manual_only ? " · manual only" : ""}
          </p>
          {item.active_recipe_version != null && (
            <p>Active recipe version: {item.active_recipe_version}</p>
          )}
          {item.latest_operation && (
            <>
              <p>
                Latest check: {item.latest_operation.status}
                {item.latest_operation.error_code
                  ? ` · ${item.latest_operation.error_code}`
                  : ""}
              </p>
              {item.latest_operation.result != null && (
                <details>
                  <summary>Review available evidence</summary>
                  <pre className="wrap">
                    {JSON.stringify(item.latest_operation.result, null, 2)}
                  </pre>
                </details>
              )}
            </>
          )}
          <div className="actions">
            <button
              disabled={
                update.isPending ||
                (item.identity_candidates?.length ?? 0) > 0 ||
                item.decision === "approved"
              }
              onClick={() =>
                update.mutate({
                  path: `/admin/source-requests/${encodeURIComponent(item.id)}`,
                  method: "PATCH",
                  body: { decision: "approved" },
                })
              }
            >
              Approve
            </button>
            <button
              disabled={
                update.isPending ||
                (item.identity_candidates?.length ?? 0) > 0 ||
                item.decision === "rejected"
              }
              onClick={() =>
                update.mutate({
                  path: `/admin/source-requests/${encodeURIComponent(item.id)}`,
                  method: "PATCH",
                  body: { decision: "rejected" },
                })
              }
            >
              Reject
            </button>
          </div>
        </div>
      ))}
    </section>
  );
}

export default function AdminSettings() {
  return (
    <>
      <SourceApprovals />
      <AIAdministration />
      <SMTPAdministration />
    </>
  );
}
