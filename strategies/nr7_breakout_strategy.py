"""
NR7突破策略
=========================================================
基于 QuantifiedStrategies 文章复现

复现 QuantifiedStrategies NR7 波动率收缩突破策略:
- NR7 = 今日波幅是过去 N 个交易日中最窄的
- 入场: NR7 触发时收盘买入（在平静期埋伏）
- 出场: 收盘价高于昨日最高价（QS Exit）

架构说明:
    Strategy (CtaTemplate) -> Signal (交易逻辑) -> Factor (指标计算)
    
    三层分离优势:
    1. 指标计算与交易执行解耦 方便单元测试
    2. 信号逻辑独立 可复用于多个策略
    3. UI变量与核心逻辑分离 实盘监控更清晰
"""
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Callable, Optional, Tuple
from zoneinfo import ZoneInfo

from vnpy.trader.object import BarData, TickData, TradeData, OrderData
from vnpy_ctastrategy import StopOrder
from vnpy.trader.constant import Interval
from vnpy_ctastrategy import (
    CtaTemplate,
    BarGenerator,
    ArrayManager,
)


# =============================================================================
# 以下代码内嵌自 session_daily.py —— 自包含，零外部依赖
# =============================================================================

@dataclass(frozen=True)
class MarketProfile:
    daily_end_hour: int
    daily_end_minute: int
    close_on_date_change: bool
    rollover_after_end_time: bool
    market_tz: str
    default_bar_tz: Optional[str]

    @property
    def end_time(self) -> time:
        return time(self.daily_end_hour, self.daily_end_minute)


CRYPTO_EXCHANGES = {
    "BINANCE", "OKX", "BYBIT", "BITGET", "GATEIO", "HUOBI", "COINBASE", "DERIBIT",
}

CN_FUTURES_EXCHANGES = {
    "CFFEX", "SHFE", "DCE", "CZCE", "INE", "GFEX",
}

US_STOCK_EXCHANGES = {
    "SMART", "NYSE", "NASDAQ", "AMEX", "ARCA", "BATS", "IEX",
}


def _parse_exchange(vt_symbol: str) -> str:
    parts = vt_symbol.split(".")
    return parts[1].upper() if len(parts) >= 2 else ""


def _parse_symbol(vt_symbol: str) -> str:
    return vt_symbol.split(".")[0]


def _infer_crypto_exchange_from_symbol(symbol: str) -> Optional[str]:
    tokens = symbol.upper().replace("-", "_").split("_")
    for token in reversed(tokens):
        if token in CRYPTO_EXCHANGES:
            return token
    return None


def infer_market_profile(vt_symbol: str) -> MarketProfile:
    exchange = _parse_exchange(vt_symbol)
    symbol = _parse_symbol(vt_symbol)

    if exchange in CN_FUTURES_EXCHANGES:
        return MarketProfile(
            daily_end_hour=14, daily_end_minute=59,
            close_on_date_change=False, rollover_after_end_time=True,
            market_tz="Asia/Shanghai", default_bar_tz="Asia/Shanghai",
        )

    if exchange in US_STOCK_EXCHANGES:
        return MarketProfile(
            daily_end_hour=16, daily_end_minute=0,
            close_on_date_change=False, rollover_after_end_time=False,
            market_tz="America/New_York", default_bar_tz="UTC",
        )

    crypto_exchange = _infer_crypto_exchange_from_symbol(symbol) if exchange == "GLOBAL" else None
    if exchange in CRYPTO_EXCHANGES or exchange == "GLOBAL" or crypto_exchange:
        return MarketProfile(
            daily_end_hour=23, daily_end_minute=59,
            close_on_date_change=True, rollover_after_end_time=False,
            market_tz="UTC", default_bar_tz="UTC",
        )

    return MarketProfile(
        daily_end_hour=23, daily_end_minute=59,
        close_on_date_change=True, rollover_after_end_time=False,
        market_tz="UTC", default_bar_tz="UTC",
    )


