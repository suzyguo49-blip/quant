import argparse
import baostock as bs
import pandas as pd
import sqlite3
import schedule
import time
from datetime import datetime, timedelta
import logging
import os

from providers.stock_schema import (
    STOCK_BASIC_COLUMNS,
    STOCK_DAILY_COLUMNS,
    migrate_stock_schema,
    vacuum_db,
)

class EnhancedStockDataCollector:
    # Baostock 长连接易失效；出现「网络接收错误」时需重登+重试
    REQUEST_INTERVAL = 0.5
    RELOGIN_EVERY = 300
    MAX_RETRIES = 5
    COOLDOWN_AFTER_ERRORS = 30

    def __init__(self, db_path='stock_data.db'):
        self.db_path = db_path
        self._logged_in = False
        self._requests_since_login = 0
        self._consecutive_errors = 0
        self._name_cache = {}
        self.init_enhanced_database()
        self.load_name_cache()

    def init_enhanced_database(self):
        """初始化增强的数据库结构"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 创建股票基本信息表（增强版）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock_basic (
                code TEXT PRIMARY KEY,
                code_name TEXT,
                ipo_date TEXT,
                industry TEXT,
                update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 创建增强的日线行情表（包含股票名称和月份分区键）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock_daily (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                pre_close REAL,
                change REAL,
                pct_chg REAL,
                volume REAL,
                amount REAL,
                adjustflag INTEGER,
                turn REAL,
                isST INTEGER,
                month_key TEXT,
                UNIQUE(symbol, trade_date)
            )
        ''')
        
        # 创建优化索引
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_symbol_date 
            ON stock_daily (symbol, trade_date)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_month 
            ON stock_daily (month_key)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_date 
            ON stock_daily (trade_date)
        ''')
        
        conn.commit()
        conn.close()
        migrate_stock_schema(self.db_path)
        logging.info("增强版数据库初始化完成")

    def get_month_key(self, date_str):
        """从日期字符串生成月份分区键 (YYYY-MM)"""
        try:
            return date_str[:7]  # 提取 YYYY-MM
        except:
            return datetime.now().strftime("%Y-%m")

    def load_name_cache(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                df = pd.read_sql(
                    "SELECT code, code_name FROM stock_basic WHERE code_name IS NOT NULL",
                    conn,
                )
            self._name_cache = dict(zip(df["code"], df["code_name"]))
        except Exception:
            self._name_cache = {}

    @staticmethod
    def _is_retryable_error(msg: str) -> bool:
        if not msg:
            return False
        keys = ("网络", "接收", "socket", "登录", "login", "断开", "超时")
        return any(k in msg for k in keys)

    def _note_request_result(self, ok: bool):
        self._requests_since_login += 1
        if ok:
            self._consecutive_errors = 0
            return
        self._consecutive_errors += 1
        if self._consecutive_errors >= 3:
            logging.warning(
                "连续 %s 次请求失败，%ss 后重连 Baostock...",
                self._consecutive_errors,
                self.COOLDOWN_AFTER_ERRORS,
            )
            time.sleep(self.COOLDOWN_AFTER_ERRORS)
            self.relogin_baostock()
            self._consecutive_errors = 0
        elif self._requests_since_login >= self.RELOGIN_EVERY:
            logging.info("已达 %s 次请求，定期重连 Baostock", self.RELOGIN_EVERY)
            self.relogin_baostock()

    def get_enhanced_stock_data(self, code, start_date, end_date):
        """获取包含股票名称的增强版股票数据（含重试与自动重连）"""
        stock_name = self._name_cache.get(code, "")

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                if not self._logged_in and not self.login_baostock():
                    return pd.DataFrame()

                rs = bs.query_history_k_data_plus(
                    code,
                    "date,open,high,low,close,preclose,volume,amount,adjustflag,turn,pctChg,isST",
                    start_date=start_date,
                    end_date=end_date,
                    frequency="d",
                    adjustflag="2",
                )

                if rs.error_code != "0":
                    msg = rs.error_msg or ""
                    self._note_request_result(False)
                    if self._is_retryable_error(msg) and attempt < self.MAX_RETRIES:
                        wait = min(2 ** attempt, 16)
                        logging.warning(
                            "获取 %s 失败(%s)，%ss 后重试 %s/%s",
                            code, msg, wait, attempt, self.MAX_RETRIES,
                        )
                        time.sleep(wait)
                        continue
                    logging.warning(f"获取 {code} 数据失败: {msg}")
                    return pd.DataFrame()

                data_list = []
                while (rs.error_code == "0") & rs.next():
                    data_list.append(rs.get_row_data())

                self._note_request_result(True)

                if not data_list:
                    return pd.DataFrame()

                result = pd.DataFrame(data_list, columns=rs.fields)
                result["symbol"] = code
                result["name"] = stock_name or code
                result["month_key"] = result["date"].apply(self.get_month_key)

                numeric_columns = [
                    "open", "high", "low", "close", "preclose",
                    "volume", "amount", "turn", "pctChg",
                ]
                for col in numeric_columns:
                    result[col] = pd.to_numeric(result[col], errors="coerce")
                result["adjustflag"] = pd.to_numeric(result["adjustflag"], errors="coerce")
                result["isST"] = pd.to_numeric(result["isST"], errors="coerce")
                result["change"] = result["close"] - result["preclose"]
                result = result.rename(
                    columns={
                        "date": "trade_date",
                        "pctChg": "pct_chg",
                        "preclose": "pre_close",
                    }
                )
                return result

            except Exception as e:
                self._note_request_result(False)
                if attempt < self.MAX_RETRIES:
                    wait = min(2 ** attempt, 16)
                    logging.warning(
                        "获取 %s 异常: %s，%ss 后重试", code, e, wait
                    )
                    time.sleep(wait)
                    continue
                logging.error(f"获取 {code} 增强数据时发生异常: {str(e)}")
                return pd.DataFrame()

        return pd.DataFrame()

    def save_enhanced_data(self, df, table_name='stock_daily'):
        """保存增强版数据到数据库"""
        if df.empty:
            return
            
        try:
            conn = sqlite3.connect(self.db_path)
            
            # 根据表名确定列映射
            if table_name == 'stock_daily':
                columns = list(STOCK_DAILY_COLUMNS)
            else:
                # stock_basic表的列映射
                columns = ['code', 'code_name', 'ipoDate', 'industry']
                # 重命名以匹配数据库列名
                df = df.rename(columns={
                    'code_name': 'code_name',
                    'ipoDate': 'ipo_date'
                })
            
            # 确保DataFrame包含所有需要的列
            for col in columns:
                if col not in df.columns:
                    if col == 'change' and 'close' in df.columns and 'pre_close' in df.columns:
                        df['change'] = df['close'] - df['pre_close']
                    elif col == 'month_key' and 'trade_date' in df.columns:
                        df['month_key'] = df['trade_date'].apply(self.get_month_key)
                    else:
                        # 对于缺失的列，设置为None
                        df[col] = None
            
            # 选择需要的列，只保留表中存在的列
            df_to_save = df[[col for col in columns if col in df.columns]].copy()
            for col in columns:
                if col not in df_to_save.columns:
                    df_to_save[col] = None
            df_to_save = df_to_save[columns]
            
            # 使用INSERT OR REPLACE处理重复数据
            placeholders = ','.join(['?'] * len(df_to_save.columns))
            sql = f'''
                INSERT OR REPLACE INTO {table_name} 
                ({','.join(df_to_save.columns)}) 
                VALUES ({placeholders})
            '''
            
            cursor = conn.cursor()
            cursor.executemany(sql, df_to_save.values.tolist())
            conn.commit()
            conn.close()
            
            logging.info(f"成功保存 {len(df)} 条增强版数据到表 {table_name}")
            
        except Exception as e:
            logging.error(f"保存增强版数据到数据库失败: {str(e)}")
            # 打印详细错误信息以便调试
            logging.error(f"DataFrame columns: {df.columns.tolist()}")
            logging.error(f"Target table: {table_name}")

    def save_stock_basic_data(self, df):
        """专门保存股票基本信息到stock_basic表"""
        if df.empty:
            return
            
        try:
            conn = sqlite3.connect(self.db_path)
            
            # 确保列名匹配
            if 'code_name' in df.columns:
                df = df.rename(columns={'code_name': 'code_name'})
            if 'ipoDate' in df.columns:
                df = df.rename(columns={'ipoDate': 'ipo_date'})
            
            basic_columns = [c for c in STOCK_BASIC_COLUMNS if c != "update_time"]
            df_basic = df[[col for col in basic_columns if col in df.columns]].copy()
            for col in basic_columns:
                if col not in df_basic.columns:
                    df_basic[col] = None
            df_basic = df_basic[basic_columns]
            
            # 添加更新时间
            df_basic['update_time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            placeholders = ','.join(['?'] * len(df_basic.columns))
            sql = f'''
                INSERT OR REPLACE INTO stock_basic 
                ({','.join(df_basic.columns)}) 
                VALUES ({placeholders})
            '''
            
            cursor = conn.cursor()
            cursor.executemany(sql, df_basic.values.tolist())
            conn.commit()
            conn.close()
            
            logging.info(f"成功保存 {len(df_basic)} 条股票基本信息到stock_basic表")
            
        except Exception as e:
            logging.error(f"保存股票基本信息失败: {str(e)}")

    def download_enhanced_history_data(self, years=10):
        """下载增强版历史数据"""
        if not self.login_baostock():
            return False
            
        try:
            end_date = datetime.now().strftime("%Y-%m-%d")
            start_date = (datetime.now() - timedelta(days=years*365)).strftime("%Y-%m-%d")
            
            # 获取股票列表
            stock_basic_df = self.get_stock_basic_info()
            if not stock_basic_df.empty:
                self.save_stock_basic_data(stock_basic_df)
            
            total_stocks = len(stock_basic_df)
            success_count = 0
            
            for index, row in stock_basic_df.iterrows():
                code = row['code']
                logging.info(f"[{index+1}/{total_stocks}] 正在下载 {code} 的增强数据...")
                
                # 获取增强版数据（包含股票名称）
                stock_data = self.get_enhanced_stock_data(code, start_date, end_date)
                
                if not stock_data.empty:
                    self.save_enhanced_data(stock_data, 'stock_daily')
                    success_count += 1
                else:
                    logging.warning(f"未获取到 {code} 的增强数据")
                
                time.sleep(self.REQUEST_INTERVAL)
            
            logging.info(f"增强版历史数据下载完成。成功: {success_count}/{total_stocks}")
            return True
            
        except Exception as e:
            logging.error(f"下载增强版历史数据时发生错误: {str(e)}")
            return False
        finally:
            self.logout_baostock()

    def login_baostock(self):
        """登录Baostock"""
        lg = bs.login()
        if lg.error_code != "0":
            logging.error(f"登录失败: {lg.error_msg}")
            self._logged_in = False
            return False
        self._logged_in = True
        self._requests_since_login = 0
        logging.info("登录Baostock成功")
        return True

    def relogin_baostock(self):
        """断线后重新登录"""
        if self._logged_in:
            try:
                bs.logout()
            except Exception:
                pass
            self._logged_in = False
        time.sleep(2)
        return self.login_baostock()

    def logout_baostock(self):
        """登出Baostock"""
        if self._logged_in:
            try:
                bs.logout()
            except Exception:
                pass
            self._logged_in = False
            logging.info("已登出Baostock")

    def get_stock_basic_info(self):
        """获取股票基本信息"""
        rs = bs.query_stock_basic()
        if rs.error_code != '0':
            logging.error(f"获取股票列表失败: {rs.error_msg}")
            return pd.DataFrame()
        
        data_list = []
        while (rs.error_code == '0') & rs.next():
            data_list.append(rs.get_row_data())
        
        if not data_list:
            return pd.DataFrame()
            
        return pd.DataFrame(data_list, columns=rs.fields)

    def get_earliest_trade_date(self):
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute("SELECT MIN(trade_date) FROM stock_daily").fetchone()
            return row[0] if row and row[0] else None
        finally:
            conn.close()

    def get_latest_trade_date(self):
        """获取数据库中最新的交易日"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(trade_date) FROM stock_daily")
        result = cursor.fetchone()[0]
        conn.close()
        return result

    def count_symbols_on_date(self, trade_date: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT symbol) FROM stock_daily WHERE trade_date = ?",
                (trade_date,),
            ).fetchone()
        return int(row[0] or 0)

    def get_expected_a_share_count(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM stock_basic
                WHERE code LIKE 'sh.6%' OR code LIKE 'sz.0%' OR code LIKE 'sz.3%'
                """
            ).fetchone()
        return int(row[0] or 0)

    def get_incomplete_dates(
        self, start_date: str, end_date: str, min_count: int
    ) -> list:
        """返回区间内覆盖率不足 min_count 的交易日"""
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql(
                """
                SELECT trade_date, COUNT(DISTINCT symbol) AS n
                FROM stock_daily
                WHERE trade_date BETWEEN ? AND ?
                GROUP BY trade_date
                HAVING n < ?
                ORDER BY trade_date
                """,
                conn,
                params=(start_date, end_date, min_count),
            )
        return df["trade_date"].tolist() if not df.empty else []

    def _next_calendar_day(self, date_str):
        return (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    def get_codes_missing_range(self, start_date, end_date):
        """返回在区间内没有任何日线记录的股票代码（用于断点续传）"""
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql(
                """
                SELECT b.code FROM stock_basic b
                WHERE (b.code LIKE 'sh.6%' OR b.code LIKE 'sz.0%' OR b.code LIKE 'sz.3%')
                  AND b.code NOT IN (
                    SELECT DISTINCT symbol FROM stock_daily
                    WHERE trade_date >= ? AND trade_date <= ?
                  )
                ORDER BY b.code
                """,
                conn,
                params=(start_date, end_date),
            )
        return df["code"].tolist()

    def download_range_data(
        self, start_date, end_date, refresh_basic=False, codes_only=None
    ):
        """下载指定日期区间的增量数据（仅拉取缺失区间）"""
        if start_date > end_date:
            logging.info(f"数据已覆盖至 {end_date}，无需更新")
            return True

        if not self.login_baostock():
            return False

        try:
            if codes_only is not None:
                codes = list(codes_only)
                stock_basic_df = pd.DataFrame({"code": codes})
            else:
                stock_basic_df = self.get_stock_basic_info()
                if stock_basic_df.empty:
                    logging.error("无法获取股票列表")
                    return False
                if refresh_basic:
                    self.save_stock_basic_data(stock_basic_df)
                    self.load_name_cache()

            total_stocks = len(stock_basic_df)
            success_count = 0
            fail_count = 0

            logging.info(
                f"增量下载区间: {start_date} ~ {end_date}，共 {total_stocks} 只股票"
            )

            for index, row in stock_basic_df.iterrows():
                code = row["code"]
                logging.info(
                    f"[{index+1}/{total_stocks}] {code} ({start_date} ~ {end_date})"
                )

                stock_data = self.get_enhanced_stock_data(code, start_date, end_date)
                if not stock_data.empty:
                    self.save_enhanced_data(stock_data, "stock_daily")
                    success_count += 1
                else:
                    fail_count += 1

                time.sleep(self.REQUEST_INTERVAL)

            logging.info(
                f"增量下载完成。成功: {success_count}/{total_stocks}，失败: {fail_count}"
            )
            if fail_count:
                logging.info(
                    "若有「网络接收错误」，请稍后执行: "
                    "python dataScrapper.py retry-failed --end %s", end_date
                )
            return fail_count == 0
        except Exception as e:
            logging.error(f"增量下载失败: {str(e)}")
            return False
        finally:
            self.logout_baostock()

    def min_coverage_count(self, min_coverage: float = 0.92) -> int:
        expected = self.get_expected_a_share_count()
        return max(3000, int(expected * min_coverage))

    def is_coverage_ok(
        self, trade_date: str, min_coverage: float = 0.92
    ) -> bool:
        if not trade_date:
            return False
        min_ok = self.min_coverage_count(min_coverage)
        n = self.count_symbols_on_date(trade_date)
        return n >= min_ok

    def retry_failed_in_range(
        self, start_date, end_date, min_coverage: float = 0.92
    ) -> bool:
        """仅重试区间内仍无数据的股票；以覆盖率达标为成功（非零失败可接受）"""
        missing = self.get_codes_missing_range(start_date, end_date)
        if not missing:
            logging.info("区间内无缺失股票，无需重试")
            return True
        logging.info("待重试 %s 只股票", len(missing))
        self.download_range_data(
            start_date, end_date, refresh_basic=False, codes_only=missing
        )
        ok = self.is_coverage_ok(end_date, min_coverage)
        if ok:
            n = self.count_symbols_on_date(end_date)
            logging.info(
                "%s 覆盖率已达标: %s 只 (目标 ≥%s)",
                end_date,
                n,
                self.min_coverage_count(min_coverage),
            )
        else:
            n = self.count_symbols_on_date(end_date)
            logging.warning(
                "%s 覆盖率仍未达标: %s 只 (目标 ≥%s)",
                end_date,
                n,
                self.min_coverage_count(min_coverage),
            )
        return ok

    def backfill_to_date(self, end_date):
        """从数据库最新交易日之后补全到指定日期"""
        latest = self.get_latest_trade_date()
        if latest:
            start_date = self._next_calendar_day(latest)
        else:
            start_date = (datetime.now() - timedelta(days=365 * 10)).strftime("%Y-%m-%d")

        today = datetime.now().strftime("%Y-%m-%d")
        target_end = min(end_date, today)
        return self.download_range_data(start_date, target_end, refresh_basic=True)

    def download_daily_update(self):
        """收盘后更新：补全自最新交易日至今日的数据（不校验覆盖率）"""
        today = datetime.now().strftime("%Y-%m-%d")
        latest = self.get_latest_trade_date()
        start_date = self._next_calendar_day(latest) if latest else today
        return self.download_range_data(start_date, today, refresh_basic=False)

    def download_daily_full_update(
        self,
        lookback_days: int = 14,
        min_coverage: float = 0.92,
        max_passes: int = 5,
    ) -> bool:
        """收盘后全量更新：新交易日 + 近 N 日覆盖率不足则 retry-failed 直至达标"""
        today = datetime.now().strftime("%Y-%m-%d")
        expected = self.get_expected_a_share_count()
        if expected < 3000:
            logging.info("stock_basic 过少，先刷新 A 股列表…")
            if not self.login_baostock():
                return False
            try:
                basic = self.get_stock_basic_info()
                if basic.empty:
                    return False
                self.save_stock_basic_data(basic)
                self.load_name_cache()
                expected = self.get_expected_a_share_count()
            finally:
                self.logout_baostock()

        min_ok = self.min_coverage_count(min_coverage)
        logging.info(
            "全量完整性目标: ≥%s 只 (universe=%s, %.0f%%)",
            min_ok,
            expected,
            min_coverage * 100,
        )

        latest = self.get_latest_trade_date()
        start_new = self._next_calendar_day(latest) if latest else today
        if start_new <= today:
            logging.info("拉取新交易日 %s ~ %s", start_new, today)
            self.download_range_data(start_new, today, refresh_basic=False)

        lookback_start = (
            datetime.now() - timedelta(days=lookback_days)
        ).strftime("%Y-%m-%d")
        for pass_i in range(1, max_passes + 1):
            incomplete = self.get_incomplete_dates(lookback_start, today, min_ok)
            if not incomplete:
                logging.info("近 %s 日覆盖率已达标", lookback_days)
                break
            logging.info("第 %s/%s 轮缺口补全: %s", pass_i, max_passes, incomplete)
            for d in incomplete:
                self.retry_failed_in_range(d, d)
        else:
            incomplete = self.get_incomplete_dates(lookback_start, today, min_ok)
            if incomplete:
                logging.warning("下列日期仍未达覆盖率阈值: %s", incomplete)
                return False

        latest = self.get_latest_trade_date()
        if latest:
            n = self.count_symbols_on_date(latest)
            logging.info("最新交易日 %s: %s 只 (目标 ≥%s)", latest, n, min_ok)
            return n >= min_ok
        return False

    def check_table_schema(self, table_name):
        """检查表结构（用于调试）"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(f"PRAGMA table_info({table_name})")
        schema = cursor.fetchall()
        conn.close()
        logging.info(f"表 {table_name} 的结构: {schema}")
        return schema

# 实用的月度查询函数
def query_monthly_data(db_path, year_month):
    """查询指定月份的所有数据"""
    conn = sqlite3.connect(db_path)
    query = "SELECT * FROM stock_daily WHERE month_key = ? ORDER BY trade_date, symbol"
    df = pd.read_sql_query(query, conn, params=(year_month,))
    conn.close()
    return df

def get_monthly_summary(db_path):
    """获取月度数据统计摘要"""
    conn = sqlite3.connect(db_path)
    query = """
        SELECT 
            month_key,
            COUNT(DISTINCT symbol) as stock_count,
            COUNT(*) as record_count,
            MIN(trade_date) as start_date,
            MAX(trade_date) as end_date,
            SUM(volume) as total_volume
        FROM stock_daily 
        GROUP BY month_key 
        ORDER BY month_key DESC
    """
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df

def fix_existing_database(db_path):
    """修复现有数据库的表结构（如果需要修改现有表）[6,7](@ref)"""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    try:
        # 检查stock_daily表是否有preclose列而不是pre_close
        cursor.execute("PRAGMA table_info(stock_daily)")
        columns = [col[1] for col in cursor.fetchall()]
        
        if 'preclose' in columns and 'pre_close' not in columns:
            # 重命名列
            cursor.execute('ALTER TABLE stock_daily RENAME COLUMN preclose TO pre_close')
            logging.info("已修复stock_daily表列名: preclose -> pre_close")
        
        # 检查stock_basic表是否有symbol列而不是code
        cursor.execute("PRAGMA table_info(stock_basic)")
        columns = [col[1] for col in cursor.fetchall()]
        
        if 'symbol' in columns and 'code' not in columns:
            # 重命名列
            cursor.execute('ALTER TABLE stock_basic RENAME COLUMN symbol TO code')
            logging.info("已修复stock_basic表列名: symbol -> code")
            
    except Exception as e:
        logging.warning(f"修复表结构时发生错误（可能不需要修复）: {e}")
    finally:
        conn.commit()
        conn.close()

import subprocess
import signal

class CaffeinatePreventer:
    """
    使用 macOS 原生 caffeinate 命令的防休眠方案（更可靠）
    """
    
    def __init__(self):
        self.caffeinate_process = None
        
    def start_keep_awake(self):
        """启动 caffeinate 进程防止休眠"""
        try:
            # 启动 caffeinate 进程（防止系统休眠和显示器关闭）
            self.caffeinate_process = subprocess.Popen([
                'caffeinate', '-d', '-i', '-s'
            ])
            logging.info("caffeinate 进程已启动，系统将保持唤醒状态")
            return True
        except Exception as e:
            logging.error(f"启动 caffeinate 失败: {str(e)}")
            return False
            
    def stop_keep_awake(self):
        """停止 caffeinate 进程"""
        if self.caffeinate_process:
            try:
                self.caffeinate_process.terminate()
                self.caffeinate_process.wait(timeout=5)
                logging.info("caffeinate 进程已停止")
            except Exception as e:
                # 如果正常终止失败，强制杀死进程
                try:
                    self.caffeinate_process.kill()
                    self.caffeinate_process.wait(timeout=2)
                except:
                    pass
                logging.error(f"停止 caffeinate 进程失败: {str(e)}")

class RobustMacEnhancedStockDataCollector(EnhancedStockDataCollector):
    """
    使用 caffeinate 的更可靠版本
    """
    
    def __init__(self, db_path='stock_data.db'):
        super().__init__(db_path)
        self.caffeinate_preventer = CaffeinatePreventer()

    def robust_download_for_mac(self, years=10):
        """
        使用 caffeinate 的可靠下载方法
        """
        try:
            # 启动防休眠模式
            if not self.caffeinate_preventer.start_keep_awake():
                logging.warning("caffeinate 启动失败，但将继续尝试下载数据")
            
            # 执行数据下载
            return self.download_enhanced_history_data(years)
            
        except KeyboardInterrupt:
            logging.info("用户中断了下载过程")
            return False
        except Exception as e:
            logging.error(f"数据下载过程中发生错误: {str(e)}")
            return False
        finally:
            # 确保清理 caffeinate 进程
            self.caffeinate_preventer.stop_keep_awake()

# 信号处理改进
def setup_signal_handlers():
    """设置信号处理器以确保程序可以优雅退出"""
    import signal
    
    def signal_handler(signum, frame):
        logging.info(f"收到信号 {signum}，正在优雅退出...")
        # 这里可以添加清理逻辑
        raise KeyboardInterrupt("用户中断")
    
    signal.signal(signal.SIGINT, signal_handler)  # Ctrl+C
    signal.signal(signal.SIGTERM, signal_handler)  # 终止信号

def make_collector(db_path: str = "stock_data.db", source=None):
    """创建数据采集器。source: tushare | baostock，默认 tushare（可用 QUANT_DATA_SOURCE 覆盖）。"""
    src = (source or os.environ.get("QUANT_DATA_SOURCE") or "tushare").strip().lower()
    collector = RobustMacEnhancedStockDataCollector(db_path)
    if src == "tushare":
        from providers.tushare_collector import TushareEnhancedStockDataCollector

        ts = TushareEnhancedStockDataCollector(db_path, base=collector)
        for name in (
            "get_stock_basic_info",
            "download_range_data",
            "get_enhanced_stock_data",
            "login_baostock",
            "logout_baostock",
        ):
            setattr(collector, name, getattr(ts, name))

        def tushare_min_coverage_count(min_coverage: float = 0.92) -> int:
            # Tushare 按日批量：停牌/未交易不入库，~88% 上市代码即满覆盖
            eff = min(float(min_coverage), 0.88)
            expected = collector.get_expected_a_share_count()
            return max(4800, int(expected * eff))

        collector.min_coverage_count = tushare_min_coverage_count
        logging.info("数据源: Tushare Pro")
    elif src == "baostock":
        logging.info("数据源: Baostock")
    else:
        raise ValueError(f"未知数据源: {src}，可选 tushare / baostock")
    return collector


def setup_logging(log_file=None):
    handlers = [logging.StreamHandler()]
    if log_file:
        os.makedirs(os.path.dirname(log_file) or '.', exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding='utf-8'))
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=handlers,
        force=True,
    )


