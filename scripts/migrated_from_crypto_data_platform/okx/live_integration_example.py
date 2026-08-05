import sys
from pathlib import Path
_CDP_OKX = Path('/root/quant/crypto-data-platform/okx').resolve()
if str(_CDP_OKX) not in sys.path:
    sys.path.insert(0, str(_CDP_OKX))

#!/usr/bin/env python3
"""
实盘资金费率集成示例
====================

展示如何在 vnpy 实盘策略中使用资金费率
"""

from datetime import datetime
from typing import Optional

from vnpy_ctastrategy import CtaTemplate, StopOrder
from vnpy.trader.object import BarData, TickData

from funding_rate_manager import FundingRateManager, Mode, FundingRateInfo


class LiveFundingRateStrategy(CtaTemplate):
    """
    实盘资金费率策略示例
    
    这个策略展示了如何在实盘中使用资金费率：
    1. 初始化时创建 FundingRateManager
    2. on_bar/on_tick 中获取实时资金费率
    3. 接近结算时获取下期预告费率
    """
    
    author = "Live Trader"
    
    # 参数
    funding_threshold = 0.0001      # 资金费率阈值
    trade_size = 0.1                # 交易数量
    pre_settlement_minutes = 30     # 结算前提醒时间（分钟）
    
    # 变量
    current_funding_rate = 0.0
    next_funding_rate = 0.0
    next_settlement_time = ""
    
    parameters = ["funding_threshold", "trade_size", "pre_settlement_minutes"]
    variables = ["current_funding_rate", "next_funding_rate", "next_settlement_time"]
    
    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        
        # 资金费率管理器
        self.funding_mgr: Optional[FundingRateManager] = None
        
        # 上期资金费率（用于检测变化）
        self._last_funding_rate: Optional[float] = None
        
        # 是否已提醒结算
        self._settlement_reminded = False
    
    def on_init(self):
        """策略初始化"""
        self.write_log("策略初始化 - 设置资金费率管理器")
        
        # 创建资金费率管理器（实盘模式）
        self.funding_mgr = FundingRateManager.create(
            symbol=self.vt_symbol,
            mode=Mode.LIVE  # 明确指定实盘模式
        )
        
        # 立即获取一次资金费率
        self._update_funding_rate()
        
        self.write_log(f"当前资金费率: {self.current_funding_rate}")
        self.write_log(f"下次结算: {self.next_settlement_time}")
    
    def on_start(self):
        """策略启动"""
        self.write_log("策略启动")
        self.put_event()
    
    def on_stop(self):
        """策略停止"""
        self.write_log("策略停止")
        if self.funding_mgr:
            self.funding_mgr.close()
    
    def on_tick(self, tick: TickData):
        """Tick 回调 - 适合高频检查资金费率变化"""
        # 每 100 个 tick 检查一次（避免过于频繁）
        if tick.datetime.second % 10 == 0:  # 每 10 秒
            self._update_funding_rate()
            self._check_settlement_approaching()
    
    def on_bar(self, bar: BarData):
        """K线回调 - 策略主逻辑"""
        # 更新资金费率
        self._update_funding_rate()
        
        # 检查是否接近结算
        self._check_settlement_approaching()
        
        # 策略逻辑
        self._execute_strategy_logic(bar)
        
        self.put_event()
    
    def _update_funding_rate(self):
        """更新资金费率信息"""
        if self.funding_mgr is None:
            return
        
        rate_info = self.funding_mgr.get_rate()
        if rate_info is None:
            return
        
        # 检测资金费率变化
        if self._last_funding_rate is not None:
            if abs(rate_info.funding_rate - self._last_funding_rate) > 0.000001:
                self.write_log(
                    f"资金费率变化: {self._last_funding_rate:.6f} -> {rate_info.funding_rate:.6f}"
                )
        
        self._last_funding_rate = rate_info.funding_rate
        
        # 更新变量（用于 UI 显示）
        self.current_funding_rate = rate_info.funding_rate
        self.next_funding_rate = rate_info.next_funding_rate or 0.0
        self.next_settlement_time = rate_info.next_funding_time.strftime("%m-%d %H:%M")
    
    def _check_settlement_approaching(self):
        """检查是否接近结算时间"""
        if self.funding_mgr is None:
            return
        
        if not self.funding_mgr.is_near_settlement(self.pre_settlement_minutes):
            self._settlement_reminded = False
            return
        
        if self._settlement_reminded:
            return
        
        # 获取最新数据（强制刷新以获取下期预告）
        rate_info = self.funding_mgr.get_rate()
        if rate_info and rate_info.next_funding_rate:
            self.write_log(
                f"⚠️ 结算提醒：{self.pre_settlement_minutes}分钟后结算，"
                f"下期预告费率: {rate_info.next_funding_rate:.6f}"
            )
            
            # 根据下期费率调整策略
            self._adjust_for_next_funding(rate_info)
        
        self._settlement_reminded = True
    
    def _adjust_for_next_funding(self, rate_info: FundingRateInfo):
        """根据下期资金费率调整持仓"""
        next_rate = rate_info.next_funding_rate
        
        if next_rate is None:
            return
        
        # 示例逻辑：如果下期费率不利，考虑提前平仓
        if self.pos > 0 and next_rate < -self.funding_threshold:
            # 持有多仓，但下期要付资金费
            self.write_log(f"下期费率 {next_rate:.6f} 不利多仓，考虑平仓")
            # self.sell(...)
            
        elif self.pos < 0 and next_rate > self.funding_threshold:
            # 持有空仓，但下期要付资金费
            self.write_log(f"下期费率 {next_rate:.6f} 不利空仓，考虑平仓")
            # self.cover(...)
    
    def _execute_strategy_logic(self, bar: BarData):
        """执行策略逻辑"""
        rate = self.current_funding_rate
        
        # 空仓时判断开仓
        if self.pos == 0:
            if rate > self.funding_threshold:
                # 资金费率高，做空收资金费
                self.write_log(f"开空信号，资金费率: {rate:.6f}")
                self.short(bar.close_price * 0.999, self.trade_size)
                
            elif rate < -self.funding_threshold:
                # 资金费率负，做多收资金费
                self.write_log(f"开多信号，资金费率: {rate:.6f}")
                self.buy(bar.close_price * 1.001, self.trade_size)
        
        # 持仓时判断平仓
        elif self.pos > 0:
            # 多仓，当资金费率转正（不利）时平仓
            if rate > 0:
                self.write_log(f"平多信号，资金费率转正: {rate:.6f}")
                self.sell(bar.close_price * 0.999, abs(self.pos))
                
        elif self.pos < 0:
            # 空仓，当资金费率转负（不利）时平仓
            if rate < 0:
                self.write_log(f"平空信号，资金费率转负: {rate:.6f}")
                self.cover(bar.close_price * 1.001, abs(self.pos))
    
    def on_stop_order(self, stop_order: StopOrder):
        """停止单回调"""
        pass
    
    def on_order(self, order):
        """委托回调"""
        pass
    
    def on_trade(self, trade):
        """成交回调"""
        self.write_log(
            f"成交 | {trade.direction.value} | "
            f"价格: {trade.price:.2f} | 数量: {trade.volume}"
        )


