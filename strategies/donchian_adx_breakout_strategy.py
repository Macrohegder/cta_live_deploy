"""
Donchian Channel + ADX Breakout Strategy (唐奇安通道 + ADX 突破策略)
=========================================================
复现 QuantifiedStrategies 比特币趋势跟踪策略:
- 买入: 价格突破过去 N 日高点，且 ADX < 阈值（低波动埋伏）
- 卖出: 价格跌破过去 N 日低点
- 只做多（顺应加密货币长期向上偏置）

核心洞察: 加密货币常在低波动盘整后暴力突破，ADX 过滤器帮助在平静期埋伏。

作者: Raymond Hsiao
来源: strategy_factory 自动生成 + 人工重构为 Signal/Factor 架构
重构时间: 2025-04

架构说明:
    Strategy (TargetPosTemplate) → Signal (交易逻辑) → Factor (指标计算)
    
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


# =============================================================================
# 策略主类 - 负责交易执行与UI交互 (TargetPosTemplate)
# =============================================================================
class DonchianAdxBreakoutStrategy(TargetPosTemplate):
    """
    唐奇安通道 + ADX 突破策略主类
    
    职责:
    - 接收行情数据 (on_tick/on_bar)
    - 调用 Signal 计算交易目标
    - 通过 TargetPosTemplate 自动 diff 下单
    - 记录运行日志 (write_log)
    - 更新UI变量 (put_event)
    
    交易逻辑:
    - 入场: 价格突破唐奇安上轨 + ADX条件满足 + (可选)趋势过滤
    - 出场: 价格跌破唐奇安下轨
    """
    
    author = "Raymond Hsiao (refactored)"

    # === 策略参数 ===
    donchian_window = 15      # Donchian 通道周期
    exit_window = 0           # 出场窗口(0表示同donchian_window)
    adx_window = 14           # ADX 周期
    adx_threshold = 25        # ADX 阈值
    adx_mode = 1              # 1=ADX<阈值(平静突破), 2=ADX>阈值(趋势突破), 3=忽略ADX
    use_long_only = True      # 只做多
    use_trend_filter = False  # 是否使用 200 MA 趋势过滤
    ma_period = 200           # 趋势过滤 MA 周期
    fixed_size = 1            # 固定交易手数
    daily_end_minute = 59

    parameters = [
        "donchian_window",
        "exit_window",
        "adx_window",
        "adx_threshold",
        "adx_mode",
        "use_long_only",
        "use_trend_filter",
        "ma_period",
        "fixed_size",
        "daily_end_minute"]
    # ==== 参数分类 ==========
    # 信号参数：影响开平仓交易信号的生成
    signal_parameters = [
        "donchian_window",
        "exit_window",
        "adx_window",
        "adx_threshold",
        "adx_mode",
        "use_long_only",
        "use_trend_filter",
        "ma_period",
        "daily_end_minute"]
    # 仓位参数：只影响仓位大小和下单执行方式
    position_parameters = [
        "fixed_size"]


    # === 运行变量 ===
    target_pos: int = 0
    dc_upper: float = 0.0     # 当前 Donchian 上轨
    dc_lower: float = 0.0     # 当前 Donchian 下轨
    adx_value: float = 0.0    # 当前 ADX 值
    ma_value: float = 0.0     # 当前 MA 值
    entry_count: int = 0      # 入场条件满足计数

    variables = [ "dc_upper", "dc_lower", "adx_value", "ma_value", "entry_count", "trading_signal"
    ]

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.bg = BarGenerator(self.on_bar)

        # Clamp window parameters to avoid TA-Lib Bad Parameter errors
        self.donchian_window = max(2, self.donchian_window)
        self.exit_window = max(2, self.exit_window) if self.exit_window > 0 else self.donchian_window
        self.adx_window = max(2, self.adx_window)
        self.ma_period = max(2, self.ma_period)

        key = get_product_name(vt_symbol)
        contract_size = cta_engine.get_size(self)
        self.write_log(
            f"[初始化] 品种: {key}, 合约乘数: {contract_size}, "
            f"参数: dc={self.donchian_window}, exit={self.exit_window}, adx={self.adx_window}, threshold={self.adx_threshold}"
        )

        self.signal = DonchianAdxSignal(
            vt_symbol=vt_symbol,
            donchian_window=self.donchian_window,
            exit_window=self.exit_window,
            adx_window=self.adx_window,
            adx_threshold=self.adx_threshold,
            adx_mode=self.adx_mode,
            use_long_only=self.use_long_only,
            use_trend_filter=self.use_trend_filter,
            ma_period=self.ma_period,
            fixed_size=self.fixed_size,
            daily_end_minute=self.daily_end_minute,
        )
        self.target_pos = 0
        self.am = self.signal.factor.am
        
        # 内部状态追踪
        self.daily_bar_count: int = 0
        self.last_minute_dt = None
        self.last_daily_dt = None

    def on_init(self):
        """
        策略初始化：加载历史数据预热指标
        
        加载足够K线用于预热唐奇安通道、ADX和均线指标
        """
        profile = resolve_market_profile(
            vt_symbol=self.vt_symbol,
            auto_daily_end=getattr(self, 'auto_daily_end', True),
            daily_end_hour=getattr(self, 'daily_end_hour', None),
            daily_end_minute=getattr(self, 'daily_end_minute', None),
        )
        end_time = time(profile.daily_end_hour, profile.daily_end_minute)
        self.write_log(
            f"[策略初始化] 请求历史数据用于合成日线并预热技术指标："
            f"auto_daily_end={getattr(self, 'auto_daily_end', True)} end_time={end_time} am_size={self.am.size}"
        )
        base = warmup_request(self.am.size, aggregation_window=1, extra_bars=30)
        ok, attempts = ensure_am_inited(self, self.am, base, prefer_daily=True)
        last = attempts[-1] if attempts else (0, "", 0, False)
        self.write_log(
            f"[策略初始化] 历史数据加载完成 | ok={ok} "
            f"| last_request={last[0]} mode={last[1]} am_count={last[2]} am_inited={last[3]}"
        )

    def on_start(self):
        """策略启动：开始接收行情并交易"""
        self.write_log(f"[on_start] 策略启动 | {self.__class__.__name__} 已启动")
        self.put_event()

    def on_stop(self):
        """策略停止：清理状态并记录"""
        self.write_log(f"[on_stop] 策略停止 | 当前持仓: {self.pos}")

    def on_tick(self, tick: TickData) -> None:
        super().on_tick(tick)
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData) -> None:
        """
        Bar数据推送（策略主循环）
        
        执行流程:
        1. 如果是分钟数据，更新日线生成器
        2. 如果是日线数据，触发交易逻辑
        3. 更新UI监控变量
        4. 记录日志
        5. 刷新UI事件
        """
        super().on_bar(bar)
        
        if bar.interval == Interval.DAILY:
            self.daily_bar_count += 1
            self.last_daily_dt = bar.datetime
            
            dt_str = bar.datetime.strftime('%Y-%m-%d')
            
            # 记录日线到达日志（实盘运维关键信息）
            self.write_log(
                f"[日线到达] #{self.daily_bar_count} | "
                f"日期={dt_str} | "
                f"O={bar.open_price:.2f} H={bar.high_price:.2f} "
                f"L={bar.low_price:.2f} C={bar.close_price:.2f} | "
                f"V={bar.volume}"
            )
            
            # 保存上一周期目标仓位用于对比
            prev_target = self.target_pos
            
            # 计算目标仓位（委托给 Signal 处理）
            self.calculate_targets(bar)
            
            # 从 Signal/Factor 更新UI监控变量
            self._update_ui_variables()
            
            # 检查信号变化并记录详细日志
            if self.target_pos != prev_target:
                self._log_signal_change(dt_str, bar, prev_target)
            
            self.set_target_pos(self.target_pos)
            self.put_event()
        else:
            self.signal.on_bar(bar)

    def _update_ui_variables(self):
        """更新UI监控变量（从 Signal/Factor 获取）"""
        self.dc_upper = self.signal.factor.dc_upper
        self.dc_lower = self.signal.factor.dc_lower
        self.adx_value = self.signal.factor.adx_value
        self.ma_value = self.signal.factor.ma_value
        self.entry_count = self.signal.factor.entry_count

    def _log_signal_change(self, dt_str: str, bar: BarData, prev_target: int):
        """
        记录信号变化日志
        
        实盘运维关键日志，用于:
        - 追踪策略决策过程
        - 排查异常交易
        - 验证策略逻辑是否正确执行
        """
        if self.target_pos > 0:
            self.trading_signal = "BUY"
            self.write_log(
                f"[{dt_str}] [信号变化] 做多入场 | "
                f"唐奇安上轨={self.dc_upper:.2f} | "
                f"ADX({self.adx_window})={self.adx_value:.1f} | "
                f"MA({self.ma_period})={self.ma_value:.2f} | "
                f"价格={bar.close_price:.2f} | "
                f"prev_target={prev_target} → target_pos={self.target_pos}"
            )
        elif self.target_pos == 0 and prev_target > 0:
            self.trading_signal = "EXIT_LONG"
            self.write_log(
                f"[{dt_str}] [信号变化] 平多出场 | "
                f"唐奇安下轨={self.dc_lower:.2f} | "
                f"价格={bar.close_price:.2f} | "
                f"prev_target={prev_target} → target_pos={self.target_pos}"
            )
        else:
            self.trading_signal = "HOLD"

    def calculate_targets(self, bar: BarData) -> None:
        self.signal.on_bar(bar)
        self.target_pos = self.signal.get_target()

    def on_trade(self, trade: TradeData) -> None:
        """
        成交回调
        
        实盘运维关键日志，记录每笔成交详情:
        - 成交方向（多/空）
        - 开平类型（开/平）
        - 成交价格和数量
        - 最新持仓
        """
        # 碎单处理（防止浮点精度问题）
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
# 因子类 - 封装指标计算
# =============================================================================
class DonchianAdxFactor:
    """
    唐奇安通道 + ADX 因子计算器
    
    职责:
    - 管理ArrayManager（K线数据缓存）
    - 计算技术指标 (Donchian通道, ADX, MA)
    - 判断交易信号
    
    指标说明:
    - Donchian通道: 过去N日的高低点通道
    - ADX: 平均趋向指数，衡量趋势强度
    - MA: 简单移动平均线，用于趋势过滤
    """

    def __init__(
        self,
        vt_symbol: str,
        donchian_window: int,
        exit_window: int,
        adx_window: int,
        adx_threshold: float,
        adx_mode: int,
        use_long_only: bool,
        use_trend_filter: bool,
        ma_period: int,
        fixed_size: int,
        daily_end_minute: int,
    ):
        self.vt_symbol = vt_symbol
        self.donchian_window = max(2, donchian_window)
        self.exit_window = max(2, exit_window) if exit_window > 0 else self.donchian_window
        self.adx_window = adx_window
        self.adx_threshold = adx_threshold
        self.adx_mode = adx_mode
        self.use_long_only = use_long_only
        self.use_trend_filter = use_trend_filter
        self.ma_period = ma_period
        self.fixed_size = fixed_size
        self.daily_end_minute = daily_end_minute

        max_window = max(donchian_window, self.exit_window, adx_window, ma_period)
        self.am = ArrayManager(max_window + 20)
        self.daily_bg = SessionDailyBarGenerator(
            on_daily_bar=self.on_daily_bar,
            vt_symbol=self.vt_symbol,
            auto_daily_end=True,
            daily_end_hour=14,
            daily_end_minute=self.daily_end_minute,
        )

        self.dc_upper = 0.0
        self.dc_lower = 0.0
        self.exit_dc_lower = 0.0
        self.adx_value = 0.0
        self.ma_value = 0.0
        self.entry_count = 0
        self.days_in_trade = 0

    def on_bar(self, bar: BarData) -> None:
        if bar.interval == Interval.DAILY:
            self.on_daily_bar(bar)
        else:
            self.daily_bg.update_bar(bar)

    def on_daily_bar(self, bar: BarData) -> None:
        self.am.update_bar(bar)
        if not self.am.inited:
            return

        # 使用前一日的 Donchian 通道（排除今日，这样 close 才可能突破）
        if len(self.am.close) >= self.donchian_window + 1:
            up_arr, down_arr = self.am.donchian(self.donchian_window, array=True)
            self.dc_upper = up_arr[-2]
            self.dc_lower = down_arr[-2]
        else:
            self.dc_upper, self.dc_lower = self.am.donchian(self.donchian_window)
        
        # 出场使用独立的 exit_window
        if self.exit_window == self.donchian_window:
            self.exit_dc_lower = self.dc_lower
        else:
            if len(self.am.close) >= self.exit_window + 1:
                _, exit_down = self.am.donchian(self.exit_window, array=True)
                self.exit_dc_lower = exit_down[-2]
            else:
                _, self.exit_dc_lower = self.am.donchian(self.exit_window)
        
        self.adx_value = self.am.adx(self.adx_window)
        self.ma_value = self.am.sma(self.ma_period, array=False) if self.use_trend_filter else 0.0

        # 统计入场条件满足数
        count = 0
        if bar.close_price > self.dc_upper:
            count += 1
        if self.adx_mode == 1 and self.adx_value < self.adx_threshold:
            count += 1
        elif self.adx_mode == 2 and self.adx_value > self.adx_threshold:
            count += 1
        elif self.adx_mode == 3:
            count += 1
        if not self.use_trend_filter or bar.close_price > self.ma_value:
            count += 1
        self.entry_count = count

        if self.days_in_trade > 0:
            self.days_in_trade += 1

    def get_target(self) -> int:
        if not self.am.inited:
            return 0

        # 入场: 突破上轨 + ADX 低于阈值 + (可选)趋势过滤
        # 注意: 使用 >= 因为 close 不可能 > 今日的高点
        adx_ok = False
        if self.adx_mode == 1:
            adx_ok = self.adx_value < self.adx_threshold
        elif self.adx_mode == 2:
            adx_ok = self.adx_value > self.adx_threshold
        elif self.adx_mode == 3:
            adx_ok = True

        long_signal = (
            self.am.close[-1] >= self.dc_upper
            and adx_ok
            and (not self.use_trend_filter or self.am.close[-1] > self.ma_value)
        )

        # 出场: 跌破 exit_window 下轨（更灵敏的出场）
        exit_signal = self.am.close[-1] <= self.exit_dc_lower

        if long_signal:
            self.days_in_trade = 1
            return self.fixed_size

        if self.days_in_trade > 0:
            if exit_signal:
                self.days_in_trade = 0
                return 0
            # 持续持仓
            return self.fixed_size

        if not self.use_long_only:
            short_signal = (
                self.am.close[-1] < self.dc_lower
                and adx_ok
                and (not self.use_trend_filter or self.am.close[-1] < self.ma_value)
            )
            if short_signal:
                return -self.fixed_size

        return 0


# =============================================================================
# 信号类 - 封装交易逻辑
# =============================================================================
class DonchianAdxSignal:
    """
    唐奇安通道 + ADX 信号处理器
    
    职责:
    - 接收Bar数据（分钟级或日线）
    - 调用 Factor 计算技术指标
    - 根据指标值生成交易信号 (target)
    
    信号逻辑:
    - 入场: 价格突破唐奇安上轨 + ADX条件 + 趋势过滤
    - 出场: 价格跌破唐奇安下轨
    """
    
    def __init__(
        self,
        vt_symbol: str,
        donchian_window: int,
        exit_window: int,
        adx_window: int,
        adx_threshold: float,
        adx_mode: int,
        use_long_only: bool,
        use_trend_filter: bool,
        ma_period: int,
        fixed_size: int,
        daily_end_minute: int,
    ):
        self.factor = DonchianAdxFactor(
            vt_symbol=vt_symbol,
            donchian_window=donchian_window,
            exit_window=exit_window,
            adx_window=adx_window,
            adx_threshold=adx_threshold,
            adx_mode=adx_mode,
            use_long_only=use_long_only,
            use_trend_filter=use_trend_filter,
            ma_period=ma_period,
            fixed_size=fixed_size,
            daily_end_minute=daily_end_minute,
        )

    def on_bar(self, bar: BarData) -> None:
        self.factor.on_bar(bar)

    def get_target(self) -> int:
        return self.factor.get_target()


def get_product_name(vt_symbol: str) -> str:
    """从 vt_symbol 中提取品种名"""
    return vt_symbol.split(".")[0]
