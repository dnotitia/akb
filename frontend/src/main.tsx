import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AppRoutes } from "@/app-routes";
import "./index.css";

// Vite dispatches `vite:preloadError` on window when a dynamically-imported
// chunk fails to load — the classic stale-deploy symptom: a client holding
// an old index.html requests a chunk hash a newer build has removed, so the
// fetch 404s (or, with the old nginx config, returned index.html and failed
// to parse as a module). Recover by reloading once onto the fresh build.
// (The event type ships with vite/client.)
const PRELOAD_RELOAD_AT = "akb.preloadReloadAt";
window.addEventListener("vite:preloadError", (event) => {
  // Loop guard: if we already reloaded for this in the last 10s, the chunk
  // is genuinely broken (not merely stale) — let the error surface to the
  // ErrorBoundary instead of reloading forever.
  const last = Number(sessionStorage.getItem(PRELOAD_RELOAD_AT) || 0);
  if (Date.now() - last < 10_000) return;
  sessionStorage.setItem(PRELOAD_RELOAD_AT, String(Date.now()));
  event.preventDefault(); // we're handling it — suppress the default throw
  window.location.reload();
});

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 30_000, retry: 1, refetchOnWindowFocus: false },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AppRoutes />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>
);
