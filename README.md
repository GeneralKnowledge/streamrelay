# StreamRelay

Resilient mobile livestreaming: a network outage becomes a delay in the broadcast, not the end of it. Working name.

Status: planning. Start with the [implementation plan](docs/README.md).

| Component | What |
|---|---|
| StreamRelay Mobile | iPhone app (Android later): records 2 s segments locally whatever the network does, uploads when it can |
| StreamRelay Server | Receives segments, plays one continuous stream out with a holding screen and catch-up, fans out to Twitch, YouTube and custom RTMP |
| StreamRelay Control | Web dashboard |
