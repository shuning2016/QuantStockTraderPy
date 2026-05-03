# Guardian Cron — cron-job.org Setup

The `/api/cron/guardian` endpoint must be triggered externally because Vercel's Hobby plan
only supports cron jobs that fire **at most once per day per path**.  
cron-job.org is free, requires no compute, and supports per-minute scheduling.

---

## One-time setup

1. Create a free account at **https://cron-job.org**
2. Go to **Cronjobs → Create cronjob**

---

## Timezone note

**cron-job.org schedules in UTC by default.** All hours below are UTC.
US Eastern Time offsets: ET = UTC-5 (winter) / UTC-4 (summer, Mar–Nov).
Market hours 9:30–16:00 ET = **13:30–20:00 UTC** (summer) / **14:30–21:00 UTC** (winter).
The hours listed below use summer (EDT) offsets. Adjust by +1 hour in November–March.

---

## Job 1 — Market-hours check (every 5 minutes, Mon–Fri 9:30–16:00 ET)

| Field | Value |
|---|---|
| URL | `https://<your-vercel-app>.vercel.app/api/cron/guardian` |
| Execution schedule | Custom |
| Days | Mon, Tue, Wed, Thu, Fri |
| Hours (UTC) | 13, 14, 15, 16, 17, 18, 19 |
| Minutes | 0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55 |
| Request method | GET |
| Request headers | `Authorization: Bearer <CRON_SECRET>` |
| Timeout | 120 s |

> `CRON_SECRET` must match the `CRON_SECRET` environment variable set in your Vercel project.

Note: 13:00 UTC = 9:00 ET so the first fires at 13:30 effectively (market opens 9:30).
Hour 19 UTC = 15:xx ET, which covers through market close at 16:00 ET.
Adjust the hours list if you want to cover pre-market (e.g. add hours 9–12 UTC).

---

## Job 2 — Off-hours check (once per hour, Mon–Fri outside market hours + weekends)

| Field | Value |
|---|---|
| URL | `https://<your-vercel-app>.vercel.app/api/cron/guardian` |
| Execution schedule | Custom |
| Days | Mon, Tue, Wed, Thu, Fri, Sat, Sun |
| Hours (UTC) | 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 20, 21, 22, 23 |
| Minutes | 0 |
| Request method | GET |
| Request headers | `Authorization: Bearer <CRON_SECRET>` |
| Timeout | 120 s |

---

## How the endpoint handles duplicate triggers

The endpoint sets a Redis lock (`guardian_lock`, TTL 90 s) on entry.  
If two jobs fire within 90 seconds of each other (e.g. overlap between Job 1 and Job 2),
the second call returns `{"status": "skipped", "reason": "guardian already running"}` with
HTTP 200 — no harm done.

---

## Environment variable reminder

Make sure `CRON_SECRET` is set in **Vercel → Settings → Environment Variables**:

```
CRON_SECRET=<random-secret-string>
```

The guardian endpoint verifies this via `_verify_cron()` (same check used by all other
Vercel cron routes). Requests without the correct header return HTTP 401.
