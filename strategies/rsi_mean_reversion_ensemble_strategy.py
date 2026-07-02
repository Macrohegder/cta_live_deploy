"""
RSI 均值回归策略 Ensemble 版本
================================

在单一策略实例内部维护多组 RSI 参数，通过投票机制生成统一的目标仓位。

交易逻辑:
- 每组参数独立运行 RsiMeanReversionSignal
- 每日收盘后汇总各组参数的 target 方向
- 根据 ensemble_mode 投票决定最终 target_pos
- 最终仓位受 max_lots 硬约束

使用方式:
    继承自 EnsembleTargetPosStrategy，只需声明 signal_cls 和 extra_parameters，
    以及处理合约乘数的动态获取。
"""
from __future__ import annotations

from vnpy.trader.object import BarData, TickData

from strategies.ensemble_target_pos_strategy import EnsembleTargetPosStrategy
from strategies.rsi_mean_reversion_strategy import (
    RsiMeanReversionSignal,
    get_product_name,
)


class RsiMeanReversionEnsembleStrategy(EnsembleTargetPosStrategy):
    """
    RSI 均值回归 Ensemble 策略

    参数:
    - ensemble_params: JSON 字符串，例如 '[{"rsi_buy_threshold":20,...}, {...}]'
    - ensemble_mode: "soft_vote" / "hard_vote_2" / "hard_vote_3" / "hard_vote_4" / "hard_vote_5"
    - max_lots: 最终仓位上限（正整数）
    """

    author = "Kimi Code (ensemble)"

    signal_cls = RsiMeanReversionSignal

    # 所有 signal 共用的基础参数
    rsi_window = 14
    atr_window = 14
    sl_atr_multiplier = 2.0
    risk_percent = 0.02
    capital = 5_000_000
    contract_size = 300
    auto_daily_end = True
    daily_end_hour = 14
    daily_end_minute = 59

    # extra_parameters 会自动拼接到 parameters 中
    extra_parameters = [
        "rsi_window",
        "atr_window",
        "sl_atr_multiplier",
        "risk_percent",
        "capital",
        "contract_size",
        "auto_daily_end",
        "daily_end_hour",
        "daily_end_minute",
    ]

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        key = get_product_name(vt_symbol)

        # 如果 setting 中没有显式指定 contract_size，则从 cta_engine 获取真实值
        contract_size = cta_engine.get_size(self)
        if contract_size and "contract_size" not in setting:
            self.contract_size = contract_size
            # contract_size 可能影响 signal 内部仓位计算，重建 signals
            self._build_signals()

        self.write_log(
            f"[初始化] 品种: {key}, 合约乘数: {self.contract_size}, "
            f"ensemble_count={self.ensemble_count}, mode={self.ensemble_mode}, max_lots={self.max_lots}"
        )

    def on_tick(self, tick: TickData) -> None:
        super().on_tick(tick)
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData) -> None:
        # 复用基类逻辑
        super().on_bar(bar)
