"""
CEX listing-opportunity scanner — config-driven, brand-agnostic.

Pipeline:
  - Intersect: Binance Square trending heat ∩ (source-CEX has perp + target-CEX doesn't)
  - Combined score = social heat + listing novelty bonus + verified-author bonus -
                     bearish discount - already-signaled discount
  - Default auto_mark_signaled=True; subsequent runs downscore the same coin
    so we don't keep re-alerting on the same opportunity.

Replaces the legacy aster-listing-scanner. All CEX URLs + brand display names
come from a config file (`--config`) or CLI overrides.

Config (JSON):
    {
      "brand_id":     "aster",
      "brand_name":   "Aster DEX",
      "source_cex":   { "name": "Binance", "base_url": "https://fapi.binance.com" },
      "target_cex":   { "name": "Aster",   "base_url": "https://fapi.asterdex.com",
                        "shrink_abort_ratio": 0.5 }
    }

CLI:
    python3 listing_opps.py \
        --config config.json \
        --save reports/TS.md \
        --json reports/TS.json
"""

import argparse
import json
import logging
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from listing_scanner import ListingScanner
from square_scraper import SquareScraper

logger = logging.getLogger("listing_opps")

# ==================== Score combo params ====================
NOVELTY_BONUS = [
    (1, 25),    # ≤1h:  +25
    (6, 15),    # ≤6h:  +15
    (24, 8),    # ≤24h: +8
]
VERIFIED_BONUS_PER_MENTION = 3
VERIFIED_BONUS_CAP = 10
BEARISH_PENALTY_MULTIPLIER = 0.8
ALREADY_SIGNALED_MULTIPLIER = 0.3


def _compute_combined_score(
    social_score: float,
    hours_since_listing: float,
    verified_mentions: int,
    tendency_bullish: int,
    tendency_bearish: int,
    already_signaled: bool = False,
) -> float:
    score = float(social_score)
    for threshold, bonus in NOVELTY_BONUS:
        if hours_since_listing <= threshold:
            score += bonus
            break
    if verified_mentions > 0:
        score += min(verified_mentions * VERIFIED_BONUS_PER_MENTION, VERIFIED_BONUS_CAP)
    if tendency_bearish > tendency_bullish and tendency_bearish > 0:
        score *= BEARISH_PENALTY_MULTIPLIER
    if already_signaled:
        score *= ALREADY_SIGNALED_MULTIPLIER
    return round(score, 1)


# --------------------------------------------------------------------------
# Config + brand resolution
# --------------------------------------------------------------------------

REQUIRED_CFG_FIELDS = ("brand_id", "brand_name", "source_cex", "target_cex")


