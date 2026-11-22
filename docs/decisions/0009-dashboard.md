# 0009: Dashboard

## Decision
Next.js (TypeScript) talking only to the public API, built in M7 once the API has settled. Until then, operational views are API endpoints, logs and Grafana.

## Consequences
The API is the only interface; the dashboard never touches Postgres or MediaMTX directly. The live preview comes from MediaMTX's HLS output for the broadcast's path, served through the API: the API checks the viewer owns the broadcast, then proxies the playlist and segments. MediaMTX's own ports stay private. The route and its caching are specified with the M7 API work, not before. A simpler server-rendered dashboard (FastAPI + HTMX) is the fallback if a second toolchain is too much to maintain alone.

## Revisit if
Reuben prefers one language on the server; then build the dashboard as server-rendered pages in FastAPI.
