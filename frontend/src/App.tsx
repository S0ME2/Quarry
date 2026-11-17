import { useIsMutating, useQueryClient } from "@tanstack/react-query";
import { activeOperation, type Operation } from "./api";
import { useResource } from "./data";
import { useEffect, useState } from "react";
import {
  IconActivity,
  IconArrowsShuffle,
  IconFolder,
  IconHammer,
  IconMenu2,
  IconStack2,
  IconTarget,
  IconSettings,
  IconShield,
  IconBell,
} from "@tabler/icons-react";
import Tenders, { TenderDetails } from "./pages/Tenders";
import Sources from "./pages/Sources";
import Campaigns from "./pages/Campaigns";
import Matching from "./pages/Matching";
import Activity from "./pages/Activity";
import { AccountGate } from "./pages/Account";
import Monitoring from "./pages/Monitoring";
import AccountSources from "./pages/AccountSources";
import AccountTenders, {
  AccountTenderDetails,
  AccountNotifications,
} from "./pages/AccountTenders";
import Administration from "./pages/Administration";
import { accountSend, setAccountSession, type User } from "./account";
import { ErrorMessage } from "./ui";
const sections = [
  ["Sources", "sources", IconFolder],
  ["Tenders", "tenders", IconHammer],
  ["Campaigns", "campaigns", IconTarget],
  ["Matching", "matching", IconArrowsShuffle],
  ["Activity", "activity", IconActivity],
] as const;
const route = () => window.location.hash.replace(/^#\/?/, "").split("/");
export default function App() {
  return <AccountGate>{(user) => <Workspace user={user} />}</AccountGate>;
}
function Workspace({ user }: { user: User }) {
  const [path, setPath] = useState(route),
    [menu, setMenu] = useState(false),
    [campaign, setCampaign] = useState("");
  const navigation = user.operator
    ? sections
    : user.role === "admin"
      ? [
          ...sections,
          ["My monitoring", "monitoring", IconSettings] as const,
          ["Administration", "admin", IconShield] as const,
        ]
      : ([
          ["Tenders", "tenders", IconHammer],
          ["Monitoring", "monitoring", IconSettings],
          ["Sources", "sources", IconFolder],
          ["Notifications", "notifications", IconBell],
        ] as const);
  const section =
    navigation.find((s) => s[1] === path[0]) ||
    navigation.find((s) => s[1] === "tenders")!;
  const sectionName = section[0];
  const [logoutError, setLogoutError] = useState<Error | null>(null);
  const [loggingOut, setLoggingOut] = useState(false);
  // Keep in-flight work observable after its initiating section unmounts.
  const mutating = useIsMutating();
  const [watchWork, setWatchWork] = useState(false);
  const client = useQueryClient();
  useEffect(() => {
    if (mutating) setWatchWork(true);
  }, [mutating]);
  const work = useResource<Operation[]>(
    watchWork && user.role === "admin" ? "/activity" : null,
    (ops) => ops.some(activeOperation),
  );
  const completed =
    work.data
      ?.filter((op) => op.finished_at)
      .map((op) => op.id + ":" + op.status)
      .join("|") || "";
  useEffect(() => {
    if (completed)
      void client.invalidateQueries({
        predicate: (q) => q.queryKey[0] != "/activity",
      });
  }, [completed, client]);

  useEffect(() => {
    const change = () => {
      setPath(route());
      setMenu(false);
      window.scrollTo(0, 0);
    };
    window.addEventListener("hashchange", change);
    return () => window.removeEventListener("hashchange", change);
  }, []);
  useEffect(() => {
    document.title = `Quarry · ${sectionName}`;
    document.getElementById("main-content")?.focus({ preventScroll: true });
  }, [sectionName, path]);
  const onOpen = (id: string) => {
    window.location.hash = `/tenders/${id}`;
  };
  const props = { campaign, setCampaign, onOpen };
  return (
    <>
      <a
        className="skip-link"
        href="#main-content"
        onClick={(e) => {
          e.preventDefault();
          document.getElementById("main-content")?.focus();
        }}
      >
        Skip to content
      </a>
      <aside className={"app-sidebar " + (menu ? "menu-open" : "")}>
        <a
          className="brand-header"
          href="#/tenders"
          aria-label="Quarry tenders"
        >
          <IconStack2 className="brand-logo-icon" size={25} />
          <span className="brand-title">Quarry</span>
        </a>
        <nav aria-label="Main navigation">
          <ul className="nav-list">
            {navigation.map(([name, key, Icon]) => (
              <li
                key={key}
                className={"nav-item " + (key === section[1] ? "active" : "")}
              >
                <a
                  href={`#/${key}`}
                  aria-current={key === section[1] ? "page" : undefined}
                  onClick={() => setMenu(false)}
                >
                  <Icon size={18} />
                  <span className="nav-text">{name}</span>
                </a>
              </li>
            ))}
          </ul>
        </nav>
        {user.operator ? (
          <div className="sidebar-footer">Local operator</div>
        ) : (
          <div className="sidebar-footer account-footer">
            <span className="account-email" title={user.email}>
              {user.email}
            </span>
            <small>
              {user.role === "admin"
                ? "Platform administrator"
                : "Your workspace"}
            </small>
            <button
              disabled={loggingOut}
              onClick={async () => {
                setLoggingOut(true);
                setLogoutError(null);
                try {
                  await accountSend("/auth/logout");
                  setAccountSession(client, null);
                  window.location.hash = "/tenders";
                } catch (failure) {
                  setLogoutError(
                    failure instanceof Error
                      ? failure
                      : new Error("Unable to sign out."),
                  );
                } finally {
                  setLoggingOut(false);
                }
              }}
            >
              {loggingOut ? "Signing out…" : "Sign out"}
            </button>
          </div>
        )}
      </aside>
      <main className="app-main" id="main-content" tabIndex={-1}>
        <header className="mobile-header">
          <a href="#/tenders" className="mobile-header-brand">
            <IconStack2 size={23} />
            Quarry
          </a>
          <button
            aria-expanded={menu}
            aria-label={menu ? "Close menu" : "Open menu"}
            onClick={() => setMenu(!menu)}
          >
            <IconMenu2 size={18} />
            Menu
          </button>
        </header>
        <ErrorMessage error={logoutError} />
        {section[1] === "monitoring" ? (
          <Monitoring user={user} />
        ) : section[1] === "admin" && user.role === "admin" ? (
          <Administration currentUser={user} />
        ) : section[1] === "notifications" ? (
          <AccountNotifications />
        ) : section[1] === "sources" ? (
          user.role === "admin" ? (
            <Sources />
          ) : (
            <AccountSources />
          )
        ) : section[1] === "campaigns" ? (
          <Campaigns />
        ) : section[1] === "matching" ? (
          <Matching {...props} />
        ) : section[1] === "activity" ? (
          <Activity />
        ) : user.role !== "admin" ? (
          path[1] ? (
            <AccountTenderDetails
              id={path[1]}
              campaignId={path[2]}
              back={() => {
                window.location.hash = "/tenders";
              }}
            />
          ) : (
            <AccountTenders
              onOpen={(id, campaignId) => {
                window.location.hash = `/tenders/${encodeURIComponent(id)}/${encodeURIComponent(campaignId)}`;
              }}
            />
          )
        ) : path[1] ? (
          <TenderDetails
            key={path[1]}
            id={path[1]}
            back={() => {
              window.location.hash = "/tenders";
            }}
            {...props}
          />
        ) : (
          <Tenders {...props} />
        )}
      </main>
    </>
  );
}
