---
name: cex-listing-scanner
description: Scan listing opportunities for a target CEX — intersect Binance Square trending heat with (source-CEX has perp ∩ target-CEX doesn't). Brand-agnostic — config-driven source/target CEX pair. Output is a ranked candidate list ready for the listing PM.
---

Scan listing opportunities for any target CEX. Two signals are intersected:

1. **Social heat** — top-trending coins on Binance Square (Trending feed; we're buying what users are already excited about).
2. **Listing gap** — coins that the *source CEX* (typically Binance perp) lists but the *target CEX* doesn't (yet). Newer source listings score higher.

The intersection — high social heat × source has perp + target doesn't — is the candidate list. Combined score weighs:
- social score (mentions × engagement)
- novelty bonus (≤1h listed = +25, ≤6h = +15, ≤24h = +8)
- verified-author bonus (×3 per verified mention, capped at +10)
- bearish discount (×0.8 if community is net-bearish)
- already-signaled discount (×0.3 to avoid re-alerting)

Output is a ranked markdown report per run, plus the same as JSON for downstream tooling.

The skill is **brand-agnostic**: target CEX URL, source CEX URL, brand display name, and shrink-abort ratio all come from `config.json`. See `examples/aster.config.example.json` for a worked example (Binance → Aster pair).

## Setup (every run)

1. Mount the plugins directory by calling `mcp__cowork__request_cowork_directory` with `path: "~/.claude/plugins"`. Find the directory whose `.claude-plugin/plugin.json` has `"name": "cex-listing-scanner"` (preferred path is `<mount>/.remote-plugins/<id>/`; fallback to a workspace folder copy). Bind that path to `$PLUGIN_DIR` — used only for staging scripts + reading config.

2. **Pin state to `~/.claude/plugins/.${BRAND_ID}-signals-state/`** — a stable mount path that survives Cowork session resets and is **shared across all monitors for the same brand** (sibling x-reply-monitor + binance-square-monitor land their state in the same dir; this scanner's state is a separate file, no conflict).
   ```bash
   PLUGINS_MOUNT=$(ls -d /sessions/*/mnt/plugins 2>/dev/null | head -1)
   STATE_DIR="$PLUGINS_MOUNT/.${BRAND_ID}-signals-state"
   mkdir -p "$STATE_DIR"
   export CEX_LISTING_SCANNER_STATE_PATH="$STATE_DIR/cex_listing_scanner_state.json"
   export SQUARE_SCRAPER_STATE_PATH="$STATE_DIR/square_scraper_state.json"   # shared with binance-square-monitor
   export CEX_LISTING_SCANNER_CONFIG="$PLUGIN_DIR/config.json"
   ```

3. Stage scripts:
   ```bash
   mkdir -p ~/cex-listing-run/reports
   cp "$PLUGIN_DIR/skills/cex-listing-scanner"/*.py ~/cex-listing-run/
   cd ~/cex-listing-run/
   ```
   Files staged:
   - `square_scraper.py` (Binance Square API client + dedup state — shared lib)
   - `listing_scanner.py` (CEX perp diff + state)
   - `listing_opps.py` (entry point — intersects social heat × listing gap)

**State files** (persistent, all in `~/.claude/plugins/.${BRAND_ID}-signals-state/`):
- `cex_listing_scanner_state.json` — source/target perp lists, new-listing timers, signaled set.
- `square_scraper_state.json` — Binance Square post_id dedup (shared with binance-square-monitor).

The cex listing scanner is robust to API failures: if either source or target API fails it aborts the run rather than mistakenly emitting "every coin is missing on target". Target-list shrink >50% also aborts (defends against API returning 200 with truncated data).

## Step 1 — Run the scanner

```bash
TS=$(date +%Y%m%d_%H%M)
python3 listing_opps.py \
  --config "$CEX_LISTING_SCANNER_CONFIG" \
  --save  reports/${TS}.md \
  --json  reports/${TS}.json
```

CLI overrides:
```bash
python3 listing_opps.py --config c.json --brand-id foo --brand-name "Foo CEX"
python3 listing_opps.py --config c.json --min-mentions 3 --top 20 --no-auto-signal
```

The script writes:
- `reports/${TS}.md` — markdown report
- `reports/${TS}.json` — structured opportunities (for downstream automation)

## Step 2 — Mirror reports + summarize

Mirror reports into `$STATE_DIR/listing-reports/` for canonical record (and optionally into a workspace folder for user view).

Then summarize back to user (≤250 words):

1. **Top 5 opportunities** — coin, combined score, listing age on source, mentions count
2. **🆕 New listings** (≤24h) — flagged separately, highest priority
3. **Skipped due to no candidates** — explain why (source/target API down, no social heat, all already-signaled)
4. `computer://` link to `reports/${TS}.md`

## Success criteria

- `reports/${TS}.json`, `reports/${TS}.md` both exist and non-empty.
- State file at `$CEX_LISTING_SCANNER_STATE_PATH` updated with current source/target snapshot + new-listing timers.
- Top 5 opportunities each have combined_score, mentions, hours_since_listing.

## Failure handling — STRICT decision tree

The scanner has 3 data sources (source CEX API, target CEX API, Binance Square scraper) and intentionally aborts rather than emitting false positives. **Read this list and follow it literally — don't shortcut into auto-retry or partial-data emit.**

### Built-in aborts (inside `listing_scanner.py` / `listing_opps.py`)

1. **Source CEX API 4xx/5xx/timeout** → `_get_tradable_symbols` returns `None` → `scan()` aborts and returns `[]`. JSON is written with `errors=[{stage: 'find_listing_opportunities', ...}]`. Final report has 0 opportunities.
2. **Target CEX API 4xx/5xx/timeout** → same — abort. **DO NOT** treat this as "target has 0 listings" (every source coin would falsely look missing).
3. **Target list shrink > `shrink_abort_ratio`** (default 0.5) → abort with explicit `ERROR` log. Defends against API returning 200 OK with truncated/wrong data.
4. **First run with no state file** → auto-init: snapshot baselines, return `[]`. NOT a failure. Next run starts producing real opportunities.
5. **No source-only coins this run** (target caught up, or no diff) → return `[]` with `errors=[]`. NOT a failure. Write empty report normally.

### Hard failures the agent must handle

6. **`square_scraper.py` ImportError on playwright** → no social heat. Tell the user once: "Square heat ranking requires playwright. Run `pip install playwright && python -m playwright install chromium`." Then write partial JSON with `errors=[{stage: 'playwright-missing', ...}]`. **Do NOT auto-install. Do NOT retry.**
7. **Square scraper returns 0 mentions but no exception** → `find_listing_opportunities` logs WARNING and returns `[]`. The scanner output is empty — distinguish "no social heat data" vs "social heat had no overlap with source-only" in the summary to user.
8. **Shrink-abort triggered** → user must investigate (target API returning bad data?). Do NOT auto-clear state and retry — the abort is the correct behavior. Tell user to verify the target CEX is healthy and run `python3 -c "from listing_scanner import ListingScanner; ls = ListingScanner(...); ls.clear_state()"` only after confirming.

### Common shortcut bugs to avoid

- ❌ "Source CEX 5xx, retry" → the scanner does not auto-retry network errors at the entry level. Don't manually retry inline; let the next scheduled run handle it.
- ❌ "Target API down, skip target check and just emit source list" → WRONG. The scanner aborts on this exact scenario for a reason. False positives are very expensive (a listing PM could ticket 200+ coins as "missing").
- ❌ "0 opportunities, write partial-run with stage=no-results" → WRONG. 0 opportunities + `errors=[]` is a valid normal-run outcome. Write the report normally and tell user "no candidates this cycle" without alarm.
- ❌ "Shrink-abort triggered, auto clear_state() and retry" → WRONG. The abort exists precisely because the target API is misbehaving — clearing state would lose dedup history without addressing the root cause.

### Recovery

- Source/target CEX failures → next scheduled run picks up cleanly. State file is unchanged on failed runs (no symbols overwritten).
- Shrink-abort → manual: confirm target CEX is healthy via `curl <target_base_url>/fapi/v1/exchangeInfo | jq '.symbols | length'`, then `clear_state()` if needed.
- playwright-missing → install once, then re-run.

## Configuration

`config.json` schema:

| Field | Required | Purpose |
| --- | --- | --- |
| `brand_id` | yes | State dir name (`~/.claude/plugins/.${brand_id}-signals-state/`). Lowercase, no spaces. |
| `brand_name` | yes | Report title. e.g. `"Aster DEX"` |
| `source_cex.name` | yes | Display label for source CEX. e.g. `"Binance"` |
| `source_cex.base_url` | yes | Base URL of source CEX's perp API (Binance-compatible /fapi/v1/exchangeInfo). |
| `target_cex.name` | yes | Display label for target CEX. e.g. `"Aster"` |
| `target_cex.base_url` | yes | Base URL of target CEX's perp API. |
| `target_cex.shrink_abort_ratio` | optional | Default 0.5. Abort if target list shrunk by more than this ratio between runs. |
