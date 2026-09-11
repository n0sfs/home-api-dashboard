// Minimal service worker: only caches the app shell (the dashboard page itself),
// never the /api/* data — this is a live-data dashboard, so serving stale cached
// API responses while "offline" would be actively misleading rather than helpful.
// Registration only succeeds in a secure context (https:, or the browser's
// localhost exemption) — see index.html's registration snippet and the README
// for why a phone on the LAN won't get this even though the icon/install still works.

const CACHE_NAME = "home-dashboard-shell-v1";
const SHELL_URL = "/";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.add(SHELL_URL))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.mode !== "navigate" || url.pathname !== SHELL_URL) {
    return; // let everything else (including all /api/* calls) go straight to network
  }

  event.respondWith(
    fetch(event.request)
      .then((response) => {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(SHELL_URL, copy));
        return response;
      })
      .catch(() => caches.match(SHELL_URL))
  );
});
