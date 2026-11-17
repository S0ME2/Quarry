import { useEffect, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { IconStack2 } from "@tabler/icons-react";
import {
  accountRequest,
  accountSend,
  AccountError,
  setAccountSession,
  type User,
} from "../account";
import { ErrorMessage, Field } from "../ui";

export function AccountGate({
  children,
}: {
  children: (user: User) => ReactNode;
}) {
  const client = useQueryClient();
  const session = useQuery({
    queryKey: ["session"],
    queryFn: async () => {
      try {
        return await accountRequest<User>("/auth/me");
      } catch (failure) {
        if (failure instanceof AccountError && failure.status === 401)
          return null;
        throw failure;
      }
    },
    retry: false,
  });
  useEffect(() => {
    const expired = () => setAccountSession(client, null);
    window.addEventListener("quarry-session-expired", expired);
    return () => window.removeEventListener("quarry-session-expired", expired);
  }, [client]);
  if (session.isPending)
    return (
      <main className="auth-layout">
        <p role="status">Opening your workspace…</p>
      </main>
    );
  if (session.data) return children(session.data);
  if (session.data === null)
    return (
      <SignIn
        onAuthenticated={(user) => {
          setAccountSession(client, user);
        }}
      />
    );
  return (
    <main className="auth-layout">
      <section className="auth-panel">
        <h1>Workspace unavailable</h1>
        <ErrorMessage error={session.error} />
        <button onClick={() => void session.refetch()}>Try again</button>
      </section>
    </main>
  );
}

export function SignIn({
  onAuthenticated,
}: {
  onAuthenticated: (user: User) => void;
}) {
  const [registering, setRegistering] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  return (
    <main className="auth-layout">
      <section className="auth-panel" aria-labelledby="auth-title">
        <div className="auth-brand">
          <IconStack2 size={28} aria-hidden="true" />
          <span>Quarry</span>
        </div>
        <h1 id="auth-title">
          {registering ? "Create your account" : "Sign in to Quarry"}
        </h1>
        <p>Find tender opportunities that match your business.</p>
        <form
          onSubmit={async (event) => {
            event.preventDefault();
            const form = new FormData(event.currentTarget);
            const body = {
              email: String(form.get("email")).trim(),
              password: String(form.get("password")),
            };
            setPending(true);
            setError(null);
            try {
              if (registering) await accountSend<User>("/auth/register", body);
              const user = await accountSend<User>("/auth/login", body);
              window.location.hash = registering ? "/monitoring" : "/tenders";
              onAuthenticated(user);
            } catch (failure) {
              setError(
                failure instanceof Error
                  ? failure
                  : new Error("Unable to sign in."),
              );
            } finally {
              setPending(false);
            }
          }}
        >
          <Field label="Email address">
            {(id) => (
              <input
                id={id}
                name="email"
                type="email"
                autoComplete="email"
                maxLength={254}
                required
              />
            )}
          </Field>
          <Field
            label="Password"
            hint={registering ? "Use at least 12 characters." : undefined}
          >
            {(id) => (
              <input
                id={id}
                name="password"
                type="password"
                autoComplete={registering ? "new-password" : "current-password"}
                minLength={registering ? 12 : undefined}
                maxLength={1024}
                required
              />
            )}
          </Field>
          <ErrorMessage error={error} />
          <button className="primary auth-submit" disabled={pending}>
            {pending
              ? "Please wait…"
              : registering
                ? "Create account"
                : "Sign in"}
          </button>
        </form>
        <div className="auth-switch">
          <span>
            {registering ? "Already have an account?" : "New to Quarry?"}
          </span>
          <button
            className="text-button"
            disabled={pending}
            onClick={() => {
              setRegistering(!registering);
              setError(null);
            }}
          >
            {registering ? "Sign in instead" : "Create an account"}
          </button>
        </div>
      </section>
    </main>
  );
}
