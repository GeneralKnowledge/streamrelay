# Phone sitting: M2 and M3

What the next iPhone sitting needs to prove, in order, with the time each takes. Everything here waits on a person with the phone; nothing is assumed passed.

## Before the sitting (laptop, no phone)

1. Build the app through the build queue: `cd ios && forge queue run --note streamrelay -- xtool dev build`. This is the first compile of `StreamRelayCapture` and `StreamRelayApp` (only `StreamRelayCore` compiles on Linux), so expect a round of compile fixes.
2. Start a server the phone can reach on the laptop's Tailscale address (enough for M2/M3; M4 uses Hetzner). Compose publishes the API on `127.0.0.1:8080` only, which the phone can't reach, so forward the tailnet port to it: `tailscale serve --bg --http=8080 127.0.0.1:8080` (or run `streamrelay-api` bound to the Tailscale address). The app accepts plain `http://` only for a Tailscale (100.64.0.0/10) or loopback address; anything else must be `https://`. Then `streamrelay-admin pair-code you@example.com`.

## At the sitting

**Needs:** one free app slot (StreamRelay is its own app; the first install spends one of the free team's 10 new App IDs for the week, then it is reused), Auto-Lock set to Never, a cable.

| # | Step | Proves | Minutes |
|---|---|---|---|
| 1 | Install (`xtool dev run`), pair with the code | Pairing, Keychain token | 3 |
| 2 | Airplane mode on, record 30 min, Stop, pull the folder (Files → StreamRelay → Recordings) | M2 reconstruction: `check_recording.py` passes, duration within 0.1 % | 32 |
| 3 | During step 2 at the 10-minute mark: lock the screen 20 s, unlock; switch to another app 20 s, back; call the phone (or Siri) | M2 matrix rows: lock, background, call. Each shows as an epoch ending `interrupted` and a new epoch | (in 2) |
| 4 | Turn Low Power Mode on for 2 minutes during step 2 | Low Power row: a new epoch at the lower bitrate, capture continues | (in 2) |
| 5 | Airplane mode off: the backlog uploads; watch "Waiting to upload" fall to 0 | M3: reconcile, catch-up order, finish | 5 |
| 6 | 20 min recording with airplane mode toggled every 1–3 min | M3 iPhone row: every segment stored once (`ledger` against the server), capture never paused (`check_recording.py` timing check) | 20 |
| 7 | With a backlog, force-quit (swipe up) mid-recording, relaunch | Force-quit row and M3 restart: completed segments survive, epoch closed `app_restart`, status shows every segment | 3 |
| 8 | Optional: thermal and low-storage rows (warm the phone; fill storage with a big file to under 500 MB) | The two remaining matrix rows | 10+ |

Results go in `tests/results/m2/<date>/` (pulled folders' `check_recording.py --json` output, a 2-minute sample of real segments, and `ios-capture-findings.md` filling the "Measured" column) and `tests/results/m3/<date>/iphone.md`.
