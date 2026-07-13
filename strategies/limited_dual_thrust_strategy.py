from datetime import time
from vnpy_ctastrategy import (
    CtaTemplate,
    StopOrder,
    TickData,
    BarData,
    TradeData,
    OrderData,
    BarGenerator,
    ArrayManager,
)


class LimitedDualThrustStrategy(CtaTemplate):
    """"""

    author = "Raymond Hsiao"

    fixed_size = 2
    k1 = 0.4
    k2 = 0.6

    exit_time_type = 1
    need_lock = 1

    trailpercent = 0.5
    range_cap = 2.0
    trade_limit = 2

    bars = []

    day_open = 0
    day_high = 0
    day_low = 0

    day_range = 0
    long_entry = 0
    short_entry = 0

    exit_time1= time(hour=14, minute=50)
    exit_time2 = time(hour=15, minute=10)

    long_entry_price = 0
    short_entry_price = 0

    intra_trade_high = 0  #日内高价

    intra_trade_low = 0

    parameters = ["k1", "k2", "exit_time_type","need_lock","fixed_size","trade_limit","trailpercent","range_cap"]
    variables = ["day_open","day_range", "long_entry", "short_entry"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        """"""
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        self.bg = BarGenerator(self.on_bar)
        self.am = ArrayManager()
        self.bars = []
        self.last_pos = 0
        self.intraday_trades_count = 0 #记录日内多空开仓交易的次数

        if self.exit_time_type == 1:
            self.exit_time = self.exit_time1
        else: self.exit_time = self.exit_time2

        if self.need_lock:
            self.lock = True
        else: self.lock = False 

    def on_init(self):
        """
        Callback when strategy is inited.
        """
        self.write_log("策略初始化")
        self.load_bar(30)

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

        if t < time(9, 0) or t > time(15, 15):
            return
            
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData):
        """
        Callback of new bar data update.
        """
        self.cancel_all()

        self.bars.append(bar)
        if len(self.bars) <= 2:
            return
        else:
            self.bars.pop(0)
        last_bar = self.bars[-2]

        if last_bar.datetime.date() != bar.datetime.date():
            if self.day_high:
                self.day_range = self.day_high - self.day_low
                self.day_range = min(self.range_cap/100 * self.day_low, self.day_range)
                
                self.long_entry = bar.open_price + self.k1 * self.day_range
                self.short_entry = bar.open_price - self.k2 * self.day_range

            self.day_open = bar.open_price
            self.day_high = bar.high_price
            self.day_low = bar.low_price
            self.intraday_trades_count = 0

        else:
            self.day_high = max(self.day_high, bar.high_price)
            self.day_low = min(self.day_low, bar.low_price)

        if not self.day_range:
            return

        if   bar.datetime.time() < self.exit_time:
            if self.pos == 0:
                self.intra_trade_low = bar.low_price

                self.intra_trade_high = bar.high_price

                if self.intraday_trades_count <= self.trade_limit:
                    if bar.close_price > self.day_open:
                        self.long_entry_price = max(self.day_high,self.long_entry)

                        self.buy(self.long_entry_price, self.fixed_size, stop=True, lock=self.lock)

                    else:
                        self.short_entry_price = min(self.day_low,self.short_entry)

                        self.short(self.short_entry_price,
                                    self.fixed_size, stop=True, lock=self.lock)

            elif self.pos > 0:
                self.intra_trade_high = max(self.intra_trade_high, bar.high_price)

                long_stop = self.intra_trade_high * (1 - self.trailpercent / 100)

                self.sell(long_stop, abs(self.pos), stop=True, lock=self.lock)


            elif self.pos < 0:
                self.intra_trade_low = min(self.intra_trade_low, bar.low_price)

                short_stop = self.intra_trade_low * (1 + self.trailpercent / 100)

                self.cover(short_stop, abs(self.pos), stop=True, lock=self.lock)
            
            # 如果上一个bar的仓位是0，那么记录交易一次
            if  self.last_pos != self.pos:
                self.intraday_trades_count +=1
            
            self.last_pos = self.pos # 记录最新仓位，作为下一个bar的仓位参考

        else:
            if self.pos > 0:
                self.sell(bar.close_price * 0.99, abs(self.pos), lock=self.lock)
            elif self.pos < 0:
                self.cover(bar.close_price * 1.01, abs(self.pos), lock=self.lock)

        self.put_event()
        

    def on_order(self, order: OrderData):
        """
        Callback of new order data update.
        """
        pass

    def on_trade(self, trade: TradeData):
        """
        Callback of new trade data update.
        """
        self.put_event()
        

    def on_stop_order(self, stop_order: StopOrder):
        """
        Callback of stop order update.
        """
        pass
