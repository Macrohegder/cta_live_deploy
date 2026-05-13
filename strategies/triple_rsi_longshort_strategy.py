from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Callable, Optional, Tuple
from zoneinfo import ZoneInfo

from vnpy.trader.object import BarData, TickData, TradeData, OrderData
from vnpy.trader.constant import Interval
from vnpy_ctastrategy.base import EngineType
from vnpy_ctastrategy import (
    TargetPosTemplate,
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
    "BINANCE",
    "OKX",
    "BYBIT",
    "BITGET",
    "GATEIO",
    "HUOBI",
    "COINBASE",
    "DERIBIT",
}

CN_FUTURES_EXCHANGES = {
    "CFFEX",
    "SHFE",
    "DCE",
    "CZCE",
    "INE",
    "GFEX",
}

US_STOCK_EXCHANGES = {
    "SMART",
    "NYSE",
    "NASDAQ",
    "AMEX",
    "ARCA",
    "BATS",
    "IEX",
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
            daily_end_hour=14,
            daily_end_minute=59,
            close_on_date_change=False,
            rollover_after_end_time=True,
            market_tz="Asia/Shanghai",
            default_bar_tz="Asia/Shanghai",
        )

    if exchange in US_STOCK_EXCHANGES:
        return MarketProfile(
            daily_end_hour=16,
            daily_end_minute=0,
            close_on_date_change=False,
            rollover_after_end_time=False,
            market_tz="America/New_York",
            default_bar_tz="UTC",
        )

    crypto_exchange = _infer_crypto_exchange_from_symbol(symbol) if exchange == "GLOBAL" else None
    if exchange in CRYPTO_EXCHANGES or exchange == "GLOBAL" or crypto_exchange:
        return MarketProfile(
            daily_end_hour=23,
            daily_end_minute=59,
            close_on_date_change=True,
            rollover_after_end_time=False,
            market_tz="UTC",
            default_bar_tz="UTC",
        )

    return MarketProfile(
        daily_end_hour=23,
        daily_end_minute=59,
        close_on_date_change=True,
        rollover_after_end_time=False,
        market_tz="UTC",
        default_bar_tz="UTC",
    )


def resolve_market_profile(
    vt_symbol: str,
    auto_daily_end: bool,
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
        daily_end_hour=h,
        daily_end_minute=m,
        close_on_date_change=inferred.close_on_date_change,
        rollover_after_end_time=inferred.rollover_after_end_time,
        market_tz=inferred.market_tz,
        default_bar_tz=inferred.default_bar_tz if bar_tz is None else bar_tz,
    )


def safe_load_bar(strategy, days: int, prefer_daily: bool = True) -> str:
    if not prefer_daily:
        strategy.load_bar(days)
        return "default"

    try:
        strategy.load_bar(days, interval=Interval.DAILY)
        return "daily_kw"
    except TypeError:
        pass

    try:
        strategy.load_bar(days, Interval.DAILY)
        return "daily_pos"
    except TypeError:
        pass

    strategy.load_bar(days)
    return "default"


def warmup_request(required_bars: int, aggregation_window: int = 1, extra_bars: int = 30) -> int:
    n = int(required_bars) * max(1, int(aggregation_window)) + int(extra_bars)
    return max(1, n)


def ensure_am_inited(
    strategy,
    am,
    base_request: int,
    prefer_daily: bool = True,
    max_rounds: int = 3,
    growth: float = 2.0,
) -> Tuple[bool, Tuple[Tuple[int, str, int, bool], ...]]:
    request = max(1, int(base_request))
    attempts = []

    for _ in range(max(1, int(max_rounds))):
        mode = safe_load_bar(strategy, request, prefer_daily=prefer_daily)
        count = int(getattr(am, "count", 0) or 0)
        inited = bool(getattr(am, "inited", False))
        attempts.append((request, mode, count, inited))
        if inited:
            break
        request = max(request + 1, int(request * float(growth)) + 1)

    return bool(getattr(am, "inited", False)), tuple(attempts)


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
            year=session_date.year,
            month=session_date.month,
            day=session_date.day,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
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


