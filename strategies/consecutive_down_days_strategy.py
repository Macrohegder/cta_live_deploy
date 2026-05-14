"""
连续下跌反转策略 (Consecutive Down Days Mean Reversion) — 自包含版
=========================================================
基于 QuantifiedStrategies 文章复现:
- 连续N日下跌后，在上升趋势中做多
- RSI(2)超卖确认入场
- RSI超买或趋势反转时出场

策略特点:
- 简单的均值回归逻辑
- 结合趋势过滤（价格在均线之上）
- 适合波动大/熊市中的反弹交易

作者: Raymond Hsiao
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
class ConsecutiveDownDaysStrategy(TargetPosTemplate):
    """
    连续下跌反转策略主类
    
    职责:
    - 接收行情数据 (on_tick/on_bar)
    - 调用 Signal 计算交易目标
    - 通过 TargetPosTemplate 自动 diff 下单
    - 记录运行日志 (write_log)
    - 更新UI变量 (put_event)
    
    交易逻辑:
    - 入场: 连续N日下跌 + 价格在均线之上 + RSI(2)超卖
    - 出场: RSI超买或趋势反转
    """
    
    author = "Raymond Hsiao (refactored)"
    
    # === 策略参数 (可在UI中调整) ===
    consecutive_days = 3          # 连续下跌天数
    trend_period = 200          # 趋势均线周期
    rsi_period = 2              # RSI计算周期
    rsi_overbought = 70         # RSI超买阈值
    fixed_size = 1              # 固定交易手数
    tick_add = 0                # 目标仓位模板的价格偏移
    auto_daily_end = True
    daily_end_minute = 59       # 日线结束分钟
    
    parameters = [
        "consecutive_days",
        "trend_period",
        "rsi_period",
        "rsi_overbought",
        "fixed_size",
        "tick_add",
        "auto_daily_end",
        "daily_end_minute"]
    # ==== 参数分类 ==========
    # 信号参数：影响开平仓交易信号的生成
    signal_parameters = [
        "consecutive_days",
        "trend_period",
        "rsi_period",
        "rsi_overbought",
        "auto_daily_end",
        "daily_end_minute"]
    # 仓位参数：只影响仓位大小和下单执行方式
    position_parameters = [
        "fixed_size",
        "tick_add"]

    
    # === 运行变量 (UI 可见，用于监控) ===
    target_pos: int = 0             # 目标仓位
    rsi_value: float = 0.0      # 当前RSI值
    sma_value: float = 0.0      # 当前均线值
    consecutive_count: int = 0  # 当前连续下跌计数
    trading_signal: str = ""    # 交易信号 (BUY/SELL/HOLD)
    
    variables = [ "rsi_value", "sma_value", "consecutive_count", "trading_signal"]

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict):
        """
        策略初始化
        
        Args:
            cta_engine: CTA引擎实例
            strategy_name: 策略名称
            vt_symbol: 合约代码 (如 "SPY.SMART")
            setting: 参数字典
        """
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        
        # 创建Bar生成器（分钟级→日线）
        self.bg = BarGenerator(self.on_bar)
        
        # 记录品种信息
        key = get_product_name(vt_symbol)
        contract_size = cta_engine.get_size(self)
        self.write_log(f"[初始化] 品种: {key}, 合约乘数: {contract_size}, 参数: {setting}")
        
        # 创建信号处理器（三层架构核心）
        self.signal = ConsecutiveDownDaysSignal(
            vt_symbol=vt_symbol,
            consecutive_days=self.consecutive_days,
            trend_period=self.trend_period,
            rsi_period=self.rsi_period,
            rsi_overbought=self.rsi_overbought,
            fixed_size=self.fixed_size,
            daily_end_minute=self.daily_end_minute,
        )
        self.target_pos = 0
        self.am = self.signal.factor.am
        
        # 内部状态追踪
        self.daily_bar_count: int = 0
        self.last_minute_dt: Optional[datetime] = None
        self.last_daily_dt: Optional[datetime] = None

    def on_init(self):
        profile = resolve_market_profile(
            vt_symbol=self.vt_symbol,
            auto_daily_end=self.auto_daily_end,
            daily_end_hour=getattr(self, 'daily_end_hour', None),
            daily_end_minute=getattr(self, 'daily_end_minute', None),
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
        """策略启动：开始接收行情并交易"""
        self.write_log(f"[策略启动] {self.__class__.__name__} 已启动")
        self.put_event()

    def on_stop(self):
        """策略停止：清理状态并记录"""
        self.write_log(f"[策略停止] 当前持仓: {self.pos}")

    def on_tick(self, tick: TickData) -> None:
        """Tick数据推送：更新Bar生成器"""
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
            self.on_daily_bar(bar)
        else:
            self.last_minute_dt = bar.datetime
            # 将分钟数据传递给信号处理器合成日线
            self.signal.on_bar(bar)

    def on_daily_bar(self, bar: BarData) -> None:
        """
        日线Bar推送（策略实际交易周期）
        
        这是策略的核心交易循环，每根日线到达时执行:
        1. 更新技术指标并计算目标仓位
        2. 根据目标仓位发送交易指令
        3. 刷新UI事件
        """
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
        
        # 执行交易
        self.set_target_pos(self.target_pos)
        self.put_event()

    def _update_ui_variables(self):
        """更新UI监控变量（从 Signal/Factor 获取）"""
        factor = self.signal.factor
        self.rsi_value = factor.rsi_value
        self.sma_value = factor.sma_value
        self.consecutive_count = factor.consecutive_count

    def _log_signal_change(self, dt_str: str, bar: BarData, prev_target: int):
        """
        记录信号变化日志
        
        实盘运维关键日志，用于:
        - 追踪策略决策过程
        - 排查异常交易
        - 验证策略逻辑是否正确执行
        """
        factor = self.signal.factor
        
        if self.target_pos > 0:
            self.trading_signal = "BUY"
            self.write_log(
                f"[{dt_str}] [信号变化] 做多入场 | "
                f"连续下跌={factor.consecutive_count}天 | "
                f"RSI({self.rsi_period})={factor.rsi_value:.2f} | "
                f"SMA({self.trend_period})={factor.sma_value:.2f} | "
                f"价格={bar.close_price:.2f} | "
                f"prev_target={prev_target} → target_pos={self.target_pos}"
            )
        elif self.target_pos == 0 and prev_target > 0:
            self.trading_signal = "EXIT_LONG"
            self.write_log(
                f"[{dt_str}] [信号变化] 平多出场 | "
                f"RSI({self.rsi_period})={factor.rsi_value:.2f} > {self.rsi_overbought} | "
                f"价格={bar.close_price:.2f} | "
                f"prev_target={prev_target} → target_pos={self.target_pos}"
            )
        else:
            self.trading_signal = "HOLD"

    def calculate_targets(self, bar: BarData) -> None:
        """
        计算目标仓位
        
        委托给 Signal 处理，实现策略逻辑与交易执行的分离。
        Signal 内部会调用 Factor 计算指标，然后根据指标值判断交易信号。
        """
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
        """委托回调（当前策略不处理）"""
        super().on_order(order)


# =============================================================================
# 信号类 - 封装交易逻辑
# =============================================================================
class ConsecutiveDownDaysSignal:
    """
    连续下跌反转信号处理器
    
    职责:
    - 接收Bar数据（分钟级或日线）
    - 调用 Factor 计算技术指标
    - 根据指标值生成交易信号 (target)
    - 管理持仓状态
    
    信号逻辑:
    - 入场: 连续N日下跌 + 价格在均线之上
    - 出场: RSI超买
    """
    
    def __init__(
        self,
        vt_symbol: str,
        consecutive_days: int,
        trend_period: int,
        rsi_period: int,
        rsi_overbought: float,
        fixed_size: int,
        daily_end_minute: int,
    ) -> None:
        """
        初始化信号处理器
        
        Args:
            vt_symbol: 合约代码
            consecutive_days: 连续下跌天数阈值
            trend_period: 趋势均线周期
            rsi_period: RSI计算周期
            rsi_overbought: RSI超买阈值
            fixed_size: 固定交易手数
            daily_end_minute: 日线结束分钟
        """
        self.vt_symbol = vt_symbol
        self.consecutive_days = consecutive_days
        self.trend_period = trend_period
        self.rsi_period = rsi_period
        self.rsi_overbought = rsi_overbought
        self.fixed_size = fixed_size
        self.daily_end_minute = daily_end_minute
        
        self.target = 0
        
        # 创建因子计算器（核心指标计算）
        self.factor = ConsecutiveDownDaysFactor(
            vt_symbol=vt_symbol,
            consecutive_days=consecutive_days,
            trend_period=trend_period,
            rsi_period=rsi_period,
            rsi_overbought=rsi_overbought,
            fixed_size=fixed_size,
            daily_end_minute=daily_end_minute,
        )

    def on_bar(self, bar: BarData) -> None:
        """
        处理分钟Bar数据
        
        将分钟数据传递给 Factor 的日线生成器合成日线
        """
        self.factor.on_bar(bar)
        self.target = self.factor.get_target()

    def on_daily_bar(self, bar: BarData) -> None:
        """
        处理日线Bar数据
        
        直接传递给 Factor 处理
        """
        self.factor.on_daily_bar(bar)
        self.target = self.factor.get_target()

    def get_target(self) -> int:
        """获取当前目标仓位"""
        return int(self.target)


# =============================================================================
# 因子类 - 封装指标计算
# =============================================================================
class ConsecutiveDownDaysFactor:
    """
    连续下跌反转因子计算器
    
    职责:
    - 管理ArrayManager（K线数据缓存）
    - 计算技术指标 (SMA, RSI)
    - 判断交易信号
    - 管理持仓状态变量
    
    指标说明:
    - SMA(trend_period): 简单移动平均线，用于趋势过滤
    - RSI(rsi_period): 相对强弱指标，用于入场/出场确认
    - 连续下跌: 连续N日收盘价低于前一日
    """
    
    def __init__(
        self,
        vt_symbol: str,
        consecutive_days: int,
        trend_period: int,
        rsi_period: int,
        rsi_overbought: float,
        fixed_size: int,
        daily_end_minute: int,
    ) -> None:
        """
        初始化因子计算器
        
        Args:
            consecutive_days: 连续下跌天数阈值
            trend_period: 趋势均线周期
            rsi_period: RSI计算周期
            rsi_overbought: RSI超买阈值
            fixed_size: 固定交易手数
            daily_end_minute: 日线结束分钟
        """
        self.vt_symbol = vt_symbol
        self.consecutive_days = consecutive_days
        self.trend_period = trend_period
        self.rsi_period = rsi_period
        self.rsi_overbought = rsi_overbought
        self.fixed_size = fixed_size
        self.daily_end_minute = daily_end_minute
        
        # 状态变量
        self.target = 0
        self.rsi_value: float = 0.0      # 当前RSI值
        self.sma_value: float = 0.0      # 当前均线值
        self.consecutive_count: int = 0  # 当前连续下跌计数
        
        # 创建K线数据管理器
        # 大小取最大指标周期 + 缓冲
        size = max(trend_period, rsi_period, consecutive_days) + 50
        self.am = ArrayManager(size=size)
        
        # 创建日线Bar生成器（用于分钟级数据合成日线）
        self.daily_bg = SessionDailyBarGenerator(
            on_daily_bar=self.on_daily_bar,
            vt_symbol=self.vt_symbol,
            auto_daily_end=True,
            daily_end_minute=self.daily_end_minute,
        )

    def on_bar(self, bar: BarData) -> None:
        """
        处理Bar数据
        
        如果是日线数据直接处理，
        如果是分钟数据则合成日线后处理
        """
        if bar.interval == Interval.DAILY:
            self.on_daily_bar(bar)
        else:
            self.daily_bg.update_bar(bar)

    def on_daily_bar(self, bar: BarData) -> None:
        """
        处理日线数据（策略实际交易周期）
        
        这是策略的核心计算逻辑，包括:
        1. 更新K线数据到ArrayManager
        2. 计算技术指标 (SMA, RSI)
        3. 判断连续下跌
        4. 根据持仓状态和指标值判断交易信号
        5. 更新持仓状态
        """
        # 更新K线数据
        self.am.update_bar(bar)
        
        # 数据未预热完成，不计算信号
        if not self.am.inited:
            return
        
        # === 指标计算 ===
        close = self.am.close
        
        # 1. 计算趋势均线
        sma = self.am.sma(self.trend_period, array=True)
        self.sma_value = sma[-1]
        
        # 2. 计算RSI
        rsi = self.am.rsi(self.rsi_period, array=True)
        self.rsi_value = rsi[-1]
        
        # 3. 判断连续下跌
        self.consecutive_count = 0
        for i in range(1, self.consecutive_days + 1):
            if close[-i] < close[-i - 1]:
                self.consecutive_count += 1
            else:
                break
        
        # === 交易信号判断 ===
        if self.target > 0:
            # === 持仓状态：检查出场条件 ===
            # 出场条件1: RSI超买
            cond_exit_1 = rsi[-self.rsi_period] > self.rsi_overbought
            
            if cond_exit_1:
                self.target = 0
            else:
                # 继续持有
                self.target = self.target
                
        elif self.target == 0:
            # === 空仓状态：检查入场条件 ===
            # 入场条件1: 连续N日下跌
            cond_1 = all(
                close[-i] < close[-i - 1]
                for i in range(1, self.consecutive_days + 1)
            )
            
            # 入场条件2: 价格在均线之上（趋势过滤）
            cond_2 = close[-1] > sma[-1]
            
            if cond_1 and cond_2:
                self.target = self.fixed_size
            else:
                self.target = 0

    def get_target(self) -> int:
        """获取当前目标仓位"""
        return self.target


# =============================================================================
# 工具函数
# =============================================================================
def get_product_name(vt_symbol: str) -> str:
    """
    从合约代码提取品种名称（去除数字部分）
    
    例如: "IF2406.CFFEX" → "IF"
          "SPY.SMART" → "SPY"
    """
    symbol = "".join(w for w in vt_symbol.split(".")[0] if not w.isdigit())
    return symbol.upper()
