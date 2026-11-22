# 0009: Dashboard

## Decision
Next.js (TypeScript) talking only to the public API, built in M7 once the API has settled. Until then, operational views are API endpoints, logs and Grafana.

## Consequences
The API is the only interface; the dashboard never touches Postgres. A simpler server-rendered dashboard (FastAPI + HTMX) is the fallback if a second toolchain is too much to maintain alone.

## Revisit if
Reuben prefers one language on the server; then build the dashboard as server-rendered pages in FastAPI.
