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

# ---- Supabase credentials (same block in every Valuatio sync script) --------
# Picks whichever configured key is actually a SERVICE key (legacy JWT with
# role=service_role, or a new sb_secret_ key), so a wrong value in ONE of the
# two secret names can't silently downgrade writes to anon. Never prints keys.
import base64 as _sb_b64, json as _sb_json, os as _sb_os, re as _sb_re
def _sb_claims(k):
    try:
        seg = k.split(".")[1]; seg += "=" * (-len(seg) % 4)
        return _sb_json.loads(_sb_b64.urlsafe_b64decode(seg))
    except Exception:
        return {}
def _sb_kind(k):
    k = (k or "").strip()
    if not k: return "missing"
    if k.startswith("sb_secret_"): return "secret"
    if k.startswith("sb_publishable_"): return "publishable"
    if k.count(".") == 2: return _sb_claims(k).get("role") or "jwt(no role)"
    return "unrecognized"
def _sb_url():
    u = (_sb_os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    return _sb_re.sub(r"/rest/v1$", "", u)
def _sb_pick_key():
    names = ("SUPABASE_SERVICE_ROLE", "SUPABASE_SERVICE_KEY", "SUPABASE_KEY", "SUPABASE_ANON_KEY")
    vals = [(n, (_sb_os.environ.get(n) or "").strip()) for n in names]
    have = [(n, v) for n, v in vals if v]
    good = [(n, v) for n, v in have if _sb_kind(v) in ("service_role", "secret")]
    name, key = (good or have or [(None, "")])[0]
    print("[supabase] " + (", ".join(f"{n}={_sb_kind(v)}" for n, v in have) or "no keys set")
          + f" -> using {name or 'none'}")
    if key and _sb_kind(key) not in ("service_role", "secret"):
        print(f"::warning::{name} is a '{_sb_kind(key)}' key, not service_role/secret - "
              "service-only tables (ticker_snapshot, regime_timeline, bot_equity) will reject writes")
    distinct = {v for n, v in have if n in names[:2]}
    if len(distinct) > 1:
        print("::warning::SUPABASE_SERVICE_ROLE and SUPABASE_SERVICE_KEY differ - set both to the same service key")
    ref = _sb_claims(key).get("ref") if key.count(".") == 2 else None
    m = _sb_re.match(r"https://([a-z0-9]+)\.supabase\.co$", _sb_url())
    if ref and m and ref != m.group(1):
        print(f"::error::key belongs to Supabase project '{ref}' but SUPABASE_URL points at '{m.group(1)}' (keys from the other project?)")
    return key
def _sb_finite(obj):
    if isinstance(obj, float):
        return obj if obj == obj and obj not in (float("inf"), float("-inf")) else None
    if isinstance(obj, dict):
        return {k: _sb_finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sb_finite(v) for v in obj]
    return obj
# -----------------------------------------------------------------------------
URL = _sb_url()
KEY = _sb_pick_key()
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
    data = json.dumps(_sb_finite(body), allow_nan=False).encode() if body is not None else None
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


# ---- Scheduled-run freshness guard -------------------------------------------
# The app writes Supabase directly whenever you edit, and commits this JSON only
# when you press "Save to Repo". A PUSH of the file is therefore always a
# deliberate, current snapshot and is synced. An hourly SCHEDULED run, however,
# would replay whatever copy sits in the repo - a months-old file would
# overwrite newer rows and its reconcile step would DELETE rows added since.
# So scheduled runs only sync a file generated within the last N hours.
MAX_AGE_H = float(os.environ.get("PORT_SYNC_MAX_AGE_H") or 36)


def _stale_for_schedule(d, label):
    if (os.environ.get("SYNC_TRIGGER") or "").strip() != "schedule":
        return False
    ts = d.get("generatedAt") if isinstance(d, dict) else None
    try:
        gen = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if gen.tzinfo is None:
            gen = gen.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - gen).total_seconds() / 3600
    except Exception:
        age_h = None
    if age_h is not None and age_h <= MAX_AGE_H:
        return False
    shown = "unknown age" if age_h is None else f"{age_h:.0f}h old"
    print(f"::notice::{label}: scheduled sync SKIPPED - repo file is {shown} (generatedAt={ts}). "
          f"Only fresh files (<= {MAX_AGE_H:.0f}h) or a direct push are synced, so a stale repo copy "
          "can never overwrite or delete newer rows written by the app.")
    return True
# -----------------------------------------------------------------------------


FAILED = []


def main():
    if not URL or not KEY:
        print("X Missing creds. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE (or SUPABASE_SERVICE_KEY).")
        return 1
    if not os.path.exists(DATA):
        print(f"No {DATA} yet — nothing to sync (the app commits it).")
        return 0
    d = json.loads(open(DATA).read())
    if _stale_for_schedule(d, "probability"):
        return 0

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
            print(f"::error::probability upsert chunk {i} -> HTTP {st}: {body[:240]}")
            FAILED.append("upsert")
    print(f"  upserted {sent}/{len(rows)}")

    # RECONCILE probability:* keys ONLY (never touch macro:*/regime:*). Guarded so
    # an empty file can't wipe theses.
    deleted = 0
    if keys and not FAILED:   # never delete after a failed upsert
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
                print(f"::error::probability reconcile delete -> HTTP {st}: {body[:200]}")
                FAILED.append("delete")
    if FAILED:
        print(f"X probability sync: {sent} upserted, {deleted} removed, FAILED: {sorted(set(FAILED))}")
        return 1
    print(f"OK probability sync complete — {sent} upserted, {deleted} stale row(s) removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
