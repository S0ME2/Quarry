import type { components } from "../schema";
import { useState, type ReactNode } from "react";
import { IconArrowLeft, IconPlus, IconRefresh } from "@tabler/icons-react";
import {
  activeOperation,
  date,
  readable,
  send,
  type Operation,
  type Source,
  type SourceWorkspace,
} from "../api";
import { useAction, useCompletedOperation, useResource } from "../data";
import {
  Check,
  Diagnostics,
  Dialog,
  Disclosure,
  ErrorMessage,
  Field,
  Form,
  Panel,
  QueryState,
  State,
  Table,
  TextField,
} from "../ui";
import { OperationDetails } from "../operations";
export default function Sources() {
  const list = useResource<{ items: Source[] }>("/sources");
  const [adding, setAdding] = useState(false),
    [selected, setSelected] = useState("");
  const add = useAction<{ source_id: string }>((r) => {
    setAdding(false);
    setSelected(r.source_id);
  });
  if (selected)
    return (
      <SourceEditor
        id={selected}
        source={list.data?.items.find((s) => s.id === selected)}
        back={() => setSelected("")}
      />
    );
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Sources</h1>
          <p>
            Your tender websites. Validate once, then collect with clear limits.
          </p>
        </div>
        <button className="primary" onClick={() => setAdding(true)}>
          <IconPlus size={18} />
          Add source
        </button>
      </div>
      <div className="toolbar-container toolbar-row">
        <span>{list.data?.items.length ?? "—"} websites</span>
        <button onClick={() => void list.refetch()}>
          <IconRefresh size={16} />
          Refresh
        </button>
      </div>
      <QueryState query={list} empty={list.data?.items.length === 0}>
        Add a website. Quarry investigates, generates and verifies its
        collection recipe.
      </QueryState>
      {!!list.data?.items.length && (
        <section className="table-card sources-card">
          <Table
            name="Sources"
            headers={[
              "Website",
              "State",
              "Recipe",
              "Tenders",
              "Last collection / coverage",
              "Details",
            ]}
          >
            {list.data.items.map((s) => (
              <tr key={s.id}>
                <td>
                  <strong>{s.name}</strong>
                  {s.url && <small className="wrap">{s.url}</small>}
                </td>
                <td>
                  <State value={s.enabled ? "enabled" : "disabled"} />
                </td>
                <td>
                  {s.id === "manual"
                    ? "Manual entry"
                    : s.recipe_version
                      ? "Validated"
                      : "Recipe needed"}
                </td>
                <td>{s.tender_count}</td>
                <td>
                  <span>
                    {s.collection_status
                      ? readable(s.collection_status)
                      : "Not collected"}
                  </span>
                  <small>
                    Coverage: {readable(s.coverage_status || "not verified")}
                  </small>
                  {s.last_success && <small>{date(s.last_success)}</small>}
                </td>
                <td>
                  {s.id !== "manual" && (
                    <button onClick={() => setSelected(s.id)}>
                      Open source
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </Table>
        </section>
      )}
      {adding && (
        <Dialog title="Add a tender website" onClose={() => setAdding(false)}>
          <p>
            Paste a public tender website address. Quarry will investigate it,
            verify real samples and activate collection when checks pass.
          </p>
          <Form
            onSubmit={(f) =>
              add.mutate({ path: "/sources", body: { url: f.get("url") } })
            }
          >
            <TextField label="Website URL" name="url" type="url" required />
            <ErrorMessage error={add.error} />
            <div className="form-footer">
              <button type="button" onClick={() => setAdding(false)}>
                Cancel
              </button>
              <button className="primary" disabled={add.isPending}>
                Add website
              </button>
            </div>
          </Form>
        </Dialog>
      )}
    </>
  );
}
function SourceEditor({
  id,
  source,
  back,
}: {
  id: string;
  source?: Source;
  back: () => void;
}) {
  const workspace = useResource<SourceWorkspace>(
    `/sources/${id}/workspace`,
    (value) => value.operations.some(activeOperation),
  );
  const [recipe, setRecipe] = useState(""),
    [count, setCount] = useState(10),
    [operation, setOperation] = useState("");
  const [bounds, setBounds] = useState({
    max_requests: 300,
    max_duration_seconds: 120,
    max_response_bytes: 10000000,
    max_total_response_bytes: 100000000,
  });
  const action = useAction<Operation>((r) => setOperation(r.id));
  const toggle = useAction();
  const manualOnly = source?.source_manual_only ?? source?.manual_only ?? false;
  const [loadError, setLoadError] = useState<Error | null>(null),
    [loadingRecipe, setLoadingRecipe] = useState(false);
  const current = operation
    ? workspace.data?.operations.find((op) => op.id === operation)
    : workspace.data?.operations.find(
        (op) =>
          op.kind === "recipe_generation" ||
          op.kind === "source_setup" ||
          op.kind === "collection",
      );
  const query = useResource<Operation>(
    operation && !current ? `/operations/${operation}` : null,
    activeOperation,
  );
  const selectedOperation = current || query.data;
  useCompletedOperation(selectedOperation);
  const pending =
    workspace.data?.operations.some(activeOperation) ||
    (selectedOperation && activeOperation(selectedOperation)) ||
    action.isPending;
  const limitsValid =
    bounds.max_total_response_bytes >= bounds.max_response_bytes;
  async function load(stage: string) {
    setLoadError(null);
    setLoadingRecipe(true);
    try {
      const saved = await send<{ text: string }>(
        `/sources/${id}/recipe?stage=${stage}`,
        undefined,
        "GET",
      );
      if (
        !recipe ||
        recipe === saved.text ||
        window.confirm(
          "Replace the current recipe editor contents with the saved version?",
        )
      )
        setRecipe(saved.text);
    } catch (e) {
      setLoadError(e as Error);
    } finally {
      setLoadingRecipe(false);
    }
  }
  return (
    <>
      <button className="back-link" onClick={back}>
        <IconArrowLeft size={17} />
        Back to Sources
      </button>
      <div className="page-head">
        <div>
          <h1>{source?.name || "New website"}</h1>
          <p className="wrap">
            {source?.url || "Manage collection and source evidence."}
          </p>
        </div>
        <div className="actions">
          <State value={source?.enabled === false ? "disabled" : "enabled"} />
          <button
            disabled={toggle.isPending}
            onClick={() =>
              toggle.mutate({
                path: `/sources/${id}`,
                method: "PATCH",
                body: { enabled: source?.enabled === false },
              })
            }
          >
            {source?.enabled === false ? "Enable source" : "Disable source"}
          </button>
        </div>
      </div>
      <ErrorMessage error={toggle.error} />
      <ErrorMessage error={action.error} />
      <QueryState query={workspace} />
      <div className="workspace-split">
        <div>
          <Panel title="Scheduled monitoring">
            <p>
              {manualOnly
                ? "Scheduled collection is paused for this website. Allow it to make this catalog source available to monitoring campaigns."
                : "This website can be collected by scheduled monitoring. Global monitoring settings still apply."}
            </p>
            <button
              disabled={toggle.isPending || !source?.recipe_version}
              onClick={() => {
                if (
                  !manualOnly ||
                  window.confirm(
                    "Allow scheduled monitoring for this website using its saved collection limits? Global monitoring settings and campaign schedules still apply.",
                  )
                )
                  toggle.mutate({
                    path: `/sources/${id}`,
                    method: "PATCH",
                    body: { manual_only: !manualOnly },
                  });
              }}
            >
              {manualOnly
                ? "Allow scheduled monitoring"
                : "Pause scheduled monitoring"}
            </button>
            {source?.global_manual_only && (
              <p className="notice">
                Global scheduled monitoring is paused. An administrator must
                turn off QUARRY_MANUAL_ONLY on the server before schedules can
                run.
              </p>
            )}
          </Panel>
          <RecipeSection ready={!!source?.recipe_version}>
            {!source?.recipe_version && (
              <div className="actions">
                <button
                  disabled={!!pending}
                  onClick={() =>
                    action.mutate({
                      path: `/sources/${id}/generate`,
                      body: {},
                    })
                  }
                >
                  {selectedOperation?.kind === "recipe_generation" &&
                  selectedOperation.status === "failed"
                    ? "Retry website investigation"
                    : "Investigate and generate recipe"}
                </button>
              </div>
            )}
            <p>
              Automatic setup checks real website evidence before activation.
              Advanced: supply a JSON recipe below to validate an existing
              recipe.
            </p>
            <Form
              onSubmit={() =>
                action.mutate({
                  path: `/sources/${id}/recipe`,
                  body: { text: recipe, ...bounds, max_tenders: 3 },
                })
              }
            >
              <Field label="Recipe JSON">
                {(key) => (
                  <textarea
                    id={key}
                    className="recipe-editor"
                    value={recipe}
                    onChange={(e) => setRecipe(e.target.value)}
                    rows={source?.recipe_version ? 5 : 10}
                    disabled={loadingRecipe}
                    required
                    spellCheck={false}
                    placeholder="Paste collection recipe JSON"
                  />
                )}
              </Field>
              <div className="actions">
                <button
                  className="primary"
                  disabled={!!pending || loadingRecipe || !limitsValid}
                >
                  Save and validate recipe
                </button>
                {source?.recipe_version && (
                  <button
                    type="button"
                    disabled={loadingRecipe}
                    onClick={() => void load("active")}
                  >
                    Load saved recipe
                  </button>
                )}
                {workspace.data?.recipe_import && (
                  <button
                    type="button"
                    disabled={loadingRecipe}
                    onClick={() => void load("draft")}
                  >
                    Load latest draft
                  </button>
                )}
              </div>
              <p className="muted">
                Validation sample: up to 3 tenders. Collection limits below
                apply independently.
              </p>
            </Form>
            <ErrorMessage error={loadError} />
          </RecipeSection>
          <Panel title="Collect tenders">
            <p>
              Collect a bounded batch from this website. Existing records are
              updated without duplicate entries.
            </p>
            <Form
              onSubmit={() =>
                action.mutate({
                  path: `/sources/${id}/collect`,
                  body: { max_tenders: count, ...bounds },
                })
              }
            >
              <Field label="Tenders to collect">
                {(key) => (
                  <input
                    id={key}
                    type="number"
                    min={1}
                    max={100}
                    required
                    value={count}
                    onChange={(e) => setCount(e.target.valueAsNumber)}
                  />
                )}
              </Field>
              <Disclosure title="Request, time and download limits">
                <div className="form-grid">
                  {(
                    [
                      ["max_requests", "Maximum requests", 1, 10000],
                      [
                        "max_duration_seconds",
                        "Maximum duration (seconds)",
                        1,
                        900,
                      ],
                      [
                        "max_response_bytes",
                        "Maximum response size (bytes)",
                        1000,
                        100000000,
                      ],
                      [
                        "max_total_response_bytes",
                        "Maximum total download (bytes)",
                        1000,
                        500000000,
                      ],
                    ] as const
                  ).map(([k, label, min, max]) => (
                    <Field key={k} label={label}>
                      {(key) => (
                        <input
                          id={key}
                          type="number"
                          required
                          min={min}
                          max={max}
                          value={bounds[k]}
                          onChange={(e) =>
                            setBounds({
                              ...bounds,
                              [k]: e.target.valueAsNumber,
                            })
                          }
                        />
                      )}
                    </Field>
                  ))}
                </div>
                {!limitsValid && (
                  <p role="alert">
                    Total download limit must cover one response.
                  </p>
                )}
              </Disclosure>
              <button
                className="primary"
                disabled={
                  !source?.recipe_version ||
                  source.enabled === false ||
                  !!pending ||
                  !limitsValid
                }
              >
                Scrape tenders
              </button>
            </Form>
            <small>
              {!source?.recipe_version &&
                "Collection is unavailable until a recipe passes verification and activation. "}
              {source?.enabled === false &&
                "Enable this source before collecting. "}
              Collection success and verified complete coverage are different.
              Review evidence after collection.
            </small>
          </Panel>
          <SourceSettings id={id} />
        </div>
        <div>
          {selectedOperation ? (
            <OperationDetails operation={selectedOperation} />
          ) : (
            <Panel title="Collection progress">
              <p>
                Investigate this website to generate and independently verify a
                recipe, or validate an existing recipe.
              </p>
            </Panel>
          )}
          {source?.warnings?.length ? (
            <Diagnostics label="Source health notes" value={source.warnings} />
          ) : null}
          <Disclosure title="Operation history">
            {workspace.data?.operations.map((op) => (
              <button
                className="history-row"
                key={op.id}
                onClick={() => setOperation(op.id)}
              >
                <span>
                  {readable(op.kind)} · {date(op.created_at)}
                </span>
                <State value={op.status} />
              </button>
            ))}
          </Disclosure>
        </div>
      </div>
    </>
  );
}
function SourceSettings({ id }: { id: string }) {
  const [open, setOpen] = useState(false);
  const config = useResource<{
    configuration: components["schemas"]["SourceOverride"];
  }>(open ? `/sources/${id}/configuration` : null);
  const save = useAction();
  return (
    <details
      className="disclosure"
      onToggle={(e) => setOpen(e.currentTarget.open)}
    >
      <summary>Source settings</summary>
      <div className="disclosure-body">
        <QueryState query={config} />
        {config.data && (
          <Form
            onSubmit={(f) =>
              save.mutate({
                path: `/sources/${id}/configuration`,
                method: "PUT",
                body: {
                  configuration: {
                    timezone: f.get("timezone"),
                    timeout_seconds: Number(f.get("timeout")),
                    batch_size: Number(f.get("batch")),
                    browser_fallback_enabled: f.has("fallback"),
                    retry: { max_attempts: Number(f.get("attempts") || 3) },
                    normalization: {
                      ...config.data!.configuration.normalization,
                      decimal_separator: f.get("decimal"),
                      thousands_separator: f.get("thousands") || null,
                      date_only_deadline: f.get("date_only"),
                      parse_failure_policy: f.get("parse_failure"),
                      date_formats: {
                        ...config.data!.configuration.normalization
                          ?.date_formats,
                        deadline: String(f.get("deadline_formats") || "")
                          .split("\n")
                          .map((x) => x.trim())
                          .filter(Boolean),
                        published_at: String(f.get("published_formats") || "")
                          .split("\n")
                          .map((x) => x.trim())
                          .filter(Boolean),
                      },
                    },
                  },
                },
              })
            }
          >
            <div className="form-grid">
              <TextField
                label="Source timezone"
                name="timezone"
                value={String(config.data.configuration.timezone)}
                required
              />
              <TextField
                label="Request timeout (seconds)"
                name="timeout"
                value={Number(config.data.configuration.timeout_seconds)}
                type="number"
                min={1}
                required
              />
              <TextField
                label="Batch size"
                name="batch"
                value={Number(config.data.configuration.batch_size)}
                type="number"
                min={1}
                required
              />
            </div>
            <Disclosure title="Date and number parsing">
              <p>
                Use source-local formats when automatic parsing needs
                adjustment. One date format per line; leave blank for ISO dates.
              </p>
              <div className="form-grid">
                <TextField
                  label="Transport attempts per request"
                  name="attempts"
                  type="number"
                  min={1}
                  required
                  value={config.data.configuration.retry?.max_attempts ?? 3}
                />
                <Field label="Decimal separator">
                  {(id) => (
                    <select
                      id={id}
                      name="decimal"
                      defaultValue={
                        config.data!.configuration.normalization
                          ?.decimal_separator ?? "."
                      }
                    >
                      <option value=".">Point (.)</option>
                      <option value=",">Comma (,)</option>
                    </select>
                  )}
                </Field>
                <Field label="Thousands separator">
                  {(id) => (
                    <select
                      id={id}
                      name="thousands"
                      defaultValue={
                        config.data!.configuration.normalization
                          ?.thousands_separator ?? ""
                      }
                    >
                      <option value="">None</option>
                      <option value=" ">Space</option>
                      <option value=",">Comma (,)</option>
                      <option value=".">Point (.)</option>
                    </select>
                  )}
                </Field>
                <Field label="Date-only deadline">
                  {(id) => (
                    <select
                      id={id}
                      name="date_only"
                      defaultValue={
                        config.data!.configuration.normalization
                          ?.date_only_deadline ?? "end_of_day"
                      }
                    >
                      <option value="end_of_day">
                        End of source-local day
                      </option>
                      <option value="start_of_day">
                        Start of source-local day
                      </option>
                      <option value="reject">Reject ambiguous dates</option>
                    </select>
                  )}
                </Field>
                <Field label="Parse failures">
                  {(id) => (
                    <select
                      id={id}
                      name="parse_failure"
                      defaultValue={
                        config.data!.configuration.normalization
                          ?.parse_failure_policy ?? "reject"
                      }
                    >
                      <option value="reject">Reject invalid data</option>
                      <option value="null_with_issue">
                        Leave missing and record an issue
                      </option>
                    </select>
                  )}
                </Field>
                <Field label="Deadline date formats">
                  {(id) => (
                    <textarea
                      id={id}
                      name="deadline_formats"
                      rows={3}
                      defaultValue={config.data!.configuration.normalization?.date_formats?.deadline?.join(
                        "\n",
                      )}
                    />
                  )}
                </Field>
                <Field label="Publication date formats">
                  {(id) => (
                    <textarea
                      id={id}
                      name="published_formats"
                      rows={3}
                      defaultValue={config.data!.configuration.normalization?.date_formats?.published_at?.join(
                        "\n",
                      )}
                    />
                  )}
                </Field>
              </div>
            </Disclosure>
            <Check
              name="fallback"
              label="Enable browser fallback"
              checked={Boolean(
                config.data.configuration.browser_fallback_enabled,
              )}
            />
            <ErrorMessage error={save.error} />
            {save.isSuccess && (
              <p role="status">
                Source settings saved. Revalidate a draft recipe if needed.
              </p>
            )}
            <button disabled={save.isPending}>Save source settings</button>
            <Diagnostics
              value={config.data.configuration}
              label="Full source configuration"
            />
          </Form>
        )}
      </div>
    </details>
  );
}

function RecipeSection({
  ready,
  children,
}: {
  ready: boolean;
  children: ReactNode;
}) {
  return ready ? (
    <Disclosure title="Collection recipe">{children}</Disclosure>
  ) : (
    <Panel title="Connect this website">{children}</Panel>
  );
}
