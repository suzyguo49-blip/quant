import argparse
import baostock as bs
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import logging
import sqlite3
import warnings

from providers.pe_growth import PeGrowthGate

warnings.filterwarnings('ignore')

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / 'stock_data.db'
SETUP_TAG = 'momentum_trend_5'

# 设置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def _bt():
    import backtrader as bt
    return bt


def _make_momentum_strategy_class():
    bt = _bt()

    class MomentumTrendStrategy(bt.Strategy):
        """动量+趋势跟踪（backtest-bt 小样例用）"""

        params = dict(
            momentum_period=5,
            trend_period=20,
            top_stocks=10,
            printlog=True,
        )

        def __init__(self):
            self.indicators = {}
            for data in self.datas:
                self.indicators[data._name] = {
                    'sma': bt.ind.SMA(data.close, period=self.p.trend_period),
                    'momentum': (data.close / data.close(-self.p.momentum_period)) - 1,
                }
            self.orders = {}
            self.stock_rankings = []

        def next(self):
            self.calculate_momentum_ranking()
            target_stocks = [s[0] for s in self.stock_rankings[: self.p.top_stocks]]
            self.rebalance_portfolio(target_stocks)

        def calculate_momentum_ranking(self):
            rankings = []
            for data in self.datas:
                if len(data) > self.p.momentum_period:
                    symbol = data._name
                    current_price = data.close[0]
                    sma = self.indicators[symbol]['sma'][0]
                    momentum = self.indicators[symbol]['momentum'][0]
                    if current_price > sma and not np.isnan(momentum):
                        rankings.append((symbol, momentum))
            rankings.sort(key=lambda x: x[1], reverse=True)
            self.stock_rankings = rankings

        def rebalance_portfolio(self, target_stocks):
            if not target_stocks:
                return
            total_value = self.broker.getvalue()
            cash_per_stock = total_value * 0.9 / len(target_stocks)
            for data in self.datas:
                pos = self.getposition(data).size
                symbol = data._name
                if pos > 0 and symbol not in target_stocks:
                    self.close(data=data)
                    if self.p.printlog:
                        self.log(f'卖出 {symbol}, 不在目标池')
                elif symbol in target_stocks and pos == 0:
                    size = int(cash_per_stock / data.close[0] / 100) * 100
                    if size > 0 and data.close[0] > self.indicators[symbol]['sma'][0]:
                        self.buy(data=data, size=size)
                        if self.p.printlog:
                            self.log(f'买入 {symbol}, 价格: {data.close[0]:.2f}')

        def log(self, txt, dt=None):
            dt = dt or self.datas[0].datetime.date(0)
            print(f'{dt.isoformat()}, {txt}')

        def notify_order(self, order):
            if order.status in [order.Submitted, order.Accepted]:
                return
            if order.status in [order.Completed]:
                if order.isbuy():
                    self.log(f'买入执行: {order.data._name}, 价格: {order.executed.price:.2f}')
                else:
                    self.log(f'卖出执行: {order.data._name}, 价格: {order.executed.price:.2f}')
            elif order.status in [order.Canceled, order.Margin, order.Rejected]:
                self.log('订单取消/失败')

    return MomentumTrendStrategy


class StockDataLoader:
    """股票数据加载器"""
    
    def __init__(self, db_path='stock_data.db'):
        self.db_path = db_path
    
    def get_stock_codes(self, date=None):
        """获取股票代码列表（示例，实际使用时需要调整）"""
        # 这里可以替换为您的股票代码获取逻辑
        sample_stocks = [
            'sh.600000',  # 浦发银行
            'sh.600036',  # 招商银行  
            'sz.000001',  # 平安银行
            'sz.000858',  # 五粮液
            'sh.600519',  # 贵州茅台
            'sz.000333',  # 美的集团
            'sh.601318',  # 中国平安
            'sz.000651',  # 格力电器
        ]
        return sample_stocks[:10]  # 限制股票数量便于测试
    
    def load_stock_data(self, code, start_date, end_date):
        """从数据库加载股票数据"""
        try:
            conn = sqlite3.connect(self.db_path)
            query = """
                SELECT trade_date, open, high, low, close, volume, amount 
                FROM stock_daily 
                WHERE symbol = ? AND trade_date BETWEEN ? AND ?
                ORDER BY trade_date
            """
            df = pd.read_sql_query(query, conn, params=(code, start_date, end_date))
            conn.close()
            
            if df.empty:
                return None
            
            # 转换为backtrader格式
            df['trade_date'] = pd.to_datetime(df['trade_date'])
            df.set_index('trade_date', inplace=True)
            df['openinterest'] = 0
            
            # 确保数据列名正确
            df = df.rename(columns={
                'open': 'open', 'high': 'high', 'low': 'low', 
                'close': 'close', 'volume': 'volume'
            })
            
            bt = _bt()
            return bt.feeds.PandasData(
                dataname=df,
                datetime=None,
                open=0, high=1, low=2, close=3, volume=4, openinterest=5,
            )
            
        except Exception as e:
            logging.error(f"加载股票 {code} 数据失败: {str(e)}")
            return None

