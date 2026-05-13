from vnpy_ctastrategy import (
    CtaTemplate,
    BarGenerator,
    ArrayManager,
    TickData,
    BarData,
    OrderData,
    TradeData,
    StopOrder
)
from datetime import time

class CincoStrategy(CtaTemplate):
    """"""

    author = "Raymond Hsiao"

    bar_window = 15
    capital = 100000
    boll_window = 42
    boll_dev = 2.2
    trailing_long = 0.65
    trailing_short = 0.65
    atr_window = 4
    risk_level = 0.008
    tick_filter = 0
    max_pos_size = 10  # 最大开仓手数（动态计算时使用）
    fixed_size = 0     # 固定手数（>0时优先使用，0表示使用动态计算）

    boll_up = 0.0
    boll_down = 0.0
    trading_size = 0
    intra_trade_high = 0.0
    intra_trade_low = 0.0
    long_stop = 0.0
    short_stop = 0.0
    atr_value = 0.0
    pos: float = 0.0

    parameters = [
        "bar_window",
        "capital",
        "boll_window",
        "boll_dev",
        "trailing_long",
        "trailing_short",
        "atr_window",
        "risk_level",
        "tick_filter",
        "max_pos_size",
        "fixed_size"    # 固定手数参数
    ]

    variables = [
        "boll_up",
        "boll_down",
        "trading_size",
        "intra_trade_high",
        "intra_trade_low",
        "long_stop",
        "short_stop",
        "atr_value"
    ]

    def __init__(
        self,
        cta_engine,
        strategy_name: str,
        vt_symbol: str,
        setting: dict,
    ):
        """"""
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        self.bg = BarGenerator(self.on_bar, self.bar_window, self.on_xmin_bar)
        self.am = ArrayManager()

        # 初始化信号字典
        self.contract_size = cta_engine.get_size(self) 
        self.write_log(f"{vt_symbol}-{self.contract_size}")
        
        # 根据fixed_size参数决定模式
        if self.fixed_size > 0:
            self.write_log(f"[固定手数模式] 固定手数: {self.fixed_size}, 布林带窗口: {self.boll_window}, 布林带偏差: {self.boll_dev}")
        else:
            self.write_log(f"[动态计算模式] 最大开仓手数: {self.max_pos_size}, 风险系数: {self.risk_level}, 布林带窗口: {self.boll_window}, 布林带偏差: {self.boll_dev}")

    def on_init(self):
        """
        Callback when strategy is inited.
        """
        self.write_log("策略初始化")

        self.load_bar(6)
    
    def on_start(self):
        """
        Callback when strategy is started.
        """
        self.write_log("策略启动")

    def on_stop(self):
        """
        Callback when strategy is stopped.
        """
        self.write_log("策略停止")
    
    def on_tick(self, tick: TickData):
        """
        Callback of new tick data update.
        """
        # 规避垃圾数据推送
        t = tick.datetime.time()

        if self.tick_filter:
            if  t < time(9, 0) or t > time(15, 15):
                return
        
        #now = datetime.now()
        #delta = tick.datetime - now
            
        # if abs(delta.total_seconds()) > 30:
        #     return 

        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData):
        """
        Callback of new bar data update.
        """ 
        
        self.bg.update_bar(bar)
    
    def on_xmin_bar(self, bar: BarData):
        """"""
        self.cancel_all()

        self.am.update_bar(bar)
        if not self.inited:
            return

        self.boll_up, self.boll_down = self.am.boll(self.boll_window, self.boll_dev)
        boll_width = self.boll_up - self.boll_down
        self.write_log(f"K线更新 - 时间: {bar.datetime}, 开: {bar.open_price:.4f}, 高: {bar.high_price:.4f}, 低: {bar.low_price:.4f}, 收: {bar.close_price:.4f}")
        self.write_log(f"技术指标 - 布林上轨: {self.boll_up:.4f}, 布林下轨: {self.boll_down:.4f}, 布林带宽度: {boll_width:.4f}")

        if not self.pos:
            if self.fixed_size > 0:
                # 固定手数模式
                self.trading_size = self.fixed_size
            else:
                # 动态计算模式
                self.atr_value = self.am.atr(self.atr_window)
                original_size = max(int(self.capital * self.risk_level / (self.atr_value*self.contract_size)), 1)
                self.trading_size = min(original_size, self.max_pos_size)
                
                # 记录日志，显示原始计算结果和限制后的结果
                if original_size > self.max_pos_size:
                    self.write_log(f"交易数量限制 - 原始计算数量: {original_size}, 限制后数量: {self.trading_size}")

            self.intra_trade_high = bar.high_price
            self.intra_trade_low = bar.low_price
            self.long_stop = 0
            self.short_stop = 0

            self.write_log(f"无持仓状态 - 设置多头条件单: 价格={self.boll_up:.4f}, 数量={self.trading_size}")
            self.buy(self.boll_up, self.trading_size, stop=True, net=True)
            
            self.write_log(f"无持仓状态 - 设置空头条件单: 价格={self.boll_down:.4f}, 数量={self.trading_size}")
            self.short(self.boll_down, self.trading_size, stop=True, net=True)
        
        elif self.pos > 0:
            self.intra_trade_high = max(self.intra_trade_high, bar.high_price)
            self.long_stop = self.intra_trade_high - self.trailing_long * boll_width
            self.long_stop = max(self.long_stop, self.boll_down)
            self.write_log(f"多头持仓 - 最高价: {self.intra_trade_high:.4f}, 止损价: {self.long_stop:.4f}, 持仓量: {self.pos}")
            self.sell(self.long_stop, abs(self.pos), stop=True, net=True)
            
        
        else:
            self.intra_trade_low = min(self.intra_trade_low, bar.low_price)
            self.short_stop = self.intra_trade_low + self.trailing_short * boll_width
            self.short_stop = min(self.short_stop, self.boll_up)
            self.write_log(f"空头持仓 - 最低价: {self.intra_trade_low:.4f}, 止损价: {self.short_stop:.4f}, 持仓量: {abs(self.pos)}")
            self.cover(self.short_stop, abs(self.pos), stop=True, net=True)
        
        self.put_event()

    def on_trade(self, trade: TradeData):
        """
        Callback of new trade data update.
        """
        self.write_log(f"交易执行 - 方向: {'多' if trade.direction == '多' else '空'}, 开平: {trade.offset}, 价格: {trade.price:.4f}, 数量: {trade.volume}, 当前持仓: {self.pos}")
        if abs(self.pos) <= 1e-7:
                self.write_log("持仓量存在7位及以上小数，调整为0")
                self.pos = 0
        self.put_event()
    
    def on_order(self, order: OrderData):
        """
        Callback of new order data update.
        """
        self.write_log(f"订单状态 - 订单号: {order.orderid}, 方向: {'多' if order.direction == '多' else '空'}, 开平: {order.offset}, 价格: {order.price:.4f}, 数量: {order.volume}, 状态: {order.status}")
        pass
    
    def on_stop_order(self, stop_order: StopOrder):
        """
        Callback of stop order update.
        """
        self.write_log(f"条件单触发 - 订单号: {stop_order.stop_orderid}, 方向: {'多' if stop_order.direction == '多' else '空'}, 价格: {stop_order.price:.4f}, 数量: {stop_order.volume}, 状态: {stop_order.status}")
        pass

def get_product_name(vt_symbol: str) -> str:
    """获取合约代码（去除数字部分）"""
    symbol  = ''.join(w for w in vt_symbol.split('.')[0] if not w.isdigit())
    return symbol.upper()