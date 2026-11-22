# Issue and PR sequence

Small PRs, each with a working demonstration and its evidence. Create these as issues once this plan is approved; labels `milestone:M1`…`M7`, `area:server|ios|protocol|dashboard|infra`, `needs-device`.

| # | PR / issue | Milestone | Acceptance criteria |
|---|---|---|---|
| 1 | **Spike S1: continuous playout** ([spec](spike-s1-playout.md)) | M1 | Every gate row in the spec measured and recorded in `RESULTS.md`; pass or a written fallback decision |
| 2 | Repo skeleton: Compose (Postgres, MediaMTX), Python package layout, CI (ruff, mypy, pytest, SQL tests) | M1 | `docker compose up` starts both services; CI green, including `tests/sql/cross_account.sql` |
| 3 | `tools/testclip`, `tools/fake-phone` (real-time, pause/reorder/duplicate/corrupt), `tools/verify` | M1 | `verify` passes on a clean upload and fails, with the right reason, on a deliberately broken stream |
| 4 | Segment store + API subset (create, init, segment, epoch end, finish, status) with the durable write sequence | M1 | Unit tests for idempotency (`201`/`200`/`409`), checksum `422`, crash between rename and commit leaves no acknowledged-but-missing segment |
| 5 | Playout service from S1: `starting/live/holding/finishing`, checkpoints | M1 | `tests/media-continuity/m1_gaps.sh` passes; evidence in `tests/results/m1/` |
| 6 | iOS capture spike (Swift): capture → AVAssetWriter fMP4 → disk + manifest | M2 `needs-device` | Segment validity + reconstruction tests pass on pulled files; 2-minute sample committed |
| 7 | iOS interruptions, storage reserve, thermal/Low Power observers | M2 `needs-device` | Condition matrix filled in `ios-capture-findings.md`, each row observed |
| 8 | Auth: pairing, device tokens, ownership checks, revocation | M3 | Cross-account API test: every endpoint `404` for the wrong account; revoked `401` |
| 9 | Media validation, limits, `507`, rate limits, path safety | M3 | Fuzzed/truncated segments `422`; storage cap `507`; traversal attempts rejected |
| 10 | iOS upload manager + background URLSession + reconcile | M3 `needs-device` | iPhone airplane-toggle run passes; capture never paused (frame-counter check) |
| 11 | Chaos harness (netem namespace, server kill, reorder/dup/corrupt) | M3 | 2-hour chaos run: per-segment ledger shows every segment stored once, no lost acked segment |
| 12 | Catch-up: speed stage, ramps, modes, drain wait | M4 | Bench catch-up test at 1.25× and 1.5×; tone pitch check; return to live within prediction |
| 13 | Restart recovery: playout checkpoint resume, client restart mid-outage | M4 | ≤ 1 s repeated, nothing skipped, one session or one reconnect < 5 s |
| 14 | Phone status UI (Live / Holding / Catching up, behind-live, backlog arithmetic) | M4 `needs-device` | Shown during the E2E run |
| 15 | **End-to-end proof: iPhone → relay → OBS** (`tests/e2e/phone-outage.md` + recorder script) | M4 `needs-device` | All 10 steps confirmed by the script; verify passes against the phone's manifest; evidence in `tests/results/m4/` |
| 16 | Destinations + forwarders + key encryption | M5 | Local isolation test; keys absent from logs/API |
| 17 | Real Twitch + YouTube run | M5 | 45-min run with outage and forced playout restart; platform behaviour recorded |
| 18+ | M6 app, M7 dashboard and release | M6, M7 | As in [milestones](milestones.md) |

## Evidence required per milestone

| Milestone | Complete only when the repository has |
|---|---|
| M1 | S1 `RESULTS.md`; `m1_gaps.sh` passing in CI or with a committed run report; receiver log showing one session |
| M2 | ffprobe + reconstruction reports from real iPhone segments; filled condition matrix; sample media |
| M3 | Chaos ledger from a 2-hour run; storage-full and late-upload test output; cross-account test output; iPhone run results |
| M4 | Bench catch-up reports at 1.25× and 1.5×; restart test output; the E2E run folder (state log, verify report, manifest export, step notes) |
| M5 | Isolation test output; real-platform run notes with screenshots of platform health |
| M6 | Notes from an unassisted field broadcast |
| M7 | Third-party install notes; port scan; load-limit test output |
