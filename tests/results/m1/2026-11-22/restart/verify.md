| Check | Gate | Measured | Result |
|---|---|---|---|
| publish sessions at the receiver | 1 | 2 | **FAIL** |
| viewer sessions (no reconnect) | 1 | 1 | pass |
| viewer disconnected only after the broadcast ended | True | False | **FAIL** |
| viewer warnings | 0 | 0 | pass |
| video PTS strictly increasing | 0 | 0 non-increasing of 864 | pass |
| video gaps over 1.5 frames | 0 | 0 (max 34.0 ms) | pass |
| audio gaps over 25 ms | 0 | 0 (max 1.0 ms) | pass |
| footage frames in source order, none twice | 0 repeated, 0 out of order | 713 frames, 0 repeated, 0 out of order | pass |
| recorded switches match the playout log | <= 1 frame | 1 switches, max 0 frame off | pass |

**Verdict: fail.**
