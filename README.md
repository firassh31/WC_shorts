# WCNET — Autonomous World Cup Short-Form Video Network

A self-driving Python ecosystem that, with **zero human input after setup**:

1. discovers live World Cup matches,
2. hunts and resolves their live YouTube broadcast feeds,
3. detects highlight events in real time (goals, cards, VAR **and** subjective
   moments like "screamer" / "unbelievable save"),
4. clips a precise 9:16 vertical short around each event using ultra-fast
   FFmpeg stream-copying, and
5. syndicates the finished clip to **YouTube Shorts** (with optional Instagram
   Reels) concurrently.

> ⚠️ **Rights notice.** Re-publishing live broadcast footage is governed by
> FIFA/broadcaster copyright. Run this only against feeds you are licensed to
> use, or content you own. The infrastructure is content-agnostic; compliance
> is your responsibility.

---

## Architecture

```
WC/
├── main.py                      # entrypoint:  `python main.py [doctor]`
├── requirements.txt
├── .env.example                 # every credential + tuning knob
└── wcnet/
    ├── config.py                # Stage 1a — pydantic-settings config model
    ├── state.py                 # Stage 1b — SQLite thread-safe dedup guard
    ├── models.py                # shared data standards (Fixture/Event/Clip)
    ├── captioning.py            # dynamic captions + event-tailored hashtags
    ├── logging_setup.py
    ├── utils/retry.py           # @resilient / @safe resiliency primitives
    ├── discovery/
    │   ├── football_api.py      # Stage 2a — API-Football live/upcoming selector
    │   └── youtube_hunter.py    # Stage 2b — YT live search + yt-dlp resolve
    ├── monitor/
    │   └── event_detector.py    # Stage 3  — dual-layer ticker (A: data, B: NL)
    ├── capture/
    │   └── clipper.py           # Stage 4  — rolling buffer + 9:16 render
    ├── publish/
    │   ├── base.py              # Publisher interface + PublishResult
    │   ├── youtube.py           # Stage 5a — Shorts resumable chunked upload
    │   ├── r2.py                #            Cloudflare R2 public-URL host
    │   ├── instagram.py         # Stage 5b — Graph API Reels (optional)
    │   └── syndicator.py        # Stage 5c — concurrent fan-out
    └── orchestrator.py          # the conductor wiring all stages together
```

### Data flow

```
 discovery loop ──► YouTube hunter ──► StreamRecorder (rolling .ts buffer, -c copy)
       │                                      ▲
       └──► MatchMonitor ticker ──► event ───┘ select [T-pre, T+post]
                  (Layer A + B)        │          │
                                       ▼          ▼
                                  ClipFactory: concat-copy + trim + 9:16 re-encode
                                       │
                                       ▼
                          Syndicator ─┬─► YouTube Shorts
                                      └─► Instagram Reels (optional, concurrent)
```

### Key design decisions

| Concern | Approach |
|---|---|
| **Config** | One validated `pydantic-settings` model; nothing else reads `os.environ`. |
| **Dedup** | SQLite (WAL) + process lock. `register_event()` is an atomic claim — first observer wins, so no event/clip is ever processed or posted twice, even across threads or restarts. |
| **Low CPU capture** | A single `ffmpeg -c copy -f segment` process writes timestamped 2-second `.ts` segments into a pruned DVR ring. Only the final ~30 s clip is re-encoded. |
| **Stream resilience** | Recorder uses FFmpeg `-reconnect`; on full drop it auto-respawns after a backoff. |
| **9:16 render** | `blur_pad` (fitted feed over a blurred fill) by default, or `center_crop`. Output is web-ready H.264/AAC + `faststart`, `<60 s` enforced in config. |
| **Autonomy** | YouTube OAuth token cached to disk. No human prompts after first auth. |
| **Concurrency** | Per-match recorder + ticker threads; a global 4-worker pool caps simultaneous render/publish jobs; publishers fan out via their own pool. |

---

## Setup

### 1. System dependency — FFmpeg
```powershell
winget install Gyan.FFmpeg     # Windows
# or: choco install ffmpeg-full
```
```bash
sudo apt-get install -y ffmpeg # Debian/Ubuntu
```

### 2. Python environment
```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate     Linux/mac:  source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Credentials
```bash
cp .env.example .env          # then edit with real values
mkdir secrets                 # drop your Google client_secrets.json here
```
- **YouTube** (required): put the downloaded `client_secrets.json` at the path in
  `YOUTUBE_CLIENT_SECRETS`. OAuth covers both Search and Shorts upload — no API
  key needed. First run opens a browser once; the token is then cached.
- **Football data** (required): a RapidAPI / sports-API key in `FOOTBALL_API_KEY`.
- **Instagram** (optional): a long-lived Page/IG token + the IG Business user id.
- **R2** (only if Instagram is used): bucket, S3 keys, endpoint, and a **public**
  base URL bound to the bucket.

### 4. Preflight & run
```bash
python main.py doctor         # validates ffmpeg + every credential
python main.py                # go live, 24/7
```

Run it forever under a supervisor:
- **Linux**: a `systemd` unit with `Restart=always`.
- **Windows**: NSSM or Task Scheduler (At startup, restart on failure).
- **Docker**: base on `python:3.12-slim`, `apt-get install ffmpeg`, `CMD ["python","main.py"]`.

---

## Tuning (env)

| Var | Default | Meaning |
|---|---|---|
| `CAPTURE_PRE_SECONDS` | 18 | build-up captured before the trigger |
| `CAPTURE_POST_SECONDS` | 10 | footage after the trigger |
| `BUFFER_WINDOW_SECONDS` | 240 | rolling DVR window kept on disk |
| `SEGMENT_SECONDS` | 2 | buffer segment granularity |
| `RENDER_MODE` | `blur_pad` | `blur_pad` or `center_crop` |
| `FOOTBALL_POLL_SECONDS` | 60 | schedule/event poll cadence (rate-limit floor 15s) |

`pre + post` is validated to stay **under 60 s** at startup.

---

## Notes & extension points

- **Layer B commentary source.** Out of the box, contextual NL detection mines
  API-Football's own `comments` field. For richer subjective detection, supply a
  `CommentaryProvider` (see `monitor/event_detector.py`) backed by a live
  text-commentary API and pass it into `MatchMonitor`.
- **Detection latency.** A clip is centered on the *wall-clock instant the event
  is detected*, which trails the live action by your poll interval. Lower
  `FOOTBALL_POLL_SECONDS` (respecting your API quota) for tighter sync, and the
  generous `CAPTURE_PRE_SECONDS` buffer absorbs the rest.
- **Quota.** YouTube `search.list` costs 100 units/call — the hunter stops at
  the first query that yields enough candidates and caches the active stream per
  fixture to avoid re-hunting.
```