def resolve_market_profile(
    vt_symbol: str,
    auto_daily_end: bool = True,
    daily_end_hour: Optional[int] = None,
    daily_end_minute: Optional[int] = None,
    bar_tz: Optional[str] = None,
) -> MarketProfile:
    inferred = infer_market_profile(vt_symbol)
    if auto_daily_end:
        if bar_tz is None:
            return inferred
        return MarketProfile(
            daily_end_hour=inferred.daily_end_hour,
            daily_end_minute=inferred.daily_end_minute,
            close_on_date_change=inferred.close_on_date_change,
            rollover_after_end_time=inferred.rollover_after_end_time,
            market_tz=inferred.market_tz,
            default_bar_tz=bar_tz,
        )
    h = inferred.daily_end_hour if daily_end_hour is None else int(daily_end_hour)
    m = inferred.daily_end_minute if daily_end_minute is None else int(daily_end_minute)
    return MarketProfile(
        daily_end_hour=h, daily_end_minute=m,
        close_on_date_change=inferred.close_on_date_change,
        rollover_after_end_time=inferred.rollover_after_end_time,
        market_tz=inferred.market_tz,
        default_bar_tz=inferred.default_bar_tz if bar_tz is None else bar_tz,
    )


class SessionDailyBarGenerator:
    def __init__(
        self,
        on_daily_bar: Callable[[BarData], None],
        vt_symbol: str,
        auto_daily_end: bool = True,
        daily_end_hour: Optional[int] = None,
        daily_end_minute: Optional[int] = None,
        bar_tz: Optional[str] = None,
    ) -> None:
        self.on_daily_bar = on_daily_bar
        self.vt_symbol = vt_symbol
        self.profile = resolve_market_profile(
            vt_symbol=vt_symbol,
            auto_daily_end=auto_daily_end,
            daily_end_hour=daily_end_hour,
            daily_end_minute=daily_end_minute,
            bar_tz=bar_tz,
        )

        self.daily_bar: Optional[BarData] = None
        self._session_date = None
        self._last_bar_dt: Optional[datetime] = None
        self._last_market_dt: Optional[datetime] = None
        self._naive_input: Optional[bool] = None

    def _hm(self, dt: datetime) -> Tuple[int, int]:
        return dt.hour, dt.minute

    def _session_date_from_market_dt(self, market_dt: datetime):
        if self.profile.rollover_after_end_time:
            if self._hm(market_dt) > (self.profile.daily_end_hour, self.profile.daily_end_minute):
                return (market_dt + timedelta(days=1)).date()
        return market_dt.date()

    def _bar_tz(self, dt: datetime) -> ZoneInfo:
        if dt.tzinfo:
            if hasattr(dt.tzinfo, "key"):
                return ZoneInfo(dt.tzinfo.key)
            return ZoneInfo(str(dt.tzinfo))
        tz = self.profile.default_bar_tz or self.profile.market_tz
        return ZoneInfo(tz)

    def _to_aware(self, dt: datetime, tz: ZoneInfo) -> datetime:
        if dt.tzinfo:
            return dt
        return dt.replace(tzinfo=tz)

    def _make_daily_bar(self, bar: BarData, session_date) -> BarData:
        dt0 = bar.datetime.replace(
            year=session_date.year, month=session_date.month, day=session_date.day,
            hour=0, minute=0, second=0, microsecond=0,
        )
        return BarData(
            gateway_name=bar.gateway_name,
            symbol=bar.symbol,
            exchange=bar.exchange,
            datetime=dt0,
            interval=Interval.DAILY,
            volume=bar.volume,
            turnover=bar.turnover,
            open_interest=bar.open_interest,
            open_price=bar.open_price,
            high_price=bar.high_price,
            low_price=bar.low_price,
            close_price=bar.close_price,
        )

    def _finalize_and_push(self) -> None:
        if not self.daily_bar:
            return
        self.daily_bar.datetime = self.daily_bar.datetime.replace(hour=0, minute=0, second=0, microsecond=0)
        self.on_daily_bar(self.daily_bar)
        self.daily_bar = None
        self._session_date = None

    def update_bar(self, bar: BarData) -> None:
        if self._naive_input is None:
            self._naive_input = bar.datetime.tzinfo is None

        if bar.interval == Interval.DAILY:
            self.on_daily_bar(bar)
            self._last_bar_dt = bar.datetime
            self._last_market_dt = bar.datetime
            return

        bar_tz = self._bar_tz(bar.datetime)
        market_tz = ZoneInfo(self.profile.market_tz)

        bar_dt = self._to_aware(bar.datetime, bar_tz)
        market_dt = bar_dt.astimezone(market_tz)
        session_date = self._session_date_from_market_dt(market_dt)

        if not self.daily_bar:
            self.daily_bar = self._make_daily_bar(bar, session_date)
            self._session_date = session_date
            self._last_bar_dt = bar_dt
            self._last_market_dt = market_dt
            return

        if session_date != self._session_date:
            self._finalize_and_push()
            self.daily_bar = self._make_daily_bar(bar, session_date)
            self._session_date = session_date
            self._last_bar_dt = bar_dt
            self._last_market_dt = market_dt
            return

        d = self.daily_bar
        d.volume += bar.volume
        d.turnover += bar.turnover
        d.open_interest = bar.open_interest
        d.high_price = max(d.high_price, bar.high_price)
        d.low_price = min(d.low_price, bar.low_price)
        d.close_price = bar.close_price

        if self._last_bar_dt:
            close_market_dt = datetime.combine(session_date, self.profile.end_time, tzinfo=market_tz)
            close_bar_dt = close_market_dt.astimezone(bar_tz)

            last_dt = self._last_bar_dt
            if last_dt < close_bar_dt <= bar_dt:
                self._finalize_and_push()
            elif self.profile.close_on_date_change and self._last_market_dt and market_dt.date() != self._last_market_dt.date():
                self._finalize_and_push()

        self._last_bar_dt = bar_dt
        self._last_market_dt = market_dt


