"""东方财富个股新闻 + 公告（AKShare）。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

POS_KW = (
    "增长", "突破", "中标", "订单", "利好", "回购", "增持", "创新高", "产能",
    "扩产", "签约", "盈利", "超预期", "涨停", "主升", "业绩预增", "扭亏", "获批",
)
NEG_KW = (
    "减持", "亏损", "立案", "调查", "警示", "下降", "违规", "暴雷", "处罚",
    "下滑", "预亏", "终止", "风险提示", "诉讼", "预减",
)
CAUTION_KW = ("异常波动", "严重异常波动", "停牌")


@dataclass
class StockNewsSnapshot:
    symbol: str
    news_titles: List[str] = field(default_factory=list)
    notice_titles: List[str] = field(default_factory=list)
    notice_types: List[str] = field(default_factory=list)


@dataclass
class NewsScore:
    score: float
    note: str
    news_hits: int = 0
    notice_hits: int = 0


def symbol_to_em_code(symbol: str) -> str:
    if symbol.startswith("sh.") or symbol.startswith("sz."):
        return symbol[3:]
    if "." in symbol:
        return symbol.split(".")[0]
    return symbol


def _parse_date(text: str) -> Optional[datetime]:
    text = (text or "").strip()
    if not text:
        return None
    if len(text) >= 19:
        try:
            return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    if len(text) >= 10:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            pass
    return None


def _in_window(dt: Optional[datetime], start: datetime, end: datetime) -> bool:
    if dt is None:
        return False
    return start.date() <= dt.date() <= end.date()


def _fetch_news_em(code: str) -> List[Tuple[str, str, datetime]]:
    import akshare as ak

    df = ak.stock_news_em(symbol=code)
    if df is None or df.empty:
        return []
    out: List[Tuple[str, str, datetime]] = []
    for _, row in df.iterrows():
        title = str(row.get("新闻标题") or "").strip()
        content = str(row.get("新闻内容") or "").strip()
        dt = _parse_date(str(row.get("发布时间") or ""))
        if title:
            out.append((title, content, dt))
    return out


def _fetch_notices_em(code: str, begin: str, end: str) -> List[Tuple[str, str, datetime]]:
    import akshare as ak

    df = ak.stock_individual_notice_report(
        security=code,
        symbol="全部",
        begin_date=begin,
        end_date=end,
    )
    if df is None or df.empty:
        return []
    out: List[Tuple[str, str, datetime]] = []
    for _, row in df.iterrows():
        title = str(row.get("公告标题") or "").strip()
        ntype = str(row.get("公告类型") or "").strip()
        dt = _parse_date(str(row.get("公告日期") or ""))
        if title:
            out.append((title, ntype, dt))
    return out


class AkShareNewsProvider:
    """按 ts_code 拉取东方财富个股新闻与公告，带请求节流与缓存。"""

    def __init__(self, lookback_days: int = 5, request_delay: float = 0.25):
        self.lookback_days = lookback_days
        self.request_delay = request_delay
        self._cache: Dict[str, StockNewsSnapshot] = {}

    def fetch(self, symbol: str, as_of: str) -> StockNewsSnapshot:
        if symbol in self._cache:
            return self._cache[symbol]

        code = symbol_to_em_code(symbol)
        end_dt = datetime.strptime(as_of, "%Y-%m-%d")
        start_dt = end_dt - timedelta(days=self.lookback_days)
        begin = start_dt.strftime("%Y%m%d")
        end = end_dt.strftime("%Y%m%d")

        news_titles: List[str] = []
        notice_titles: List[str] = []
        notice_types: List[str] = []

        try:
            for title, _content, dt in _fetch_news_em(code):
                if _in_window(dt, start_dt, end_dt):
                    news_titles.append(title)
            time.sleep(self.request_delay)
            for title, ntype, dt in _fetch_notices_em(code, begin, end):
                if _in_window(dt, start_dt, end_dt):
                    notice_titles.append(title)
                    notice_types.append(ntype or "公告")
        except Exception as exc:
            logger.warning("AKShare 新闻/公告拉取失败 %s: %s", symbol, exc)

        snap = StockNewsSnapshot(
            symbol=symbol,
            news_titles=news_titles,
            notice_titles=notice_titles,
            notice_types=notice_types,
        )
        self._cache[symbol] = snap
        time.sleep(self.request_delay)
        return snap

    @property
    def items_fetched(self) -> int:
        return sum(
            len(s.news_titles) + len(s.notice_titles) for s in self._cache.values()
        )

    @property
    def symbols_fetched(self) -> int:
        return len(self._cache)


def score_snapshot(snap: StockNewsSnapshot, lookback_days: int = 5) -> NewsScore:
    score = 50.0
    pos_ann = 0
    neg_ann = 0
    pos_news = 0
    neg_news = 0
    best_note = ""

    for title, ntype in zip(snap.notice_titles, snap.notice_types):
        text = f"{title} {ntype}"
        if any(k in text for k in NEG_KW) or ntype in ("风险提示",):
            score -= 22
            neg_ann += 1
            if not best_note:
                best_note = f"公告:{title[:36]}"
        elif any(k in text for k in CAUTION_KW):
            score -= 8
            neg_ann += 1
            if not best_note:
                best_note = f"公告:{title[:36]}"
        elif any(k in text for k in POS_KW):
            score += 18
            pos_ann += 1
            if not best_note or best_note.startswith("新闻:"):
                best_note = f"公告:{title[:36]}"

    for title in snap.news_titles[:8]:
        if any(k in title for k in NEG_KW):
            score -= 12
            neg_news += 1
            if not best_note:
                best_note = f"新闻:{title[:36]}"
        elif any(k in title for k in POS_KW):
            score += 10
            pos_news += 1
            if not best_note:
                best_note = f"新闻:{title[:36]}"

    score = max(0.0, min(100.0, score))
    notice_hits = len(snap.notice_titles)
    news_hits = len(snap.news_titles)

    if not best_note:
        if notice_hits or news_hits:
            if snap.notice_titles:
                best_note = f"公告:{snap.notice_titles[0][:36]}"
            else:
                best_note = f"新闻:{snap.news_titles[0][:36]}"
        else:
            return NewsScore(
                score=50.0,
                note=f"近{lookback_days}日无公告/新闻",
                news_hits=0,
                notice_hits=0,
            )

    extra: List[str] = []
    if notice_hits:
        extra.append(f"公告{notice_hits}条")
    if news_hits:
        extra.append(f"新闻{news_hits}条")
    if pos_ann or neg_ann or pos_news or neg_news:
        tags = []
        if pos_ann:
            tags.append(f"公告利好{pos_ann}")
        if neg_ann:
            tags.append(f"公告利空{neg_ann}")
        if pos_news:
            tags.append(f"新闻利好{pos_news}")
        if neg_news:
            tags.append(f"新闻利空{neg_news}")
        extra.append("，".join(tags))
    note = best_note
    if extra:
        note += f"（{'；'.join(extra)}）"
    return NewsScore(
        score=score,
        note=note,
        news_hits=news_hits,
        notice_hits=notice_hits,
    )


def score_symbol_news(
    provider: AkShareNewsProvider,
    symbol: str,
    as_of: str,
) -> Tuple[float, str]:
    snap = provider.fetch(symbol, as_of)
    result = score_snapshot(snap)
    return result.score, result.note