def load_config(config_path: str | None) -> dict[str, Any]:
    path = config_path or os.environ.get("CEX_LISTING_SCANNER_CONFIG")
    if not path:
        return {}
    p = Path(os.path.expanduser(path))
    if not p.exists():
        raise FileNotFoundError(f"config not found: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def resolve_settings(args, cfg: dict) -> dict[str, Any]:
    out = {
        "brand_id": args.brand_id or cfg.get("brand_id"),
        "brand_name": args.brand_name or cfg.get("brand_name"),
        "source_cex": cfg.get("source_cex") or {},
        "target_cex": cfg.get("target_cex") or {},
    }
    missing = [k for k in REQUIRED_CFG_FIELDS if not out.get(k)]
    if missing:
        raise SystemExit(
            f"missing required config field(s): {missing}. "
            f"Pass --config <path-with-these-fields>."
        )
    for sub in ("source_cex", "target_cex"):
        for k in ("name", "base_url"):
            if not out[sub].get(k):
                raise SystemExit(f"config.{sub}.{k} required")
    return out


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------

def find_listing_opportunities(
    scraper: SquareScraper,
    listing: ListingScanner,
    min_mentions: int = 2,
    auto_mark_signaled: bool = True,
) -> list[dict]:
    logger.info("=== compute listing opportunities (intersection) ===")
    social_results = scraper.scan()
    if not social_results:
        logger.warning("Square: no social heat data")
        return []
    listing_results = listing.scan()
    if not listing_results:
        logger.warning("no source-only coins")
        return []

    heat_by_coin = {r["coin"]: r for r in social_results}
    gap_by_coin = {r["coin"]: r for r in listing_results}

    opportunities = []
    for coin, heat in heat_by_coin.items():
        if heat["mentions"] < min_mentions:
            continue
        gap = gap_by_coin.get(coin)
        if not gap:
            continue
        combined = _compute_combined_score(
            social_score=heat["score"],
            hours_since_listing=gap["hours_since_listing"],
            verified_mentions=heat["verified_mentions"],
            tendency_bullish=heat["tendency_bullish"],
            tendency_bearish=heat["tendency_bearish"],
            already_signaled=gap.get("already_signaled", False),
        )
        engagement_total = (
            heat["total_likes"] + heat["total_comments"] + heat["total_shares"]
        )
        opportunities.append({
            "coin": coin,
            "symbol": gap["symbol"],
            "combined_score": combined,
            "social_score": heat["score"],
            "listing_score": gap["score"],
            "mentions": heat["mentions"],
            "engagement_total": engagement_total,
            "total_likes": heat["total_likes"],
            "total_comments": heat["total_comments"],
            "total_shares": heat["total_shares"],
            "total_views": heat["total_views"],
            "verified_mentions": heat["verified_mentions"],
            "tendency_bullish": heat["tendency_bullish"],
            "tendency_bearish": heat["tendency_bearish"],
            "hours_since_listing": gap["hours_since_listing"],
            "is_new_listing": gap["is_new"],
            "already_signaled": gap.get("already_signaled", False),
            "sources": ["social", "listing_gap"],
        })

    opportunities.sort(key=lambda x: x["combined_score"], reverse=True)

    logger.info(
        f"candidates: social-hot {len(heat_by_coin)} × source-only {len(gap_by_coin)} → "
        f"intersection {len(opportunities)} opportunities"
    )

    if auto_mark_signaled and opportunities:
        listing.mark_many_signaled([o["symbol"] for o in opportunities])
        logger.info(f"mark_signaled {len(opportunities)} symbols")

    return opportunities


def run_snapshot(
    settings: dict[str, Any],
    min_mentions: int = 2,
    auto_mark_signaled: bool = True,
) -> dict[str, Any]:
    timestamp = datetime.now(timezone.utc).isoformat()
    src = settings["source_cex"]
    tgt = settings["target_cex"]
    logger.info(
        f"=== {settings['brand_name']} listing-opportunity snapshot @ {timestamp} "
        f"(source={src['name']} / target={tgt['name']}) ==="
    )

    scraper = SquareScraper()
    listing = ListingScanner(
        source_cex_name=src["name"],
        source_cex_base_url=src["base_url"],
        target_cex_name=tgt["name"],
        target_cex_base_url=tgt["base_url"],
        target_shrink_abort_ratio=tgt.get("shrink_abort_ratio", 0.5),
    )
    errors: list[dict[str, str]] = []
    opportunities: list[dict] = []
    try:
        opportunities = find_listing_opportunities(
            scraper, listing,
            min_mentions=min_mentions,
            auto_mark_signaled=auto_mark_signaled,
        )
    except Exception as e:
        logger.error(f"find_listing_opportunities failed: {e}\n{traceback.format_exc()}")
        errors.append({
            "stage": "find_listing_opportunities",
            "error": f"{type(e).__name__}: {e}",
        })
    finally:
        try:
            scraper.stop()
        except Exception:
            pass

    return {
        "timestamp": timestamp,
        "brand_id": settings["brand_id"],
        "brand_name": settings["brand_name"],
        "source_cex_name": src["name"],
        "target_cex_name": tgt["name"],
        "stats": {
            "opportunities_count": len(opportunities),
            "new_listings_in_opportunities": sum(
                1 for o in opportunities if o["is_new_listing"]
            ),
            "errors_count": len(errors),
            "partial": bool(errors),
        },
        "opportunities": opportunities,
        "errors": errors,
    }


def format_report(snapshot: dict[str, Any], top: int = 15) -> str:
    ts = snapshot.get("timestamp", "")
    stats = snapshot.get("stats", {})
    opps = snapshot.get("opportunities", [])
    errors = snapshot.get("errors", [])
    bn = snapshot.get("brand_name", "Brand")
    src = snapshot.get("source_cex_name", "Source")
    tgt = snapshot.get("target_cex_name", "Target")

    lines = [
        f"# {bn} listing-opportunity snapshot",
        "",
        f"_{ts}_",
        "",
        f"**Opportunities**: {stats.get('opportunities_count', 0)} "
        f"(of which {stats.get('new_listings_in_opportunities', 0)} are ≤24h-new on {src})",
        "",
    ]
    if errors:
        lines.append("> ⚠️ Partial snapshot — failures:")
        for e in errors:
            lines.append(f"> - `{e.get('stage', '?')}`: {e.get('error', '')}")
        lines.append("")

    lines.append(f"## 📈 High social heat × {src} has + {tgt} doesn't")
    lines.append("")
    if not opps:
        lines.append("_no candidates this run_")
        lines.append("")
    else:
        for i, o in enumerate(opps[:top]):
            tags = []
            if o.get("is_new_listing"):
                tags.append(f"🆕 listed {o['hours_since_listing']:.1f}h ago")
            if o.get("verified_mentions", 0) > 0:
                tags.append(f"✓ verified ×{o['verified_mentions']}")
            if o.get("tendency_bullish", 0) > o.get("tendency_bearish", 0):
                tags.append("📈 bullish")
            elif o.get("tendency_bearish", 0) > o.get("tendency_bullish", 0):
                tags.append("📉 bearish")
            if o.get("already_signaled"):
                tags.append("🔁 prev-signaled")
            tag_str = f" — {' · '.join(tags)}" if tags else ""

            lines.append(
                f"### {i+1}. `{o['coin']}` ({o['symbol']}) · "
                f"combined **{o['combined_score']:.0f}**{tag_str}"
            )
            lines.append("")
            lines.append(
                f"- Social: **{o['mentions']}** posts · "
                f"{o['total_likes']}❤ {o['total_comments']}💬 {o['total_shares']}🔁 {o['total_views']}👀"
            )
            lines.append(f"- Sentiment: bull {o['tendency_bullish']} / bear {o['tendency_bearish']}")
            lines.append(
                f"- {src} listed: "
                + (f"{o['hours_since_listing']:.1f}h ago"
                   if o["hours_since_listing"] < 999 else "before tracking started")
            )
            lines.append(
                f"- Score breakdown: social {o['social_score']:.0f} · "
                f"listing-novelty {o['listing_score']:.0f} · "
                f"combined **{o['combined_score']:.0f}**"
            )
            lines.append("")
    return "\n".join(lines)


def _write_file(path_str: str, content: str, label: str):
    p = Path(path_str)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    print(f"[wrote {label}] {p}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="CEX listing-opportunity scanner (config-driven)")
    parser.add_argument("--config", help="Path to config.json (or env CEX_LISTING_SCANNER_CONFIG)")
    parser.add_argument("--brand-id", help="Override config brand_id")
    parser.add_argument("--brand-name", help="Override config brand_name")
    parser.add_argument("--save", metavar="PATH", help="Write markdown to PATH")
    parser.add_argument("--json", metavar="PATH", help="Write JSON snapshot to PATH")
    parser.add_argument("--min-mentions", type=int, default=2)
    parser.add_argument("--no-auto-signal", action="store_true")
    parser.add_argument("--top", type=int, default=15)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    cfg = load_config(args.config)
    settings = resolve_settings(args, cfg)
    snapshot = run_snapshot(
        settings,
        min_mentions=args.min_mentions,
        auto_mark_signaled=not args.no_auto_signal,
    )
    report = format_report(snapshot, top=args.top)
    print(report)
    if args.save:
        _write_file(args.save, report, "markdown")
    if args.json:
        _write_file(args.json, json.dumps(snapshot, ensure_ascii=False, indent=2), "JSON")


if __name__ == "__main__":
    main()
