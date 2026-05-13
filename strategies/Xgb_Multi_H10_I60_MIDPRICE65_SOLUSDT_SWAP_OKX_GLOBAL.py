import os
import pickle
import joblib
import numpy as np
import pandas as pd
from datetime import datetime, time, timedelta
import talib as ta

from vnpy.trader.constant import Direction
from vnpy.trader.utility import round_to
from vnpy.trader.utility import BarGenerator

from vnpy_ctastrategy import (
    CtaTemplate,
    TargetPosTemplate,
    StopOrder,
    TickData,
    BarData,
    TradeData,
    OrderData,
    ArrayManager,
)

import talib as ta
import numpy as np
import pandas as pd

def natr(df: pd.DataFrame, window: int) -> np.ndarray:
    """计算ATR指标"""
    return ta.NATR(df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy(), window)


def midprice(df: pd.DataFrame, window: int) -> np.ndarray:
    """计算MIDPRICE指标"""
    return ta.MIDPRICE(df["high"].to_numpy(), df["low"].to_numpy(), window)


def ema(df: pd.DataFrame, window: int) -> np.ndarray:
    """计算指数移动平均线指标"""
    return ta.EMA(df["close"].to_numpy(), window)



class Xgb_Multi_H10_I60_MIDPRICE65_SOLUSDT_SWAP_OKX_GLOBAL(TargetPosTemplate):
    """
    Based on XGBoost Algorithm
    Features: ['NATR_55', 'NATR_80', 'EMA_35', 'MIDPRICE_65']
    """

    author = "XGBoost Strategy Generator"

    # Strategy Parameters
    holding_window = 10
    fixed_size = 1
    payup = 3
    pos_tolerance = 1e-6
    
    # Strategy Variables
    holding_count = 0
    target_pos = 0
    
    parameters = ["holding_window", "fixed_size", "payup"]
    variables = ["holding_count", "target_pos"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        self.bg = BarGenerator(self.on_bar, 60, self.on_window_bar)
        
        # Ensure ArrayManager is large enough for the max window needed
        self.am = ArrayManager(size=300 + 30)

        # Load Model - 支持多种路径解析
        self.model_path = "strategies/Xgb_Multi_H10_I60_MIDPRICE65_SOLUSDT_SWAP_OKX_GLOBAL.joblib"
        self.model = None
        load_errors = []
        
        # 尝试多个可能的路径
        possible_paths = [
            self.model_path,  # 1. 原始路径
            os.path.join("deploy_strategies_xgb", os.path.basename(self.model_path)),  # 2. deploy_strategies_xgb 根目录
            os.path.join(os.path.dirname(__file__), os.path.basename(self.model_path)),  # 3. 策略文件所在目录
            os.path.basename(self.model_path),  # 4. 当前工作目录
        ]
        
        # 如果策略在 symbol/strategy_name/ 子目录中，尝试上级目录
        current_file_dir = os.path.dirname(__file__)
        parent_dir = os.path.dirname(current_file_dir)
        grandparent_dir = os.path.dirname(parent_dir)
        
        possible_paths.extend([
            os.path.join(parent_dir, os.path.basename(self.model_path)),  # 5. 父目录 (symbol/)
            os.path.join(grandparent_dir, os.path.basename(self.model_path)),  # 6. 祖父目录 (deploy_strategies_xgb/)
        ])
        
        for path in possible_paths:
            if os.path.exists(path):
                try:
                    self.model = joblib.load(path)
                    self.write_log(f"Model loaded from: {path}")
                    break
                except Exception as e:
                    load_errors.append(f"{path}: {e}")
        
        if self.model is None:
            error_msg = f"Failed to load model from any path. Attempted: {possible_paths}. Errors: {load_errors}"
            self.write_log(error_msg)
            raise FileNotFoundError(error_msg)

    def on_init(self):
        self.write_log("Strategy Initialization")
        self.load_bar(30)
        self.tick_add = self.payup * self.get_pricetick()

    def on_start(self):
        self.write_log("Strategy Start")

    def on_stop(self):
        self.write_log("Strategy Stop")

    def on_tick(self, tick: TickData):
        super().on_tick(tick)
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData):
        super().on_bar(bar)
        self.bg.update_bar(bar)
        
        # Order management logic
        if abs(self.pos - self.target_pos) > self.pos_tolerance and not self.active_orderids:
            self.trade()

    def on_window_bar(self, bar: BarData):
        self.write_log(f"Window Bar Triggered: {bar.datetime}, Close: {bar.close_price}")
        am = self.am
        am.update_bar(bar)
        if not am.inited:
            return

        # Prepare Data for Feature Calculation
        # We construct a DataFrame-like structure or just pass arrays to functions
        # The injected feature functions expect a DataFrame with 'open','high','low','close','volume'
        
        df = pd.DataFrame({
            'open': am.open,
            'high': am.high,
            'low': am.low,
            'close': am.close,
            'volume': am.volume
        })

        # Calculate Features
        features = []
        
        val_NATR_55 = natr(df, 55)
        features.append(val_NATR_55[-1] if isinstance(val_NATR_55, np.ndarray) else val_NATR_55)
        val_NATR_80 = natr(df, 80)
        features.append(val_NATR_80[-1] if isinstance(val_NATR_80, np.ndarray) else val_NATR_80)
        val_EMA_35 = ema(df, 35)
        features.append(val_EMA_35[-1] if isinstance(val_EMA_35, np.ndarray) else val_EMA_35)
        val_MIDPRICE_65 = midprice(df, 65)
        features.append(val_MIDPRICE_65[-1] if isinstance(val_MIDPRICE_65, np.ndarray) else val_MIDPRICE_65)

        # Check for NaNs
        X_input = np.array([features])
        if np.isnan(X_input).any():
            return

        # Predict
        # Assumption: Model returns 0 (Short), 1 (Neutral), 2 (Long) OR similar
        # Adjust mapping based on training labels
        pred = self.model.predict(X_input)[0]
        
        self.write_log(f"Model Prediction: {pred}, Current HoldingCount: {self.holding_count}")
        
        # Map Prediction to Target Position
        target = 0
        
        # Label Mapping: 0: Short, 1: Neutral, 2: Long
        if pred == 2:
            target = self.fixed_size
            self.holding_count = self.holding_window
            self.write_log(f"Long Signal: Reset HoldingCount to {self.holding_window}")
        elif pred == 0:
            target = -self.fixed_size
            self.holding_count = self.holding_window
            self.write_log(f"Short Signal: Reset HoldingCount to {self.holding_window}")
        else:
            # Neutral/Hold logic
            self.holding_count -= 1
            self.write_log(f"Neutral Signal: HoldingCount decremented to {self.holding_count}")
            if self.holding_count <= 0:
                target = 0
                self.write_log("Holding Period Ended: Closing Position")
            else:
                target = self.target_pos
                self.write_log(f"Holding Period Active: Keeping TargetPos {target}")

        
        # Log signal changes
        if self.target_pos != target:
             self.write_log(f"Signal Changed: Pred={pred}, Target={target}, HoldingCount={self.holding_count}")
             
        self.set_target_pos(target)
        self.put_event()

    def trade(self):
        if abs(self.pos - self.target_pos) < self.pos_tolerance:
            return
        
        # Log trade execution attempt
        self.write_log(f"Executing Trade: CurrentPos={self.pos}, Target={self.target_pos}")
        super().trade()
        
    def on_trade(self, trade: TradeData):
        """成交回调"""
        self.write_log(f"交易执行 - 方向: {'多' if trade.direction == Direction.LONG else '空'}, 开平: {trade.offset}, 价格: {trade.price:.4f}, 数量: {trade.volume}, 当前持仓: {self.pos}")
        if abs(self.pos) <= 1e-7:
            self.write_log("持仓量存在7位及以上小数，调整为0")
            self.pos = 0
        self.put_event()