def run_daemon_scheduler(collector, run_time="16:30"):
    """常驻进程：每个交易日收盘后定时拉取当日数据"""
    def job():
        logging.info("定时任务触发：开始每日增量更新")
        collector.download_daily_update()

    schedule.every().monday.at(run_time).do(job)
    schedule.every().tuesday.at(run_time).do(job)
    schedule.every().wednesday.at(run_time).do(job)
    schedule.every().thursday.at(run_time).do(job)
    schedule.every().friday.at(run_time).do(job)

    logging.info(f"定时调度已启动，每个交易日 {run_time} 执行每日更新")
    while True:
        schedule.run_pending()
        time.sleep(60)


def parse_args():
    parser = argparse.ArgumentParser(description="A股日线数据爬取与定时更新")
    parser.add_argument(
        '--db',
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stock_data.db'),
        help='SQLite 数据库路径',
    )
    parser.add_argument(
        '--source',
        default=None,
        choices=('tushare', 'baostock'),
        help='数据源（默认 tushare；也可用环境变量 QUANT_DATA_SOURCE）',
    )
    sub = parser.add_subparsers(dest='command', required=True)

    backfill = sub.add_parser('backfill', help='补全历史数据到指定日期')
    backfill.add_argument('--end', required=True, help='目标结束日期，如 2026-05-16')

    retry = sub.add_parser(
        'retry-failed', help='仅重试区间内仍无行情的股票（断点续传）'
    )
    retry.add_argument('--start', default=None, help='区间开始，默认 2025-11-29')
    retry.add_argument('--end', required=True, help='区间结束，如 2026-05-16')

    sub.add_parser('daily', help='每日收盘后全量更新（新日+缺口重试至覆盖率达标）')
    sub.add_parser('daily-quick', help='仅拉取新交易日，不校验全市场覆盖率')

    daemon = sub.add_parser('daemon', help='常驻进程，交易日收盘后定时更新')
    daemon.add_argument('--time', default='16:30', help='每日执行时间，默认 16:30')

    full = sub.add_parser('full', help='全量下载近 N 年数据')
    full.add_argument('--years', type=int, default=10)

    rebuild = sub.add_parser(
        'tushare-rebuild',
        help='Tushare 按日覆盖重建（扩展字段，INSERT OR REPLACE，不增行数）',
    )
    rebuild.add_argument('--start', default=None, help='起始交易日，默认库内最早日')
    rebuild.add_argument('--end', default=None, help='结束交易日，默认今天')
    rebuild.add_argument(
        '--vacuum',
        action='store_true',
        help='重建完成后 VACUUM 回收 REPLACE 产生的空闲页',
    )

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
    log_file = os.path.join(log_dir, f"stock_scraper_{datetime.now().strftime('%Y%m%d')}.log")
    setup_logging(log_file)
    setup_signal_handlers()

    collector = make_collector(db_path=args.db, source=args.source)

    try:
        if args.command == 'backfill':
            print(f"开始补全数据至 {args.end} ...")
            if collector.caffeinate_preventer.start_keep_awake():
                success = collector.backfill_to_date(args.end)
            else:
                success = collector.backfill_to_date(args.end)
            collector.caffeinate_preventer.stop_keep_awake()
        elif args.command == 'retry-failed':
            start = args.start or "2025-11-29"
            print(f"重试缺失股票 {start} ~ {args.end} ...")
            success = collector.retry_failed_in_range(start, args.end)
        elif args.command == 'daily':
            print("开始每日全量更新（新交易日 + 缺口重试）...")
            if collector.caffeinate_preventer.start_keep_awake():
                success = collector.download_daily_full_update()
            else:
                success = collector.download_daily_full_update()
        elif args.command == 'daily-quick':
            print("开始快速增量（不校验覆盖率）...")
            success = collector.download_daily_update()
        elif args.command == 'daemon':
            if collector.caffeinate_preventer.start_keep_awake():
                logging.info("防休眠已启用")
            run_daemon_scheduler(collector, run_time=args.time)
            success = True
        elif args.command == 'full':
            success = collector.robust_download_for_mac(years=args.years)
        elif args.command == 'tushare-rebuild':
            if (args.source or os.environ.get('QUANT_DATA_SOURCE', 'tushare')).lower() != 'tushare':
                print("tushare-rebuild 需 --source tushare")
                success = False
            else:
                start = args.start or collector.get_earliest_trade_date()
                end = args.end or datetime.now().strftime("%Y-%m-%d")
                if not start:
                    print("库内无历史数据，请指定 --start")
                    success = False
                else:
                    print(f"Tushare 覆盖重建 {start} ~ {end}（扩展字段，原位 REPLACE）...")
                    if collector.caffeinate_preventer.start_keep_awake():
                        success = collector.download_range_data(start, end, refresh_basic=True)
                    else:
                        success = collector.download_range_data(start, end, refresh_basic=True)
                    if success and args.vacuum:
                        vacuum_db(args.db)
        else:
            success = False

        if args.command != 'daemon':
            if success:
                latest = collector.get_latest_trade_date()
                print(f"✅ 完成！数据库最新交易日: {latest}")
            else:
                print("❌ 执行失败，请查看日志")
    except KeyboardInterrupt:
        print("\n程序被用户中断")
    finally:
        collector.caffeinate_preventer.stop_keep_awake()