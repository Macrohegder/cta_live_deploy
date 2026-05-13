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
from datetime import timedelta, datetime,time

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
    min_trade_size = 1

    boll_up = 0
    boll_down = 0
    trading_size = 0
    intra_trade_high = 0
    intra_trade_low = 0
    long_stop = 0
    short_stop = 0
    atr_value = 0

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
        "min_trade_size"
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

    def on_init(self):
        """
        Callback when strategy is inited.
        """
        self.write_log("策略初始化")

        self.load_bar(20)
    
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

        if not self.pos:
            self.atr_value = self.am.atr(self.atr_window)

            self.trading_size = max( int(self.capital * self.risk_level / (self.atr_value*self.contract_size) ), self.min_trade_size)

            self.intra_trade_high = bar.high_price
            self.intra_trade_low = bar.low_price
            self.long_stop = 0
            self.short_stop = 0

            self.buy(self.boll_up, self.trading_size, stop=True, net=True)
            self.short(self.boll_down, self.trading_size, stop=True, net=True)
        
        elif self.pos > 0:
            self.intra_trade_high = max(self.intra_trade_high, bar.high_price)
            self.long_stop = self.intra_trade_high - self.trailing_long * boll_width
            self.long_stop = max(self.long_stop, self.boll_down)
            self.sell(self.long_stop, abs(self.pos), stop=True, net=True)
        
        else:
            self.intra_trade_low = min(self.intra_trade_low, bar.low_price)
            self.short_stop = self.intra_trade_low + self.trailing_short * boll_width
            self.short_stop = min(self.short_stop, self.boll_up)
            self.cover(self.short_stop, abs(self.pos), stop=True, net=True)
        
        self.put_event()

    def on_trade(self, trade: TradeData):
        """
        Callback of new trade data update.
        """
        self.put_event()
    
    def on_order(self, order: OrderData):
        """
        Callback of new order data update.
        """
        pass
    
    def on_stop_order(self, stop_order: StopOrder):
        """
        Callback of stop order update.
        """
        pass

def get_product_name(vt_symbol: str) -> str:
    """获取合约代码（去除数字部分）"""
    symbol  = ''.join(w for w in vt_symbol.split('.')[0] if not w.isdigit())
    return symbol.upper()