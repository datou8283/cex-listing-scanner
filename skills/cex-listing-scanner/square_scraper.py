"""
幣安廣場社群監控模組
- Playwright 建立瀏覽器 session (一次性, 過時自動重建)
- 在瀏覽器內平行 call 幣安內部 feed API (帶 cookies)
- 多語言支援 (預設 en + zh-CN)
- 多頁抓取 + 單頁失敗自動重試
- 兩種分析模式:
    1. scan()          → 幣種熱度排名 (交易系統用)
    2. scan_mentions() → 關鍵字提及監控 (brand-agnostic, 由 caller 傳 keywords)
- 去重狀態: 保序、atomic 寫入

需安裝:
    pip install playwright
    python -m playwright install chromium

CLI:
    python square_scraper.py heat
    python square_scraper.py mentions <kw1> <kw2>    # 顯式傳關鍵字 (必填)
    # (legacy) 'aster foo bar' was the old default; now caller must always pass.
    python square_scraper.py clear                   # 清去重狀態
"""

import json
import logging
import os
import re
import tempfile
import time
from collections import OrderedDict, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Pattern

logger = logging.getLogger("square_scraper")

SQUARE_URL = "https://www.binance.com/en/square"
POST_URL_TEMPLATE = "https://www.binance.com/en/square/post/{post_id}"

# 預設值 (全部可透過 constructor 覆寫)
DEFAULT_FEED_PAGES = 15
DEFAULT_LANGUAGES = ["en", "zh-CN"]
DEFAULT_SESSION_WARMUP_MS = 8000
DEFAULT_SESSION_MAX_AGE_SECONDS = 7200  # 2h
DEFAULT_MAX_DEDUP_SIZE = 5000
DEFAULT_PAGE_RETRY = 1

# 去重狀態預設路徑。Resolution order:
#   1. env var SQUARE_SCRAPER_STATE_PATH 指向完整檔案路徑 (skill 可用這個
#      把 state 釘到 plugin 目錄, 避免 Cowork session 重新啟動導致 $HOME 歸零)
#   2. Fallback: $HOME/.square_scraper_state.json (舊行為)
def _resolve_default_state_path() -> Path:
    env_path = os.environ.get("SQUARE_SCRAPER_STATE_PATH")
    if env_path:
        return Path(os.path.expanduser(env_path))
    return Path.home() / ".square_scraper_state.json"


DEFAULT_STATE_PATH = _resolve_default_state_path()

# DEFAULT_KEYWORDS removed — this scraper is brand-agnostic now.
# Callers MUST pass `keywords=[...]` to scan_mentions / scan_mentions_via_search.
# See companion square_brand_monitor.py entry point that loads keywords from config.json.
_LEGACY_DEFAULT_KEYWORDS_REMOVED = [
    # Kept here as reference to the original aster-brand-monitor defaults.
    # If you need them, copy into your config.json as `"keywords": [...]`.
    "aster", "asterdex", "asterdex.com", "$aster", "#aster", "#asterdex",
]

# 一次平行抓 N 頁 feed 的 JS
# args: { pageIndices: [1, 2, ...], lang: 'en' }
# 回傳: [{pageIndex, ok, data|error}, ...]
FETCH_JS = """
async (args) => {
    const { pageIndices, lang } = args;

    const fetchPage = async (pageIndex) => {
        try {
            const resp = await fetch('/bapi/composite/v9/friendly/pgc/feed/feed-recommend/list', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'clienttype': 'web',
                    'lang': lang,
                    'csrftoken': document.cookie.match(/csrftoken=([^;]*)/)?.[1] || 'd41d8cd98f00b204e9800998ecf8427e',
                    'bnc-uuid': document.cookie.match(/bnc-uuid=([^;]*)/)?.[1] || '',
                    'x-trace-id': crypto.randomUUID(),
                    'x-ui-request-trace': crypto.randomUUID(),
                    'versioncode': 'web',
                    'bnc-time-zone': Intl.DateTimeFormat().resolvedOptions().timeZone,
                },
                body: JSON.stringify({
                    pageIndex: pageIndex,
                    pageSize: 20,
                    scene: 'web-homepage',
                    contentIds: [],
                }),
                credentials: 'include',
            });
            const data = await resp.json();
            return { pageIndex, ok: true, data };
        } catch(e) {
            return { pageIndex, ok: false, error: e.message };
        }
    };

    const results = await Promise.all(pageIndices.map(fetchPage));
    return results;
}
"""


