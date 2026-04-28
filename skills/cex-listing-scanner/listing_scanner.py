"""
CEX listing-gap scanner (brand-agnostic, config-driven).

Strategy: source-CEX just listed coin X → target-CEX hasn't yet → high-probability listing opportunity for the target CEX.
Pipeline:
  - periodically diff source-CEX vs target-CEX perp lists
  - find "source has + target doesn't have" coins
  - track first-seen timestamps for novelty scoring
  - persistent state across cron runs (so the cold-start doesn't lose timer info)
  - abort safely if either CEX API fails (so we don't mistakenly treat the entire source list as "target missing")

Config (loaded by listing_opps.py and passed in here):
  source_cex_name      e.g. "Binance"      — display label
  source_cex_base_url  e.g. "https://fapi.binance.com"
  target_cex_name      e.g. "Aster"
  target_cex_base_url  e.g. "https://fapi.asterdex.com"
  target_shrink_abort_ratio  e.g. 0.5  (target list shrinking >50% triggers abort)

State env vars:
  CEX_LISTING_SCANNER_STATE_PATH  — path to JSON state. Skill should pin this to
    `~/.claude/plugins/.${BRAND_ID}-signals-state/cex_listing_scanner_state.json`
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("cex_listing_scanner")


def _resolve_default_state_path() -> Path:
    env = os.environ.get("CEX_LISTING_SCANNER_STATE_PATH")
    if env:
        return Path(os.path.expanduser(env))
    return Path.home() / ".cex_listing_scanner_state.json"


class ListingScanner:
    """
    Brand-agnostic listing-gap scanner.

    Required ctor args (no hardcoded brand names):
      source_cex_name / source_cex_base_url:
          where to read the "all listings" reference (typically Binance perp)
      target_cex_name / target_cex_base_url:
          where to compare listing gap (your CEX — listing opportunity if absent)
      target_shrink_abort_ratio:
          if target_cex returns API 200 but list shrunk by more than this ratio
          vs the previous run, abort the scan (defends against wrong/empty API
          responses being interpreted as "target missing all coins")
    """

    def __init__(
        self,
        source_cex_name: str,
        source_cex_base_url: str,
        target_cex_name: str,
        target_cex_base_url: str,
        target_shrink_abort_ratio: float = 0.5,
        state_path: Optional[str] = None,
    ):
        self.source_name = source_cex_name
        self.source_url = source_cex_base_url.rstrip("/")
        self.target_name = target_cex_name
        self.target_url = target_cex_base_url.rstrip("/")
        self.target_shrink_abort_ratio = float(target_shrink_abort_ratio)

        self._source_symbols: set[str] = set()
        self._target_symbols: set[str] = set()
        self._initialized = False

        # New-listing tracker: {symbol: first_seen_datetime}
        self._new_listings: dict[str, datetime] = {}

        # Already-signaled set (avoid duplicate alerts; downscored on re-hit)
        self._signaled: set[str] = set()

        self._state_path = (
            Path(state_path) if state_path else _resolve_default_state_path()
        )
        self._load_state()

    # ==================== State persistence ====================

    def _load_state(self):
        try:
            if not self._state_path.exists():
                return
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            # Backward-compat: old aster-listing-scanner state used
            # "binance_symbols" / "aster_symbols" keys. Read both.
            self._source_symbols = set(
                data.get("source_symbols")
                or data.get("binance_symbols")
                or []
            )
            self._target_symbols = set(
                data.get("target_symbols")
                or data.get("aster_symbols")
                or []
            )
            self._signaled = set(data.get("signaled", []))

            self._new_listings = {}
            for sym, iso in (data.get("new_listings") or {}).items():
                try:
                    dt = datetime.fromisoformat(iso)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    self._new_listings[sym] = dt
                except (ValueError, TypeError):
                    continue

            self._initialized = bool(self._source_symbols)
            logger.info(
                f"loaded state: {self.source_name} {len(self._source_symbols)} | "
                f"{self.target_name} {len(self._target_symbols)} | "
                f"tracking {len(self._new_listings)} | "
                f"signaled {len(self._signaled)}"
            )
        except Exception as e:
            logger.warning(f"state read failed ({self._state_path}): {e}")

    def _save_state(self):
        """Atomic write: tmp + rename"""
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "source_symbols": sorted(self._source_symbols),
                "target_symbols": sorted(self._target_symbols),
                "new_listings": {
                    sym: dt.isoformat()
                    for sym, dt in self._new_listings.items()
                },
                "signaled": sorted(self._signaled),
                "source_name": self.source_name,
                "target_name": self.target_name,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix=".lsstate_",
                suffix=".tmp",
                dir=str(self._state_path.parent),
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, self._state_path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.warning(f"state write failed ({self._state_path}): {e}")

    def clear_state(self):
        """Clear all state (next scan re-initializes; tracking timers reset)"""
        self._source_symbols.clear()
        self._target_symbols.clear()
        self._new_listings.clear()
        self._signaled.clear()
        self._initialized = False
        try:
            if self._state_path.exists():
                self._state_path.unlink()
            logger.info(f"cleared listing state: {self._state_path}")
        except Exception as e:
            logger.warning(f"clear state failed: {e}")

    # ==================== Exchange API ====================

    def _get_tradable_symbols(
        self, base_url: str, name: str
    ) -> Optional[set[str]]:
        """
        Get the perp listings of a CEX (Binance-compatible /fapi/v1/exchangeInfo).
        Returns: set on success; None on failure (note: empty set means API OK
        but no contracts — that's distinct from None).
        """
        try:
            url = f"{base_url}/fapi/v1/exchangeInfo"
            resp = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if "symbols" not in data or not isinstance(data["symbols"], list):
                logger.warning(f"{name} returned unexpected shape: keys={list(data.keys())[:5]}")
                return None
            symbols = set()
            for item in data["symbols"]:
                if (item.get("status") == "TRADING"
                        and item.get("contractType") == "PERPETUAL"
                        and item.get("symbol", "").endswith("USDT")):
                    symbols.add(item["symbol"])
            return symbols
        except Exception as e:
            logger.warning(f"{name} exchangeInfo failed: {e}")
            return None

    def _initialize(self) -> bool:
        """First run: seed baseline (only when no state file or post-clear)"""
        logger.info("initializing CEX symbol baselines...")
        source = self._get_tradable_symbols(self.source_url, self.source_name)
        target = self._get_tradable_symbols(self.target_url, self.target_name)
        if source is None or target is None:
            logger.warning("init failed: at least one CEX API unreachable")
            return False
        self._source_symbols = source
        self._target_symbols = target
        self._initialized = True
        self._save_state()
        logger.info(
            f"init done: {self.source_name} {len(self._source_symbols)} | "
            f"{self.target_name} {len(self._target_symbols)} | "
            f"{self.source_name}-only {len(self._source_symbols - self._target_symbols)}"
        )
        return True

    # ==================== Scan ====================

    def scan(self) -> list[dict]:
        """
        Diff scan:
          1. fetch both sides (any failure → abort)
          2. abort if target list shrunk abnormally
          3. compute "source has + target doesn't" set, score by novelty

        Returns: [{"symbol", "coin", "score", "hours_since_listing", "is_new",
                   "already_signaled", "source"}, ...]
        """
        if not self._initialized:
            if not self._initialize():
                return []

        logger.info("=== listing-gap scan start ===")

        current_source = self._get_tradable_symbols(self.source_url, self.source_name)
        current_target = self._get_tradable_symbols(self.target_url, self.target_name)

        # === Guard #1: any API failure → abort ===
        if current_source is None:
            logger.warning(f"{self.source_name} API failed, skip scan")
            return []
        if current_target is None:
            logger.warning(
                f"{self.target_name} API failed, skip scan "
                f"(avoid mistakenly treating entire {self.source_name} list as missing on target)"
            )
            return []

        # === Guard #2: target list shrunk abnormally → abort ===
        if (self._target_symbols
                and len(current_target) < len(self._target_symbols) * self.target_shrink_abort_ratio):
            logger.error(
                f"{self.target_name} list shrunk abnormally "
                f"({len(self._target_symbols)} → {len(current_target)}, "
                f"< {self.target_shrink_abort_ratio * 100:.0f}%), refusing to use. "
                f"Manual confirm + clear_state() then re-run."
            )
            return []

        now = datetime.now(timezone.utc)

        # Detect newly-listed on source (vs prev snapshot)
        truly_new = current_source - self._source_symbols
        if truly_new:
            logger.info(f"{self.source_name} just listed: {truly_new}")
            for sym in truly_new:
                if sym not in self._new_listings:
                    self._new_listings[sym] = now

        # Update memory
        self._source_symbols = current_source
        self._target_symbols = current_target

        # "source has + target doesn't"
        source_only = current_source - current_target

        # Detect target catching up (or source delisting)
        for sym in list(self._new_listings.keys()):
            if sym in current_target:
                elapsed = (now - self._new_listings[sym]).total_seconds() / 3600
                logger.info(f"{self.target_name} caught up on {sym} ({elapsed:.1f}h after source listing)")
                del self._new_listings[sym]
            elif sym not in current_source:
                # source delisted (rare) → drop tracking
                del self._new_listings[sym]

        # Persist (timers + symbol baselines)
        self._save_state()

        if not source_only:
            logger.info(f"no {self.source_name}-only coins")
            return []

        # === Score ===
        results = []

        for symbol in source_only:
            coin = symbol.replace("USDT", "")
            first_seen = self._new_listings.get(symbol)
            if first_seen:
                hours_since = (now - first_seen).total_seconds() / 3600
            else:
                # not in tracker = was source-only before our state existed
                hours_since = 999

            if hours_since <= 1:
                score = 100
            elif hours_since <= 6:
                score = 80 - (hours_since - 1) * 6  # 80 → 50
            elif hours_since <= 24:
                score = 50 - (hours_since - 6) * 1.67  # 50 → 20
            else:
                score = 10

            if symbol in self._signaled:
                score *= 0.3  # downscore previously-signaled

            results.append({
                "symbol": symbol,
                "coin": coin,
                "score": round(max(score, 0), 1),
                "hours_since_listing": round(hours_since, 1),
                "is_new": hours_since <= 24,
                "already_signaled": symbol in self._signaled,
                "source": "listing_gap",
            })

        results.sort(key=lambda x: x["score"], reverse=True)

        new_count = sum(1 for r in results if r["is_new"])
        logger.info(
            f"{self.source_name}-only: {len(results)} | new (≤24h): {new_count}"
        )
        for i, r in enumerate(results[:5]):
            tag = "NEW!" if r["is_new"] else ""
            sig_tag = " (signaled)" if r["already_signaled"] else ""
            logger.info(
                f"  #{i+1} {r['coin']} | "
                f"score {r['score']:.0f} | "
                f"listed {r['hours_since_listing']:.1f}h ago {tag}{sig_tag}"
            )

        logger.info(f"=== listing-gap scan done ===")
        return results

    # ==================== Signaled management ====================

    def mark_signaled(self, symbol: str, save: bool = True):
        """Mark as already-signaled. save=False for batched calls."""
        if symbol not in self._signaled:
            self._signaled.add(symbol)
            if save:
                self._save_state()

    def mark_many_signaled(self, symbols: list[str]):
        """Batch mark (one state write, more efficient)"""
        if not symbols:
            return
        before = len(self._signaled)
        self._signaled.update(symbols)
        if len(self._signaled) != before:
            self._save_state()

    def unmark_signaled(self, symbol: str):
        """Un-mark (manual override)"""
        if symbol in self._signaled:
            self._signaled.discard(symbol)
            self._save_state()

    def get_signaled(self) -> set[str]:
        return set(self._signaled)