def run_backtest():
    """运行回测（8 只样例股，需安装 backtrader）"""
    bt = _bt()
    StrategyCls = _make_momentum_strategy_class()
    logging.info("开始回测...")
    
    cerebro = bt.Cerebro()
    
    # 2. 设置初始资金和手续费
    initial_cash = 100000
    cerebro.broker.setcash(initial_cash)
    cerebro.broker.setcommission(commission=0.001)  # 0.1%手续费
    
    # 3. 加载数据
    data_loader = StockDataLoader()
    start_date = '2024-01-01'
    end_date = '2025-11-30'
    
    stock_codes = data_loader.get_stock_codes()
    data_count = 0
    
    for code in stock_codes:
        data = data_loader.load_stock_data(code, start_date, end_date)
        if data is not None and len(data) > 100:  # 确保有足够数据
            cerebro.adddata(data, name=code)
            data_count += 1
            logging.info(f"已加载 {code} 数据，共 {len(data)} 条记录")
    
    if data_count == 0:
        logging.error("没有加载到有效数据，请检查数据库")
        return None
    
    logging.info(f"成功加载 {data_count} 只股票数据")
    
    # 4. 添加策略
    cerebro.addstrategy(StrategyCls, printlog=True)
    
    # 5. 添加分析器
    cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe', riskfreerate=0.03)
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trades')
    
    # 6. 运行回测
    logging.info("开始执行回测...")
    results = cerebro.run()
    strategy = results[0]
    
    # 7. 输出结果
    print("\n" + "="*60)
    print("回测结果汇总")
    print("="*60)
    
    # 最终资金
    final_value = cerebro.broker.getvalue()
    total_return = (final_value - initial_cash) / initial_cash * 100
    
    print(f"初始资金: {initial_cash:,.2f}元")
    print(f"最终资金: {final_value:,.2f}元")
    print(f"总收益率: {total_return:.2f}%")
    
    # 分析器结果
    returns_analysis = strategy.analyzers.returns.get_analysis()
    drawdown_analysis = strategy.analyzers.drawdown.get_analysis()
    sharpe_analysis = strategy.analyzers.sharpe.get_analysis()
    trade_analysis = strategy.analyzers.trades.get_analysis()
    
    if 'rnorm100' in returns_analysis:
        annual_return = returns_analysis['rnorm100']
        print(f"年化收益率: {annual_return:.2f}%")
    
    if 'max' in drawdown_analysis:
        max_drawdown = drawdown_analysis.max.drawdown
        print(f"最大回撤: {abs(max_drawdown):.2f}%")
    
    if 'sharperatio' in sharpe_analysis:
        sharpe_ratio = sharpe_analysis['sharperatio']
        print(f"夏普比率: {sharpe_ratio:.2f}")
    
    if 'total' in trade_analysis:
        total_trades = trade_analysis.total.closed
        won_trades = trade_analysis.won.total if trade_analysis.won else 0
        win_rate = (won_trades / total_trades * 100) if total_trades > 0 else 0
        print(f"总交易次数: {total_trades}")
        print(f"胜率: {win_rate:.2f}%")
    
    # 8. 绘图
    try:
        cerebro.plot(style='candlestick', volume=False)
    except Exception as e:
        logging.warning(f"绘图失败: {e}, 尝试简单绘图...")
        cerebro.plot(style='line')
    
    return strategy