class SquareScraper:
    """
    幣安廣場爬蟲
    Playwright 建 session → 瀏覽器內平行 fetch API → 兩種分析模式
    """

    def __init__(
        self,
        state_path: Optional[str] = None,
        feed_pages: int = DEFAULT_FEED_PAGES,
        languages: Optional[list[str]] = None,
        session_warmup_ms: int = DEFAULT_SESSION_WARMUP_MS,
        session_max_age_seconds: int = DEFAULT_SESSION_MAX_AGE_SECONDS,
        max_dedup_size: int = DEFAULT_MAX_DEDUP_SIZE,
        page_retry: int = DEFAULT_PAGE_RETRY,
    ):
        self._feed_pages = feed_pages
        self._languages = list(languages) if languages else list(DEFAULT_LANGUAGES)
        self._session_warmup_ms = session_warmup_ms
        self._session_max_age = session_max_age_seconds
        self._max_dedup_size = max_dedup_size
        self._page_retry = page_retry

        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._started = False
        self._session_started_at: Optional[float] = None  # monotonic
        self._consecutive_failures = 0

        # 本次執行抓到的原始貼文快取 (scan / scan_mentions 共用)
        self._last_posts: list[dict] = []

        # 去重狀態 (OrderedDict 保 insertion order, 截斷時可丟掉最舊的)
        # 路徑決定順序: ctor 傳入 state_path > env SQUARE_SCRAPER_STATE_PATH > $HOME/.square_scraper_state.json
        # env var 在 __init__ 時才解析, skill 可以先 export 再 import.
        self._state_path = (
            Path(os.path.expanduser(state_path))
            if state_path
            else _resolve_default_state_path()
        )
        self._processed_ids: "OrderedDict[str, None]" = self._load_state()

    # ==================== 狀態 (去重) ====================

    def _load_state(self) -> "OrderedDict[str, None]":
        try:
            if self._state_path.exists():
                data = json.loads(self._state_path.read_text(encoding="utf-8"))
                return OrderedDict(
                    (str(x), None) for x in data.get("processed_post_ids", [])
                )
        except Exception as e:
            logger.warning(f"讀取狀態檔失敗 ({self._state_path}): {e}")
        return OrderedDict()

    def _save_state(self):
        """Atomic write: 寫到 tmp file 再 os.replace. 保序截斷: 丟掉最舊的."""
        try:
            # 截斷到上限 (OrderedDict popitem(last=False) = 最舊的)
            while len(self._processed_ids) > self._max_dedup_size:
                self._processed_ids.popitem(last=False)

            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "processed_post_ids": list(self._processed_ids.keys()),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix=".sqstate_",
                suffix=".tmp",
                dir=str(self._state_path.parent),
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, self._state_path)  # atomic on POSIX/NTFS
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.warning(f"寫入狀態檔失敗 ({self._state_path}): {e}")

    def clear_dedup_state(self):
        """清空去重記錄 (不影響 _last_posts 快取)"""
        self._processed_ids.clear()
        try:
            if self._state_path.exists():
                self._state_path.unlink()
            logger.info(f"已清空去重狀態: {self._state_path}")
        except Exception as e:
            logger.warning(f"清空狀態檔失敗: {e}")

    def get_dedup_size(self) -> int:
        return len(self._processed_ids)

    # ==================== 瀏覽器 ====================

    def _session_expired(self) -> bool:
        if self._session_started_at is None:
            return True
        return (time.monotonic() - self._session_started_at) > self._session_max_age

    def _ensure_browser(self) -> bool:
        """確保瀏覽器 session 已建立且未過期"""
        if self._started and self._page and not self._session_expired():
            return True

        if self._started and self._session_expired():
            age = int(time.monotonic() - (self._session_started_at or 0))
            logger.info(f"Session 已過期 ({age}s > {self._session_max_age}s), 重建...")
            self.stop()

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error(
                "playwright 未安裝! 請執行:\n"
                "  pip install playwright\n"
                "  python -m playwright install chromium"
            )
            return False

        try:
            logger.info("啟動 Chromium 建立 session...")
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
            self._context = self._browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1440, "height": 900},
            )
            self._page = self._context.new_page()

            logger.info("載入幣安廣場建立 session...")
            self._page.goto(SQUARE_URL, wait_until="domcontentloaded", timeout=45000)
            self._page.wait_for_timeout(self._session_warmup_ms)

            self._started = True
            self._session_started_at = time.monotonic()
            logger.info("瀏覽器 session 已就緒")
            return True

        except Exception as e:
            logger.error(f"瀏覽器啟動失敗: {e}")
            self.stop()
            return False

    def stop(self):
        """關閉瀏覽器"""
        try:
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        self._browser = None
        self._playwright = None
        self._context = None
        self._page = None
        self._started = False
        self._session_started_at = None
        self._last_posts = []

    # ==================== 抓取 ====================

    def _fetch_pages_batch(
        self, page_indices: list[int], lang: str
    ) -> dict[int, dict]:
        """一次平行抓多頁. 回傳 {pageIndex: 結果 dict}"""
        if not page_indices:
            return {}
        try:
            results = self._page.evaluate(
                FETCH_JS,
                {"pageIndices": page_indices, "lang": lang},
            )
        except Exception as e:
            logger.warning(f"批次 fetch 異常 (lang={lang}): {e}")
            return {}
        return {r["pageIndex"]: r for r in (results or []) if r}

    def _fetch_raw_posts(self, force: bool = False) -> list[dict]:
        """
        抓 feed 多頁 (多語言並行 + 單頁重試), 回傳原始 post dict 列表. 依 post_id 去重.
        同一個 SquareScraper 實例, scan / scan_mentions 共用結果 (除非 force=True).
        """
        if self._last_posts and not force:
            return self._last_posts

        if not self._ensure_browser():
            logger.warning("瀏覽器不可用")
            return []

        all_posts: list[dict] = []
        seen_ids: set[str] = set()

        for lang in self._languages:
            logger.info(f"抓取 lang={lang} (共 {self._feed_pages} 頁, 平行)")
            pending_pages = list(range(1, self._feed_pages + 1))

            # 第一次平行抓全部頁
            batch = self._fetch_pages_batch(pending_pages, lang)

            failed_pages: list[int] = []
            early_stop = False
            for pg in pending_pages:
                r = batch.get(pg)
                if not r or not r.get("ok"):
                    failed_pages.append(pg)
                    continue
                data = r.get("data") or {}
                if data.get("code") != "000000":
                    # API 錯誤 (例如 rate limit): 當成失敗可重試
                    logger.debug(
                        f"lang={lang} page {pg} API code={data.get('code')}"
                    )
                    failed_pages.append(pg)
                    continue
                posts = (data.get("data") or {}).get("vos", [])
                if not posts:
                    # 這頁空 = 之後頁大概也空, 記下但不重試
                    early_stop = True
                    continue
                for post in posts:
                    pid = self._get_post_id(post)
                    if pid and pid not in seen_ids:
                        seen_ids.add(pid)
                        all_posts.append(post)

            if early_stop:
                # 空頁代表 feed 見底, 後續頁大概也空, 重試沒意義
                failed_pages = [p for p in failed_pages if p == 1]  # 只保留 page 1 重試

            # 失敗頁重試 N 次 (仍是平行)
            for attempt in range(self._page_retry):
                if not failed_pages:
                    break
                logger.info(
                    f"lang={lang}: 重試 {len(failed_pages)} 頁 "
                    f"(第 {attempt + 1}/{self._page_retry} 次)"
                )
                retry_batch = self._fetch_pages_batch(failed_pages, lang)
                still_failed: list[int] = []
                for pg in failed_pages:
                    r = retry_batch.get(pg)
                    if not r or not r.get("ok"):
                        still_failed.append(pg)
                        continue
                    data = r.get("data") or {}
                    if data.get("code") != "000000":
                        still_failed.append(pg)
                        continue
                    for post in (data.get("data") or {}).get("vos", []):
                        pid = self._get_post_id(post)
                        if pid and pid not in seen_ids:
                            seen_ids.add(pid)
                            all_posts.append(post)
                failed_pages = still_failed

            if failed_pages:
                logger.warning(
                    f"lang={lang}: 最終放棄 {len(failed_pages)} 頁: {failed_pages}"
                )

        # 失敗偵測: 整體無貼文 → 累計失敗, 連續 3 次就重建 session
        if not all_posts:
            self._consecutive_failures += 1
            logger.warning(
                f"本次無抓到貼文 (連續失敗 {self._consecutive_failures})"
            )
            if self._consecutive_failures >= 3:
                logger.warning("連續失敗達到閾值, 重建 session...")
                self.stop()
        else:
            self._consecutive_failures = 0

        logger.info(
            f"抓取完成: {len(all_posts)} 篇唯一貼文 "
            f"(跨 {len(self._languages)} 語言)"
        )
        self._last_posts = all_posts
        return all_posts

    # ==================== 欄位擷取 helpers ====================

    @staticmethod
    def _get_post_id(post: dict) -> str:
        for key in ("id", "postId", "contentId", "feedId"):
            v = post.get(key)
            if v:
                return str(v)
        return ""

    @staticmethod
    def _get_post_title(post: dict) -> str:
        for key in ("title", "subject", "heading"):
            v = post.get(key)
            if v and isinstance(v, str):
                return v
        return ""

    @staticmethod
    def _get_post_content(post: dict) -> str:
        for key in ("content", "textContent", "summary", "contentText", "plainText", "description"):
            v = post.get(key)
            if v:
                return v if isinstance(v, str) else str(v)
        return ""

    @staticmethod
    def _get_author_name(post: dict) -> str:
        for key in ("authorName", "authorNickname", "nickName", "userName", "nickname"):
            v = post.get(key)
            if v and isinstance(v, str):
                return v
        return ""

    @staticmethod
    def _get_post_time(post: dict) -> Optional[datetime]:
        for key in ("createTime", "publishTime", "date", "timestamp", "gmtCreate"):
            v = post.get(key)
            if not v:
                continue
            try:
                ts = int(v)
                if ts > 10**12:  # ms
                    ts = ts / 1000
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except (ValueError, TypeError):
                continue
        return None

    # ==================== 公開 API: 幣種熱度 ====================

    def scan(self, posts: Optional[list[dict]] = None) -> list[dict]:
        """
        掃描幣安廣場社群熱度. 統計各幣種提及、互動、情緒, 計算熱度分.

        Args:
            posts: 可選。外部傳入的貼文; None 則自動抓取 (共用 _last_posts 快取)

        回傳: [{"coin": "BTC", "mentions": 20, "score": 85, ...}, ...]
        """
        logger.info("=== 開始幣安廣場熱度掃描 ===")

        if posts is None:
            posts = self._fetch_raw_posts()

        if not posts:
            logger.warning("無法取得幣安廣場數據")
            return []

        logger.info(f"共取得 {len(posts)} 篇貼文")

        coin_stats: dict[str, dict] = defaultdict(lambda: {
            "mentions": 0,
            "total_likes": 0,
            "total_comments": 0,
            "total_shares": 0,
            "total_views": 0,
            "verified_mentions": 0,
            "tendency_bullish": 0,
            "tendency_bearish": 0,
            "has_futures": False,
        })

        for post in posts:
            pairs = post.get("tradingPairsV2") or post.get("tradingPairs") or []
            if not pairs:
                continue

            likes = post.get("likeCount", 0) or 0
            comments = post.get("commentCount", 0) or 0
            shares = post.get("shareCount", 0) or 0
            views = post.get("viewCount", 0) or 0
            is_verified = post.get("authorVerificationType", 0) in (1, 2)
            tendency = post.get("tendency", "")

            for pair in pairs:
                code = (pair.get("code") or "").upper()
                if not code:
                    continue

                supported = pair.get("supportedMarkets") or []
                has_futures = "FUTURES_UM" in supported
                futures_symbol = pair.get("futuresSymbol", "")

                stats = coin_stats[code]
                stats["mentions"] += 1
                stats["total_likes"] += likes
                stats["total_comments"] += comments
                stats["total_shares"] += shares
                stats["total_views"] += views
                if is_verified:
                    stats["verified_mentions"] += 1
                if has_futures or futures_symbol:
                    stats["has_futures"] = True
                if tendency == "bullish":
                    stats["tendency_bullish"] += 1
                elif tendency == "bearish":
                    stats["tendency_bearish"] += 1

        if not coin_stats:
            logger.warning("未找到任何幣種提及")
            return []

        coin_stats = {k: v for k, v in coin_stats.items() if v["has_futures"]}
        if not coin_stats:
            logger.warning("無合約幣種被提及")
            return []

        results = []
        max_mentions = max(s["mentions"] for s in coin_stats.values()) or 1
        max_engagement = max(
            s["total_likes"] + s["total_comments"] + s["total_shares"]
            for s in coin_stats.values()
        ) or 1

        for code, stats in coin_stats.items():
            mention_score = (stats["mentions"] / max_mentions) * 50
            engagement = (
                stats["total_likes"]
                + stats["total_comments"] * 2
                + stats["total_shares"] * 3
            )
            engagement_score = (engagement / max_engagement) * 30
            verified_bonus = min(stats["verified_mentions"] * 10, 20)
            heat_score = mention_score + engagement_score + verified_bonus

            results.append({
                "coin": code,
                "mentions": stats["mentions"],
                "score": round(heat_score, 1),
                "name": code,
                "rank": 0,
                "total_likes": stats["total_likes"],
                "total_comments": stats["total_comments"],
                "total_shares": stats["total_shares"],
                "total_views": stats["total_views"],
                "verified_mentions": stats["verified_mentions"],
                "tendency_bullish": stats["tendency_bullish"],
                "tendency_bearish": stats["tendency_bearish"],
            })

        results.sort(key=lambda x: x["score"], reverse=True)
        for i, r in enumerate(results):
            r["rank"] = i

        for i, r in enumerate(results[:10]):
            tags = []
            if r["verified_mentions"] > 0:
                tags.append(f"認證×{r['verified_mentions']}")
            if r["tendency_bullish"] > r["tendency_bearish"]:
                tags.append("偏多")
            elif r["tendency_bearish"] > r["tendency_bullish"]:
                tags.append("偏空")
            tag_str = f" [{','.join(tags)}]" if tags else ""
            logger.info(
                f"  熱度 #{i+1}: {r['coin']} | "
                f"提及 {r['mentions']} | "
                f"{r['total_likes']}❤ {r['total_comments']}💬 {r['total_views']}👀 | "
                f"分數 {r['score']:.0f}{tag_str}"
            )

        logger.info(f"=== 廣場掃描完成: {len(results)} 個合約幣種 ===")
        return results

    # ==================== 公開 API: 關鍵字監控 ====================

    def _match_posts_against_keywords(
        self,
        posts: list[dict],
        keywords_lower: list[str],
        use_dedup: bool,
        include_content: bool,
        max_content_length: Optional[int],
    ) -> tuple[list[dict], list[str]]:
        """
        核心關鍵字比對邏輯 (scan_mentions 和 scan_mentions_via_search 共用).
        只 filter, 不排序, 不寫 state. 呼叫方負責排序與 save_state.

        回傳: (results, new_processed_post_ids)
        """
        # hashtag / $ticker 用 substring; 其他用 word boundary regex
        compiled: list[tuple[str, Optional[Pattern]]] = []
        for kw in keywords_lower:
            if kw.startswith(("#", "$")):
                compiled.append((kw, None))
            else:
                pattern = re.compile(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])")
                compiled.append((kw, pattern))

        # ticker 形式關鍵字 → 比對 trading pair code
        pair_match_candidates: set[str] = set()
        for kw in keywords_lower:
            stripped = kw.lstrip("$#").upper()
            if stripped and stripped.isalnum() and len(stripped) <= 10:
                pair_match_candidates.add(stripped)

        results: list[dict] = []
        new_processed: list[str] = []

        for post in posts:
            post_id = self._get_post_id(post)
            if not post_id:
                continue

            if use_dedup and post_id in self._processed_ids:
                continue

            title = self._get_post_title(post)
            content = self._get_post_content(post)
            author = self._get_author_name(post)

            haystack = f"{title}\n{content}\n{author}".lower()

            matched: list[str] = []
            for kw, pattern in compiled:
                if kw in matched:
                    continue
                if pattern is None:
                    if kw in haystack:
                        matched.append(kw)
                else:
                    if pattern.search(haystack):
                        matched.append(kw)

            trading_pairs = post.get("tradingPairsV2") or post.get("tradingPairs") or []
            pair_codes = [(p.get("code") or "").upper() for p in trading_pairs if p.get("code")]
            for code in pair_codes:
                if code in pair_match_candidates:
                    tag = f"pair:{code}"
                    if tag not in matched:
                        matched.append(tag)

            if not matched:
                continue

            is_verified = post.get("authorVerificationType", 0) in (1, 2)
            post_time = self._get_post_time(post)

            if include_content:
                if max_content_length and len(content) > max_content_length:
                    final_content = content[:max_content_length] + "..."
                else:
                    final_content = content
            else:
                final_content = content[:500]

            results.append({
                "post_id": post_id,
                "url": POST_URL_TEMPLATE.format(post_id=post_id),
                "title": title[:200],
                "content": final_content,
                "matched_keywords": matched,
                "author": author,
                "author_verified": is_verified,
                "post_time": post_time.isoformat() if post_time else None,
                "likes": post.get("likeCount", 0) or 0,
                "comments": post.get("commentCount", 0) or 0,
                "shares": post.get("shareCount", 0) or 0,
                "views": post.get("viewCount", 0) or 0,
                "tendency": post.get("tendency", ""),
                "trading_pairs": pair_codes,
            })
            new_processed.append(post_id)

        return results, new_processed

    @staticmethod
    def _mention_sort_key(r: dict) -> tuple:
        return (
            1 if r.get("author_verified") else 0,
            (r.get("likes", 0) or 0)
            + (r.get("comments", 0) or 0) * 2
            + (r.get("shares", 0) or 0) * 3,
        )

    def _commit_dedup_state(self, new_processed: list[str]):
        """追加新 post_id 到 dedup state 並落盤."""
        if not new_processed:
            return
        for pid in new_processed:
            # 若存在則會被移到末尾 (OrderedDict 保序)
            self._processed_ids.pop(pid, None)
            self._processed_ids[pid] = None
        self._save_state()

    def scan_mentions(
        self,
        keywords: Optional[Iterable[str]] = None,
        posts: Optional[list[dict]] = None,
        use_dedup: bool = True,
        include_content: bool = True,
        max_content_length: Optional[int] = 2000,
    ) -> list[dict]:
        """
        關鍵字監控 (feed-based): 找出 trending feed 貼文中標題 / 內容 / 作者名 /
        交易對 tag 含指定關鍵字的貼文. 覆蓋面受限於 trending feed, 會漏掉冷門貼文 —
        需更高命中率時改呼叫 scan_mentions_via_search.

        Args:
            keywords: 關鍵字列表 (大小寫不敏感). REQUIRED — 不接受 None
            posts: 外部傳入的貼文; None 則自動抓取
            use_dedup: True 過濾已處理過的 post_id
            include_content: True 回傳完整 content (依 max_content_length 裁)
            max_content_length: content 最大長度 (避免 JSON 膨脹); None = 不裁

        回傳: list[dict], 依「認證作者 > 互動熱度」排序
        """
        if keywords is None:
            raise ValueError("keywords required — pass list of strings; brand-agnostic scraper has no defaults")

        keywords_lower = [k.lower().strip() for k in keywords if k and k.strip()]
        if not keywords_lower:
            logger.warning("未提供關鍵字")
            return []

        if posts is None:
            posts = self._fetch_raw_posts()

        if not posts:
            logger.warning("無貼文資料")
            return []

        logger.info(f"=== 開始關鍵字監控 (feed, {len(posts)} 篇貼文) ===")
        logger.info(f"關鍵字: {keywords_lower}")

        results, new_processed = self._match_posts_against_keywords(
            posts=posts,
            keywords_lower=keywords_lower,
            use_dedup=use_dedup,
            include_content=include_content,
            max_content_length=max_content_length,
        )

        results.sort(key=self._mention_sort_key, reverse=True)

        if use_dedup:
            self._commit_dedup_state(new_processed)

        logger.info(
            f"=== 關鍵字匹配 (feed): {len(results)} 篇 "
            f"(本次新增 {len(new_processed)} 個 post_id; "
            f"去重清單總計 {len(self._processed_ids)}) ==="
        )
        for i, r in enumerate(results[:10]):
            v = "✓" if r["author_verified"] else " "
            preview = (r["title"] or r["content"][:60]).replace("\n", " ")[:80]
            logger.info(
                f"  #{i+1} {v} @{r['author']} | "
                f"{r['likes']}❤ {r['comments']}💬 | "
                f"kw={r['matched_keywords']} | {preview}"
            )

        return results

    # ==================== 公開 API: 關鍵字監控 (search API) ====================

    def scan_mentions_via_search(
        self,
        keywords: Optional[Iterable[str]] = None,
        pages_per_term: int = 3,
        page_size: int = 20,
        use_dedup: bool = True,
        include_content: bool = True,
        max_content_length: Optional[int] = 2000,
    ) -> list[dict]:
        """
        用幣安廣場 search API 直接搜關鍵字 (比 scan_mentions 命中率高).
        scan_mentions 只看 trending feed, 會漏掉冷門貼文; 本方法直接打 search endpoint.

        實作:
            1. 開獨立 tab 導航 /en/square/search?s=<term>
            2. 攔截頁面自己發的 search request, 拿到 device-info / csrftoken 等 header
            3. 用 page.evaluate() replay 這些 header 抓 pageIndex=1..N
            4. cardType 過濾 BUZZ_SHORT / BUZZ_LONG, 再套 _match_posts_against_keywords
               (相同 word-boundary 邏輯, 避免短詞誤中長詞如 "aster" 誤中 "asteroid")

        搜尋詞: 把 keywords 去掉 $# 前綴後去重, 每個 term 各打 N 頁.

        Args:
            keywords: 關鍵字列表. REQUIRED
            pages_per_term: 每個 search term 抓幾頁 (預設 3)
            use_dedup: True 過濾已處理過的 post_id (與 scan_mentions 共用 state)
            include_content / max_content_length: 同 scan_mentions

        回傳: list[dict] (shape 與 scan_mentions 完全一致)
        """
        if keywords is None:
            raise ValueError("keywords required — pass list of strings; brand-agnostic scraper has no defaults")

        keywords_lower = [k.lower().strip() for k in keywords if k and k.strip()]
        if not keywords_lower:
            logger.warning("未提供關鍵字")
            return []

        # 搜尋詞: 去 $# 前綴, 保留所有獨特詞 (不假設 Binance search 是 substring)
        search_terms = sorted({k.lstrip("$#") for k in keywords_lower if k.lstrip("$#")})
        if not search_terms:
            logger.warning("無可用搜尋詞")
            return []

        if not self._ensure_browser():
            logger.warning("瀏覽器不可用 (search)")
            return []

        logger.info(
            f"=== 開始關鍵字監控 (search, terms={search_terms}, pages_per_term={pages_per_term}) ==="
        )

        raw_posts = self._fetch_search_posts(search_terms, pages_per_term, page_size)
        if not raw_posts:
            logger.warning("search API 無貼文 (或頁面開不起來)")
            return []

        logger.info(f"search API 抓到 {len(raw_posts)} 篇唯一 BUZZ 貼文")

        results, new_processed = self._match_posts_against_keywords(
            posts=raw_posts,
            keywords_lower=keywords_lower,
            use_dedup=use_dedup,
            include_content=include_content,
            max_content_length=max_content_length,
        )

        results.sort(key=self._mention_sort_key, reverse=True)

        if use_dedup:
            self._commit_dedup_state(new_processed)

        logger.info(
            f"=== 關鍵字匹配 (search): {len(results)} 篇 "
            f"(本次新增 {len(new_processed)} 個 post_id; "
            f"去重清單總計 {len(self._processed_ids)}) ==="
        )
        for i, r in enumerate(results[:10]):
            v = "✓" if r["author_verified"] else " "
            preview = (r["title"] or r["content"][:60]).replace("\n", " ")[:80]
            logger.info(
                f"  [S#{i+1}] {v} @{r['author']} | "
                f"{r['likes']}❤ {r['comments']}💬 | "
                f"kw={r['matched_keywords']} | {preview}"
            )

        return results

    # ==================== Search API: 瀏覽器內抓 ====================

    def _fetch_search_posts(
        self, search_terms: list[str], pages_per_term: int, page_size: int = 20
    ) -> list[dict]:
        """
        對每個 search term, 在瀏覽器內用攔截到的 header replay fetch pageIndex=1..N.
        回傳: 跨 term 去重後的 raw post dict list (只保 BUZZ_SHORT / BUZZ_LONG).
        """
        if not search_terms or not self._context:
            return []

        # 開獨立 tab 以免影響 feed scraping 用的 self._page
        try:
            search_page = self._context.new_page()
        except Exception as e:
            logger.warning(f"開新 tab 失敗: {e}")
            return []

        SEARCH_ENDPOINT_FRAGMENT = "pgc/feed/search/list"
        captured: dict = {}

        def _on_request(req):
            if SEARCH_ENDPOINT_FRAGMENT in req.url and "headers" not in captured:
                captured["headers"] = dict(req.headers)

        search_page.on("request", _on_request)

        try:
            # 用第一個 term 的真實 URL, 讓頁面自然發一次 search request 方便攔 header
            from urllib.parse import quote as _urlquote
            warmup_term = search_terms[0]
            search_url = (
                f"https://www.binance.com/en/square/search?s={_urlquote(warmup_term)}"
            )
            try:
                search_page.goto(
                    search_url, wait_until="domcontentloaded", timeout=45000
                )
            except Exception as e:
                logger.warning(f"導航 search 頁失敗: {e}")
                search_page.close()
                return []

            # 等頁面自己觸發 search request (最多等 15s)
            for _ in range(30):
                if "headers" in captured:
                    break
                search_page.wait_for_timeout(500)

            if "headers" not in captured:
                logger.warning("未攔到 search API request headers; 放棄 search")
                search_page.close()
                return []

            # 準備 replay header: 丟掉 browser 會自己塞的項目
            drop = {
                "cookie", "host", "content-length", "accept-encoding",
                "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "origin",
                "x-trace-id", "x-ui-request-trace",  # 每次產新的
            }
            replay_headers = {
                k: v for k, v in captured["headers"].items()
                if k.lower() not in drop
            }
            replay_headers["content-type"] = "application/json"

            fetch_js = """
            async ({term, pageIdx, pageSize, headers}) => {
                const h = Object.assign({}, headers, {
                    'x-trace-id': crypto.randomUUID(),
                    'x-ui-request-trace': crypto.randomUUID(),
                });
                const resp = await fetch('/bapi/composite/v2/friendly/pgc/feed/search/list', {
                    method: 'POST',
                    headers: h,
                    body: JSON.stringify({
                        scene: 'web',
                        pageIndex: pageIdx,
                        pageSize: pageSize,
                        searchContent: term,
                        type: 1,
                    }),
                    credentials: 'include',
                });
                return await resp.json();
            }
            """

            seen_ids: set[str] = set()
            all_posts: list[dict] = []

            for term in search_terms:
                logger.info(f"search term='{term}': 抓 pageIndex=1..{pages_per_term}")
                for pg in range(1, pages_per_term + 1):
                    try:
                        data = search_page.evaluate(
                            fetch_js,
                            {"term": term, "pageIdx": pg, "pageSize": page_size,
                             "headers": replay_headers},
                        )
                    except Exception as e:
                        logger.warning(f"search fetch 異常 term={term} page={pg}: {e}")
                        break

                    if not isinstance(data, dict):
                        logger.warning(f"search 非預期回傳 term={term} page={pg}")
                        break
                    if data.get("code") != "000000":
                        logger.warning(
                            f"search API code={data.get('code')} msg={data.get('message')} "
                            f"term={term} page={pg}"
                        )
                        break

                    vos = ((data.get("data") or {}).get("vos") or [])
                    post_cards = [
                        v for v in vos
                        if (v.get("cardType") or "") in ("BUZZ_SHORT", "BUZZ_LONG")
                    ]
                    if not post_cards:
                        # 本 term 見底
                        break

                    added = 0
                    for v in post_cards:
                        pid = self._get_post_id(v)
                        if pid and pid not in seen_ids:
                            seen_ids.add(pid)
                            all_posts.append(v)
                            added += 1
                    # 整頁都是別 term 已抓過的: 下一頁大概也沒新的 → 停
                    if added == 0:
                        break

            return all_posts
        finally:
            try:
                search_page.close()
            except Exception:
                pass