class UnifiedStrategy(CtaTemplate):
    """
    统一策略（实盘和回测共用同一套代码）
    
    通过 FundingRateManager 自动区分实盘和回测
    """
    
    author = "Unified"
    
    funding_threshold = 0.0001
    trade_size = 0.1
    
    parameters = ["funding_threshold", "trade_size"]
    variables = ["funding_rate"]
    
    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.funding_mgr: Optional[FundingRateManager] = None
        self.funding_rate = 0.0
    
    def on_init(self):
        """初始化 - 自动判断实盘/回测"""
        # 判断是实盘还是回测
        is_live = hasattr(self.cta_engine, 'main_engine')
        mode = Mode.LIVE if is_live else Mode.BACKTEST
        
        self.write_log(f"初始化模式: {mode.value}")
        
        self.funding_mgr = FundingRateManager.create(self.vt_symbol, mode)
    
    def on_bar(self, bar: BarData):
        """K线回调 - 实盘和回测完全一致"""
        # 统一接口，无需区分实盘/回测
        rate_info = self.funding_mgr.get_rate_for_bar(bar)
        
        if rate_info is None:
            return
        
        self.funding_rate = rate_info.funding_rate
        
        # 策略逻辑...
        if self.pos == 0:
            if rate_info.funding_rate > self.funding_threshold:
                self.short(bar.close_price, self.trade_size)
            elif rate_info.funding_rate < -self.funding_threshold:
                self.buy(bar.close_price, self.trade_size)
        
        self.put_event()
    
    def on_stop(self):
        if self.funding_mgr:
            self.funding_mgr.close()


# ==================== 使用说明 ====================

USAGE = """
实盘资金费率使用指南
====================

1. 在策略中使用
---------------

from funding_rate_manager import FundingRateManager, Mode

class MyStrategy(CtaTemplate):
    def on_init(self):
        # 方式1：明确指定模式
        mode = Mode.LIVE if self.trading else Mode.BACKTEST
        self.funding_mgr = FundingRateManager.create(self.vt_symbol, mode)
        
        # 方式2：自动推断（推荐）
        self.funding_mgr = FundingRateManager.create(self.vt_symbol)
    
    def on_bar(self, bar):
        # 统一接口，无需关心是实盘还是回测
        rate_info = self.funding_mgr.get_rate_for_bar(bar)
        if rate_info:
            current_rate = rate_info.funding_rate
            next_rate = rate_info.next_funding_rate
            # ... 你的策略逻辑

2. 实盘特有功能
--------------

- 自动缓存（5分钟），避免频繁请求
- 结算前提醒（is_near_settlement）
- 下期预告费率（next_funding_rate）
- 自动刷新（结算后自动获取新费率）

3. 回测特有功能
--------------

- 从数据库查询历史资金费率
- 精确匹配 K 线时间
- 无需网络请求

4. 注意事项
----------

- 实盘需要网络访问 OKX API
- 首次启动时会请求一次 API
- 每 5 分钟或结算后自动刷新
- 网络错误时会使用缓存数据
"""

if __name__ == "__main__":
    print(USAGE)
