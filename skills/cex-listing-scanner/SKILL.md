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

## Failure handling

- Source CEX API failure → abort scan, partial JSON written with `errors[]`.
- Target CEX API failure → abort (don't emit false positives — every coin would look "missing").
- Target list shrink >`shrink_abort_ratio` → abort, log "manual confirm + clear_state() then re-run".
- Square scraper failure → abort scan (no social heat = no candidates).

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