# ==================== CLI 測試 ====================

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    mode = sys.argv[1] if len(sys.argv) > 1 else "mentions"
    scraper = SquareScraper()
    try:
        if mode == "heat":
            results = scraper.scan()
            print(f"\n=== 前 10 大熱門幣 ===")
            for r in results[:10]:
                print(f"  #{r['rank']+1} {r['coin']}: 分數 {r['score']} (提及 {r['mentions']})")

        elif mode == "mentions":
            kws = sys.argv[2:] if len(sys.argv) > 2 else None
            results = scraper.scan_mentions(keywords=kws)
            print(f"\n=== [feed] 共 {len(results)} 篇匹配貼文 ===")
            for r in results[:20]:
                tag = "✓" if r["author_verified"] else " "
                print(f"\n[{r['post_time']}] {tag} @{r['author']}")
                print(f"  {r['url']}")
                print(f"  kw: {r['matched_keywords']}")
                print(f"  {(r['title'] or r['content'][:150]).strip()}")

        elif mode == "search-mentions":
            kws = sys.argv[2:] if len(sys.argv) > 2 else None
            results = scraper.scan_mentions_via_search(keywords=kws)
            print(f"\n=== [search] 共 {len(results)} 篇匹配貼文 ===")
            for r in results[:20]:
                tag = "✓" if r["author_verified"] else " "
                print(f"\n[{r['post_time']}] {tag} @{r['author']}")
                print(f"  {r['url']}")
                print(f"  kw: {r['matched_keywords']}")
                print(f"  {(r['title'] or r['content'][:150]).strip()}")

        elif mode == "clear":
            scraper.clear_dedup_state()
            print("去重狀態已清空")

        else:
            print(
                f"Unknown mode: {mode}. "
                "Use 'heat' | 'mentions [kw...]' | 'search-mentions [kw...]' | 'clear'."
            )
    finally:
        scraper.stop()