class TripleRsiLongShortStrategy(TargetPosTemplate):
    author = "VeighNa AI"

    rsi_period = 5
    ma_period = 200
    rsi_entry_long = 30
    rsi_exit_long = 50
    rsi_prev_threshold_long = 60
    rsi_entry_short = 70
    rsi_exit_short = 50
    rsi_prev_threshold_short = 40
    fixed_size = 1
    auto_daily_end = True
    # 日线截断时间固定为 00:00（北京时间午夜），不参与优化
    daily_end_hour = 0
    daily_end_minute = 0

    parameters = [
        "rsi_period",
        "ma_period",
        "rsi_entry_long",
        "rsi_exit_long",
        "rsi_prev_threshold_long",
        "rsi_entry_short",
        "rsi_exit_short",
        "rsi_prev_threshold_short",
        "fixed_size",
        "auto_daily_end",
        # "daily_end_hour",    # 固定值，不参与优化
        # "daily_end_minute",  # 固定值，不参与优化
    ]
    # ==== 参数分类 ==========
    # 信号参数：影响开平仓交易信号的生成
    signal_parameters = [
        "rsi_period",
        "ma_period",
        "rsi_entry_long",
        "rsi_exit_long",
        "rsi_prev_threshold_long",
        "rsi_entry_short",
        "rsi_exit_short",
        "rsi_prev_threshold_short",
        "auto_daily_end"]
    # 仓位参数：只影响仓位大小和下单执行方式
    position_parameters = [
        "fixed_size"]


    target_pos: int = 0
    rsi_value: float = 0.0
    rsi_1day_ago: float = 0.0
    rsi_2day_ago: float = 0.0
    rsi_3day_ago: float = 0.0
    ma_value: float = 0.0
    entry_count_long: int = 0
    entry_count_short: int = 0

    variables = [
        "rsi_value",
        "rsi_1day_ago",
        "rsi_2day_ago",
        "rsi_3day_ago",
        "ma_value",
        "entry_count_long",
        "entry_count_short"]

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.bg = BarGenerator(self.on_bar)

        key = get_product_name(vt_symbol)
        contract_size = cta_engine.get_size(self)
        self.write_log(f"[初始化] 品种: {key}, 合约乘数: {contract_size}, 参数: {setting}")

        self.signal = TripleRsiLongShortSignal(
            vt_symbol=vt_symbol,
            rsi_period=self.rsi_period,
            ma_period=self.ma_period,
            rsi_entry_long=self.rsi_entry_long,
            rsi_exit_long=self.rsi_exit_long,
            rsi_prev_threshold_long=self.rsi_prev_threshold_long,
            rsi_entry_short=self.rsi_entry_short,
            rsi_exit_short=self.rsi_exit_short,
            rsi_prev_threshold_short=self.rsi_prev_threshold_short,
            fixed_size=self.fixed_size,
            daily_end_hour=self.daily_end_hour,
            daily_end_minute=self.daily_end_minute,
        )

        self.target_pos = 0
        self.am = self.signal.factor.am
        self.last_tick: Optional[TickData] = None  # 保存最新tick

    def on_init(self):
        profile = resolve_market_profile(
            vt_symbol=self.vt_symbol,
            auto_daily_end=self.auto_daily_end,
            daily_end_hour=self.daily_end_hour,
            daily_end_minute=self.daily_end_minute,
        )
        end_time = time(profile.daily_end_hour, profile.daily_end_minute)
        self.write_log(
            f"[策略初始化] 请求历史数据用于合成日线并预热技术指标："
            f"auto_daily_end={self.auto_daily_end} end_time={end_time} am_size={self.am.size}"
        )
        base = warmup_request(self.am.size, aggregation_window=1, extra_bars=30)
        ok, attempts = ensure_am_inited(self, self.am, base, prefer_daily=True)
        last = attempts[-1] if attempts else (0, "", 0, False)
        self.write_log(
            f"[策略初始化] 历史数据加载完成 | ok={ok} "
            f"| last_request={last[0]} mode={last[1]} am_count={last[2]} am_inited={last[3]}"
        )

    def on_start(self):
        self.write_log("[策略启动] 策略启动")

    def on_stop(self):
        self.write_log(f"[策略停止] 策略停止，当前持仓: {self.pos}")

    def on_tick(self, tick: TickData) -> None:
        super().on_tick(tick)
        self.last_tick = tick  # 保存最新tick
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData) -> None:
        super().on_bar(bar)

        prev_target = self.target_pos
        self.calculate_targets(bar)

        self.rsi_value = self.signal.factor.rsi_value
        self.rsi_1day_ago = self.signal.factor.rsi_1day_ago
        self.rsi_2day_ago = self.signal.factor.rsi_2day_ago
        self.rsi_3day_ago = self.signal.factor.rsi_3day_ago
        self.ma_value = self.signal.factor.ma_value
        self.entry_count_long = self.signal.factor.entry_count_long
        self.entry_count_short = self.signal.factor.entry_count_short

        dt_str = bar.datetime.strftime('%Y-%m-%d %H:%M:%S')
        if self.target_pos != prev_target:
            if self.target_pos > prev_target:
                if self.target_pos > 0:
                    self.write_log(
                        f"[{dt_str}] [日线-信号] Triple RSI做多 | RSI: {self.rsi_value:.1f}(↓{self.rsi_1day_ago:.1f}↓{self.rsi_2day_ago:.1f}), "
                        f"3天前: {self.rsi_3day_ago:.1f}, 条件满足: {self.entry_count_long}/4"
                    )
                else:
                    self.write_log(
                        f"[{dt_str}] [日线-信号] 多头减仓/平仓 | 目标: {self.target_pos}"
                    )
            elif self.target_pos < prev_target:
                if self.target_pos < 0:
                    self.write_log(
                        f"[{dt_str}] [日线-信号] Triple RSI做空 | RSI: {self.rsi_value:.1f}(↑{self.rsi_1day_ago:.1f}↑{self.rsi_2day_ago:.1f}), "
                        f"3天前: {self.rsi_3day_ago:.1f}, 条件满足: {self.entry_count_short}/4"
                    )
                else:
                    self.write_log(
                        f"[{dt_str}] [日线-信号] 空头减仓/平仓 | 目标: {self.target_pos}"
                    )

        self.set_target_pos(self.target_pos)
        self.put_event()

    def calculate_targets(self, bar: BarData) -> None:
        self.signal.on_bar(bar)
        self.target_pos = self.signal.get_target()

    def on_trade(self, trade: TradeData) -> None:
        """
        成交回调
        """
        if abs(self.pos) <= 1e-7:
            self.write_log(f"[碎单处理] 持仓量 {self.pos} 清零")
            self.pos = 0
        
        direction = '多' if trade.direction.value == '多' else '空'
        offset = trade.offset.value
        
        self.write_log(
            f"[成交回报] 方向={direction} | "
            f"开平={offset} | "
            f"价格={trade.price:.2f} | "
            f"数量={trade.volume} | "
            f"最新持仓={self.pos}"
        )
        self.put_event()

    def on_order(self, order: OrderData) -> None:
        super().on_order(order)


