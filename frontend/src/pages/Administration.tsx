import { useState } from "react";
import AdminSettings from "./AdminSettings";
import {
  type AccountCampaign,
  type AccountTenders,
  type User,
  useAccountAction,
  useAccountResource,
} from "../account";
import { useDebounced } from "../data";
import { Dialog, ErrorMessage, QueryState, State, Table } from "../ui";

function UserDetails({ user, onClose }: { user: User; onClose: () => void }) {
  const campaigns = useAccountResource<{ items: AccountCampaign[] }>(
    `/admin/users/${user.id}/campaigns`,
  );
  const tenders = useAccountResource<AccountTenders>(
    `/admin/users/${user.id}/tenders`,
  );
  return (
    <Dialog title={user.email} onClose={onClose} wide>
      <p>
        {user.role === "admin" ? "Platform administrator" : "User"} ·{" "}
        {user.active ? "Active account" : "Inactive account"}
      </p>
      <h3>Monitoring campaigns</h3>
      <QueryState query={campaigns} empty={campaigns.data?.items.length === 0}>
        No monitoring campaigns.
      </QueryState>
      {campaigns.data?.items.map((campaign) => (
        <div className="user-campaign" key={campaign.id}>
          <strong>{campaign.criteria.name}</strong>
          <State value={campaign.active ? "active" : "paused"} />
          <p>
            {campaign.criteria.description ||
              campaign.criteria.include_keywords.join(", ") ||
              "No keyword restrictions"}
          </p>
          <small>
            Sources: {campaign.criteria.source_ids.join(", ")} · Every{" "}
            {campaign.interval_seconds / 3600} hours ·{" "}
            {campaign.criteria.notifications.channel}
          </small>
        </div>
      ))}
      <h3>Search results</h3>
      <QueryState query={tenders} empty={tenders.data?.items.length === 0}>
        No results.
      </QueryState>
      {!!tenders.data?.items.length && (
        <Table name="User results" headers={["Tender", "Source", "Relevance"]}>
          {tenders.data.items.map((tender) => (
            <tr key={`${tender.id}-${tender.campaign_id}`}>
              <td>{tender.title || "Untitled tender"}</td>
              <td>{tender.source}</td>
              <td>
                <State value={tender.relevance} />
              </td>
            </tr>
          ))}
        </Table>
      )}
    </Dialog>
  );
}

export default function Administration({ currentUser }: { currentUser: User }) {
  const [search, setSearch] = useState("");
  const [active, setActive] = useState("");
  const [role, setRole] = useState("");
  const [selected, setSelected] = useState<User | null>(null);
  const [change, setChange] = useState<{
    user: User;
    patch: { active?: boolean; role?: string };
  } | null>(null);
  const q = useDebounced(search);
  const params = new URLSearchParams({ q });
  if (active) params.set("active", active);
  if (role) params.set("role", role);
  const users = useAccountResource<{ items: User[] }>(`/admin/users?${params}`);
  const update = useAccountAction(() => setChange(null));
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Administration</h1>
          <p>Manage account access and inspect each user's monitoring.</p>
        </div>
      </div>
      <div className="admin-links">
        <a href="#/sources">Manage platform catalog</a>
        <a href="#/activity">System runs and logs</a>
        <a href="#/tenders">System tender results</a>
      </div>
      <div className="toolbar-container toolbar-row">
        <input
          type="search"
          aria-label="Search users by email"
          placeholder="Search users by email"
          value={search}
          maxLength={254}
          onChange={(event) => setSearch(event.target.value)}
        />
        <select
          aria-label="Account status filter"
          value={active}
          onChange={(event) => setActive(event.target.value)}
        >
          <option value="">All account statuses</option>
          <option value="true">Active</option>
          <option value="false">Inactive</option>
        </select>
        <select
          aria-label="Role filter"
          value={role}
          onChange={(event) => setRole(event.target.value)}
        >
          <option value="">All roles</option>
          <option value="user">User</option>
          <option value="admin">Platform admin</option>
        </select>
      </div>
      <QueryState query={users} empty={users.data?.items.length === 0}>
        No users match your filters.
      </QueryState>
      {!!users.data?.items.length && (
        <section className="table-card account-users-table">
          <Table
            name="Users"
            headers={["Account", "Role", "Status", "Actions"]}
          >
            {users.data.items.map((user) => (
              <tr key={user.id}>
                <td>
                  <button
                    className="record-link"
                    onClick={() => setSelected(user)}
                  >
                    {user.email}
                  </button>
                  {user.id === currentUser.id && <small>Your account</small>}
                </td>
                <td>{user.role === "admin" ? "Platform admin" : "User"}</td>
                <td>
                  <State value={user.active ? "active" : "disabled"} />
                </td>
                <td>
                  <div className="actions">
                    <button onClick={() => setSelected(user)}>
                      View monitoring
                    </button>
                    <button
                      disabled={user.id === currentUser.id}
                      onClick={() => {
                        update.reset();
                        setChange({ user, patch: { active: !user.active } });
                      }}
                    >
                      {user.active ? "Deactivate" : "Activate"}
                    </button>
                    <button
                      disabled={user.id === currentUser.id}
                      onClick={() => {
                        update.reset();
                        setChange({
                          user,
                          patch: {
                            role: user.role === "admin" ? "user" : "admin",
                          },
                        });
                      }}
                    >
                      {user.role === "admin" ? "Remove admin" : "Make admin"}
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </Table>
        </section>
      )}
      <AdminSettings />
      {selected && (
        <UserDetails user={selected} onClose={() => setSelected(null)} />
      )}
      {change && (
        <Dialog
          title="Confirm account access change"
          onClose={() => setChange(null)}
        >
          <p>
            {change.patch.role
              ? `Change ${change.user.email} to ${change.patch.role === "admin" ? "Platform Admin with global access" : "a standard user"}?`
              : `${change.patch.active ? "Activate" : "Deactivate"} ${change.user.email}?`}
          </p>
          {change.patch.active === false && (
            <p>The user will lose access to their account.</p>
          )}
          <ErrorMessage error={update.error} />
          <div className="form-footer">
            <button onClick={() => setChange(null)}>Cancel</button>
            <button
              className="primary"
              disabled={update.isPending}
              onClick={() =>
                update.mutate({
                  path: `/admin/users/${change.user.id}`,
                  body: change.patch,
                  method: "PATCH",
                })
              }
            >
              {update.isPending ? "Saving…" : "Confirm change"}
            </button>
          </div>
        </Dialog>
      )}
    </>
  );
}
