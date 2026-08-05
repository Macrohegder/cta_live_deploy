"""
IBS + RSI Mean Reversion Ensemble Strategy
==========================================

在单一策略实例内部维护多组 IBS+RSI 参数，通过投票机制生成统一的目标仓位。

交易逻辑:
- 每组参数独立运行 IbsRsiMeanReversionSignal
- 每日收盘后汇总各组参数的 target 方向
- 根据 ensemble_mode 投票决定最终 target_pos
- 最终仓位受 max_lots 硬约束

部署规则（示例）:
- IH889.CFFEX: soft_vote, max_lots=1
- T889.CFFEX: hard_vote_3, max_lots=1
"""
from __future__ import annotations

from datetime import time
from typing import List

from vnpy.trader.object import BarData

from strategies.ensemble_target_pos_strategy import (
    EnsembleTargetPosStrategy,
)
from strategies.ibs_rsi_mean_reversion_strategy import (
    IbsRsiMeanReversionSignal,
    resolve_market_profile,
    warmup_request,
    ensure_am_inited,
    get_product_name,
)


class IbsRsiMeanReversionEnsembleStrategy(EnsembleTargetPosStrategy):
    """
    IBS + RSI 均值回归 Ensemble 策略

    参数:
    - ensemble_params: JSON 字符串，例如 '[{"rsi_period":20,"ibs_threshold":0.25,"rsi_threshold":45}, {...}]'
    - ensemble_mode: "soft_vote" / "hard_vote_2" / "hard_vote_3" / "hard_vote_4" / "hard_vote_5"
    - max_lots: 最终仓位上限（正整数）
    """

    author = "Kimi Code (ensemble)"

    # ensemble 配置（继承自基类，此处保留便于文档化）
    ensemble_mode = "soft_vote"
    max_lots = 1
    ensemble_params = ""  # JSON 字符串，每组参数一个 dict

    parameters = [
        "ensemble_mode",
        "max_lots",
        "ensemble_params",
    ]

    variables = EnsembleTargetPosStrategy.base_variables

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        key = get_product_name(vt_symbol)
        self.write_log(
            f"[初始化] 品种: {key}, "
            f"ensemble_count={self.ensemble_count}, mode={self.ensemble_mode}, max_lots={self.max_lots}"
        )

    def _build_signals(self):
        """根据 ensemble_params 创建 IBS+RSI signal 列表。"""
        self.signals: List[IbsRsiMeanReversionSignal] = []
        for params in self._param_list:
            signal = IbsRsiMeanReversionSignal(
                vt_symbol=self.vt_symbol,
                rsi_period=params["rsi_period"],
                ibs_threshold=params["ibs_threshold"],
                rsi_threshold=params["rsi_threshold"],
                fixed_size=1,  # 策略层固定 1 手，生成单位敞口日盈亏
                daily_end_minute=params.get("daily_end_minute", 59),
            )
            self.signals.append(signal)
        self.ensemble_count = len(self.signals)

    def on_init(self):
        profile = resolve_market_profile(
            vt_symbol=self.vt_symbol,
            auto_daily_end=getattr(self, 'auto_daily_end', True),
            daily_end_hour=getattr(self, 'daily_end_hour', None),
            daily_end_minute=getattr(self, 'daily_end_minute', None),
        )
        end_time = time(profile.daily_end_hour, profile.daily_end_minute)

        # 取所有 signal 中最大的预热需求
        max_warmup = 0
        for signal in self.signals:
            base = warmup_request(signal.factor.am.size, aggregation_window=1, extra_bars=30)
            max_warmup = max(max_warmup, base)

        self.write_log(
            f"[策略初始化] 请求历史数据用于合成日线并预热技术指标："
            f"auto_daily_end={getattr(self, 'auto_daily_end', True)} end_time={end_time} warmup={max_warmup}"
        )

        # 用第一个 signal 的 am 做 ensure_am_inited（所有 signal 共享同一组历史数据）
        ok, attempts = ensure_am_inited(self, self.signals[0].factor.am, max_warmup, prefer_daily=True)
        last = attempts[-1] if attempts else (0, "", 0, False)
        self.write_log(
            f"[策略初始化] 历史数据加载完成 | ok={ok} "
            f"| last_request={last[0]} mode={last[1]} am_count={last[2]} am_inited={last[3]}"
        )

    def on_start(self):
        self.write_log("[on_start] 策略启动")

    def on_stop(self):
        self.write_log(f"[on_stop] 策略停止，当前持仓: {self.pos}")
