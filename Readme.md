# TRAPP2-PORT — Portfolio Backend Repo

Durable, cross-device store for the **Valuatio portfolio**: holdings, watchlists, the full transaction ledger, cash position, and the GoodGlobe Index. This is the portfolio counterpart to the trading-bot repo (TRAPP2-BOT) — it keeps your portfolio data safe outside the browser so it survives a cleared cache or a move to a new device.

## Why this repo exists

Valuatio stores your portfolio in the browser's `localStorage`. That's fast, but it's per-device and can be wiped by clearing site data. This repo is the **source of truth**: you export a single JSON snapshot from the app, commit it here, and import it on any device to restore everything.

## How data flows

The app runs entirely in the browser and has **no GitHub write token**, so it can't push here directly (same as TRAPP2-BOT and XTRAPP). The flow:

1. In the app: **Home (⌂ top-left) → Portfolio Backend · TRAPP2-PORT → ⤓ Export Portfolio JSON**. This downloads `portfolio_data.json`.
2. Commit that file to this repo at **`data/portfolio_data.json`** (the **Open Repo to Commit ↗** button jumps straight to the file editor on GitHub).
3. On any device: **⤒ Import from Repo** pulls it back and restores holdings, watchlists, transactions, cash, and the GoodGlobe Index.

The app reads it from:
```
https://raw.githubusercontent.com/GoodGlobeLLC/TRAPP2-PORT/main/data/portfolio_data.json
```

## What's in `data/portfolio_data.json`

A single JSON file (schema `valuatio-portfolio/v1`):

| Key | What it is |
|---|---|
| `generatedAt` | ISO timestamp of the export |
| `counts` | quick tallies (portfolio / holdings / watching / tracking / avoid / transactions) |
| `cashPosition` | running cash balance |
| `portfolio` | the full position list — **authoritative** for re-import |
| `byRole` | the same positions split into `holdings` / `watching` / `tracking` / `avoid` / `other` for easy reading |
| `transactions` | the immutable buy/sell ledger (unioned by `id` on import, so nothing is ever lost) |
| `goodGlobeIndex` | GoodGlobe Index members + levels |
| `goodGlobeCurve` | the index value curve over time |

## Import behavior (how restore works)

- **`portfolio`** is a full snapshot — importing **replaces** the local position list with the repo's.
- **`transactions`** are **unioned by `id`** — any trade in the repo OR locally is kept, so re-importing never drops a transaction you recorded on another device.
- **`cashPosition`**, **`goodGlobeIndex`**, and **`goodGlobeCurve`** are restored from the snapshot.

Because of the transaction union, the safe workflow across devices is: export → commit from device A, then import on device B before making new trades there.

## Folder layout

```
TRAPP2-PORT/
  README.md
  data/
    portfolio_data.json        ← the snapshot (committed from the app's export)
  .github/workflows/
    validate.yml               ← sanity-checks the JSON on every commit
```

## Notes

- This repo is **for portfolio data only** — it's separate from TRAPP2-BOT (trading-bot trades) and the data pipelines (TRAPP2 / TRAPP2-1 / etc.).
- Each export is a full snapshot, so committing a new one replaces the old; git history keeps prior snapshots if you ever want to look back.
- The file is trade metadata only — no API keys or secrets — so a public repo is fine.