class TripleRsiLongShortSignal:
    def __init__(
        self,
        vt_symbol: str,
        rsi_period: int,
        ma_period: int,
        rsi_entry_long: int,
        rsi_exit_long: int,
        rsi_prev_threshold_long: int,
        rsi_entry_short: int,
        rsi_exit_short: int,
        rsi_prev_threshold_short: int,
        fixed_size: int,
        daily_end_hour: int,
        daily_end_minute: int,
    ) -> None:
        self.vt_symbol = vt_symbol
        self.rsi_period = rsi_period
        self.ma_period = ma_period
        self.rsi_entry_long = rsi_entry_long
        self.rsi_exit_long = rsi_exit_long
        self.rsi_prev_threshold_long = rsi_prev_threshold_long
        self.rsi_entry_short = rsi_entry_short
        self.rsi_exit_short = rsi_exit_short
        self.rsi_prev_threshold_short = rsi_prev_threshold_short
        self.fixed_size = fixed_size
        self.daily_end_hour = daily_end_hour
        self.daily_end_minute = daily_end_minute

        self.target = 0

        self.factor = TripleRsiLongShortFactor(
            vt_symbol=self.vt_symbol,
            rsi_period=self.rsi_period,
            ma_period=self.ma_period,
            rsi_entry_long=self.rsi_entry_long,
            rsi_exit_long=self.rsi_exit_long,
            rsi_prev_threshold_long=self.rsi_prev_threshold_long,
            rsi_entry_short=self.rsi_entry_short,
            rsi_exit_short=self.rsi_exit_short,
            rsi_prev_threshold_short=self.rsi_prev_threshold_short,
            fixed_size=self.fixed_size,
            daily_end_hour=self.daily_end_hour,
            daily_end_minute=self.daily_end_minute,
        )

    def on_bar(self, bar: BarData) -> None:
        self.factor.on_bar(bar)
        self.target = self.factor.get_target()

    def get_target(self) -> int:
        return int(self.target)