# =============================================================================
# 策略主类 - 负责交易执行与UI交互
# =============================================================================
class Nr7BreakoutStrategy(CtaTemplate):
    """
    NR7突破策略主类
    
    职责:
    - 接收行情数据 (on_tick/on_bar)
    - 调用 Signal 计算交易目标
    - 执行下单逻辑 (send_orders)
    - 记录运行日志 (write_log)
    - 更新UI变量 (put_event)
    
    交易逻辑:
    - 入场: 7日波动范围最小后突破
    - 出场: 反向突破或持有期满
    """
    
    author = "Raymond Hsiao (refactored)"

    # === 策略参数 ===
    nr_lookback = 7           # NR 回看周期（默认 NR7）
    use_trend_filter = True   # 是否使用趋势过滤
    trend_ma_period = 20      # 趋势 MA 周期
    use_long_only = True      # 只做多
    fixed_size = 1            # 固定交易手数
    _daily_end_minute = 59

    parameters = [
        "nr_lookback",
        "use_trend_filter",
        "trend_ma_period",
        "use_long_only",
        "fixed_size"]

    # === 运行变量 ===
    target: int = 0
    today_range: float = 0.0      # 今日波幅
    min_range_n: float = 0.0      # 过去 N 日最小波幅
    is_nr7: bool = False          # 是否触发 NR7
    ma_value: float = 0.0         # 当前 MA 值
    prev_high: float = 0.0        # 昨日最高价
    days_in_trade: int = 0        # 持仓天数

    variables = [
        "today_range", "min_range_n", "is_nr7",
        "ma_value", "prev_high", "days_in_trade"
    ]

    # ========== 参数分类 ==========
    # 信号参数：影响开平仓交易信号的生成
    signal_parameters = [
        "nr_lookback",
        "use_trend_filter",
        "trend_ma_period",
        "use_long_only"]
    # 仓位参数：只影响仓位大小和下单执行方式
    position_parameters = [
        "fixed_size"]

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.bg = BarGenerator(self.on_bar)

        key = get_product_name(vt_symbol)
        contract_size = cta_engine.get_size(self)
        self.write_log(
            f"[初始化] 品种: {key}, 合约乘数: {contract_size}, NR{self.nr_lookback}"
        )

        self.signal = Nr7Signal(
            vt_symbol=vt_symbol,
            nr_lookback=self.nr_lookback,
            use_trend_filter=self.use_trend_filter,
            trend_ma_period=self.trend_ma_period,
            use_long_only=self.use_long_only,
            fixed_size=self.fixed_size,
            daily_end_minute = self._daily_end_minute,
        )
        self.target = 0
        self.am = self.signal.factor.am

    def on_init(self):
        """策略初始化：加载历史K线数据完成指标预热"""
        end_time = time(self._daily_end_hour, self._daily_end_minute) if hasattr(self, '_daily_end_hour') else time(23, 59)
        self.write_log(
            f"[策略初始化] 请求历史数据用于合成日线并预热技术指标："
            f"end_time={end_time} am_size={self.am.size}"
        )
        self.load_bar(self.am.size)

    def on_start(self):
        """策略启动"""
        self.write_log(f"[策略启动] {self.__class__.__name__} 已启动。")
        self.put_event()

    def on_stop(self):
        """策略停止"""
        self.write_log("[策略停止] 策略已停止运行。")

    def on_tick(self, tick: TickData) -> None:
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData) -> None:
        self.cancel_all()

        prev_target = self.target
        self.calculate_targets(bar)

        # 更新 UI 变量
        self.today_range = self.signal.factor.today_range
        self.min_range_n = self.signal.factor.min_range_n
        self.is_nr7 = self.signal.factor.is_nr7
        self.ma_value = self.signal.factor.ma_value
        self.prev_high = self.signal.factor.prev_high
        self.days_in_trade = self.signal.factor.days_in_trade

        dt_str = bar.datetime.strftime('%Y-%m-%d %H:%M:%S')
        if self.target != prev_target:
            if self.target > 0:
                self.write_log(
                    f"[{dt_str}] [信号] NR7做多 | 波幅: {self.today_range:.2f}, "
                    f"MA{self.trend_ma_period}: {self.ma_value:.2f}, 昨日高: {self.prev_high:.2f}"
                )
            elif self.target == 0 and prev_target > 0:
                self.write_log(
                    f"[{dt_str}] [信号] 突破昨日高平仓 | 价格: {bar.close_price:.2f}, 昨日高: {self.prev_high:.2f}"
                )

        self.send_orders(bar, dt_str)
        self.put_event()

    def calculate_targets(self, bar: BarData) -> None:
        self.signal.on_bar(bar)
        self.target = self.signal.get_target()

    def send_orders(self, bar: BarData, dt_str: str = None) -> None:
        if dt_str is None:
            dt_str = bar.datetime.strftime('%Y-%m-%d %H:%M:%S')
        if self.target == self.pos:
            return

        if self.target > self.pos:
            self._process_long_orders(bar, dt_str)
        else:
            self._process_short_orders(bar, dt_str)

    def _process_long_orders(self, bar: BarData, dt_str: str = None) -> None:
        if dt_str is None:
            dt_str = bar.datetime.strftime('%Y-%m-%d %H:%M:%S')
        diff = self.target - self.pos
        cover_volume = min(diff, abs(self.pos)) if self.pos < 0 else 0
        buy_volume = diff - cover_volume
        if cover_volume:
            if self.trading:
                self.write_log(f"[{dt_str}] [下单] 平空仓 | 价格: {price:.2f}, 数量: {cover_volume}")
            self.cover(price, cover_volume)
        if buy_volume:
            if self.trading:
                self.write_log(f"[{dt_str}] [下单] 开多仓(NR7) | 价格: {price:.2f}, 数量: {buy_volume}")
            self.buy(price, buy_volume)

    def _process_short_orders(self, bar: BarData, dt_str: str = None) -> None:
        if dt_str is None:
            dt_str = bar.datetime.strftime('%Y-%m-%d %H:%M:%S')
        diff = self.target - self.pos
        sell_volume = min(abs(diff), self.pos) if self.pos > 0 else 0
        short_volume = abs(diff) - sell_volume
        if sell_volume:
            if self.trading:
                self.write_log(f"[{dt_str}] [下单] 平多仓 | 价格: {price:.2f}, 数量: {sell_volume}")
            self.sell(price, sell_volume)
        if short_volume:
            if self.trading:
                self.write_log(f"[{dt_str}] [下单] 开空仓 | 价格: {price:.2f}, 数量: {short_volume}")
            self.short(price, short_volume)

    def on_trade(self, trade: TradeData):
        """成交回报推送：刷新UI显示最新持仓"""
        self.write_log(
            f"[成交回报] 方向={trade.direction.value}, 成交量={trade.volume}, "
            f"成交均价={trade.price}, 最新持仓={self.pos}"
        )
        self.put_event()

    def on_order(self, order: OrderData) -> None:
        pass

    def on_stop_order(self, stop_order: StopOrder) -> None:
        pass