def optimize_parameters():
    """参数优化函数"""
    bt = _bt()
    StrategyCls = _make_momentum_strategy_class()
    cerebro = bt.Cerebro(optreturn=False)
    
    # 添加数据（简化示例）
    data_loader = StockDataLoader()
    stock_codes = data_loader.get_stock_codes()[:5]  # 少量股票加速优化
    
    for code in stock_codes:
        data = data_loader.load_stock_data(code, '2024-01-01', '2025-11-30')
        if data is not None:
            cerebro.adddata(data, name=code)
    
    # 参数优化范围
    cerebro.optstrategy(
        StrategyCls,
        momentum_period=range(20, 40, 5),      # 动量周期20-35天
        trend_period=range(15, 25, 5),        # 均线周期15-20天
        top_stocks=range(5, 15, 5)           # 持仓股票数5-10只
    )
    
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe')
    
    # 运行优化（耗时操作，谨慎使用）
    logging.info("开始参数优化...")
    opt_results = cerebro.run(maxcpus=1)
    
    # 找出最佳参数
    best_sharpe = -999
    best_params = {}
    
    for result in opt_results:
        for strategy in result:
            if strategy.analyzers.sharpe.get_analysis():
                sharpe = strategy.analyzers.sharpe.get_analysis().get('sharperatio', -999)
                if sharpe > best_sharpe:
                    best_sharpe = sharpe
                    best_params = {
                        'momentum_period': strategy.params.momentum_period,
                        'trend_period': strategy.params.trend_period,
                        'top_stocks': strategy.params.top_stocks
                    }
    
    print(f"最佳参数: {best_params}")
    print(f"最佳夏普比率: {best_sharpe:.2f}")
    
    return best_params


def is_a_share(code: str) -> bool:
    if not code or len(code) < 8:
        return False
    if code.startswith('sh.000') or code.startswith('sz.399'):
        return False
    return code.startswith('sh.6') or code.startswith('sz.0') or code.startswith('sz.3')


def _lot_shares(budget: float, price: float) -> int:
    if not np.isfinite(budget) or not np.isfinite(price) or price <= 0 or budget <= 0:
        return 0
    return int(budget / price / 100) * 100


def _price_at(closes: pd.DataFrame, sym: str, dt: pd.Timestamp) -> float:
    """当日收盘价；缺失时用最近有效价（仅估值/容错）"""
    if sym not in closes.columns:
        return float('nan')
    px = closes.at[dt, sym] if dt in closes.index else float('nan')
    if pd.notna(px) and float(px) > 0:
        return float(px)
    hist = closes[sym].loc[:dt].dropna()
    return float(hist.iloc[-1]) if len(hist) else float('nan')


def load_close_panel(db_path: Path, data_start: str, end: str) -> tuple[pd.DataFrame, pd.Series]:
    """加载全市场收盘价矩阵与 ST 标记（name 含 ST）"""
    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql(
            """
            SELECT symbol, trade_date, close, name
            FROM stock_daily
            WHERE trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """,
            conn,
            params=(data_start, end),
        )
    if df.empty:
        return pd.DataFrame(), pd.Series(dtype=bool)

    df = df[df['symbol'].map(is_a_share)]
    df['close'] = pd.to_numeric(df['close'], errors='coerce')
    df = df.dropna(subset=['close'])
    df['trade_date'] = pd.to_datetime(df['trade_date'])

    st_flag = (
        df.groupby('symbol')['name']
        .last()
        .fillna('')
        .str.contains('ST', case=False, na=False)
    )

    closes = df.pivot_table(index='trade_date', columns='symbol', values='close', aggfunc='last')
    closes = closes.sort_index()
    return closes, st_flag


def select_target_pool(
    closes: pd.DataFrame,
    dt: pd.Timestamp,
    st_flag: pd.Series,
    momentum_period: int,
    trend_period: int,
    top_stocks: int,
    exclude_symbols: Optional[list] = None,
) -> list[str]:
    """E-001：5日动量前 top_stocks，且收盘 > MA20，排除 ST"""
    picks = rank_momentum_candidates(
        closes, dt, st_flag, momentum_period, trend_period, top_stocks, exclude_symbols
    )
    return [p['symbol'] for p in picks]


