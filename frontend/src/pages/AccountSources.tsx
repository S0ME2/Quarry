import { useState } from "react";
import { IconPlus, IconRefresh } from "@tabler/icons-react";
import {
  type AccountSource,
  useAccountAction,
  useAccountResource,
} from "../account";
import {
  Dialog,
  ErrorMessage,
  Form,
  QueryState,
  State,
  Table,
  TextField,
} from "../ui";

export default function AccountSources() {
  const query = useAccountResource<{ items: AccountSource[] }>(
    "/account/sources",
    true,
  );
  const [adding, setAdding] = useState(false);
  const add = useAccountAction<AccountSource>(() => setAdding(false));
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Sources</h1>
          <p>
            Choose ready websites when creating a monitoring campaign, or add a
            public tender website.
          </p>
        </div>
        <button className="primary" onClick={() => setAdding(true)}>
          <IconPlus size={18} />
          Add website
        </button>
      </div>
      <div className="toolbar-container toolbar-row">
        <span>{query.data?.items.length ?? "—"} available websites</span>
        <a href="#/monitoring">Configure monitoring</a>
        <button onClick={() => void query.refetch()}>
          <IconRefresh size={16} />
          Refresh
        </button>
      </div>
      <QueryState query={query} empty={query.data?.items.length === 0}>
        Request a public tender website. An administrator will review it before
        it becomes available.
      </QueryState>
      {!!query.data?.items.length && (
        <section className="table-card">
          <Table
            name="Available sources"
            headers={["Website", "Availability", "Type", "Actions"]}
          >
            {query.data.items.map((source) => (
              <tr key={source.id}>
                <td>
                  <strong>{source.name}</strong>
                  <small className="wrap">{source.url}</small>
                  {source.error && (
                    <small className="error-text">{source.error}</small>
                  )}
                </td>
                <td>
                  <State value={source.status} />
                  {source.status === "UNDER_REVIEW" && (
                    <small>Awaiting administrator review.</small>
                  )}
                  {["NEEDS_ATTENTION", "REJECTED"].includes(source.status) && (
                    <small>Unavailable for automatic monitoring.</small>
                  )}
                </td>
                <td>{source.custom ? "Your website" : "Catalog"}</td>
                <td>
                  {source.status === "APPROVED" && (
                    <a href="#/monitoring">Use in campaign</a>
                  )}
                </td>
              </tr>
            ))}
          </Table>
        </section>
      )}
      {adding && (
        <Dialog
          title="Add a public tender website"
          onClose={() => setAdding(false)}
        >
          <p>
            An administrator will review your request. Websites must also pass
            an independent technical check before monitoring is available.
          </p>
          <Form
            onSubmit={(form) =>
              add.mutate({
                path: "/account/sources",
                body: {
                  name: String(form.get("name")).trim(),
                  url: String(form.get("url")).trim(),
                },
              })
            }
          >
            <TextField label="Website name" name="name" required />
            <TextField
              label="Public website URL"
              name="url"
              type="url"
              required
              placeholder="https://example.org/tenders"
            />
            <ErrorMessage error={add.error} />
            <div className="form-footer">
              <button type="button" onClick={() => setAdding(false)}>
                Cancel
              </button>
              <button className="primary" disabled={add.isPending}>
                {add.isPending ? "Adding…" : "Submit request"}
              </button>
            </div>
          </Form>
        </Dialog>
      )}
    </>
  );
}
