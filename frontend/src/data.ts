import { useEffect, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { request, send } from "./api";
export function useResource<T>(
  path: string | null,
  running?: (value: T) => boolean,
) {
  return useQuery({
    queryKey: [path],
    enabled: !!path,
    queryFn: ({ signal }) => request<T>(path!, { signal }),
    staleTime: 30_000,
    refetchOnWindowFocus: true,
    retry: false,
    refetchInterval: running
      ? (q) => (q.state.data && running(q.state.data) ? 1200 : false)
      : false,
  });
}
export function useAction<T = unknown>(after?: (result: T) => void) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      path,
      body,
      method,
    }: {
      path: string;
      body: unknown;
      method?: string;
    }) => send<T>(path, body, method),
    onSuccess: async (result) => {
      after?.(result);
      await client.invalidateQueries();
    },
  });
}

export function useCompletedOperation(
  operation: import("./api").Operation | undefined,
) {
  const client = useQueryClient();
  const id = operation?.id,
    status = operation?.status;
  useEffect(() => {
    if (
      id &&
      status &&
      !["queued", "running", "awaiting_approval"].includes(status)
    )
      void client.invalidateQueries({
        predicate: (q) => !String(q.queryKey[0]).startsWith("/operations/"),
      });
  }, [id, status, client]);
}
export function useDebounced<T>(value: T, delay = 180) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}
