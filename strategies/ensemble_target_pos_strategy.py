"""
Ensemble Target Position Strategy Base Class
=============================================

为 vn.py CTA 策略提供统一的 ensemble 投票框架。

设计目标:
- 把多参数投票、目标仓位生成、仓位上限控制等通用逻辑下沉到基类
- 子策略只需关注:
  1. 定义 signal 类
  2. 声明策略专属参数（extra_parameters）
  3. 如有特殊 warmup 需求，可覆盖 on_init()

使用方式（最简版）:
    class MyEnsembleStrategy(EnsembleTargetPosStrategy):
        signal_cls = MySignal
        extra_parameters = ["my_param"]

        def _build_signals(self):
            # 如果 MySignal 构造函数兼容，甚至可以不写这一行
            super()._build_signals()

使用方式（自定义 signal 构造）:
    class MyEnsembleStrategy(EnsembleTargetPosStrategy):
        extra_parameters = ["rsi_window"]

        def _build_signals(self):
            self.signals = [
                MySignal(self.vt_symbol, rsi_window=self.rsi_window, **params, fixed_size=1)
                for params in self._param_list
            ]
            self.ensemble_count = len(self.signals)
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import List, Optional, Type

import numpy as np

from vnpy.trader.object import BarData, TickData, TradeData
from vnpy_ctastrategy import (
    TargetPosTemplate,
    BarGenerator,
)


class EnsembleTargetPosStrategy(TargetPosTemplate):
    """
    Ensemble 目标仓位策略基类。

    参数:
    - ensemble_params: JSON 字符串，例如 '[{"...": ...}, {...}]'
    - ensemble_mode: "soft_vote" / "hard_vote_2" / "hard_vote_3" / "hard_vote_4" / "hard_vote_5"
    - max_lots: 最终仓位上限（正整数）

    子类可覆盖:
    - signal_cls: Signal 类（可选，用于默认 _build_signals）
    - extra_parameters: 策略专属参数列表
    - _build_signals(): 自定义 signal 构造
    - on_init(): 自定义历史数据预热
    """

    author = "Kimi Code (ensemble base)"

    # ensemble 配置
    ensemble_mode = "soft_vote"
    max_lots = 1
    ensemble_params = ""  # JSON 字符串，每组参数一个 dict

    # 策略专属参数，子类覆盖。基类会自动拼接到 parameters 中。
    extra_parameters: List[str] = []

    # 可选：指定 signal 类，基类可用默认方式构造 signals
    signal_cls: Optional[Type] = None

    # UI 变量
    target_pos: int = 0
    current_vote: int = 0
    long_votes: int = 0
    short_votes: int = 0
    ensemble_count: int = 0

    base_parameters = [
        "ensemble_mode",
        "max_lots",
        "ensemble_params",
    ]
    base_variables = [
        "target_pos",
        "current_vote",
        "long_votes",
        "short_votes",
        "ensemble_count",
    ]

    # 子类通常不需要手动覆盖 parameters / variables
    parameters = base_parameters
    variables = base_variables

    def __init_subclass__(cls, **kwargs):
        """
        自动把 extra_parameters 拼接到 parameters 中。
        如果子类已经显式定义了 parameters，则尊重子类定义。
        """
        super().__init_subclass__(**kwargs)
        # 只在子类没有显式覆盖 parameters 时自动拼接
        if "parameters" not in cls.__dict__:
            cls.parameters = cls.base_parameters + list(cls.extra_parameters)
        if "variables" not in cls.__dict__:
            cls.variables = cls.base_variables

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.bg = BarGenerator(self.on_bar)

        # 解析 ensemble_params 并创建 signals
        self._validate_and_parse_params()
        self._build_signals()

        self.target_pos = 0
        self.current_vote = 0
        self.long_votes = 0
        self.short_votes = 0
        self.vote_history: List[dict] = []

        self.write_log(
            f"[EnsembleBase-初始化] 品种: {vt_symbol}, "
            f"ensemble_count={self.ensemble_count}, mode={self.ensemble_mode}, max_lots={self.max_lots}"
        )

    def _validate_and_parse_params(self):
        """校验 ensemble_params 是否为合法 JSON 列表。"""
        if not self.ensemble_params:
            raise ValueError("ensemble_params 不能为空，需提供至少一组参数")

        try:
            param_list = json.loads(self.ensemble_params)
        except json.JSONDecodeError as e:
            raise ValueError(f"ensemble_params 不是合法 JSON: {self.ensemble_params}") from e

        if not isinstance(param_list, list) or len(param_list) == 0:
            raise ValueError("ensemble_params 必须是包含至少一个 dict 的 list")

        self._param_list = param_list

    def _build_signals(self):
        """
        根据 self._param_list 创建 self.signals。

        默认实现:
        - 如果子类定义了 signal_cls，则用 extra_parameters 作为公共 kwargs，
          把每组 params 展开后构造 signal。
        - 否则抛出 NotImplementedError，要求子类覆盖。

        子类覆盖时，创建完成后必须设置:
        - self.signals: List[Signal]
        - self.ensemble_count: int
        """
        if self.signal_cls is None:
            raise NotImplementedError(
                "子类必须实现 _build_signals()，或设置 signal_cls 让基类默认构造"
            )

        common_kwargs = {p: getattr(self, p) for p in self.extra_parameters}
        self.signals = []
        for params in self._param_list:
            merged = {**common_kwargs, **params, "fixed_size": 1}
            signal = self.signal_cls(self.vt_symbol, **merged)
            self.signals.append(signal)
        self.ensemble_count = len(self.signals)

    def _warmup_bars(self) -> int:
        """
        计算预热所需的 bar 数量。

        默认实现：取所有 signal 中技术指标 am 的最大 size + 30 根缓冲。
        如果 signal 没有 factor.am，则返回 0。
        子类可覆盖。
        """
        if not self.signals:
            return 0

        max_size = 0
        for signal in self.signals:
            am = getattr(getattr(signal, "factor", None), "am", None)
            if am is not None:
                max_size = max(max_size, getattr(am, "size", 0))

        return max_size + 30 if max_size else 0

    def on_init(self):
        """默认初始化：加载历史数据预热技术指标。子类可覆盖。"""
        warmup = self._warmup_bars()
        self.write_log(
            f"[策略初始化] 请求历史数据用于合成日线并预热技术指标：warmup={warmup}"
        )

        if warmup > 0 and self.signals:
            am = getattr(getattr(self.signals[0], "factor", None), "am", None)
            if am is not None:
                # 尝试多次请求历史数据直到 am 初始化完成
                ok, attempts = self._ensure_am_inited(am, warmup)
                last = attempts[-1] if attempts else (0, 0, False)
                self.write_log(
                    f"[策略初始化] 历史数据加载完成 | ok={ok} "
                    f"| last_request={last[0]} am_count={last[1]} am_inited={last[2]}"
                )

    def _ensure_am_inited(self, am, required: int) -> tuple:
        """
        辅助方法：请求历史数据直到 am 初始化完成或达到上限。
        返回: (ok, [(request_count, am_count, am_inited), ...])
        """
        attempts = []
        for multiplier in [1, 2, 3, 5]:
            request_count = required * multiplier
            try:
                self.load_bar(request_count)
            except Exception:
                pass
            am_count = getattr(am, "count", 0)
            am_inited = getattr(am, "inited", False)
            attempts.append((request_count, am_count, am_inited))
            if am_inited:
                return True, attempts
        return False, attempts

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

        # 更新每个 signal
        for signal in self.signals:
            signal.on_bar(bar)

        # 投票生成目标仓位
        self.target_pos = self._vote_target()
        self.current_vote = self.target_pos

        # 记录日志
        dt_str = bar.datetime.strftime("%Y-%m-%d %H:%M:%S")
        if self.target_pos != prev_target:
            if self.target_pos > 0:
                self.write_log(
                    f"[{dt_str}] [ensemble-信号] 做多 | "
                    f"long_votes={self.long_votes}, short_votes={self.short_votes}, target={self.target_pos}"
                )
            elif self.target_pos < 0:
                self.write_log(
                    f"[{dt_str}] [ensemble-信号] 做空 | "
                    f"long_votes={self.long_votes}, short_votes={self.short_votes}, target={self.target_pos}"
                )
            else:
                self.write_log(
                    f"[{dt_str}] [ensemble-信号] 平仓 | "
                    f"long_votes={self.long_votes}, short_votes={self.short_votes}"
                )

        self.set_target_pos(self.target_pos)
        self.put_event()

    def _vote_target(self) -> int:
        """根据各 signal 的 target 投票生成最终目标仓位。"""
        votes = [int(np.sign(signal.get_target())) for signal in self.signals]
        target, self.long_votes, self.short_votes = vote_target(
            votes, self.ensemble_mode, self.max_lots
        )
        self.vote_history.append({
            "datetime": datetime.now().isoformat(),
            "votes": votes,
            "long_votes": self.long_votes,
            "short_votes": self.short_votes,
            "target": target,
        })
        return target

    def on_trade(self, trade: TradeData) -> None:
        if abs(self.pos) <= 1e-7:
            self.write_log(f"[碎单处理] 持仓量 {self.pos} 清零")
            self.pos = 0

        direction = "多" if trade.direction.value == "多" else "空"
        offset = trade.offset.value

        self.write_log(
            f"[成交回报] 方向={direction} | "
            f"开平={offset} | "
            f"价格={trade.price:.2f} | "
            f"数量={trade.volume} | "
            f"最新持仓={self.pos}"
        )
        self.put_event()


def vote_target(votes: List[int], ensemble_mode: str, max_lots: int) -> tuple:
    """
    纯函数：根据 vote 列表投票生成最终目标仓位。

    支持模式:
    - soft_vote: 净票数决定方向
    - hard_vote_N: 至少 N 票同向才开仓

    返回: (target, long_votes, short_votes)
    """
    long_votes = sum(1 for v in votes if v > 0)
    short_votes = sum(1 for v in votes if v < 0)
    net = sum(votes)

    if ensemble_mode == "soft_vote":
        if net > 0:
            return max_lots, long_votes, short_votes
        elif net < 0:
            return -max_lots, long_votes, short_votes
        else:
            return 0, long_votes, short_votes
    elif ensemble_mode == "hard_vote_2":
        if long_votes >= 2:
            return max_lots, long_votes, short_votes
        elif short_votes >= 2:
            return -max_lots, long_votes, short_votes
        else:
            return 0, long_votes, short_votes
    elif ensemble_mode == "hard_vote_3":
        if long_votes >= 3:
            return max_lots, long_votes, short_votes
        elif short_votes >= 3:
            return -max_lots, long_votes, short_votes
        else:
            return 0, long_votes, short_votes
    elif ensemble_mode == "hard_vote_4":
        if long_votes >= 4:
            return max_lots, long_votes, short_votes
        elif short_votes >= 4:
            return -max_lots, long_votes, short_votes
        else:
            return 0, long_votes, short_votes
    elif ensemble_mode == "hard_vote_5":
        if long_votes >= 5:
            return max_lots, long_votes, short_votes
        elif short_votes >= 5:
            return -max_lots, long_votes, short_votes
        else:
            return 0, long_votes, short_votes
    else:
        raise ValueError(f"Unknown ensemble_mode: {ensemble_mode}")
