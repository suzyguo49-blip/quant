import baostock as bs
import pandas as pd
import numpy as np
import sqlite3
import backtrader as bt
from datetime import datetime, time, timedelta
import logging
import sys
import os

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

class StockFilter:
    """股票筛选器 - 获取并过滤有效的A股股票"""
    
    @staticmethod
    def get_all_stock_codes(date=None):
        """
        获取指定日期所有A股股票代码
        
        Parameters:
        date: 指定日期，默认为None（最近交易日）
        
        Returns:
        list: 股票代码列表
        """
        try:
            # 登录baostock
            lg = bs.login()
            if lg.error_code != '0':
                logging.error(f"登录失败: {lg.error_msg}")
                return []
            
            # 获取股票数据
            if date is None:
                stock_df = bs.query_all_stock().get_data()
            else:
                stock_df = bs.query_all_stock(day=date).get_data()
            
            # 如果数据为空，寻找最近交易日[7]
            if len(stock_df) == 0:
                logging.info("当日无数据，寻找最近交易日...")
                delta = 1
                while len(stock_df) == 0 and delta <= 10:  # 最多向前找10天
                    target_date = (datetime.now() - timedelta(days=delta)).strftime("%Y-%m-%d")
                    stock_df = bs.query_all_stock(day=target_date).get_data()
                    delta += 1
            
            bs.logout()
            
            if len(stock_df) == 0:
                logging.error("无法获取有效的股票数据")
                return []
            
            # 筛选A股股票（排除指数、基金等
            # 上证A股: sh.600000 - sh.605999, sh.688000 - sh.688999 (科创板)
            # 深证A股: sz.000001 - sz.004999, sz.300000 - sz.300999 (创业板)
            condition = (
                (stock_df['code'].str.startswith('sh.6') & 
                 ((stock_df['code'] >= 'sh.600000') & (stock_df['code'] <= 'sh.605999')) |
                 (stock_df['code'] >= 'sh.688000') & (stock_df['code'] <= 'sh.688999'))
            ) | (
                (stock_df['code'].str.startswith('sz.0') & 
                 (stock_df['code'] >= 'sz.000001') & (stock_df['code'] <= 'sz.004999')) |
                (stock_df['code'].str.startswith('sz.3') & 
                 (stock_df['code'] >= 'sz.300000') & (stock_df['code'] <= 'sz.300999'))
            )
            
            a_stocks = stock_df[condition]
            logging.info(f"获取到 {len(a_stocks)} 只A股股票")
            
            return a_stocks['code'].tolist()
            
        except Exception as e:
            logging.error(f"获取股票代码时发生错误: {str(e)}")
            return []

    @staticmethod
    def filter_stocks(stock_codes, filters=None):
        """
        过滤股票代码
        
        Parameters:
        stock_codes: 股票代码列表
        filters: 过滤条件字典
        
        Returns:
        list: 过滤后的股票代码列表
        """
        if filters is None:
            filters = {
                'exclude_st': True,      # 排除ST股票
                'exclude_suspended': True, # 排除停牌股票
                'min_price': 3.0,        # 最低价格
                'max_price': 200.0,      # 最高价格
            }
        
        filtered_stocks = []
        
        try:
            lg = bs.login()
            
            for code in stock_codes:
                try:
                    # 获取股票基本信息
                    rs = bs.query_stock_basic(code=code)
                    if rs.error_code != '0':
                        continue
                    
                    stock_info = []
                    while (rs.error_code == '0') & rs.next():
                        stock_info.append(rs.get_row_data())
                    
                    if not stock_info:
                        continue
                    
                    info = stock_info[0]
                    stock_name = info[1] if len(info) > 1 else ""
                    
                    # 应用过滤条件
                    exclude = False
                    
                    # 排除ST股票
                    if filters['exclude_st'] and stock_name:
                        if "ST" in stock_name or "*ST" in stock_name:
                            exclude = True
                    
                    if exclude:
                        continue
                    
                    # 获取最新股价进行价格过滤
                    k_data = bs.query_history_k_data_plus(
                        code, "close", 
                        start_date=(datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d"),
                        end_date=datetime.now().strftime("%Y-%m-%d"),
                        frequency="d", 
                        adjustflag="3"
                    ).get_data()
                    
                    if not k_data.empty:
                        latest_close = float(k_data.iloc[-1]['close'])
                        
                        if (filters['min_price'] and latest_close < filters['min_price']) or \
                           (filters['max_price'] and latest_close > filters['max_price']):
                            continue
                    
                    filtered_stocks.append(code)
                    
                except Exception as e:
                    logging.warning(f"处理股票 {code} 时发生错误: {str(e)}")
                    continue
            
            bs.logout()
            logging.info(f"过滤后剩余 {len(filtered_stocks)} 只股票")
            
        except Exception as e:
            logging.error(f"过滤股票时发生错误: {str(e)}")
        
        return filtered_stocks

class IntradayStrategy(bt.Strategy):
    """尾盘买入早盘卖出策略"""
    
    params = (
        ('tail_start', time(14, 30)),    # 尾盘买入时间
        ('morning_sell', time(9, 45)),   # 早盘卖出时间
        ('position_ratio', 0.8),         # 仓位比例
        ('stop_loss', 0.03),             # 止损比例 3%
    )
    
    def __init__(self):
        self.order = None
        self.bought_today = False
        self.dataclose = self.datas[0].close
        self.last_trade_date = None

    def log(self, txt, dt=None):
        '''日志函数'''
        dt = dt or self.datas[0].datetime.date(0)
        print(f'{dt.isoformat()}, {txt}')

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return
        
        if order.status in [order.Completed]:
            if order.isbuy():
                self.log(f'买入执行, 价格: {order.executed.price:.2f}, 成本: {order.executed.value:.2f}, 佣金: {order.executed.comm:.2f}')
            elif order.issell():
                self.log(f'卖出执行, 价格: {order.executed.price:.2f}, 成本: {order.executed.value:.2f}, 佣金: {order.executed.comm:.2f}')
            
            self.bar_executed = len(self)
        
        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log('订单取消/保证金不足/拒绝')
        
        self.order = None

    def next(self):
        current_time = self.data.datetime.time()
        current_date = self.data.datetime.date(0)
        
        # 新的一天重置状态
        if self.last_trade_date != current_date:
            self.bought_today = False
            self.last_trade_date = current_date
        
        if self.order:
            return
        
        # 尾盘买入逻辑
        if (not self.bought_today and not self.position and 
            current_time >= self.params.tail_start):
            
            size = int(self.broker.getcash() * self.params.position_ratio / self.dataclose[0])
            if size > 0:
                self.order = self.buy(size=size)
                self.bought_today = True
                self.buy_price = self.dataclose[0]  # 记录买入价格
        
        # 早盘卖出逻辑
        elif (self.bought_today and self.position and 
              current_time >= self.params.morning_sell):
            
            self.order = self.sell(size=self.position.size)
        
        # 止损逻辑
        elif (self.position and self.bought_today and 
              self.dataclose[0] <= self.buy_price * (1 - self.params.stop_loss)):
            
            self.log(f'止损触发, 价格: {self.dataclose[0]:.2f}')
            self.order = self.sell(size=self.position.size)

def prepare_stock_data(symbol, start_date, end_date, db_path=None):
    """准备股票数据用于回测"""
    try:
        if db_path and os.path.exists(db_path):
            # 从数据库读取数据[2](@ref)
            conn = sqlite3.connect(db_path)
            query = """
            SELECT trade_date, open, high, low, close, volume, amount 
            FROM stock_daily 
            WHERE symbol = ? AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """
            df = pd.read_sql_query(query, conn, params=(symbol, start_date, end_date))
            conn.close()
        else:
            # 直接从baostock获取数据
            lg = bs.login()
            rs = bs.query_history_k_data_plus(
                symbol,
                "date,open,high,low,close,volume,amount",
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="2"  # 前复权
            )
            
            data_list = []
            while (rs.error_code == '0') & rs.next():
                data_list.append(rs.get_row_data())
            
            df = pd.DataFrame(data_list, columns=rs.fields)
            bs.logout()
        
        if df.empty:
            return None
        
        # 数据预处理
        df['date'] = pd.to_datetime(df['date'] if 'date' in df.columns else df['trade_date'])
        df.set_index('date', inplace=True)
        
        # 转换数据类型
        numeric_cols = ['open', 'high', 'low', 'close', 'volume', 'amount']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        
        df['openinterest'] = 0
        
        # 重命名列以匹配backtrader
        column_mapping = {
            'open': 'open', 'high': 'high', 'low': 'low', 
            'close': 'close', 'volume': 'volume', 'amount': 'amount'
        }
        df = df.rename(columns={k: v for k, v in column_mapping.items() if k in df.columns})
        
        return bt.feeds.PandasData(dataname=df)
        
    except Exception as e:
        logging.error(f"准备股票 {symbol} 数据时发生错误: {str(e)}")
        return None

def run_backtest_for_stock(symbol, start_date, end_date, initial_cash=100000, db_path=None):
    """单只股票回测"""
    cerebro = bt.Cerebro()
    
    # 设置经纪商参数
    cerebro.broker.setcash(initial_cash)
    cerebro.broker.setcommission(commission=0.0008)  # 万8佣金
    cerebro.broker.set_slippage_perc(0.001)  # 0.1%滑点
    
    # 添加策略
    cerebro.addstrategy(IntradayStrategy)
    
    # 添加数据
    data = prepare_stock_data(symbol, start_date, end_date, db_path)
    if data is None:
        logging.warning(f"无法为 {symbol} 准备数据")
        return None
    
    cerebro.adddata(data)
    
    # 添加分析器
    cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe', riskfreerate=0.03, annualize=True)
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trades')
    
    # 运行回测
    try:
        results = cerebro.run()
        return results[0] if results else None
    except Exception as e:
        logging.error(f"回测 {symbol} 时发生错误: {str(e)}")
        return None

def calculate_performance(strategy_instance, initial_cash, symbol):
    """计算性能指标"""
    if not strategy_instance:
        return None
    
    try:
        final_value = strategy_instance.broker.getvalue()
        total_return = (final_value - initial_cash) / initial_cash
        
        # 获取分析结果
        returns_analysis = strategy_instance.analyzers.returns.get_analysis()
        drawdown_analysis = strategy_instance.analyzers.drawdown.get_analysis()
        sharpe_analysis = strategy_instance.analyzers.sharpe.get_analysis()
        trade_analysis = strategy_instance.analyzers.trades.get_analysis()
        
        # 计算年化收益率
        if hasattr(strategy_instance, 'datas') and len(strategy_instance.datas[0]) > 0:
            days = len(strategy_instance.datas[0])
            if days > 0:
                annual_return = (1 + total_return) ** (252/days) - 1
            else:
                annual_return = 0
        else:
            annual_return = returns_analysis.get('rnorm100', 0) / 100 if 'rnorm100' in returns_analysis else 0
        
        performance = {
            'symbol': symbol,
            'initial_cash': initial_cash,
            'final_value': final_value,
            'total_return': total_return,
            'annual_return': annual_return,
            'max_drawdown': drawdown_analysis.max.drawdown if hasattr(drawdown_analysis.max, 'drawdown') else 0,
            'sharpe_ratio': sharpe_analysis.get('sharperatio', 0) if sharpe_analysis else 0,
            'total_trades': trade_analysis.total.closed if trade_analysis else 0,
            'win_rate': (trade_analysis.won.total / trade_analysis.total.closed 
                        if trade_analysis and trade_analysis.total.closed > 0 else 0)
        }
        
        return performance
        
    except Exception as e:
        logging.error(f"计算性能指标时发生错误: {str(e)}")
        return None

def batch_backtest_all_stocks(start_date, end_date, db_path=None, max_stocks=50, initial_cash=100000):
    """批量回测所有股票"""
    
    logging.info("开始获取所有A股股票代码...")
    
    # 获取所有股票代码
    all_stocks = StockFilter.get_all_stock_codes()
    
    if not all_stocks:
        logging.error("无法获取股票代码列表")
        return None
    
    logging.info(f"共获取到 {len(all_stocks)} 只股票，开始过滤...")
    
    # 过滤股票
    filtered_stocks = StockFilter.filter_stocks(all_stocks)
    
    if not filtered_stocks:
        logging.error("过滤后无有效股票")
        return None
    
    # 限制股票数量以避免运行时间过长
    test_stocks = filtered_stocks[:max_stocks]
    logging.info(f"选择前 {len(test_stocks)} 只股票进行回测")
    
    results = []
    
    for i, symbol in enumerate(test_stocks, 1):
        logging.info(f"[{i}/{len(test_stocks)}] 正在回测 {symbol}...")
        
        strategy = run_backtest_for_stock(symbol, start_date, end_date, initial_cash, db_path)
        performance = calculate_performance(strategy, initial_cash, symbol)
        
        if performance:
            results.append(performance)
            logging.info(f"{symbol}: 年化收益 {performance['annual_return']:.2%}, "
                        f"最大回撤 {performance['max_drawdown']:.2%}, "
                        f"夏普比率 {performance['sharpe_ratio']:.2f}")
        else:
            logging.warning(f"{symbol} 回测失败")
    
    return results

def analyze_results(results):
    """分析回测结果"""
    if not results:
        logging.error("无有效回测结果")
        return None
    
    df = pd.DataFrame(results)
    
    # 基本统计
    print("\n" + "="*60)
    print("回测结果统计摘要")
    print("="*60)
    print(f"回测股票数量: {len(df)}")
    print(f"平均年化收益率: {df['annual_return'].mean():.2%}")
    print(f"收益率中位数: {df['annual_return'].median():.2%}")
    print(f"最大年化收益率: {df['annual_return'].max():.2%}")
    print(f"最小年化收益率: {df['annual_return'].min():.2%}")
    print(f"平均最大回撤: {df['max_drawdown'].mean():.2%}")
    print(f"平均夏普比率: {df['sharpe_ratio'].mean():.2f}")
    print(f"盈利股票比例: {(df['total_return'] > 0).sum() / len(df):.2%}")
    
    # 显示前10名
    print("\n" + "="*60)
    print("表现最好的10只股票")
    print("="*60)
    top_10 = df.nlargest(10, 'annual_return')[['symbol', 'annual_return', 'max_drawdown', 'sharpe_ratio']]
    for _, row in top_10.iterrows():
        print(f"{row['symbol']}: 年化{row['annual_return']:.2%}, "
              f"回撤{row['max_drawdown']:.2%}, 夏普{row['sharpe_ratio']:.2f}")
    
    return df

# 主程序
if __name__ == "__main__":
    # 设置回测参数
    start_date = "2024-01-01"
    end_date = "2025-11-30"
    db_path = "stock_data.db"  # 如果有数据库可以指定路径
    initial_cash = 100000
    max_stocks = 100  # 最大回测股票数量
    
    logging.info("开始批量回测...")
    
    # 运行批量回测
    results = batch_backtest_all_stocks(
        start_date=start_date,
        end_date=end_date,
        db_path=db_path,
        max_stocks=max_stocks,
        initial_cash=initial_cash
    )
    
    # 分析结果
    if results:
        result_df = analyze_results(results)
        
        # 保存结果到CSV
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = f"backtest_results_{timestamp}.csv"
        result_df.to_csv(output_file, index=False, encoding='utf-8-sig')
        logging.info(f"详细结果已保存到: {output_file}")
        
        # 显示策略统计
        profitable_stocks = len([r for r in results if r['total_return'] > 0])
        print(f"\n策略总结:")
        print(f"- 测试股票总数: {len(results)}")
        print(f"- 盈利股票数量: {profitable_stocks}")
        print(f"- 胜率: {profitable_stocks/len(results):.2%}")
        print(f"- 平均持仓天数: 1 (T+1策略)")
    else:
        logging.error("回测失败，无结果返回")