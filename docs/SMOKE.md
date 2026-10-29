# make smoke on the VPS (07 §1.12)

The live tests (`tests/live/test_<key>_live.py`, `@pytest.mark.live(source=…)`) make one
real request per collector and tell whether the fixture-era selector still finds rows.
They run only on the VPS, in the compose `smoke` service (the app image plus pytest and
`tests/`), and write nothing but `collector_runs` rows (`status='skipped'`,
`meta.note='smoke'`; a host breaker ends a row `failed` / `host_blocked`).

## Go-live step: install the cron

`make smoke` does nothing by itself; the host's cron runs it. Once, on the VPS, after
`make up` (06 §7.1):

```
cd /path/to/clientScrapper
(crontab -l 2>/dev/null; make -s smoke-cron) | crontab -
crontab -l        # two lines: weekly and quarterly
```

`make smoke-cron` prints two lines for this checkout:

- weekly, Sunday 05:00 Tashkent: every live test but the `quarterly` ones;
- quarterly, the 1st of January/April/July/October 05:30 Tashkent: all of them (`FULL=1`).

Cron runs a line in the host's time zone (UTC on a stock Ubuntu VPS), so each line fires
hourly and runs only when `TZ=Asia/Tashkent date` says it is the hour (`SMOKE_TZ`
overrides the zone). The output goes to `data/smoke.log`.

## What the owner sees

The next morning's digest carries «Tekshiruv: ✓ N / ✗ M / oʻtkazildi K» (a skipped live
test is listed as skipped, never counted green) and names the changed sources. A weekly
run that stopped coming — none recorded for 8 days after an earlier one — is said as
«⚠️ Haftalik tekshiruv N kundan beri boʻlmadi». Before the first run there is no line at
all: installing the cron is this checklist's step, not something the digest can know.

## By hand

```
make smoke            # the weekly set, now
make smoke FULL=1     # every live test
```

A session that collected no live test yet (before the collectors bring theirs) exits 0
with «smoke: no live test yet».
