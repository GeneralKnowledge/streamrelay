| Check | Gate | Measured | Result |
|---|---|---|---|
| publish sessions at the receiver | 1 | 1 | pass |
| viewer sessions (no reconnect) | 1 | 1 | pass |
| viewer disconnected only after the broadcast ended | True | True | pass |
| viewer warnings | 0 | 0 | pass |
| video PTS strictly increasing | 0 | 0 non-increasing of 36451 | pass |
| video gaps over 1.5 frames | 0 | 0 (max 34.0 ms) | pass |
| audio gaps over 25 ms | 0 | 0 (max 1.0 ms) | pass |
| footage frames in source order, none twice | 0 repeated, 0 out of order | 25020 frames, 0 repeated, 0 out of order | pass |
| recorded switches match the playout log | <= 1 frame | 8 switches, max 0 frame off | pass |
| gap 1: holding visible after uploads stopped | <= 10 s | 6.2 s | pass |
| gap 1: footage back after uploads resumed | <= 12 s | 2.3 s | pass |
| gap 2: holding visible after uploads stopped | <= 10 s | 6.3 s | pass |
| gap 2: footage back after uploads resumed | <= 12 s | 6.3 s | pass |
| gap 3: holding visible after uploads stopped | <= 10 s | 6.3 s | pass |
| gap 3: footage back after uploads resumed | <= 12 s | 6.2 s | pass |

**Verdict: pass.**
