"""
VP-MACD 策略 (量价调整MACD)
=========================================================
基于 arXiv:2604.26063v1 / 微信公众号文章复现

核心逻辑:
  1. 量价加权价格 P* = close × (range/close) × (|body|/range)
     = close × σ × r
     其中 σ = (high-low)/close, r = |close-open|/(high-low)
  2. VP-MACD Line = EMA(fast_period, P*) - EMA(slow_period, P*)
  3. Signal Line = EMA(signal_period, VP-MACD Line)
  4. 入场: VP-MACD Line > λ × Signal Line (金叉变体)
  5. 出场: VP-MACD Line < Signal Line (死叉)

参数:
  - lambda_sensitivity: 灵敏度参数 (0.80~1.00)
  - fast_period: MACD快线 (默认12)
  - slow_period: MACD慢线 (默认26)
  - signal_period: 信号线 (默认9)

作者: strategy_factory 自动生成 + 人工调优
生成时间: 2026-05-08
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Callable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from vnpy.trader.object import BarData, TickData, TradeData, OrderData
from vnpy.trader.constant import Interval
from vnpy_ctastrategy import (
    CtaTemplate,
    BarGenerator,
    ArrayManager,
    StopOrder,
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
# 工具函数
# =============================================================================

def _ema_numpy(values: np.ndarray, period: int) -> np.ndarray:
    """使用numpy计算EMA序列"""
    if len(values) < period:
        return values.copy()
    alpha = 2.0 / (period + 1)
    result = np.zeros_like(values, dtype=float)
    result[0] = values[0]
    for i in range(1, len(values)):
        result[i] = alpha * values[i] + (1.0 - alpha) * result[i - 1]
    return result


# =============================================================================
# 策略主类
# =============================================================================
class VpMacdStrategy(CtaTemplate):
    """
    VP-MACD 量价调整MACD策略

    核心逻辑:
      - 量价加权价格 P* = close × σ × r
      - VP-MACD Line = EMA_fast(P*) - EMA_slow(P*)
      - Signal Line = EMA_signal(VP-MACD Line)
      - 金叉变体入场: VP-MACD Line > λ × Signal Line
      - 死叉出场: VP-MACD Line < Signal Line
    """

    author = "strategy_factory"

    # === 策略参数 ===
    lambda_sensitivity = 0.90    # 灵敏度参数 λ (0.80~1.00)
    fast_period = 12             # MACD快线周期
    slow_period = 26             # MACD慢线周期
    signal_period = 9            # 信号线周期
    volume_ma_period = 20        # 成交量均线周期
    fixed_size = 1               # 固定交易手数

    parameters = [
        "lambda_sensitivity",
        "fast_period",
        "slow_period",
        "signal_period",
        "volume_ma_period",
        "fixed_size",
    ]

    # === 运行变量 ===
    vp_price: float = 0.0
    vp_macd_line: float = 0.0
    signal_line: float = 0.0
    vp_macd_line_prev: float = 0.0
    signal_line_prev: float = 0.0

    variables = [
        "vp_price",
        "vp_macd_line",
        "signal_line",
        "vp_macd_line_prev",
        "signal_line_prev",
    ]

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.bg = BarGenerator(self.on_bar)
        self.dbg = SessionDailyBarGenerator(
            self.on_daily_bar,
            vt_symbol=vt_symbol,
            auto_daily_end=True,
        )
        # ArrayManager用于获取close/high/low/open/volume数组
        self.am = ArrayManager(max(self.slow_period, self.signal_period, self.volume_ma_period) + 50)
        # 缓存vp_price历史用于EMA计算
        self._vp_prices: List[float] = []
        self._vp_macd_lines: List[float] = []
        self._signal_lines: List[float] = []
        self._volumes: List[float] = []

        self.write_log(
            f"[初始化] VpMacdStrategy @ {vt_symbol} | "
            f"λ={self.lambda_sensitivity}, fast={self.fast_period}, slow={self.slow_period}, signal={self.signal_period}"
        )

    def on_init(self):
        self.write_log("[策略初始化] 加载历史数据用于预热指标...")
        self.load_bar(self.am.size)

    def on_start(self):
        self.write_log(f"[策略启动] VpMacdStrategy 已启动")
        self.put_event()

    def on_stop(self):
        self.write_log("[策略停止] 策略已停止")

    def on_tick(self, tick: TickData) -> None:
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData) -> None:
        self.dbg.update_bar(bar)

    def on_daily_bar(self, bar: BarData) -> None:
        """日线Bar推送 —— 策略实际交易周期"""
        self.am.update_bar(bar)
        if not self.am.inited:
            return

        dt_str = bar.datetime.strftime('%Y-%m-%d %H:%M:%S')
        prev_pos = self.pos

        # ========== 1. 计算量价加权价格 P* ==========
        close_arr = self.am.close
        high_arr = self.am.high
        low_arr = self.am.low
        open_arr = self.am.open

        # 日频简化：P* = close × (1 + scale × direction × σ × r × volume_ratio)
        # scale=3.0 确保偏离足够产生EMA交叉信号
        # direction = sign(close - open), σ = (high-low)/close, r = |close-open|/(high-low)
        range_arr = high_arr - low_arr
        sigma = np.where(range_arr > 1e-9, range_arr / close_arr, 0.0)
        body_arr = np.abs(close_arr - open_arr)
        r = np.where(range_arr > 1e-9, body_arr / range_arr, 0.0)
        direction = np.where(close_arr > open_arr, 1.0, np.where(close_arr < open_arr, -1.0, 0.0))

        # 成交量因子：相对20日均量的比率，上限2.0
        volume_arr = self.am.volume
        volume_ratio = 1.0
        if len(volume_arr) >= self.volume_ma_period:
            volume_sma = np.mean(volume_arr[-self.volume_ma_period:])
            if volume_sma > 0:
                volume_ratio = min(float(volume_arr[-1]) / volume_sma, 2.0)

        vp_price_arr = close_arr * (1.0 + 3.0 * direction * sigma * r * volume_ratio)
        vp_price = float(vp_price_arr[-1])
        self._vp_prices.append(vp_price)
        self._volumes.append(float(volume_arr[-1]))

        # 限制缓存长度
        max_cache = self.slow_period + self.signal_period + 20
        if len(self._vp_prices) > max_cache:
            self._vp_prices = self._vp_prices[-max_cache:]
        if len(self._volumes) > max_cache:
            self._volumes = self._volumes[-max_cache:]

        # ========== 2. 计算 VP-MACD Line 和 Signal Line ==========
        vp_macd_line = 0.0
        signal_line = 0.0

        if len(self._vp_prices) >= self.slow_period:
            vp_arr = np.array(self._vp_prices, dtype=float)
            ema_fast = _ema_numpy(vp_arr, self.fast_period)
            ema_slow = _ema_numpy(vp_arr, self.slow_period)
            vp_macd_arr = ema_fast - ema_slow
            vp_macd_line = float(vp_macd_arr[-1])
            self._vp_macd_lines.append(vp_macd_line)

            if len(self._vp_macd_lines) > max_cache:
                self._vp_macd_lines = self._vp_macd_lines[-max_cache:]

            if len(self._vp_macd_lines) >= self.signal_period:
                macd_arr = np.array(self._vp_macd_lines, dtype=float)
                signal_arr = _ema_numpy(macd_arr, self.signal_period)
                signal_line = float(signal_arr[-1])
                self._signal_lines.append(signal_line)

                if len(self._signal_lines) > max_cache:
                    self._signal_lines = self._signal_lines[-max_cache:]

        # 保存上一周期值用于金叉判断
        vp_macd_line_prev = self.vp_macd_line
        signal_line_prev = self.signal_line

        # ========== 3. 信号判断 ==========
        long_signal = False
        short_signal = False

        # 金叉变体入场: VP-MACD Line > λ × Signal Line（且前一日不满足）
        if vp_macd_line != 0.0 and signal_line != 0.0:
            threshold = self.lambda_sensitivity * signal_line
            long_signal = vp_macd_line > threshold and vp_macd_line_prev <= self.lambda_sensitivity * signal_line_prev
            short_signal = vp_macd_line < threshold and vp_macd_line_prev >= self.lambda_sensitivity * signal_line_prev

        # ========== 4. 交易执行 ==========
        # 先撤销所有未成交委托
        self.cancel_all()

        # 出场判断（死叉）
        exit_long = self.pos > 0 and vp_macd_line < signal_line and vp_macd_line_prev >= signal_line_prev
        exit_short = self.pos < 0 and vp_macd_line > signal_line and vp_macd_line_prev <= signal_line_prev

        if exit_long:
            self.sell(bar.close_price, abs(self.pos))
            self.write_log(f"[{dt_str}] [出场] 多单死叉平仓 | VP-MACD={vp_macd_line:.4f}, Signal={signal_line:.4f}")
        elif exit_short:
            self.cover(bar.close_price, abs(self.pos))
            self.write_log(f"[{dt_str}] [出场] 空单死叉平仓 | VP-MACD={vp_macd_line:.4f}, Signal={signal_line:.4f}")

        # 入场判断（金叉变体）
        if long_signal and self.pos <= 0:
            if self.pos < 0:
                self.cover(bar.close_price, abs(self.pos))
            self.buy(bar.close_price, self.fixed_size)
            self.write_log(
                f"[{dt_str}] [信号] 做多 | 价格:{bar.close_price:.2f} "
                f"VP-MACD:{vp_macd_line:.4f} Signal:{signal_line:.4f} λ:{self.lambda_sensitivity}"
            )
        elif short_signal and self.pos >= 0:
            if self.pos > 0:
                self.sell(bar.close_price, self.pos)
            self.short(bar.close_price, self.fixed_size)
            self.write_log(
                f"[{dt_str}] [信号] 做空 | 价格:{bar.close_price:.2f} "
                f"VP-MACD:{vp_macd_line:.4f} Signal:{signal_line:.4f} λ:{self.lambda_sensitivity}"
            )

        # ========== 5. 更新UI变量 ==========
        self.vp_price = vp_price
        self.vp_macd_line_prev = vp_macd_line_prev
        self.signal_line_prev = signal_line_prev
        self.vp_macd_line = vp_macd_line
        self.signal_line = signal_line

        self.put_event()

    def on_trade(self, trade: TradeData):
        self.write_log(
            f"[成交回报] 方向={trade.direction.value}, 成交量={trade.volume}, "
            f"成交均价={trade.price:.2f}, 最新持仓={self.pos}"
        )
        self.put_event()

    def on_order(self, order: OrderData) -> None:
        pass
