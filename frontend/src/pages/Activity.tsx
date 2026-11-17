import { useState } from "react";
import { IconRefresh } from "@tabler/icons-react";
import {
  activeOperation,
  date,
  readable,
  type Operation,
  type Source,
} from "../api";
import { useResource } from "../data";
import { Field, QueryState, State, Table } from "../ui";
import { operationName, OperationDetails } from "../operations";
export default function Activity() {
  const query = useResource<Operation[]>("/activity", (ops) =>
    ops.some(activeOperation),
  );
  const sources = useResource<{ items: Source[] }>("/sources");
  const [filter, setFilter] = useState(""),
    [state, setState] = useState(""),
    [selected, setSelected] = useState("");
  const operations = query.data || [];
  const items = operations.filter(
    (op) =>
      (!state || op.status === state) &&
      `${operationName(op.kind)} ${sources.data?.items.find((s) => s.id === op.source_id)?.name || ""} ${op.error_code || ""}`
        .toLowerCase()
        .includes(filter.toLowerCase()),
  );
  const detail = useResource<Operation>(
    selected ? `/operations/${selected}` : null,
    activeOperation,
  );
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Activity</h1>
          <p>
            Follow collection and matching, with a clear record of what
            happened.
          </p>
        </div>
        <button onClick={() => void query.refetch()}>
          <IconRefresh size={16} />
          Refresh
        </button>
      </div>
      <div className="toolbar-container">
        <div className="toolbar-row">
          <Field label="Search activity">
            {(id) => (
              <input
                id={id}
                type="search"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder="Search operations or websites"
              />
            )}
          </Field>
          <Field label="Operation status">
            {(id) => (
              <select
                id={id}
                value={state}
                onChange={(e) => setState(e.target.value)}
              >
                <option value="">All operations</option>
                {[
                  "queued",
                  "running",
                  "completed",
                  "activated",
                  "failed",
                  "awaiting_approval",
                ].map((s) => (
                  <option key={s} value={s}>
                    {readable(s)}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <span className="muted">
            Latest 100 operations · {operations.filter(activeOperation).length}{" "}
            in progress
          </span>
        </div>
      </div>
      <QueryState query={query} empty={!!query.data && !items.length}>
        Collection and matching operations appear here. Adjust filters if no
        operations match.
      </QueryState>
      {items.length > 0 && (
        <div className="workspace-split operations-grid">
          <section className="table-card">
            <Table
              name="Activity"
              headers={["Operation", "Status", "Started", "Details"]}
            >
              {items.map((op) => (
                <tr key={op.id}>
                  <td>
                    <strong>{operationName(op.kind)}</strong>
                    <small>
                      {sources.data?.items.find((s) => s.id === op.source_id)
                        ?.name ||
                        (!op.source_id
                          ? "Campaign evaluation"
                          : "Website operation")}
                    </small>
                    {op.error_code && (
                      <small className="error-text">
                        {readable(op.error_code)}
                      </small>
                    )}
                  </td>
                  <td>
                    <State value={op.status} />
                  </td>
                  <td>{date(op.started_at || op.created_at)}</td>
                  <td>
                    <button onClick={() => setSelected(op.id)}>
                      Inspect operation
                    </button>
                  </td>
                </tr>
              ))}
            </Table>
          </section>
          {selected && (
            <div>
              <QueryState query={detail} />
              {detail.data && <OperationDetails operation={detail.data} />}
            </div>
          )}
        </div>
      )}
    </>
  );
}
