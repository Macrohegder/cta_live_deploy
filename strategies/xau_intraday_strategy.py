"""
XAU Intraday Trend Following Strategy
======================================
XAU/黄金日内趋势跟踪策略 - 基于Cinco模板
"""
from vnpy_ctastrategy import (
    CtaTemplate,
    BarGenerator,
    ArrayManager,
    TickData,
    BarData,
    OrderData,
    TradeData,
    StopOrder,
)

class XauIntradayStrategy(CtaTemplate):
    """XAU日内趋势跟踪策略"""
    author = "Cinco Research - XAU Intraday"
    
    # === 策略参数 ===
    bar_window: int = 60              # K线合成周期（分钟）- 1小时
    capital: int = 100000             # 资金规模
    lookback_days: int = 3            # 回看天数
    risk_level: float = 0.01          # 风险系数
    max_pos_size: int = 5             # 最大开仓手数限制（动态模式使用）
    fixed_size: int = 0               # 固定手数（>0时优先使用，0表示使用max_pos_size）
    
    # === 运行变量 ===
    upper_band: float = 0.0           # 动态边界上轨
    lower_band: float = 0.0           # 动态边界下轨
    vwap_value: float = 0.0           # 当前VWAP值
    trading_size: int = 1             # 固定交易数量
    intra_trade_high: float = 0.0
    intra_trade_low: float = 0.0
    
    parameters = [
        "bar_window",
        "capital",
        "lookback_days",
        "risk_level",
        "max_pos_size",
        "fixed_size",
    ]
    
    variables = [
        "upper_band",
        "lower_band",
        "vwap_value",
        "trading_size",
        "intra_trade_high",
        "intra_trade_low",
    ]
    
    def __init__(
        self,
        cta_engine,
        strategy_name: str,
        vt_symbol: str,
        setting: dict,
    ):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        # ✅ 严格按照 Cinco 模板写法
        # BarGenerator: 将1分钟K线合成为 bar_window 分钟K线
        self.bg = BarGenerator(self.on_bar, self.bar_window, self.on_hour_bar)
        self.am = ArrayManager()
        self.hourly_bars = []
        self.contract_size = 100  # 黄金合约乘数
        self.write_log(f"{vt_symbol} - 合约乘数: {self.contract_size}")
    
    def on_init(self):
        """策略初始化 - 已优化load_bar天数"""
        self.write_log("策略初始化 - XAU Intraday")
        # ✅ 优化: 只加载计算指标所需的最少天数
        # 注意: load_bar(n) 的 n 是天数，不是 bar 数量！
        # 策略需要 lookback_days 天的数据来计算动态边界
        # 实盘时压缩到 (lookback_days + 2) 天
        min_days = self.lookback_days + 2
        self.load_bar(min_days)
    
    def on_start(self):
        self.write_log("策略启动")
    
    def on_stop(self):
        self.write_log("策略停止")
    
    def on_tick(self, tick: TickData):
        self.bg.update_tick(tick)
    
    def on_bar(self, bar: BarData):
        """接收1分钟K线，交给BarGenerator合成1小时"""
        self.bg.update_bar(bar)
    
    def on_hour_bar(self, bar: BarData):
        """1小时K线回调 - BarGenerator合成后触发"""
        self.cancel_all()
        self.am.update_bar(bar)
        
        # 存储1小时K线数据
        self.hourly_bars.append({
            'datetime': bar.datetime,
            'high': bar.high_price,
            'low': bar.low_price,
            'close': bar.close_price,
            'volume': bar.volume,
        })
        
        max_bars = self.lookback_days * 24 + 20
        if len(self.hourly_bars) > max_bars:
            self.hourly_bars.pop(0)
        
        # 需要至少 lookback_days * 12 小时数据
        if len(self.hourly_bars) < self.lookback_days * 12:
            return
        
        # 计算动态边界
        lookback = self.lookback_days * 24
        recent_bars = self.hourly_bars[-lookback:]
        self.upper_band = max(b['high'] for b in recent_bars)
        self.lower_band = min(b['low'] for b in recent_bars)
        
        # 计算VWAP
        typical_sum = sum((b['high'] + b['low'] + b['close']) / 3 * max(b['volume'], 1) for b in recent_bars)
        volume_sum = sum(max(b['volume'], 1) for b in recent_bars)
        self.vwap_value = typical_sum / volume_sum if volume_sum > 0 else bar.close_price
        
        if not self.pos:
            # === 无持仓：突破开仓 ===
            # 优先使用fixed_size，否则使用max_pos_size
            self.trading_size = self.fixed_size if self.fixed_size > 0 else self.max_pos_size
            self.intra_trade_high = bar.high_price
            self.intra_trade_low = bar.low_price
            # 在上下轨挂条件单
            self.buy(self.upper_band, self.trading_size, stop=True, net=True)
            self.short(self.lower_band, self.trading_size, stop=True, net=True)
        elif self.pos > 0:
            # === 多头持仓 ===
            self.intra_trade_high = max(self.intra_trade_high, bar.high_price)
            stop_price = self.vwap_value * 0.995
            self.sell(stop_price, abs(self.pos), stop=True, net=True)
        else:
            # === 空头持仓 ===
            self.intra_trade_low = min(self.intra_trade_low, bar.low_price)
            stop_price = self.vwap_value * 1.005
            self.cover(stop_price, abs(self.pos), stop=True, net=True)
        
        self.put_event()
    
    def on_trade(self, trade: TradeData):
        """成交回调 - 碎单处理"""
        if abs(self.pos) <= 1e-7:
            self.pos = 0
        self.put_event()
    
    def on_order(self, order: OrderData):
        pass
    
    def on_stop_order(self, stop_order: StopOrder):
        pass
