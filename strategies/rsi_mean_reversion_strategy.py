"""
RSI均值回归策略 (自包含版)
=========================================================

交易逻辑:
- 入场: RSI超卖做多
- 出场: RSI超买平仓

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
from vnpy_ctastrategy.base import EngineType


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
        symbol = _parse_symbol(vt_symbol)
        root = "".join(c for c in symbol if not c.isdigit())
        if exchange == "CFFEX" and root in {"IF", "IH", "IC", "IM"}:
            end_hour, end_minute = 15, 0
        elif exchange == "CFFEX" and root in {"T", "TF", "TS", "TL"}:
            end_hour, end_minute = 15, 15
        else:
            end_hour, end_minute = 14, 59
        return MarketProfile(
            daily_end_hour=end_hour,
            daily_end_minute=end_minute,
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
class RsiMeanReversionStrategy(TargetPosTemplate):
    """
    RSI均值回归策略主类
    
    职责:
    - 接收行情数据 (on_tick/on_bar)
    - 调用 Signal 计算交易目标
    - 通过 TargetPosTemplate 自动 diff 下单
    - 记录运行日志 (write_log)
    - 更新UI变量 (put_event)
    
    交易逻辑:
    - 入场: RSI超卖做多
    - 出场: RSI超买平仓
    """
    
    author = "Raymond Hsiao (refactored)"

    rsi_window = 14
    rsi_buy_threshold = 30
    rsi_sell_threshold = 70
    rsi_exit_mean = 50
    atr_window = 14
    sl_atr_multiplier = 2.0
    risk_percent = 0.02
    capital = 1_000_000
    contract_size = 300
    auto_daily_end = True
    daily_end_hour = 14
    daily_end_minute = 59

    parameters = [
        "rsi_window",
        "rsi_buy_threshold",
        "rsi_sell_threshold",
        "rsi_exit_mean",
        "atr_window",
        "sl_atr_multiplier",
        "risk_percent",
        "capital",
        "contract_size"]

    signal_parameters = [
        "rsi_window",
        "rsi_buy_threshold",
        "rsi_sell_threshold",
        "rsi_exit_mean",
        "atr_window",
        "sl_atr_multiplier",
        "risk_percent",
        "capital",
        "contract_size"]
    position_parameters = [
        "auto_daily_end",
        "daily_end_hour",
        "daily_end_minute"]

    # === 运行变量 (UI 可见) ===
    target_pos: int = 0                     # 目标仓位
    rsi_value: float = 0.0                  # 当前RSI值
    atr_value: float = 0.0                  # 当前ATR值
    entry_price: float = 0.0                # 入场价格
    current_stop: float = 0.0               # 当前止损价
    trade_size: int = 0                     # 交易手数
    
    variables = [ "rsi_value", "atr_value", "entry_price", "current_stop", "trade_size"]

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.bg = BarGenerator(self.on_bar)

        key = get_product_name(vt_symbol)
        contract_size = cta_engine.get_size(self)
        if contract_size and "contract_size" not in setting:
            self.contract_size = contract_size
        self.write_log(
            f"[初始化] 品种: {key}, 合约乘数: {self.contract_size}, 参数: {setting}"
        )

        self.signal = RsiMeanReversionSignal(
            vt_symbol=vt_symbol,
            rsi_window=self.rsi_window,
            rsi_buy_threshold=self.rsi_buy_threshold,
            rsi_sell_threshold=self.rsi_sell_threshold,
            rsi_exit_mean=self.rsi_exit_mean,
            atr_window=self.atr_window,
            sl_atr_multiplier=self.sl_atr_multiplier,
            risk_percent=self.risk_percent,
            capital=self.capital,
            contract_size=self.contract_size,
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
        self.write_log("[on_start] 策略启动")

    def on_stop(self):
        self.write_log(f"[on_stop] 策略停止，当前持仓: {self.pos}")

    def on_tick(self, tick: TickData) -> None:
        super().on_tick(tick)
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData) -> None:
        super().on_bar(bar)
        
        prev_target = self.target_pos
        prev_stop = self.current_stop
        self.calculate_targets(bar)
        
        # 更新UI变量
        self.rsi_value = self.signal.factor.rsi_value
        self.atr_value = self.signal.factor.atr_value
        self.trade_size = self.signal.factor.trade_size
        
        # 检查信号变化并记录日志
        dt_str = bar.datetime.strftime('%Y-%m-%d %H:%M:%S')
        if self.target_pos != prev_target:
            if self.target_pos > 0:
                self.write_log(f"[{dt_str}] [日线-信号] RSI超卖做多 | RSI: {self.rsi_value:.1f} < {self.rsi_buy_threshold}, 手数: {self.trade_size}")
            elif self.target_pos < 0:
                self.write_log(f"[{dt_str}] [日线-信号] RSI超买做空 | RSI: {self.rsi_value:.1f} > {self.rsi_sell_threshold}, 手数: {self.trade_size}")
            else:
                is_stop_loss = (prev_target > 0 and bar.close_price <= prev_stop) or (prev_target < 0 and bar.close_price >= prev_stop)
                exit_reason = "止损" if is_stop_loss else "RSI回归均值"
                self.write_log(f"[{dt_str}] [日线-信号] 平仓 | 原因: {exit_reason}, RSI: {self.rsi_value:.1f}")
        
        self.set_target_pos(self.target_pos)
        self.put_event()

    def calculate_targets(self, bar: BarData) -> None:
        self.signal.on_bar(bar)
        self.target_pos = self.signal.get_target()
        
        # 更新持仓状态
        if self.target_pos != 0:
            self.entry_price = self.signal.factor.entry_price
            self.current_stop = self.signal.factor.current_stop
        else:
            self.entry_price = 0.0
            self.current_stop = 0.0

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
class RsiMeanReversionSignal:
    """
    RSI均值回归策略信号处理器
    
    职责:
    - 接收Bar数据（分钟级或日线）
    - 调用 Factor 计算技术指标
    - 根据指标值生成交易信号 (target)
    
    信号逻辑:
    - 入场: RSI超卖做多
    - 出场: RSI超买平仓
    """
    def __init__(
        self,
        vt_symbol: str,
        rsi_window: int,
        rsi_buy_threshold: int,
        rsi_sell_threshold: int,
        rsi_exit_mean: int,
        atr_window: int,
        sl_atr_multiplier: float,
        risk_percent: float,
        capital: int,
        contract_size: int,
        auto_daily_end: bool,
        daily_end_hour: int,
        daily_end_minute: int,
        fixed_size: int = None,
    ) -> None:
        self.vt_symbol = vt_symbol
        self.rsi_window = rsi_window
        self.rsi_buy_threshold = rsi_buy_threshold
        self.rsi_sell_threshold = rsi_sell_threshold
        self.rsi_exit_mean = rsi_exit_mean
        self.atr_window = atr_window
        self.sl_atr_multiplier = sl_atr_multiplier
        self.risk_percent = risk_percent
        self.capital = capital
        self.contract_size = contract_size
        self.auto_daily_end = auto_daily_end
        self.daily_end_hour = daily_end_hour
        self.daily_end_minute = daily_end_minute
        self.fixed_size = fixed_size

        self.target = 0

        self.factor = RsiMeanReversionFactor(
            rsi_window=self.rsi_window,
            rsi_buy_threshold=self.rsi_buy_threshold,
            rsi_sell_threshold=self.rsi_sell_threshold,
            rsi_exit_mean=self.rsi_exit_mean,
            atr_window=self.atr_window,
            sl_atr_multiplier=self.sl_atr_multiplier,
            risk_percent=self.risk_percent,
            capital=self.capital,
            contract_size=self.contract_size,
            auto_daily_end=self.auto_daily_end,
            daily_end_hour=self.daily_end_hour,
            daily_end_minute=self.daily_end_minute,
            fixed_size=self.fixed_size,
        )

    def on_bar(self, bar: BarData) -> None:
        self.factor.on_bar(bar)
        self.target = self.factor.get_target()

    def get_target(self) -> int:
        return int(self.target)


# =============================================================================
# 因子类 - 封装指标计算
# =============================================================================
class RsiMeanReversionFactor:
    """
    RSI均值回归策略因子计算器
    
    职责:
    - 管理ArrayManager（K线数据缓存）
    - 计算技术指标
    - 判断交易信号
    """
    def __init__(
        self,
        rsi_window: int,
        rsi_buy_threshold: int,
        rsi_sell_threshold: int,
        rsi_exit_mean: int,
        atr_window: int,
        sl_atr_multiplier: float,
        risk_percent: float,
        capital: int,
        contract_size: int,
        auto_daily_end: bool,
        daily_end_hour: int,
        daily_end_minute: int,
        fixed_size: int = None,
    ) -> None:
        self.rsi_window = rsi_window
        self.rsi_buy_threshold = rsi_buy_threshold
        self.rsi_sell_threshold = rsi_sell_threshold
        self.rsi_exit_mean = rsi_exit_mean
        self.atr_window = atr_window
        self.sl_atr_multiplier = sl_atr_multiplier
        self.risk_percent = risk_percent
        self.capital = capital
        self.contract_size = contract_size
        self.auto_daily_end = auto_daily_end
        self.daily_end_hour = daily_end_hour
        self.daily_end_minute = daily_end_minute
        self.fixed_size = fixed_size

        # UI变量
        self.rsi_value = 0.0
        self.atr_value = 0.0
        self.entry_price = 0.0
        self.current_stop = 0.0
        self.trade_size = 0
        self.target = 0

        size = max(rsi_window, atr_window) + 50
        self.am = ArrayManager(size=size)
        self.daily_bg = SessionDailyBarGenerator(
            on_daily_bar=self.on_daily_bar,
            vt_symbol="",
            auto_daily_end=self.auto_daily_end,
            daily_end_hour=self.daily_end_hour,
            daily_end_minute=self.daily_end_minute,
        )

    def on_bar(self, bar: BarData) -> None:
        if bar.interval == Interval.DAILY:
            self.on_daily_bar(bar)
        else:
            self.daily_bg.update_bar(bar)
            
            # Intraday stop loss check
            if self.target > 0 and bar.close_price <= self.current_stop:
                self.target = 0
            elif self.target < 0 and bar.close_price >= self.current_stop:
                self.target = 0

    def on_daily_bar(self, bar: BarData) -> None:
        self.am.update_bar(bar)
        if not self.am.inited:
            return

        self.rsi_value = self.am.rsi(self.rsi_window)
        self.atr_value = self.am.atr(self.atr_window)

        if self.target == 0:
            if self.rsi_value < self.rsi_buy_threshold or self.rsi_value > self.rsi_sell_threshold:
                sl_dist = self.atr_value * self.sl_atr_multiplier
                if self.fixed_size is not None:
                    size = self.fixed_size
                else:
                    size = 1
                    if sl_dist > 0:
                        risk_amount = self.capital * self.risk_percent
                        raw_size = risk_amount / (sl_dist * self.contract_size)
                        size = max(1, int(raw_size))
                self.trade_size = size
                if self.rsi_value < self.rsi_buy_threshold:
                    self.target = size
                    self.entry_price = bar.close_price
                    self.current_stop = self.entry_price - sl_dist
                else:
                    self.target = -size
                    self.entry_price = bar.close_price
                    self.current_stop = self.entry_price + sl_dist
        elif self.target > 0:
            sl_dist = self.atr_value * self.sl_atr_multiplier
            new_stop = bar.close_price - sl_dist
            self.current_stop = max(self.current_stop, new_stop)
            if self.rsi_value > self.rsi_exit_mean:
                self.target = 0
        elif self.target < 0:
            sl_dist = self.atr_value * self.sl_atr_multiplier
            new_stop = bar.close_price + sl_dist
            self.current_stop = min(self.current_stop, new_stop)
            if self.rsi_value < self.rsi_exit_mean:
                self.target = 0

    def get_target(self) -> int:
        return self.target


def get_product_name(vt_symbol: str) -> str:
    symbol = "".join(w for w in vt_symbol.split(".")[0] if not w.isdigit())
    return symbol.upper()
