"""
Williams %R Mean Reversion Simple Strategy (QuantifiedStrategies) (自包含版)
==================================================================
基于 Williams %R 超买超卖指标的均值回归策略。

入场规则:
  - Williams %R < wr_entry (默认-80) → 买入（超卖）
  - 可选: SMA200 趋势过滤 (use_ma_filter)

出场规则:
  - Williams %R > wr_exit (默认-20) → 卖出（超买回到正常区间）

Williams %R 计算:
  - WR = (highest_high - close) / (highest_high - lowest_low) * -100
  - WR < -80 = 超卖 (买入信号)
  - WR > -20 = 超买 (卖出信号)

作者: Raymond Hsiao (refactored)
来源: strategy_factory 自动生成 + 人工重构为 Signal/Factor 架构
重构时间: 2025-04

架构说明:
    Strategy (TargetPosTemplate) -> Signal (交易逻辑) -> Factor (指标计算)
    
    三层分离优势:
    1. 指标计算与交易执行解耦，方便单元测试
    2. 信号逻辑独立，可复用于多个策略
    3. UI变量与核心逻辑分离，实盘监控更清晰

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

import numpy as np

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
class WilliamsRMrSimpleStrategy(TargetPosTemplate):
    """
    Williams %R均值回归简单策略主类
    
    职责:
    - 接收行情数据 (on_tick/on_bar)
    - 调用 Signal 计算交易目标
    - 通过 TargetPosTemplate 自动 diff 下单
    - 记录运行日志 (write_log)
    - 更新UI变量 (put_event)
    
    交易逻辑:
    - 入场: WR < entry阈值（超卖）
    - 出场: WR > exit阈值（超买）
    - 可选: SMA趋势过滤
    """
    
    author = "Raymond Hsiao (refactored)"

    # === 策略参数 ===
    wr_period = 14
    wr_entry = -80
    wr_exit = -20
    use_ma_filter = False
    ma_period = 200
    fixed_size = 1
    tick_add = 0
    auto_daily_end = True
    daily_end_hour = 14
    daily_end_minute = 59

    parameters = [
        "wr_period", "wr_entry", "wr_exit",
        "use_ma_filter", "ma_period",
        "fixed_size", "tick_add",
        "auto_daily_end", "daily_end_hour", "daily_end_minute"]
    signal_parameters = [
        "wr_period", "wr_entry", "wr_exit",
        "use_ma_filter", "ma_period",
        "auto_daily_end", "daily_end_hour", "daily_end_minute"]
    position_parameters = [
        "fixed_size", "tick_add"]

    # === 运行变量 ===
    target_pos: int = 0
    wr_value: float = 0.0
    ma_value: float = 0.0
    trading_signal: str = ""

    variables = [ "wr_value", "ma_value", "trading_signal"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        
        self.bg = BarGenerator(self.on_bar)
        
        key = get_product_name(vt_symbol)
        contract_size = cta_engine.get_size(self) if hasattr(cta_engine, 'get_size') else 1
        self.write_log(
            f"[初始化] Williams %R简单均值回归策略 | 品种: {key}, 合约乘数: {contract_size}, 参数: {setting}"
        )
        
        self.signal = WilliamsRMrSimpleSignal(
            vt_symbol=vt_symbol,
            wr_period=self.wr_period,
            wr_entry=self.wr_entry,
            wr_exit=self.wr_exit,
            use_ma_filter=self.use_ma_filter,
            ma_period=self.ma_period,
            fixed_size=self.fixed_size,
            auto_daily_end=self.auto_daily_end,
            daily_end_hour=self.daily_end_hour,
            daily_end_minute=self.daily_end_minute,
        )
        self.target_pos = 0
        self.am = self.signal.factor.am
        
        self.daily_bar_count: int = 0
        self.last_daily_dt = None

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
        self.write_log(f"[策略启动] {self.__class__.__name__} 已启动")
        self.put_event()

    def on_stop(self):
        self.write_log(f"[策略停止] 策略停止 | 当前持仓: {self.pos}")

    def on_tick(self, tick: TickData) -> None:
        super().on_tick(tick)
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData) -> None:
        super().on_bar(bar)
        if bar.interval == Interval.DAILY:
            self.on_daily_bar(bar)
        else:
            self.signal.on_bar(bar)

    def on_daily_bar(self, bar: BarData) -> None:
        self.daily_bar_count += 1
        self.last_daily_dt = bar.datetime
        
        dt_str = bar.datetime.strftime('%Y-%m-%d')
        
        self.write_log(
            f"[日线到达] #{self.daily_bar_count} | "
            f"日期={dt_str} | "
            f"O={bar.open_price:.2f} H={bar.high_price:.2f} "
            f"L={bar.low_price:.2f} C={bar.close_price:.2f} | "
            f"V={bar.volume}"
        )
        
        prev_target = self.target_pos
        
        self.calculate_targets(bar)
        
        self._update_ui_variables()
        
        if self.target_pos != prev_target:
            self._log_signal_change(dt_str, bar, prev_target)
        
        self.set_target_pos(self.target_pos)
        self.put_event()

    def _update_ui_variables(self):
        self.wr_value = self.signal.factor.wr_value
        self.ma_value = self.signal.factor.ma_value

    def _log_signal_change(self, dt_str: str, bar: BarData, prev_target: int):
        if self.target_pos > 0:
            self.trading_signal = "BUY"
            self.write_log(
                f"[{dt_str}] [信号变化] 做多入场 | "
                f"WR={self.wr_value:.1f} < {self.wr_entry} | "
                f"MA={self.ma_value:.2f} | "
                f"价格={bar.close_price:.2f} | "
                f"prev_target={prev_target} -> target_pos={self.target_pos}"
            )
        elif self.target_pos == 0 and prev_target > 0:
            self.trading_signal = "EXIT_LONG"
            self.write_log(
                f"[{dt_str}] [信号变化] 平多出场 | "
                f"WR={self.wr_value:.1f} > {self.wr_exit} | "
                f"价格={bar.close_price:.2f} | "
                f"prev_target={prev_target} -> target_pos={self.target_pos}"
            )
        else:
            self.trading_signal = "HOLD"

    def calculate_targets(self, bar: BarData) -> None:
        self.signal.on_daily_bar(bar)
        self.target_pos = self.signal.get_target()

    def on_trade(self, trade: TradeData) -> None:
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
class WilliamsRMrSimpleSignal:
    """
    Williams %R均值回归简单策略信号处理器
    
    职责:
    - 接收Bar数据（分钟级或日线）
    - 调用 Factor 计算技术指标
    - 根据指标值生成交易信号 (target)
    """
    
    def __init__(
        self,
        vt_symbol: str,
        wr_period: int,
        wr_entry: float,
        wr_exit: float,
        use_ma_filter: bool,
        ma_period: int,
        fixed_size: int,
        auto_daily_end: bool,
        daily_end_hour: int,
        daily_end_minute: int,
    ):
        self.vt_symbol = vt_symbol
        self.wr_period = wr_period
        self.wr_entry = wr_entry
        self.wr_exit = wr_exit
        self.use_ma_filter = use_ma_filter
        self.ma_period = ma_period
        self.fixed_size = fixed_size
        self.auto_daily_end = auto_daily_end
        self.daily_end_hour = daily_end_hour
        self.daily_end_minute = daily_end_minute
        
        self.factor = WilliamsRMrSimpleFactor(
            wr_period=wr_period,
            wr_entry=wr_entry,
            wr_exit=wr_exit,
            use_ma_filter=use_ma_filter,
            ma_period=ma_period,
            fixed_size=fixed_size,
            auto_daily_end=auto_daily_end,
            daily_end_hour=daily_end_hour,
            daily_end_minute=daily_end_minute,
        )
    
    def on_bar(self, bar: BarData) -> None:
        self.factor.on_bar(bar)
    
    def on_daily_bar(self, bar: BarData) -> None:
        self.factor.on_daily_bar(bar)
    
    def get_target(self) -> int:
        return self.factor.get_target()


# =============================================================================
# 因子类 - 封装指标计算
# =============================================================================
class WilliamsRMrSimpleFactor:
    """
    Williams %R均值回归简单策略因子计算器
    
    职责:
    - 管理ArrayManager（K线数据缓存）
    - 计算Williams %R和MA
    - 判断交易信号
    """
    
    def __init__(
        self,
        wr_period: int,
        wr_entry: float,
        wr_exit: float,
        use_ma_filter: bool,
        ma_period: int,
        fixed_size: int,
        auto_daily_end: bool,
        daily_end_hour: int,
        daily_end_minute: int,
    ):
        self.wr_period = wr_period
        self.wr_entry = wr_entry
        self.wr_exit = wr_exit
        self.use_ma_filter = use_ma_filter
        self.ma_period = ma_period
        self.fixed_size = fixed_size
        self.auto_daily_end = auto_daily_end
        self.daily_end_hour = daily_end_hour
        self.daily_end_minute = daily_end_minute
        
        self.am = ArrayManager(size=max(wr_period, ma_period, 50) + 50)
        
        self.daily_bg = SessionDailyBarGenerator(
            on_daily_bar=self.on_daily_bar,
            vt_symbol=getattr(self, 'vt_symbol', ''),
            auto_daily_end=self.auto_daily_end,
            daily_end_hour=self.daily_end_hour,
            daily_end_minute=self.daily_end_minute,
        )
        
        self.target = 0
        self.wr_value = 0.0
        self.ma_value = 0.0
    
    def on_bar(self, bar: BarData) -> None:
        if bar.interval == Interval.DAILY:
            self.on_daily_bar(bar)
        else:
            self.daily_bg.update_bar(bar)
    
    def on_daily_bar(self, bar: BarData) -> None:
        self.am.update_bar(bar)
        
        if not self.am.inited:
            return
        
        close = self.am.close[-1]
        
        # 计算Williams %R
        if len(self.am.high) < self.wr_period + 1:
            return
        highest_high = np.max(self.am.high[-(self.wr_period + 1):-1])
        lowest_low = np.min(self.am.low[-(self.wr_period + 1):-1])
        
        if highest_high == lowest_low:
            self.wr_value = -50.0
        else:
            self.wr_value = (highest_high - close) / (highest_high - lowest_low) * -100
        
        # 计算MA
        self.ma_value = self.am.sma(self.ma_period) if self.use_ma_filter else 0.0
        
        # 持仓状态
        if self.target > 0:
            if self.wr_value > self.wr_exit:
                self.target = 0
            else:
                self.target = self.fixed_size
            return
        
        # 空仓状态
        if self.target == 0:
            entry_condition = self.wr_value < self.wr_entry
            if self.use_ma_filter and self.ma_value is not None:
                entry_condition = entry_condition and close > self.ma_value
            if entry_condition:
                self.target = self.fixed_size
            else:
                self.target = 0
    
    def get_target(self) -> int:
        return self.target


# =============================================================================
# 工具函数
# =============================================================================
def get_product_name(vt_symbol: str) -> str:
    """从合约代码提取品种名称（去除数字部分）"""
    if '.' in vt_symbol:
        symbol = vt_symbol.split('.')[0]
    else:
        symbol = vt_symbol
    product = ''.join([c for c in symbol if not c.isdigit()])
    return product.upper()