class TripleRsiLongShortFactor:
    def __init__(
        self,
        vt_symbol: str,
        rsi_period: int,
        ma_period: int,
        rsi_entry_long: int,
        rsi_exit_long: int,
        rsi_prev_threshold_long: int,
        rsi_entry_short: int,
        rsi_exit_short: int,
        rsi_prev_threshold_short: int,
        fixed_size: int,
        daily_end_hour: int,
        daily_end_minute: int,
    ) -> None:
        self.vt_symbol = vt_symbol
        self.rsi_period = rsi_period
        self.ma_period = ma_period
        self.rsi_entry_long = rsi_entry_long
        self.rsi_exit_long = rsi_exit_long
        self.rsi_prev_threshold_long = rsi_prev_threshold_long
        self.rsi_entry_short = rsi_entry_short
        self.rsi_exit_short = rsi_exit_short
        self.rsi_prev_threshold_short = rsi_prev_threshold_short
        self.fixed_size = fixed_size
        self.daily_end_hour = daily_end_hour
        self.daily_end_minute = daily_end_minute

        self.target = 0
        self.daily_close_prices: list = []
        self.daily_rsi_values: list = []
        self.last_daily_date: Optional[object] = None

        self.rsi_value = 0.0
        self.rsi_1day_ago = 0.0
        self.rsi_2day_ago = 0.0
        self.rsi_3day_ago = 0.0
        self.ma_value = 0.0
        self.entry_count_long = 0
        self.entry_count_short = 0

        size = ma_period + 50
        self.am = ArrayManager(size=size)
        # 确保小时和分钟在有效范围内
        hour = max(0, min(23, self.daily_end_hour))
        minute = max(0, min(59, self.daily_end_minute))
        self.bg = SessionDailyBarGenerator(
            on_daily_bar=self.on_daily_bar,
            vt_symbol=self.vt_symbol,
            auto_daily_end=True,
            daily_end_hour=hour,
            daily_end_minute=minute,
        )

    def on_bar(self, bar: BarData) -> None:
        if bar.interval == Interval.DAILY:
            self.on_daily_bar(bar)
        else:
            self.bg.update_bar(bar)

    def _calculate_rsi(self, prices: list, period: int) -> float:
        if len(prices) < period + 1:
            return 50.0
        gains = []
        losses = []
        for i in range(1, period + 1):
            change = prices[-i] - prices[-i - 1]
            if change > 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def on_daily_bar(self, bar: BarData) -> None:
        current_date = bar.datetime.date()
        if self.last_daily_date is None or current_date != self.last_daily_date:
            if self.last_daily_date is not None and len(self.daily_close_prices) >= self.rsi_period + 1:
                rsi = self._calculate_rsi(self.daily_close_prices, self.rsi_period)
                self.daily_rsi_values.append(rsi)
            self.daily_close_prices.append(bar.close_price)
            self.last_daily_date = current_date
        else:
            self.daily_close_prices[-1] = bar.close_price

        if len(self.daily_close_prices) < self.ma_period + 5:
            return
        if len(self.daily_rsi_values) < 4:
            return

        self.rsi_value = self._calculate_rsi(self.daily_close_prices, self.rsi_period)
        self.rsi_1day_ago = self.daily_rsi_values[-1]
        self.rsi_2day_ago = self.daily_rsi_values[-2]
        self.rsi_3day_ago = self.daily_rsi_values[-3]
        self.ma_value = sum(self.daily_close_prices[-self.ma_period:]) / self.ma_period

        condition_long_1 = self.rsi_value < self.rsi_entry_long
        condition_long_2 = self.rsi_value < self.rsi_1day_ago and self.rsi_1day_ago < self.rsi_2day_ago
        condition_long_3 = self.rsi_3day_ago < self.rsi_prev_threshold_long
        condition_long_4 = bar.close_price > self.ma_value

        condition_short_1 = self.rsi_value > self.rsi_entry_short
        condition_short_2 = self.rsi_value > self.rsi_1day_ago and self.rsi_1day_ago > self.rsi_2day_ago
        condition_short_3 = self.rsi_3day_ago > self.rsi_prev_threshold_short
        condition_short_4 = bar.close_price < self.ma_value

        self.entry_count_long = sum([condition_long_1, condition_long_2, condition_long_3, condition_long_4])
        self.entry_count_short = sum([condition_short_1, condition_short_2, condition_short_3, condition_short_4])
        entry_signal_long = condition_long_1 and condition_long_2 and condition_long_3 and condition_long_4
        entry_signal_short = condition_short_1 and condition_short_2 and condition_short_3 and condition_short_4

        exit_signal_long = self.rsi_value >= self.rsi_exit_long and self.rsi_1day_ago < self.rsi_exit_long
        exit_signal_short = self.rsi_value <= self.rsi_exit_short and self.rsi_1day_ago > self.rsi_exit_short

        if self.target == 0:
            if entry_signal_long and not entry_signal_short:
                self.target = self.fixed_size
            elif entry_signal_short and not entry_signal_long:
                self.target = -self.fixed_size
            elif entry_signal_long and entry_signal_short:
                if self.rsi_value >= 50:
                    self.target = -self.fixed_size
                else:
                    self.target = self.fixed_size
        elif self.target > 0:
            if exit_signal_long:
                self.target = 0
        elif self.target < 0:
            if exit_signal_short:
                self.target = 0

    def get_target(self) -> int:
        return self.target


def get_product_name(vt_symbol: str) -> str:
    symbol = "".join(w for w in vt_symbol.split(".")[0] if not w.isdigit())
    return symbol.upper()
