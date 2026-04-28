# cex-listing-scanner

A Claude plugin that **scans listing opportunities for any target CEX** by intersecting two signals:
1. **Social heat** — top-trending coins on Binance Square (Trending feed).
2. **Listing gap** — coins that the *source CEX* lists but the *target CEX* doesn't yet.

The intersection — high-discussion coins where the source has perp but the target doesn't — is the candidate list. Each candidate gets a combined score weighing social heat, listing novelty (≤1h listed = +25 bonus), verified-author boost, bearish discount, and prior-signal penalty.

Brand-agnostic. Drop in a config file with your source/target CEX URLs and brand name and it works for any pair.

Companion to [`binance-square-monitor`](../binance-square-monitor) and [`x-reply-monitor`](https://github.com/datou8283/x-reply-monitor) — all three share the per-brand state directory at `~/.claude/plugins/.${brand_id}-signals-state/`.

---

## What you get

- **`skills/cex-listing-scanner/`** — the scanner: scrapes Binance Square trending, diffs source vs target CEX perp lists, computes combined score, writes ranked report.
- **`examples/aster.config.example.json`** — Aster DEX config (source=Binance, target=Aster) used as the worked example.

The scanner is **defensive**: if either CEX API fails or returns suspiciously truncated data, it aborts the run rather than emitting false positives. State persists across runs so first-seen timers survive cron restarts.

---

## Quick start

1. **Install the plugin**:
   ```bash
   cd ~/.claude/plugins
   git clone https://github.com/<owner>/cex-listing-scanner.git
   ```

2. **Create a config file**:
   ```bash
   cp ~/.claude/plugins/cex-listing-scanner/examples/aster.config.example.json \
      ~/.claude/plugins/cex-listing-scanner/config.json
   # edit brand_id / brand_name / source_cex / target_cex
   ```

3. **Trigger a run** — by chat (`run cex-listing-scanner`) or via a scheduled task.

The output:
- `reports/<TS>.md` — ranked listing-opportunity report
- `reports/<TS>.json` — same as structured JSON

---

## How it differs from naive listing-gap diff

- **Defensive abort**: if source or target CEX API fails (or returns truncated data shrinking the target list >50%), the scan aborts rather than mistakenly flagging every coin as "missing on target". Listing decisions are bidirectional and irreversible — false positives are expensive.
- **Novelty-aware**: a coin that source listed 1 hour ago scores higher than one that's been there for weeks. Combined with social heat, the report surfaces the "Aster needs to list this in the next 24h" candidates.
- **De-dup across runs**: previously-flagged opportunities are still listed but at 0.3× score — so you see "this candidate is still hot" without it dominating the report.
- **State-shared per-brand**: drops state in the same dir as sibling monitors (binance-square-monitor / x-reply-monitor) for the same brand_id, so all signals roll up under one folder.

---

## Configuration

`config.json` (or any path you set via `CEX_LISTING_SCANNER_CONFIG`):

| Field | Required | Purpose |
| --- | --- | --- |
| `brand_id` | yes | State dir name (`~/.claude/plugins/.${brand_id}-signals-state/`). |
| `brand_name` | yes | Display label for reports. |
| `source_cex.name` | yes | Source CEX display name (e.g. `"Binance"`) — where the trending heat comes from. |
| `source_cex.base_url` | yes | Source CEX perp API base (Binance-compatible /fapi/v1/exchangeInfo). |
| `target_cex.name` | yes | Target CEX display name (e.g. `"Aster"`) — your CEX. |
| `target_cex.base_url` | yes | Target CEX perp API base. |
| `target_cex.shrink_abort_ratio` | optional | Default 0.5. Abort if target list shrinks more than this between runs. |

### Environment variables

| Var | Effect |
| --- | --- |
| `CEX_LISTING_SCANNER_CONFIG` | Path to config.json |
| `CEX_LISTING_SCANNER_STATE_PATH` | Override scanner state file (default `~/.cex_listing_scanner_state.json`) |
| `SQUARE_SCRAPER_STATE_PATH` | Override Binance Square dedup state (default `$HOME/...`) — set to your brand state dir to share with binance-square-monitor |

---

## Worked example: Aster DEX

```bash
PLUGINS_MOUNT=$(ls -d /sessions/*/mnt/plugins 2>/dev/null | head -1)  # in Cowork sandbox
STATE_DIR="$PLUGINS_MOUNT/.aster-signals-state"
export CEX_LISTING_SCANNER_STATE_PATH="$STATE_DIR/cex_listing_scanner_state.json"
export SQUARE_SCRAPER_STATE_PATH="$STATE_DIR/square_scraper_state.json"
export CEX_LISTING_SCANNER_CONFIG="$PLUGIN_DIR/config.json"

mkdir -p ~/cex-listing-run/reports
cp $PLUGIN_DIR/skills/cex-listing-scanner/*.py ~/cex-listing-run/
cd ~/cex-listing-run/

TS=$(date +%Y%m%d_%H%M)
python3 listing_opps.py --config "$CEX_LISTING_SCANNER_CONFIG" \
  --save reports/${TS}.md --json reports/${TS}.json
```

Typical output: 2-5 ranked candidates per run (Binance recently listed, hot on Square, Aster doesn't have yet). Each gets a combined score 30-110.

---

## Failure modes the skill handles

| Symptom | Behavior |
| --- | --- |
| Source CEX API failure | Abort scan, partial JSON with `errors[]` |
| Target CEX API failure | Abort (don't emit false positives — every coin would look missing) |
| Target list shrink > `shrink_abort_ratio` | Abort, log "manual confirm + `clear_state()` then re-run" |
| Square scraper failure | Abort (no social heat = no candidates) |
| First run (no state file) | Auto-init: snapshot baselines, return empty (timers start fresh next run) |

---

## License

MIT. See [LICENSE](./LICENSE).
