import {
  useEffect,
  useId,
  useRef,
  type ReactNode,
  type FormEvent,
} from "react";
import type { UseQueryResult } from "@tanstack/react-query";
import { IconX, IconRefresh, IconAlertCircle } from "@tabler/icons-react";
import { readable, type Page } from "./api";
export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: (id: string) => ReactNode;
}) {
  const id = useId();
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {children(id)}
      {hint && <small>{hint}</small>}
    </div>
  );
}
export function State({ value }: { value: string | null | undefined }) {
  const text = value ? readable(value) : "Not provided";
  return (
    <span
      className={`state ${value && /^(match|open|active|enabled|verified|completed|activated|healthy|accepted|ready|relevant|delivered)$/i.test(value) ? "positive" : value && /fail|invalid|error|disabled|unsupported|not_relevant/i.test(value) ? "negative" : value && /unsure|stale|pending|running|review|uncertain|blocked|checking/i.test(value) ? "warning" : ""}`}
    >
      {text}
    </span>
  );
}
export function ErrorMessage({ error }: { error: Error | null | undefined }) {
  return error ? (
    <div role="alert" className="error-banner">
      <IconAlertCircle size={18} />
      <span>{error.message}</span>
    </div>
  ) : null;
}
export function QueryState({
  query,
  empty,
  children,
}: {
  query: UseQueryResult<unknown>;
  empty?: boolean;
  children?: ReactNode;
}) {
  if (query.isPending)
    return (
      <div className="loading" role="status">
        <span className="spinner" />
        Loading…
      </div>
    );
  if (query.isError)
    return (
      <div className="panel">
        <ErrorMessage error={query.error} />
        <button onClick={() => void query.refetch()}>
          <IconRefresh size={16} />
          Try again
        </button>
      </div>
    );
  if (empty)
    return (
      <div className="empty">
        <h2>No records yet</h2>
        <p>{children || "Records will appear here when available."}</p>
      </div>
    );
  return null;
}
export function Panel({
  title,
  children,
  className = "",
}: {
  title?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={"panel " + className}>
      {title && <h2>{title}</h2>}
      {children}
    </section>
  );
}
export function Disclosure({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <details className="disclosure">
      <summary>{title}</summary>
      <div className="disclosure-body">{children}</div>
    </details>
  );
}
export function Diagnostics({
  value,
  label = "Technical details",
}: {
  value: unknown;
  label?: string;
}) {
  return (
    <Disclosure title={label}>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </Disclosure>
  );
}
export function Dialog({
  title,
  onClose,
  children,
  wide = false,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const id = useId();
  useEffect(() => {
    const node = ref.current;
    const previous = document.activeElement as HTMLElement | null;
    node?.showModal();
    return () => {
      node?.close();
      previous?.focus();
    };
  }, []);
  return (
    <dialog
      ref={ref}
      aria-labelledby={id}
      className={wide ? "wide-dialog" : ""}
      onCancel={onClose}
      onClick={(e) => {
        if (e.target === e.currentTarget) {
          const rect = e.currentTarget.getBoundingClientRect();
          if (
            e.clientX < rect.left ||
            e.clientX > rect.right ||
            e.clientY < rect.top ||
            e.clientY > rect.bottom
          )
            onClose();
        }
      }}
    >
      <div className="dialog-head">
        <h2 id={id}>{title}</h2>
        <button type="button" aria-label="Close dialog" onClick={onClose}>
          <IconX size={18} />
        </button>
      </div>
      {children}
    </dialog>
  );
}
export function Table({
  name,
  headers,
  children,
}: {
  name: string;
  headers: ReactNode[];
  children: ReactNode;
}) {
  return (
    <div
      className="table-scroll"
      role="region"
      aria-label={`${name} table`}
      tabIndex={0}
    >
      <table aria-label={name}>
        <thead>
          <tr>
            {headers.map((h, i) => (
              <th key={i} scope="col">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}
export function Pager({
  page,
  offset,
  onChange,
  limit = 25,
}: {
  page: Page<unknown>;
  offset: number;
  onChange: (n: number) => void;
  limit?: number;
}) {
  return (
    <div className="pagination-bar">
      <span>
        {page.total
          ? `${offset + 1}–${Math.min(offset + limit, page.total)} of ${page.total}`
          : "0 records"}
      </span>
      <div className="actions">
        <button
          disabled={offset === 0}
          onClick={() => onChange(Math.max(0, offset - limit))}
        >
          Previous
        </button>
        <button
          disabled={offset + limit >= page.total}
          onClick={() => onChange(offset + limit)}
        >
          Next
        </button>
      </div>
    </div>
  );
}
export function Form({
  children,
  onSubmit,
  className = "",
}: {
  children: ReactNode;
  onSubmit: (data: FormData) => void;
  className?: string;
}) {
  return (
    <form
      className={className}
      onSubmit={(event: FormEvent<HTMLFormElement>) => {
        event.preventDefault();
        onSubmit(new FormData(event.currentTarget));
      }}
    >
      {children}
    </form>
  );
}
export function TextField({
  label,
  name,
  value,
  required = false,
  type = "text",
  min,
  max,
  step,
  placeholder,
}: {
  label: string;
  name: string;
  value?: string | number | null;
  required?: boolean;
  type?: string;
  min?: number;
  max?: number;
  step?: string;
  placeholder?: string;
}) {
  return (
    <Field label={label}>
      {(id) => (
        <input
          id={id}
          name={name}
          defaultValue={value ?? ""}
          type={type}
          required={required}
          min={min}
          max={max}
          step={step}
          placeholder={placeholder}
        />
      )}
    </Field>
  );
}
export function Check({
  label,
  name,
  checked = false,
  onChange,
}: {
  label: string;
  name?: string;
  checked?: boolean;
  onChange?: (checked: boolean) => void;
}) {
  return (
    <label className="check">
      <input
        type="checkbox"
        name={name}
        {...(onChange
          ? { checked, onChange: (e) => onChange(e.target.checked) }
          : { defaultChecked: checked })}
      />
      <span>{label}</span>
    </label>
  );
}
