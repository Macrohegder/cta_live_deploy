"""
MACD柱状图策略 (自包含版)
=========================================================
基于 QuantifiedStrategies 文章复现

策略特点:
- MACD柱状图连续下跌后反转做多
- 收盘价上涨出场

作者: Raymond Hsiao
来源: strategy_factory 自动生成 + 人工重构为 Signal/Factor 架构
重构时间: 2025-4

架构说明:
    Strategy (TargetPosTemplate) -> Signal (交易逻辑) -> Factor (指标计算)
    
    三层分离优势:
    1. 指标计算与交易执行解耦 方便单元测试
    2. 信号逻辑独立 可复用于多个策略
    3. UI变量与核心逻辑分离 实盘监控更清晰

实盘特性:
    - 继承 TargetPosTemplate，自动 diff 下单，避免手动的开平仓逻辑错误
    - 自包含 SessionDailyBarGenerator / ensure_am_inited，零外部依赖，
      解决实盘部署时的 import 路径和日线合成问题
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Callable, Optional, Tuple
from zoneinfo import ZoneInfo

from vnpy.trader.object import BarData, TickData, TradeData, OrderData
from vnpy.trader.constant import Interval
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


# =============================================================================
# 策略主类 - 负责交易执行与UI交互 (TargetPosTemplate)
# =============================================================================
class MacdHistogramStrategy(TargetPosTemplate):
    """
    MACD柱状图策略主类
    
    职责:
    - 接收行情数据 (on_tick/on_bar)
    - 调用 Signal 计算交易目标
    - 通过 TargetPosTemplate 自动 diff 下单
    - 记录运行日志 (write_log)
    - 更新UI变量 (put_event)
    
    交易逻辑:
    - 入场: MACD柱状图连续下跌后反转做多
    - 出场: 收盘价上涨
    """
    
    author = "Raymond Hsiao (refactored)"

    # === 策略参数 ===
    fast_period = 12          # MACD快线周期
    slow_period = 26          # MACD慢线周期
    signal_period = 9         # MACD信号线周期
    decline_days = 4          # 柱状图连续下跌天数
    fixed_size = 100          # 固定交易手数
    tick_add = 0              # 目标仓位模板的价格偏移
    auto_daily_end = True
    daily_end_hour = 14
    daily_end_minute = 59

    parameters = [
        "fast_period",
        "slow_period",
        "signal_period",
        "decline_days",
        "fixed_size",
        "tick_add",
        "auto_daily_end",
        "daily_end_hour",
        "daily_end_minute"]
    # ==== 参数分类 ==========
    # 信号参数：影响开平仓交易信号的生成
    signal_parameters = [
        "fast_period",
        "slow_period",
        "signal_period",
        "decline_days",
        "auto_daily_end",
        "daily_end_hour",
        "daily_end_minute"]
    # 仓位参数：只影响仓位大小和下单执行方式
    position_parameters = [
        "fixed_size",
        "tick_add"]


    # === 运行变量 ===
    target_pos: int = 0
    hist_value: float = 0.0       # 当前MACD柱状图值
    entry_flag: int = 0         # 入场标志

    variables = [ "hist_value", "entry_flag"]

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.bg = BarGenerator(self.on_bar)

        key = get_product_name(vt_symbol)
        contract_size = cta_engine.get_size(self)
        self.write_log(
            f"[初始化] 品种: {key}, 合约乘数: {contract_size}, MACD({self.fast_period},{self.slow_period},{self.signal_period})"
        )

        self.signal = MacdHistogramSignal(
            vt_symbol=vt_symbol,
            fast_period=self.fast_period,
            slow_period=self.slow_period,
            signal_period=self.signal_period,
            decline_days=self.decline_days,
            fixed_size=self.fixed_size,
            auto_daily_end=self.auto_daily_end,
            daily_end_hour=self.daily_end_hour,
            daily_end_minute=self.daily_end_minute,
        )
        self.target_pos = 0
        self.am = self.signal.factor.am

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
        self.write_log("[策略启动] MACD Histogram 策略启动")

    def on_stop(self):
        self.write_log(f"[策略停止] 策略停止，当前持仓: {self.pos}")

    def on_tick(self, tick: TickData) -> None:
        super().on_tick(tick)
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData) -> None:
        super().on_bar(bar)

        prev_target = self.target_pos
        self.calculate_targets(bar)

        # 更新 UI 变量
        self.hist_value = self.signal.factor.hist_value
        self.entry_flag = self.signal.factor.entry_flag

        dt_str = bar.datetime.strftime('%Y-%m-%d %H:%M:%S')
        if self.target_pos != prev_target:
            if self.target_pos > 0:
                self.write_log(
                    f"[{dt_str}] [日线-信号] MACD做多 | 柱状图: {self.hist_value:.4f}, 连续下跌{self.decline_days}天"
                )
            elif self.target_pos == 0 and prev_target > 0:
                self.write_log(
                    f"[{dt_str}] [日线-信号] 收盘价上涨平仓 | 价格: {bar.close_price:.2f}"
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


# =============================================================================
# 信号类 - 封装交易逻辑
# =============================================================================
class MacdHistogramSignal:
    """
    MACD柱状图策略信号处理器
    
    职责:
    - 接收Bar数据（分钟级或日线）
    - 调用 Factor 计算技术指标
    - 根据指标值生成交易信号 (target)
    
    信号逻辑:
    - 入场: MACD柱状图连续下跌后反转做多
    - 出场: 收盘价上涨
    """
    def __init__(
        self,
        vt_symbol: str,
        fast_period: int,
        slow_period: int,
        signal_period: int,
        decline_days: int,
        fixed_size: int,
        auto_daily_end: bool,
        daily_end_hour: int,
        daily_end_minute: int,
    ) -> None:
        self.vt_symbol = vt_symbol
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.signal_period = signal_period
        self.decline_days = decline_days
        self.fixed_size = fixed_size
        self.auto_daily_end = auto_daily_end
        self.daily_end_hour = daily_end_hour
        self.daily_end_minute = daily_end_minute

        self.target = 0

        self.factor = MacdHistogramFactor(
            fast_period=self.fast_period,
            slow_period=self.slow_period,
            signal_period=self.signal_period,
            decline_days=self.decline_days,
            fixed_size=self.fixed_size,
            auto_daily_end=self.auto_daily_end,
            daily_end_hour=self.daily_end_hour,
            daily_end_minute=self.daily_end_minute,
        )

    def on_bar(self, bar: BarData) -> None:
        self.factor.on_bar(bar)
        self.target = self.factor.get_target()

    def get_target(self) -> int:
        return int(self.target)


# =============================================================================
# 因子类 - 封装指标计算
# =============================================================================
class MacdHistogramFactor:
    """
    MACD柱状图策略因子计算器
    
    职责:
    - 管理ArrayManager（K线数据缓存）
    - 计算MACD指标
    - 判断交易信号
    """
    def __init__(
        self,
        fast_period: int,
        slow_period: int,
        signal_period: int,
        decline_days: int,
        fixed_size: int,
        auto_daily_end: bool,
        daily_end_hour: int,
        daily_end_minute: int,
    ) -> None:
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.signal_period = signal_period
        self.decline_days = decline_days
        self.fixed_size = fixed_size
        self.auto_daily_end = auto_daily_end
        self.daily_end_hour = daily_end_hour
        self.daily_end_minute = daily_end_minute

        # UI变量
        self.hist_value = 0.0
        self.entry_flag = 0
        self.target = 0

        total_needed = fast_period + slow_period + signal_period + decline_days + 50
        self.am = ArrayManager(size=total_needed)
        self.daily_bg = SessionDailyBarGenerator(
            on_daily_bar=self.on_daily_bar,
            vt_symbol=getattr(self, 'vt_symbol', ''),
            auto_daily_end=self.auto_daily_end,
            daily_end_hour=self.daily_end_hour,
            daily_end_minute=self.daily_end_minute,
        )

    def on_bar(self, bar: BarData) -> None:
        if bar.interval == Interval.DAILY:
            self.on_daily_bar(bar)
        else:
            self.daily_bg.update_bar(bar)

    def on_daily_bar(self, bar: BarData) -> None:
        self.am.update_bar(bar)
        if not self.am.inited:
            return

        dif, dea, hist = self.am.macd(self.fast_period, self.slow_period, self.signal_period, array=False)
        self.hist_value = hist

        # Need enough bars for decline_days check
        total_needed = self.fast_period + self.slow_period + self.signal_period + self.decline_days
        if len(self.am.close) < total_needed:
            return

        hist_arr = self.am.macd(self.fast_period, self.slow_period, self.signal_period, array=True)[2]

        # Current position handling
        if self.target > 0:
            # Exit when close > previous close
            if bar.close_price > self.am.close[-2]:
                self.target = 0
                self.entry_flag = 0
            else:
                self.target = self.fixed_size
            return

        # Entry condition (long only)
        # 1. MACD Histogram fell decline_days in a row
        falling = True
        for i in range(1, self.decline_days):
            if hist_arr[-i] >= hist_arr[-i - 1]:
                falling = False
                break

        # 2. The decline_days-th latest bar was below zero
        below_zero = hist_arr[-self.decline_days] < 0

        # 3. Current close < previous close
        close_falling = bar.close_price < self.am.close[-2]

        if falling and below_zero and close_falling:
            self.target = self.fixed_size
            self.entry_flag = 1
        else:
            self.target = 0
            self.entry_flag = 0

    def get_target(self) -> int:
        return self.target


def get_product_name(vt_symbol: str) -> str:
    symbol = "".join(w for w in vt_symbol.split(".")[0] if not w.isdigit())
    return symbol.upper()
