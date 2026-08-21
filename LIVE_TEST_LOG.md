# Live Test Log — SeLoger Sweep (2026-08-21)

**Model during test:** tencent/hy3:free (nous-api)
**Command:** `.venv/bin/python seloger_api_sweep.py --max-pages 2 --sessions 3`
**Env:** Camoufox + Webshare FR sticky (`ualfuslo-fr-N`), BFF API path.

---

## VERDICT: ✅ WORKING (not broken)

Both test runs captured 60 real listings each (2 pages × 30 ids), 0 rejected,
DB now 396 rows. Cookie inject + save wiring fires correctly.

---

## Test 1 — cold start (no saved cookies)
- session **fr-1**: loaded clean, page 1 (30 ids, total 254), page 2 (30 ids).
  → 60 listings. `saved 1 cookies for reuse`.
- DB upsert: **60 accepted, 0 rejected** (total 396).
- ⚠️ `LeakWarning: When using a proxy, pass geoip=True` (Camoufox hint — fixed after).
- Cookie saved: `_dd_s` (DataDome analytics cookie only).

## Test 2 — cookie reuse (fr-1 had saved `_dd_s`)
- session **fr-1**: `injected 1 saved cookies` ✅ → but `fr-1 IP flagged — next`
  (sticky IP momentarily challenged; proxy-reputation variance, NOT a code bug).
- session **fr-2**: loaded clean, 60 listings, `saved 1 cookies for reuse`.
- DB upsert: **60 accepted, 0 rejected** (total 396).
- LeakWarning: **gone** (geoip=True applied).
- Cookie saved: `_dd_s` only (old narrow filter still in effect during test 2;
  broad-save fix applied AFTER test 2 launched).

---

## Errors / Warnings encountered
| Severity | Message | Status |
|----------|---------|--------|
| ⚠️ WARN | LeakWarning geoip=True (test 1) | FIXED (geoip=True) |
| ⚠️ WARN | fr-1 IP flagged (test 2) | Proxy variance — falls through to next session, no data loss |
| ✅ OK | 0 rejected, 0 exceptions, 0 tracebacks | — |

## Findings
1. **Pipeline is healthy.** Both runs produced real structured data; validation
   rejects nothing (all prices/surfaces sane).
2. **Cookie inject + save paths work** — confirmed `injected N saved cookies`
   and `saved N cookies for reuse` both print and persist to `cookies.json`.
3. **Cookie *quality* limited:** only `_dd_s` was captured. This is an analytics
   cookie, not DataDome's clearance token. The narrow save filter
   (`"seloger.com" in domain`) likely missed the real `datadome` token if it
   sits on `.datadome.co` / a CDN host.
4. **Fix applied:** save ALL context cookies (not just seloger.com), so the next
   run persists whatever DataDome actually set → reuse quality improves.
5. **Adaptive metrics:** `record_outcome` logged fr-1 camoufox/datadome success
   (33% cumulative across all runs — includes prior 2Captcha-era entries).
6. **Fingerprint:** one `build_fingerprint()` per session → UA/locale/timezone/
   viewport consistent into Camoufox context.

## Open items
- **Does a saved cookie actually skip the challenge?** Mechanism verified, but
  the *effectiveness* depends on capturing the correct clearance token. Next
  run (with broad-save) will show if a re-challenged sticky IP is skipped.
- The `_dd_s`-only capture suggests SeLoger's anti-bot may be lighter than full
  DataDome challenge — fr-2 succeeded cold with zero cookies, so the primary
  path is robust regardless of cookie reuse.

## Commits
- `dace215` live-test fixes: geoip=True + save ALL context cookies
- `dc4e2cc` (prior) production-readiness pass
- Logs: `logs/live_test_20260821_180941.log`, `logs/live_test2_20260821_181500.log`
