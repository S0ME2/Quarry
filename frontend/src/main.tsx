import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import "./fonts.css";
import "./clearwork.css";
import "./app.css";
import "./sections.css";
import "./account.css";
const client = new QueryClient({
  defaultOptions: { queries: { retry: false, refetchOnReconnect: true } },
});
createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={client}>
    <App />
  </QueryClientProvider>,
);