def rank_momentum_candidates(
    closes: pd.DataFrame,
    dt: pd.Timestamp,
    st_flag: pd.Series,
    momentum_period: int,
    trend_period: int,
    top_stocks: int,
    exclude_symbols: Optional[list] = None,
    pe_gate=None,
) -> list[dict]:
    """返回动量候选明细（含 rank / mom / ma20）"""
    exclude = set(exclude_symbols or [])
    hist = closes.loc[:dt]
    if len(hist) < trend_period + 1:
        return []
    row = hist.iloc[-1]
    mom = hist.iloc[-1] / hist.iloc[-1 - momentum_period] - 1
    ma = hist.rolling(trend_period).mean().iloc[-1]
    eligible = (row > ma) & mom.notna() & (row > 0)
    eligible = eligible & ~st_flag.reindex(eligible.index, fill_value=False)
    ranked = mom[eligible].sort_values(ascending=False)
    out = []
    for sym in ranked.index:
        if sym in exclude:
            continue
        if pe_gate is not None and not pe_gate.ok(sym):
            continue
        out.append({
            'symbol': sym,
            'close': float(row[sym]),
            'ma20': float(ma[sym]),
            'mom5': float(mom[sym]),
            'rank': len(out) + 1,
        })
        if len(out) >= top_stocks:
            break
    return out


def scan_momentum_picks(
    db_path: Path,
    as_of: str,
    top_stocks: int = 4,
    momentum_period: int = 5,
    trend_period: int = 20,
    exclude_symbols: Optional[list] = None,
) -> list[dict]:
    """收盘后动量池扫描（momentum_trend_5 / E-001）"""
    data_start = (pd.Timestamp(as_of) - pd.Timedelta(days=45)).strftime('%Y-%m-%d')
    closes, st_flag = load_close_panel(db_path, data_start, as_of)
    if closes.empty:
        return []
    dt = pd.Timestamp(as_of)
    if dt not in closes.index:
        valid = closes.index[closes.index <= dt]
        if len(valid) == 0:
            return []
        dt = valid[-1]

    pe_gate = PeGrowthGate(db_path, pd.Timestamp(dt).strftime("%Y-%m-%d"))
    picks = rank_momentum_candidates(
        closes, dt, st_flag, momentum_period, trend_period, top_stocks, exclude_symbols, pe_gate=pe_gate
    )
    if not picks:
        return picks

    syms = [p['symbol'] for p in picks]
    with sqlite3.connect(db_path) as conn:
        placeholders = ','.join(['?'] * len(syms))
        names = pd.read_sql(
            f"""
            SELECT symbol, name FROM stock_daily
            WHERE symbol IN ({placeholders}) AND trade_date <= ?
            ORDER BY trade_date DESC
            """,
            conn,
            params=syms + [as_of],
        )
    name_map = names.drop_duplicates('symbol').set_index('symbol')['name'].to_dict()
    for p in picks:
        p['name'] = str(name_map.get(p['symbol'], p['symbol']))
        p['mom5_pct'] = round(p['mom5'] * 100, 2)
    return picks


