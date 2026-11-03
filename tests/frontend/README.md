# Loading and weak-network checks

Run the frontend regression suite with `pnpm test:frontend`. It includes real
loopback HTTP requests covering dropped connections, HTTP 503, stalled response
headers and bodies, terminal 404/invalid JSON, bounded retries, cache recovery,
and continuing to load other years after one year fails.

To inspect the production build under a constrained connection:

```sh
pnpm build
PORT=42731 LATENCY_MS=150 KIB_PER_SECOND=128 node tests/frontend/serve-network.mjs
```

Open `http://127.0.0.1:42731/`. All responses share 128 KiB/s (about 1 Mbps),
with a 150 ms delay per request, gzip where appropriate, and no browser caching.
The JSON lines on stdout record request start, duration, response size and status.
An optional positional argument selects a different build directory for comparison.
The server binds only to loopback. Stop it with Ctrl+C.

To simulate temporary or persistent failures:

```sh
# One failed historical-year request; should recover automatically.
PORT=42732 FAIL_PATTERN=year_2025 node tests/frontend/serve-network.mjs

# Exhaust automatic retries; other years should load and Retry should recover.
PORT=42733 FAIL_PATTERN=year_2025 FAIL_COUNT=3 node tests/frontend/serve-network.mjs

# Initial manifest failure; Try again should recover after the three failures.
PORT=42734 FAIL_PATTERN=manifest FAIL_COUNT=3 node tests/frontend/serve-network.mjs
```

`FAIL_ALWAYS=1` keeps the matching resource unavailable. Restart the server to
reset its failure counters. The client makes at most three attempts with 500 ms
and 1,000 ms backoff, and a 15 s timeout per attempt including body download.
404 and invalid JSON are not automatically retried. A user retry starts a new
bounded set of attempts; successfully loaded years are reused.

## 2026-11-03 verification

Compared the production build at `2dfc7c5b` with this optimization using a fresh
origin for each build, 150 ms latency and shared 128 KiB/s bandwidth. In one
local run, the current-year JSON completed 3.33 s after the initial HTML request
before the changes and 2.99 s after. The manifest request started at 2.36 s and
1.92 s respectively. Restoring Vite's dependency preload removes serial script
discovery waits.

These are server request timings, **not first-paint or Lighthouse scores**.
External map tiles and the baseline's Google Fonts were outside the limiter.
The new fonts share the local bandwidth budget, so this is not a controlled
whole-page speed benchmark: the large Mapbox script completed at 8.33 s before
and 8.62 s after in that same run. Its payload was left intact to preserve map
features. No general percentage speedup is claimed.

The normal page has no external Google Fonts CSS dependency after the change.
It requests five local font files (about 115 KB total) on the dashboard; other
language subsets and weights remain demand-loaded. The entry CSS increases
from about 3.7 KB to 4.7 KB gzip, replacing the two external stylesheets. Font
files are unchanged IBM Plex WOFF2 files with the original Unicode ranges and
`font-display: swap`; they are not embedded in CSS. Vite hashes their filenames
for cacheable deployment.

Browser checks passed for automatic 503 recovery, manual recovery after three
failures, continued loading of 2024 while 2025 failed, initial-page recovery,
year switching, weekly filtering and activity pagination. At 1920×1080, sampled
sidebar text and map dimensions exactly matched the baseline. The 390 px mobile
view had no horizontal overflow; at 3840 px the sidebar remained 416 px and map
864 px wide. The running records and generated data were unchanged.