class Nr7Factor:
    """NR7 信号因子"""

    def __init__(
        self,
        vt_symbol: str,
        nr_lookback: int,
        use_trend_filter: bool,
        trend_ma_period: int,
        use_long_only: bool,
        fixed_size: int,
        daily_end_minute: int,
    ):
        self.vt_symbol = vt_symbol
        self.nr_lookback = nr_lookback
        self.use_trend_filter = use_trend_filter
        self.trend_ma_period = trend_ma_period
        self.use_long_only = use_long_only
        self.fixed_size = fixed_size
        self._daily_end_minute = daily_end_minute

        self.am = ArrayManager(max(nr_lookback, trend_ma_period) + 20)
        self.bg = SessionDailyBarGenerator(
            self.on_daily_bar,
            vt_symbol=vt_symbol,
            auto_daily_end=True,
        )

        self.today_range = 0.0
        self.min_range_n = 0.0
        self.is_nr7 = False
        self.ma_value = 0.0
        self.prev_high = 0.0
        self.days_in_trade = 0
        self.has_position = False

    def on_bar(self, bar: BarData) -> None:
        if bar.interval == Interval.DAILY:
            self.am.update_bar(bar)
            self.on_daily_bar(bar)
        else:
            self.bg.update_bar(bar)

    def on_daily_bar(self, bar: BarData):
        """
        日线Bar推送（策略实际交易周期）：
        1. 撤销所有未成交委托
        2. 更新技术指标并计算目标仓位
        3. 根据目标仓位发送交易指令
        4. 刷新UI事件
        """
        if not self.am.inited:
            return

        # 计算今日波幅和过去 N 日最小波幅
        high_arr = self.am.high
        low_arr = self.am.low
        close_arr = self.am.close

        n = self.nr_lookback
        if len(high_arr) < n + 1:
            return

        # 今日波幅 (倒数第一个)
        self.today_range = high_arr[-1] - low_arr[-1]
        # 过去 n 日（含今日）的最小波幅
        ranges = high_arr[-n:] - low_arr[-n:]
        self.min_range_n = ranges.min()
        self.is_nr7 = self.today_range <= self.min_range_n + 1e-9

        self.prev_high = high_arr[-2] if len(high_arr) >= 2 else high_arr[-1]
        self.ma_value = self.am.sma(self.trend_ma_period, array=False) if self.use_trend_filter else 0.0

        if self.days_in_trade > 0:
            self.days_in_trade += 1

    def get_target(self) -> int:
        if not self.am.inited:
            return 0

        close = self.am.close[-1]

        # 出场: 高于昨日最高价
        if self.has_position:
            if close > self.prev_high:
                self.has_position = False
                self.days_in_trade = 0
                return 0
            return self.fixed_size

        # 入场: NR7 + (可选)趋势过滤
        long_signal = self.is_nr7
        if self.use_trend_filter:
            long_signal = long_signal and close > self.ma_value

        if long_signal:
            self.has_position = True
            self.days_in_trade = 1
            return self.fixed_size

        if not self.use_long_only:
            short_signal = self.is_nr7
            if self.use_trend_filter:
                short_signal = short_signal and close < self.ma_value
            if short_signal:
                self.has_position = True
                self.days_in_trade = 1
                return -self.fixed_size

        return 0


class Nr7Signal:
    def __init__(
        self,
        vt_symbol: str,
        nr_lookback: int,
        use_trend_filter: bool,
        trend_ma_period: int,
        use_long_only: bool,
        fixed_size: int,
        daily_end_minute: int,
    ):
        self.factor = Nr7Factor(
            vt_symbol=vt_symbol,
            nr_lookback=nr_lookback,
            use_trend_filter=use_trend_filter,
            trend_ma_period=trend_ma_period,
            use_long_only=use_long_only,
            fixed_size=fixed_size,
            daily_end_minute=daily_end_minute,
        )

    def on_bar(self, bar: BarData) -> None:
        self.factor.on_bar(bar)

    def get_target(self) -> int:
        return self.factor.get_target()


def get_product_name(vt_symbol: str) -> str:
    return vt_symbol.split(".")[0]


# test
