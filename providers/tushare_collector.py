"""Tushare Pro 日线采集（按交易日批量，兼容 stock_data.db  schema）。"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

import pandas as pd
import tushare as ts

from providers.stock_schema import DAILY_BASIC_TS_FIELDS, STOCK_BASIC_TS_FIELDS, STOCK_DAILY_COLUMNS
from providers.symbols import bs_to_ts, is_a_share_bs, ts_to_bs
from providers.tushare_config import load_tushare_token

logger = logging.getLogger(__name__)


class TushareEnhancedStockDataCollector:
    """
    与 EnhancedStockDataCollector 相同对外接口（daily / daily-quick / retry-failed / backfill）。
    核心差异：pro.daily(trade_date=) 一次拉全市场，不再逐股 socket。
    """

    REQUEST_INTERVAL = 0.35
    MAX_RETRIES = 5

    def __init__(self, db_path: str = "stock_data.db", base=None):
        if base is not None:
            self._base = base
        else:
            from dataScrapper import EnhancedStockDataCollector

            self._base = EnhancedStockDataCollector(db_path)
        self.db_path = db_path
        self._pro = None
        self._latest_adj: Optional[pd.DataFrame] = None

    def __getattr__(self, name):
        return getattr(self._base, name)

    def _init_pro(self):
        if self._pro is not None:
            return self._pro
        token = load_tushare_token()
        ts.set_token(token)
        self._pro = ts.pro_api(token)
        logger.info("Tushare Pro 已初始化")
        return self._pro

    @staticmethod
    def _norm_date(d: str) -> str:
        return d.replace("-", "")

    @staticmethod
    def _to_db_date(d: str) -> str:
        d = str(d)
        if len(d) == 8 and d.isdigit():
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        return d[:10]

    def _call(self, fn, **kwargs) -> pd.DataFrame:
        pro = self._init_pro()
        last_err = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                df = fn(**kwargs)
                if df is None:
                    return pd.DataFrame()
                return df
            except Exception as e:
                last_err = e
                wait = min(2 ** attempt, 16)
                logger.warning("Tushare 调用失败(%s)，%ss 后重试 %s/%s", e, wait, attempt, self.MAX_RETRIES)
                time.sleep(wait)
        logger.error("Tushare 调用最终失败: %s", last_err)
        return pd.DataFrame()

    def get_trade_dates(self, start_date: str, end_date: str) -> List[str]:
        pro = self._init_pro()
        df = self._call(
            pro.trade_cal,
            exchange="SSE",
            start_date=self._norm_date(start_date),
            end_date=self._norm_date(end_date),
            is_open="1",
        )
        if df.empty:
            return []
        return [self._to_db_date(d) for d in df["cal_date"].tolist()]

    def _refresh_latest_adj(self, as_of: str):
        pro = self._init_pro()
        df = self._call(
            pro.adj_factor,
            trade_date=self._norm_date(as_of),
        )
        if df.empty:
            logger.warning("未获取到 %s 的 adj_factor", as_of)
            self._latest_adj = pd.DataFrame(columns=["ts_code", "adj_factor"])
            return
        self._latest_adj = df[["ts_code", "adj_factor"]].copy()

    def get_stock_basic_info(self) -> pd.DataFrame:
        pro = self._init_pro()
        df = self._call(
            pro.stock_basic,
            exchange="",
            list_status="L",
            fields=STOCK_BASIC_TS_FIELDS,
        )
        if df.empty:
            return pd.DataFrame()
        df = df[df["ts_code"].str.endswith((".SH", ".SZ"))].copy()
        df["code"] = df["ts_code"].map(ts_to_bs)
        df = df[df["code"].map(is_a_share_bs)]
        df["code_name"] = df["name"]
        df["ipo_date"] = df["list_date"].apply(self._to_db_date)
        keep = [
            "code", "code_name", "ipo_date", "industry", "area", "market",
            "exchange", "is_hs", "list_status", "act_name", "act_ent_type", "cnspell",
        ]
        return df[[c for c in keep if c in df.columns]]

    def _fetch_one_date(self, trade_date: str) -> pd.DataFrame:
        td = self._norm_date(trade_date)

        daily = self._call(self._init_pro().daily, trade_date=td)
        if daily.empty:
            return pd.DataFrame()

        basic = self._call(
            self._init_pro().daily_basic,
            trade_date=td,
            fields=DAILY_BASIC_TS_FIELDS,
        )
        adj = self._call(self._init_pro().adj_factor, trade_date=td)

        df = daily.copy()
        if not basic.empty:
            drop = [c for c in ("close", "trade_date") if c in basic.columns]
            df = df.merge(basic.drop(columns=drop, errors="ignore"), on="ts_code", how="left")
        if not adj.empty:
            df = df.merge(adj[["ts_code", "adj_factor"]], on="ts_code", how="left")

        if self._latest_adj is not None and not self._latest_adj.empty:
            df = df.merge(
                self._latest_adj.rename(columns={"adj_factor": "adj_latest"}),
                on="ts_code",
                how="left",
            )
            mask = (
                df["adj_factor"].notna()
                & df["adj_latest"].notna()
                & (df["adj_latest"] > 0)
            )
            ratio = df.loc[mask, "adj_factor"] / df.loc[mask, "adj_latest"]
            for col in ("open", "high", "low", "close", "pre_close"):
                if col in df.columns:
                    df.loc[mask, col] = (df.loc[mask, col] * ratio).round(4)

        df["symbol"] = df["ts_code"].map(ts_to_bs)
        df = df[df["symbol"].map(is_a_share_bs)]
        names = self._base._name_cache
        df["name"] = df["symbol"].map(lambda s: names.get(s, s))
        df["trade_date"] = trade_date
        df["month_key"] = df["trade_date"].str[:7]

        if "turnover_rate" in df.columns:
            df["turn"] = df["turnover_rate"]

        # Tushare: vol=手, amount=千元；股本/市值保持原单位（万股/万元）
        df["volume"] = pd.to_numeric(df.get("vol"), errors="coerce") * 100
        df["amount"] = pd.to_numeric(df.get("amount"), errors="coerce") * 1000
        df["turn"] = pd.to_numeric(df.get("turn"), errors="coerce")
        df["pct_chg"] = pd.to_numeric(df.get("pct_chg"), errors="coerce")
        for col in ("open", "high", "low", "close", "pre_close"):
            df[col] = pd.to_numeric(df.get(col), errors="coerce")
        for col in (
            "turnover_rate_f", "volume_ratio", "pe", "pe_ttm", "pb", "ps", "ps_ttm",
            "dv_ratio", "dv_ttm", "total_share", "float_share", "free_share",
            "total_mv", "circ_mv", "adj_factor",
        ):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df["change"] = df["close"] - df["pre_close"]
        df["adjustflag"] = 2
        df["isST"] = df["name"].str.contains("ST", case=False, na=False).astype(int)

        return df[[c for c in STOCK_DAILY_COLUMNS if c in df.columns]]

    def download_range_data(
        self,
        start_date: str,
        end_date: str,
        refresh_basic: bool = False,
        codes_only=None,
    ) -> bool:
        if start_date > end_date:
            logger.info("数据已覆盖至 %s，无需更新", end_date)
            return True

        if codes_only is not None:
            logger.info("Tushare 按日批量模式：忽略 codes_only=%s 只，重拉日期区间", len(codes_only))

        if refresh_basic or self.get_expected_a_share_count() < 3000:
            basic = self.get_stock_basic_info()
            if not basic.empty:
                self.save_stock_basic_data(basic)
                self.load_name_cache()

        dates = self.get_trade_dates(start_date, end_date)
        if not dates:
            logger.warning("区间内无交易日 %s ~ %s", start_date, end_date)
            return False

        self._refresh_latest_adj(end_date)
        ok_days = 0
        for i, d in enumerate(dates, 1):
            logger.info("[%s/%s] 拉取 Tushare 日线 %s", i, len(dates), d)
            df = self._fetch_one_date(d)
            if not df.empty:
                self.save_enhanced_data(df, "stock_daily")
                ok_days += 1
                logger.info("  → 保存 %s 条", len(df))
            else:
                logger.warning("  → %s 无数据", d)
            time.sleep(self.REQUEST_INTERVAL)

        logger.info("Tushare 区间完成 %s ~ %s：%s/%s 个交易日有数据", start_date, end_date, ok_days, len(dates))
        return ok_days > 0

    def login_baostock(self):
        self._init_pro()
        return True

    def logout_baostock(self):
        pass

    def get_enhanced_stock_data(self, code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """单股补数 fallback（区间按日过滤）"""
        ts_code = bs_to_ts(code)
        df = self._call(
            self._init_pro().daily,
            ts_code=ts_code,
            start_date=self._norm_date(start_date),
            end_date=self._norm_date(end_date),
        )
        if df.empty:
            return pd.DataFrame()
        frames = []
        for td in df["trade_date"].unique():
            day = self._fetch_one_date(self._to_db_date(td))
            sub = day[day["symbol"] == code]
            if not sub.empty:
                frames.append(sub)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
