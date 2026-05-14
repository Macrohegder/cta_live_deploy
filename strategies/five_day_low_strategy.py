"""
5日低点均值回归策略 (5-Day Low Mean Reversion Strategy)
=========================================================
基于 QuantifiedStrategies 文章复现:
- 当价格触及 N 日低点时买入 (均值回归)
- 添加简单价格过滤器与持有期管理
- 增强版: 加入 ATR 止损/止盈、波动率过滤

策略特点:
- 买入短期弱势 (5日低点)
- betting on a snapback
- 适合波动较大的市场 (如加密货币)

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
from typing import Callable, Optional, Tuple, List
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
class FiveDayLowStrategy(TargetPosTemplate):
    """
    5日低点均值回归策略主类
    
    职责:
    - 接收行情数据 (on_tick/on_bar)
    - 调用 Signal 计算交易目标
    - 通过 TargetPosTemplate 自动 diff 下单
    - 记录运行日志 (write_log)
    - 更新UI变量 (put_event)
    
    交易逻辑:
    - 入场: 价格创N日低点 + (可选)下跌日过滤
    - 出场: 持有期满或MA出场或ATR止损/止盈
    """
    
    author = "Raymond Hsiao (refactored)"

    # === 策略参数 (可在UI中调整) ===
    lookback_days = 5
    max_holding_days = 5
    use_ma_exit = True
    ma_window = 5
    require_down_day = True
    use_stop_loss = True
    sl_atr_multiplier = 3.0
    atr_window = 14
    use_take_profit = False
    tp_atr_multiplier = 5.0
    fixed_size = 1
    daily_end_minute = 59

    parameters = [
        "lookback_days",
        "max_holding_days",
        "use_ma_exit",
        "ma_window",
        "require_down_day",
        "use_stop_loss",
        "sl_atr_multiplier",
        "atr_window",
        "use_take_profit",
        "tp_atr_multiplier",
        "fixed_size",
        "daily_end_minute"]
    # ==== 参数分类 ==========
    # 信号参数：影响开平仓交易信号的生成
    signal_parameters = [
        "lookback_days",
        "max_holding_days",
        "use_ma_exit",
        "ma_window",
        "require_down_day",
        "use_stop_loss",
        "sl_atr_multiplier",
        "atr_window",
        "use_take_profit",
        "tp_atr_multiplier",
        "daily_end_minute"]
    # 仓位参数：只影响仓位大小和下单执行方式
    position_parameters = [
        "fixed_size"]


    # === 运行变量 (UI 可见，用于监控) ===
    target_pos: int = 0
    days_low: float = 0.0
    days_to_exit: int = 0
    ma_value: float = 0.0
    is_5day_low: bool = False
    atr_value: float = 0.0
    current_stop: float = 0.0
    current_tp: float = 0.0
    days_in_trade: int = 0
    trading_signal: str = ""

    variables = [ "days_low", "days_to_exit", "ma_value", 
        "is_5day_low", "atr_value", "current_stop", 
        "current_tp", "days_in_trade", "trading_signal"
    ]

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict):
        """
        策略初始化
        
        Args:
            cta_engine: CTA引擎实例
            strategy_name: 策略名称
            vt_symbol: 合约代码
            setting: 参数字典
        """
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        
        # 创建Bar生成器
        self.bg = BarGenerator(self.on_bar)
        
        # 记录品种信息
        key = get_product_name(vt_symbol)
        contract_size = cta_engine.get_size(self) if hasattr(cta_engine, 'get_size') else 1
        self.write_log(
            f"[初始化] 5日低点策略 | 品种: {key}, 合约乘数: {contract_size}, 参数: {setting}"
        )
        
        # 创建信号处理器
        self.signal = FiveDayLowSignal(
            vt_symbol=vt_symbol,
            lookback_days=self.lookback_days,
            max_holding_days=self.max_holding_days,
            use_ma_exit=self.use_ma_exit,
            ma_window=self.ma_window,
            require_down_day=self.require_down_day,
            use_stop_loss=self.use_stop_loss,
            sl_atr_multiplier=self.sl_atr_multiplier,
            atr_window=self.atr_window,
            use_take_profit=self.use_take_profit,
            tp_atr_multiplier=self.tp_atr_multiplier,
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
        self.days_low = self.signal.factor.days_low
        self.days_to_exit = self.signal.factor.days_to_exit
        self.ma_value = self.signal.factor.ma_value
        self.is_5day_low = self.signal.factor.is_5day_low
        self.atr_value = self.signal.factor.atr_value
        self.current_stop = self.signal.factor.current_stop
        self.current_tp = self.signal.factor.current_tp
        self.days_in_trade = self.signal.factor.days_in_trade

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
                f"{self.lookback_days}日低点={self.days_low:.2f} | "
                f"MA({self.ma_window})={self.ma_value:.2f} | "
                f"ATR={self.atr_value:.2f} | "
                f"价格={bar.close_price:.2f} | "
                f"prev_target={prev_target} → target_pos={self.target_pos}"
            )
        elif self.target_pos == 0 and prev_target > 0:
            self.trading_signal = "EXIT_LONG"
            exit_reason = ""
            if self.days_in_trade >= self.max_holding_days:
                exit_reason = "持有期满"
            elif self.use_ma_exit and bar.close_price > self.ma_value:
                exit_reason = "MA出场"
            elif self.use_stop_loss and bar.close_price < self.current_stop:
                exit_reason = "止损"
            elif self.use_take_profit and bar.close_price > self.current_tp:
                exit_reason = "止盈"
            else:
                exit_reason = "条件触发"
            self.write_log(
                f"[{dt_str}] [信号变化] 平多出场 | "
                f"原因={exit_reason} | "
                f"持仓天数={self.days_in_trade} | "
                f"价格={bar.close_price:.2f} | "
                f"prev_target={prev_target} → target_pos={self.target_pos}"
            )
        else:
            self.trading_signal = "HOLD"

    def calculate_targets(self, bar: BarData) -> None:
        """
        计算目标仓位
        
        委托给 Signal 处理，实现策略逻辑与交易执行的分离
        """
        self.signal.on_bar(bar)
        self.target_pos = self.signal.get_target()

    def on_trade(self, trade: TradeData) -> None:
        """
        成交回调
        
        实盘运维关键日志，记录每笔成交详情
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
class FiveDayLowSignal:
    """
    5日低点均值回归信号处理器
    
    职责:
    - 接收Bar数据（分钟级或日线）
    - 调用 Factor 计算技术指标
    - 根据指标值生成交易信号 (target)
    
    信号逻辑:
    - 入场: 价格创N日低点 + (可选)下跌日过滤
    - 出场: 持有期满或MA出场或ATR止损/止盈
    """
    
    def __init__(
        self,
        vt_symbol: str,
        lookback_days: int,
        max_holding_days: int,
        use_ma_exit: bool,
        ma_window: int,
        require_down_day: bool,
        use_stop_loss: bool,
        sl_atr_multiplier: float,
        atr_window: int,
        use_take_profit: bool,
        tp_atr_multiplier: float,
        fixed_size: int,
        daily_end_minute: int,
    ):
        """
        初始化信号处理器
        
        Args:
            vt_symbol: 合约代码
            lookback_days: 回看天数
            max_holding_days: 最大持有天数
            use_ma_exit: 是否使用MA出场
            ma_window: MA周期
            require_down_day: 是否要求下跌日
            use_stop_loss: 是否使用止损
            sl_atr_multiplier: 止损ATR倍数
            atr_window: ATR周期
            use_take_profit: 是否使用止盈
            tp_atr_multiplier: 止盈ATR倍数
            fixed_size: 固定交易手数
            daily_end_minute: 日线结束分钟
        """
        self.vt_symbol = vt_symbol
        self.lookback_days = lookback_days
        self.max_holding_days = max_holding_days
        self.use_ma_exit = use_ma_exit
        self.ma_window = ma_window
        self.require_down_day = require_down_day
        self.use_stop_loss = use_stop_loss
        self.sl_atr_multiplier = sl_atr_multiplier
        self.atr_window = atr_window
        self.use_take_profit = use_take_profit
        self.tp_atr_multiplier = tp_atr_multiplier
        self.fixed_size = fixed_size
        self.daily_end_minute = daily_end_minute
        
        # 创建因子计算器
        self.factor = FiveDayLowFactor(
            lookback_days=lookback_days,
            max_holding_days=max_holding_days,
            use_ma_exit=use_ma_exit,
            ma_window=ma_window,
            require_down_day=require_down_day,
            use_stop_loss=use_stop_loss,
            sl_atr_multiplier=sl_atr_multiplier,
            atr_window=atr_window,
            use_take_profit=use_take_profit,
            tp_atr_multiplier=tp_atr_multiplier,
            fixed_size=fixed_size,
            daily_end_minute=daily_end_minute,
        )
    
    def on_bar(self, bar: BarData) -> None:
        """
        处理Bar数据
        
        Args:
            bar: BarData 日线数据
        """
        self.factor.on_bar(bar)
    
    def get_target(self) -> int:
        """获取当前目标仓位"""
        return self.factor.get_target()


