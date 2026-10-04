#!/usr/bin/env python3
"""
sync_probability_to_supabase.py — push data/probability_data.json -> Supabase
`analytics_kv` (the shared ANALYTICS/PORT project), one row per thesis keyed
`probability:<TICKER>`. Server-side (GitHub Actions), SERVICE-ROLE key.

The app exports the probability theses file into this repo (validate-probability.yml
sanity-checks it). Nothing was pushing it to Supabase — this is that missing step.
It mirrors the app's own analytics_kv probability push (key `probability:<TICKER>`,
value = the thesis object).

FRESH, NO DUPLICATES:
  Upsert on `key` (resolution=merge-duplicates). A reconcile step then deletes
  `probability:*` rows whose ticker is no longer in the file — and ONLY those
  (macro:* / regime:* keys are never touched). Guarded so an empty/missing file
  can't wipe anything.

TIMEZONE:
  updated_at is Eastern wall-clock tagged +00:00 so Supabase's UTC display shows
  local Eastern time (see now_iso).

Env (repo secrets):
  SUPABASE_URL           the shared ANALYTICS/PORT project URL
  SUPABASE_SERVICE_ROLE  the service_role key   (SUPABASE_SERVICE_KEY also accepted)
"""
import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta

URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
KEY = (os.environ.get("SUPABASE_SERVICE_ROLE")
       or os.environ.get("SUPABASE_SERVICE_KEY")
       or os.environ.get("SUPABASE_KEY")
       or os.environ.get("SUPABASE_ANON_KEY") or "")
TABLE = "analytics_kv"
DATA = "data/probability_data.json"


# --------------------------------------------------------------- timezone ----
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None


def _eastern_now():
    if _ET is not None:
        return datetime.now(_ET)
    u = datetime.now(timezone.utc); y = u.year

    def nth_sunday(month, n):
        d = datetime(y, month, 1, tzinfo=timezone.utc)
        return 1 + ((6 - d.weekday()) % 7) + (n - 1) * 7
    start = datetime(y, 3, nth_sunday(3, 2), 7, tzinfo=timezone.utc)
    end = datetime(y, 11, nth_sunday(11, 1), 6, tzinfo=timezone.utc)
    return u + timedelta(hours=(-4 if start <= u < end else -5))


def now_iso():
    return _eastern_now().replace(tzinfo=timezone.utc).isoformat()


# ------------------------------------------------------------------ http -----
def _req(method, path, body=None, headers=None):
    h = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    if headers:
        h.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _thesis_ticker(t):
    if not isinstance(t, dict):
        return None
    raw = t.get("_raw", t) if isinstance(t.get("_raw"), dict) else t
    return (t.get("ticker") or t.get("primaryTicker")
            or raw.get("ticker") or raw.get("primaryTicker"))


def main():
    if not URL or not KEY:
        print("X Missing creds. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE (or SUPABASE_SERVICE_KEY).")
        return 1
    if not os.path.exists(DATA):
        print(f"No {DATA} yet — nothing to sync (the app commits it).")
        return 0
    d = json.loads(open(DATA).read())

    if isinstance(d, list):
        theses = d
    elif isinstance(d, dict):
        theses = d.get("theses")
    else:
        print("X unexpected top-level type"); return 0
    if not isinstance(theses, list):
        print("X 'theses' is not an array"); return 0

    now = now_iso()
    rows, keys, missing = [], set(), 0
    for t in theses:
        tic = _thesis_ticker(t)
        if not tic:
            missing += 1
            continue
        key = f"probability:{str(tic).upper()}"
        if key in keys:
            continue
        keys.add(key)
        rows.append({"key": key, "value": t, "updated_at": now})

    print(f"Probability -> Supabase ({URL}) · {len(rows)} thesis row(s), {missing} without a ticker")

    sent = 0
    for i in range(0, len(rows), 100):
        chunk = rows[i:i + 100]
        st, body = _req("POST", f"/rest/v1/{TABLE}?on_conflict=key", chunk,
                        {"Prefer": "resolution=merge-duplicates,return=minimal"})
        if st in (200, 201, 204):
            sent += len(chunk)
        else:
            print(f"  upsert chunk {i} -> HTTP {st}: {body[:240]}")
    print(f"  upserted {sent}/{len(rows)}")

    # RECONCILE probability:* keys ONLY (never touch macro:*/regime:*). Guarded so
    # an empty file can't wipe theses.
    deleted = 0
    if keys:
        st, body = _req("GET", f"/rest/v1/{TABLE}?key=like.probability:*&select=key")
        existing = set()
        if st == 200:
            try:
                existing = {r["key"] for r in json.loads(body) if isinstance(r, dict) and "key" in r}
            except Exception:
                existing = set()
        stale = [k for k in existing if k not in keys]
        for i in range(0, len(stale), 50):
            chunk = stale[i:i + 50]
            in_list = ",".join('"%s"' % str(x).replace('"', "") for x in chunk)
            st, body = _req("DELETE", f"/rest/v1/{TABLE}?key=in.({urllib.parse.quote(in_list)})", None,
                            {"Prefer": "return=minimal"})
            if st in (200, 204):
                deleted += len(chunk)
            else:
                print(f"  reconcile delete -> HTTP {st}: {body[:200]}")
    print(f"OK probability sync complete — {sent} upserted, {deleted} stale row(s) removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