def run_momentum_backtest(
    db_path: Path,
    start: str,
    end: str,
    momentum_period: int = 5,
    trend_period: int = 20,
    top_stocks: int = 10,
    initial_cash: float = 100_000.0,
    invest_ratio: float = 0.9,
    commission: float = 0.001,
    save_csv: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    全市场 pandas 回测（setup_tag=momentum_trend_5）
  每日收盘调仓：动量前 N + 站上 MA20，等权，整手。
    """
    data_start = (pd.Timestamp(start) - pd.Timedelta(days=45)).strftime('%Y-%m-%d')
    closes, st_flag = load_close_panel(db_path, data_start, end)
    if closes.empty:
        logging.error('未加载到行情，请检查 %s', db_path)
        return pd.DataFrame(), pd.DataFrame()

    trade_dates = closes.index[(closes.index >= pd.Timestamp(start)) & (closes.index <= pd.Timestamp(end))]
    if len(trade_dates) == 0:
        logging.error('回测区间无交易日')
        return pd.DataFrame(), pd.DataFrame()

    cash = initial_cash
    positions: dict[str, dict] = {}
    daily_rows = []
    trade_rows = []
    prev_total = initial_cash

    for dt in trade_dates:
        target = select_target_pool(
            closes, dt, st_flag, momentum_period, trend_period, top_stocks
        )
        price_row = closes.loc[dt]
        actions = []

        # 卖出：不在目标池或破 MA20
        hist = closes.loc[:dt]
        ma_row = hist.rolling(trend_period).mean().iloc[-1] if len(hist) >= trend_period else pd.Series()
        for sym in list(positions.keys()):
            raw_px = price_row.get(sym) if sym in price_row.index else float('nan')
            if pd.isna(raw_px):
                continue
            px = float(raw_px)
            if sym not in target or (sym in ma_row.index and px < ma_row[sym]):
                pos = positions.pop(sym)
                proceeds = pos['shares'] * float(px) * (1 - commission)
                cash += proceeds
                pnl = (float(px) / pos['entry_price'] - 1) * 100
                trade_rows.append({
                    'symbol': sym,
                    'setup_tag': SETUP_TAG,
                    'entry_date': pos['entry_date'],
                    'exit_date': dt.strftime('%Y-%m-%d'),
                    'entry_price': pos['entry_price'],
                    'exit_price': float(px),
                    'pnl_pct': round(pnl, 4),
                    'exit_reason': 'rank_or_ma',
                })
                actions.append(f'卖出 {sym}')

        # 买入 / 调仓至等权
        if target:
            stock_val = sum(
                positions[s]['shares'] * _price_at(closes, s, dt)
                for s in positions
                if np.isfinite(_price_at(closes, s, dt))
            )
            total = cash + stock_val
            if not np.isfinite(total) or total <= 0:
                total = prev_total
            deploy = total * invest_ratio
            per_slot = deploy / len(target) if target else 0
            if not np.isfinite(per_slot) or per_slot <= 0:
                per_slot = 0
            for sym in target:
                raw_px = price_row.get(sym) if sym in price_row.index else float('nan')
                if pd.isna(raw_px) or float(raw_px) <= 0:
                    continue
                px = float(raw_px)
                cur_shares = positions.get(sym, {}).get('shares', 0)
                target_shares = _lot_shares(per_slot, float(px))
                if target_shares <= cur_shares:
                    continue
                buy_shares = target_shares - cur_shares
                cost = buy_shares * float(px) * (1 + commission)
                if cost > cash or buy_shares <= 0:
                    continue
                cash -= cost
                if sym in positions:
                    old = positions[sym]
                    old_cost = old['shares'] * old['entry_price']
                    new_cost = buy_shares * float(px)
                    new_shares = old['shares'] + buy_shares
                    positions[sym] = {
                        'shares': new_shares,
                        'entry_price': (old_cost + new_cost) / new_shares,
                        'entry_date': old['entry_date'],
                    }
                else:
                    positions[sym] = {
                        'shares': buy_shares,
                        'entry_price': float(px),
                        'entry_date': dt.strftime('%Y-%m-%d'),
                    }
                actions.append(f'买入 {sym} {buy_shares}股')

        stock_value = sum(
            positions[s]['shares'] * _price_at(closes, s, dt)
            for s in positions
            if np.isfinite(_price_at(closes, s, dt))
        )
        total = cash + stock_value
        day_ret = (total / prev_total - 1) * 100 if prev_total > 0 else 0.0
        cum_ret = (total / initial_cash - 1) * 100
        daily_rows.append({
            'date': dt.strftime('%Y-%m-%d'),
            'holdings': ','.join(positions.keys()) if positions else '现金',
            'n_holdings': len(positions),
            'target_pool': ','.join(target) if target else '',
            'cash': round(cash, 2),
            'stock_value': round(stock_value, 2),
            'total': round(total, 2),
            'day_return_pct': round(day_ret, 4),
            'cum_return_pct': round(cum_ret, 4),
            'actions': '; '.join(actions) if actions else '—',
        })
        prev_total = total

    daily = pd.DataFrame(daily_rows)
    trades = pd.DataFrame(trade_rows)
    _print_momentum_report(
        daily, trades, start, end,
        momentum_period, trend_period, top_stocks, initial_cash,
    )

    if save_csv and not daily.empty:
        out_dir = ROOT / 'docs/trading-system/backtests'
        out_dir.mkdir(parents=True, exist_ok=True)
        tag = f"{start}_{end}_m5".replace('-', '')
        daily_path = out_dir / f'momentum_trend_5_journal_{tag}.csv'
        trades_path = out_dir / f'momentum_trend_5_trades_{tag}.csv'
        daily.to_csv(daily_path, index=False, encoding='utf-8-sig')
        if not trades.empty:
            trades.to_csv(trades_path, index=False, encoding='utf-8-sig')
        print(f'\n日记已保存: {daily_path}')
        if not trades.empty:
            print(f'交易明细已保存: {trades_path}')

    return daily, trades


def _print_momentum_report(
    daily: pd.DataFrame,
    trades: pd.DataFrame,
    start: str,
    end: str,
    momentum_period: int,
    trend_period: int,
    top_stocks: int,
    initial_cash: float,
):
    print('\n' + '=' * 68)
    print(f'回测 {SETUP_TAG}  {start} ~ {end}')
    print(
        f'参数: {momentum_period}日动量 | MA{trend_period} | 持仓{top_stocks}只 | '
        f'期初 {initial_cash:,.0f} 元'
    )
    print('=' * 68)
    if daily.empty:
        print('无回测结果')
        return

    final = float(daily.iloc[-1]['total'])
    total_ret = (final / initial_cash - 1) * 100
    max_dd = 0.0
    peak = initial_cash
    for t in daily['total']:
        v = float(t)
        peak = max(peak, v)
        dd = (v / peak - 1) * 100
        max_dd = min(max_dd, dd)

    rets = daily['day_return_pct'].astype(float)
    sharpe = 0.0
    if rets.std() > 0:
        sharpe = (rets.mean() / rets.std()) * (252 ** 0.5)

    print(f'交易日: {len(daily)}')
    print(f'期末资产: {final:,.2f} 元')
    print(f'总收益率: {total_ret:.2f}%')
    print(f'最大回撤: {abs(max_dd):.2f}%')
    print(f'夏普比率(日频年化): {sharpe:.2f}')
    print(f'日均持仓数: {daily["n_holdings"].mean():.1f}')

    if not trades.empty:
        wins = (trades['pnl_pct'] > 0).sum()
        print(f'平仓笔数: {len(trades)}  胜率: {wins / len(trades) * 100:.1f}%')
        print(f'单笔均盈亏: {trades["pnl_pct"].mean():.2f}%')
    else:
        print('平仓笔数: 0（区间内可能仅有浮盈持仓）')

    print('\n最近 5 日:')
    cols = ['date', 'n_holdings', 'day_return_pct', 'cum_return_pct', 'actions']
    print(daily[cols].tail(5).to_string(index=False))
    print('=' * 68)


def main():
    parser = argparse.ArgumentParser(description='动量+趋势策略 (momentum_trend_5)')
    parser.add_argument('--db', default=str(DEFAULT_DB))
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_bt = sub.add_parser('backtest', help='全市场 pandas 回测（推荐）')
    p_bt.add_argument('--start', required=True)
    p_bt.add_argument('--end', default=None)
    p_bt.add_argument('--momentum', type=int, default=5)
    p_bt.add_argument('--ma', type=int, default=20)
    p_bt.add_argument('--top', type=int, default=10)
    p_bt.add_argument('--cash', type=float, default=100_000.0)
    p_bt.add_argument('--no-save', action='store_true')

    sub.add_parser('backtest-bt', help='Backtrader 小样例（8只股票）')

    args = parser.parse_args()
    db = Path(args.db)

    if args.cmd == 'backtest':
        end = args.end
        if not end:
            with sqlite3.connect(db) as conn:
                end = pd.read_sql(
                    'SELECT MAX(trade_date) AS d FROM stock_daily', conn
                ).iloc[0]['d']
        run_momentum_backtest(
            db, args.start, end,
            momentum_period=args.momentum,
            trend_period=args.ma,
            top_stocks=args.top,
            initial_cash=args.cash,
            save_csv=not args.no_save,
        )
    elif args.cmd == 'backtest-bt':
        run_backtest()


if __name__ == '__main__':
    main()