# =============================================================================
# 因子类 - 封装指标计算
# =============================================================================
class FiveDayLowFactor:
    """
    5日低点均值回归因子计算器
    
    职责:
    - 管理ArrayManager（K线数据缓存）
    - 计算技术指标 (N日低点, MA, ATR)
    - 判断交易信号
    
    指标说明:
    - N日低点: 过去N个交易日的最低价
    - MA: 简单移动平均线，用于出场判断
    - ATR: 平均真实波幅，用于止损/止盈
    """
    
    def __init__(
        self,
        lookback_days: int,
        max_holding_days: int,
        use_ma_exit: bool,
        ma_window: int,
        require_down_day: bool,
        use_stop_loss: bool,
        sl_atr_multiplier: float,
        atr_window: int,
        use_take_profit: bool,
        tp_atr_multiplier: float,
        fixed_size: int,
        daily_end_minute: int,
    ):
        """
        初始化因子计算器
        
        Args:
            lookback_days: 回看天数
            max_holding_days: 最大持有天数
            use_ma_exit: 是否使用MA出场
            ma_window: MA周期
            require_down_day: 是否要求下跌日
            use_stop_loss: 是否使用止损
            sl_atr_multiplier: 止损ATR倍数
            atr_window: ATR周期
            use_take_profit: 是否使用止盈
            tp_atr_multiplier: 止盈ATR倍数
            fixed_size: 固定交易手数
            daily_end_minute: 日线结束分钟
        """
        self.lookback_days = lookback_days
        self.max_holding_days = max_holding_days
        self.use_ma_exit = use_ma_exit
        self.ma_window = ma_window
        self.require_down_day = require_down_day
        self.use_stop_loss = use_stop_loss
        self.sl_atr_multiplier = sl_atr_multiplier
        self.atr_window = atr_window
        self.use_take_profit = use_take_profit
        self.tp_atr_multiplier = tp_atr_multiplier
        self.fixed_size = fixed_size
        self.daily_end_minute = daily_end_minute
        
        # 创建K线数据管理器
        max_window = max(lookback_days, ma_window, atr_window)
        self.am = ArrayManager(max_window + 20)
        
        # 创建日线Bar生成器
        self.daily_bg = SessionDailyBarGenerator(
            on_daily_bar=self.on_daily_bar,
            vt_symbol="",  # 会在 Signal 中设置
            auto_daily_end=True,
            daily_end_hour=14,
            daily_end_minute=daily_end_minute,
        )
        
        # 状态变量
        self.target = 0
        self.days_low = 0.0
        self.days_to_exit = 0
        self.ma_value = 0.0
        self.is_5day_low = False
        self.atr_value = 0.0
        self.current_stop = 0.0
        self.current_tp = 0.0
        self.days_in_trade = 0
        self.close_history: List[float] = []
    
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
        2. 计算技术指标 (N日低点, MA, ATR)
        3. 判断交易信号
        4. 更新持仓状态
        """
        # 更新K线数据
        self.am.update_bar(bar)
        self.close_history.append(bar.close_price)
        if len(self.close_history) > max(self.lookback_days, self.ma_window, self.atr_window) + 10:
            self.close_history = self.close_history[-(max(self.lookback_days, self.ma_window, self.atr_window) + 10):]
        
        # 数据未预热完成，不计算信号
        if not self.am.inited:
            return
        
        # === 指标计算 ===
        n = min(self.lookback_days, len(self.close_history) - 1)
        if n <= 0:
            return
        
        # 1. 计算N日低点
        recent_closes = self.close_history[-(n + 1):-1]
        self.days_low = min(recent_closes) if recent_closes else bar.low_price
        
        # 2. 计算MA
        self.ma_value = self.am.sma(self.ma_window) if self.use_ma_exit else 0.0
        
        # 3. 计算ATR
        self.atr_value = self.am.atr(self.atr_window) if (self.use_stop_loss or self.use_take_profit) else 0.0
        
        # 4. 判断是否为阴线
        is_down_day = bar.close_price < bar.open_price
        
        # 5. 判断5日低点条件
        self.is_5day_low = bar.close_price <= self.days_low * 1.001
        
        # === 交易信号判断 ===
        if self.target > 0:
            # === 持仓状态：检查出场条件 ===
            self.days_in_trade += 1
            
            # 出场条件1: 持有期满
            if self.days_in_trade >= self.max_holding_days:
                self.target = 0
                self.days_in_trade = 0
                return
            
            # 出场条件2: MA出场
            if self.use_ma_exit and bar.close_price > self.ma_value:
                self.target = 0
                self.days_in_trade = 0
                return
            
            # 出场条件3: ATR止损
            if self.use_stop_loss and bar.close_price < self.current_stop:
                self.target = 0
                self.days_in_trade = 0
                return
            
            # 出场条件4: ATR止盈
            if self.use_take_profit and bar.close_price > self.current_tp:
                self.target = 0
                self.days_in_trade = 0
                return
            
            # 继续持有
            self.target = self.fixed_size
            
        elif self.target == 0:
            # === 空仓状态：检查入场条件 ===
            # 入场条件1: 5日低点
            cond_1 = self.is_5day_low
            
            # 入场条件2: (可选)下跌日
            cond_2 = (not self.require_down_day) or is_down_day
            
            if cond_1 and cond_2:
                self.target = self.fixed_size
                self.days_in_trade = 1
                
                # 计算止损/止盈价格
                if self.use_stop_loss:
                    self.current_stop = bar.close_price - self.atr_value * self.sl_atr_multiplier
                if self.use_take_profit:
                    self.current_tp = bar.close_price + self.atr_value * self.tp_atr_multiplier
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
    if '.' in vt_symbol:
        symbol = vt_symbol.split('.')[0]
    else:
        symbol = vt_symbol
    # 去除数字后缀
    product = ''.join([c for c in symbol if not c.isdigit()])
    return product.upper()
