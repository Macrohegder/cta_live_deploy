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

def midprice(df: pd.DataFrame, window: int) -> np.ndarray:
    """计算MIDPRICE指标"""
    return ta.MIDPRICE(df["high"].to_numpy(), df["low"].to_numpy(), window)


def sma(df: pd.DataFrame, window: int) -> np.ndarray:
    """计算简单移动平均线指标"""
    return ta.SMA(df["close"].to_numpy(), window)


def trix(df: pd.DataFrame, window: int) -> np.ndarray:
    """计算TRIX指标"""
    return ta.TRIX(df["close"].to_numpy(), window)


def aroon(df: pd.DataFrame, window: int) -> np.ndarray:
    """计算AROON指标"""
    return ta.AROONOSC(df["high"].to_numpy(), df["low"].to_numpy(), window)


def ema(df: pd.DataFrame, window: int) -> np.ndarray:
    """计算指数移动平均线指标"""
    return ta.EMA(df["close"].to_numpy(), window)


def natr(df: pd.DataFrame, window: int) -> np.ndarray:
    """计算ATR指标"""
    return ta.NATR(df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy(), window)


def kama(df: pd.DataFrame, window: int) -> np.ndarray:
    """计算自适应移动平均线指标"""
    return ta.KAMA(df["close"].to_numpy(), window)


def adosc(df: pd.DataFrame, fast_window: int, slow_window: int) -> np.ndarray:
    """计算ADOSC指标"""
    return ta.ADOSC(
        df["high"].to_numpy(),
        df["low"].to_numpy(),
        df["close"].to_numpy(),
        df['volume'].to_numpy(),
        fast_window,
        slow_window
    )



class Xgb_Multi_H55_I60_KAMA10_ETHUSDT_SWAP_OKX_GLOBAL(TargetPosTemplate):
    """
    Based on XGBoost Algorithm
    Features: ['KAMA_10', 'KAMA_15', 'KAMA_25', 'NATR_65', 'AROON_70', 'MIDPRICE_50', 'KAMA_35', 'EMA_25', 'MIDPRICE_20', 'ADOSC_70_140', 'TRIX_60', 'MIDPRICE_40', 'NATR_10', 'SMA_80', 'EMA_15']
    """

    author = "XGBoost Strategy Generator"

    # Strategy Parameters
    holding_window = 55
    fixed_size = 1
    payup = 3
    pos_tolerance = 1e-6
    confidence_threshold = 0.6
    
    # Strategy Variables
    holding_count = 0
    target_pos = 0
    
    parameters = ["holding_window", "fixed_size", "payup", "confidence_threshold"]
    variables = ["holding_count", "target_pos"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        self.bg = BarGenerator(self.on_bar, 60, self.on_window_bar)
        
        # Ensure ArrayManager is large enough for the max window needed
        self.am = ArrayManager(size=300 + 30)

        # Load Model
        self.model_path = "strategies/Xgb_Multi_H55_I60_KAMA10_ETHUSDT_SWAP_OKX_GLOBAL.joblib"
        try:
            # 1. Try Loading from Absolute/Relative Path
            self.model = joblib.load(self.model_path)
        except Exception as e:
            # 2. Try Loading from current directory or deploy_strategies_xgb (Fallback for mining)
            try:
                import os
                filename = os.path.basename(self.model_path)
                
                # Check deploy_strategies_xgb
                mining_path = os.path.join("deploy_strategies_xgb", filename)
                if os.path.exists(mining_path):
                    self.model = joblib.load(mining_path)
                else:
                    # Check current directory
                    self.model = joblib.load(filename)
            except Exception as e2:
                self.write_log(f"Failed to load model: {e}, {e2}")
                raise e

    def on_init(self):
        self.write_log("Strategy Initialization")
        # In Backtesting, load_bar calls database. 
        # But we are using file-based backtesting in run_xgb.py and manually populating history_data.
        # So we should strictly rely on the engine's history data if available.
        # However, BacktestingEngine.load_bar defaults to DB query.
        
        # To avoid DB error in mining environment:
        try:
             self.load_bar(30)
        except Exception as e:
             self.write_log(f"Load bar failed (expected if DB not configured): {e}")
             # In backtesting, if history_data is pre-loaded, on_init might be called BEFORE replay?
             # Actually, run_backtesting calls on_init, then replays history_data.
             # But load_bar is for "init stage" data. 
             # If we fail here, we might miss initialization data. 
             # But since we use ArrayManager with enough size, it might self-correct after 30 bars.
             pass
             
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
        
        val_KAMA_10 = kama(df, 10)
        features.append(val_KAMA_10[-1] if isinstance(val_KAMA_10, np.ndarray) else val_KAMA_10)
        val_KAMA_15 = kama(df, 15)
        features.append(val_KAMA_15[-1] if isinstance(val_KAMA_15, np.ndarray) else val_KAMA_15)
        val_KAMA_25 = kama(df, 25)
        features.append(val_KAMA_25[-1] if isinstance(val_KAMA_25, np.ndarray) else val_KAMA_25)
        val_NATR_65 = natr(df, 65)
        features.append(val_NATR_65[-1] if isinstance(val_NATR_65, np.ndarray) else val_NATR_65)
        val_AROON_70 = aroon(df, 70)
        features.append(val_AROON_70[-1] if isinstance(val_AROON_70, np.ndarray) else val_AROON_70)
        val_MIDPRICE_50 = midprice(df, 50)
        features.append(val_MIDPRICE_50[-1] if isinstance(val_MIDPRICE_50, np.ndarray) else val_MIDPRICE_50)
        val_KAMA_35 = kama(df, 35)
        features.append(val_KAMA_35[-1] if isinstance(val_KAMA_35, np.ndarray) else val_KAMA_35)
        val_EMA_25 = ema(df, 25)
        features.append(val_EMA_25[-1] if isinstance(val_EMA_25, np.ndarray) else val_EMA_25)
        val_MIDPRICE_20 = midprice(df, 20)
        features.append(val_MIDPRICE_20[-1] if isinstance(val_MIDPRICE_20, np.ndarray) else val_MIDPRICE_20)
        val_ADOSC_70_140 = adosc(df, 70, 140)
        features.append(val_ADOSC_70_140[-1] if isinstance(val_ADOSC_70_140, np.ndarray) else val_ADOSC_70_140)
        val_TRIX_60 = trix(df, 60)
        features.append(val_TRIX_60[-1] if isinstance(val_TRIX_60, np.ndarray) else val_TRIX_60)
        val_MIDPRICE_40 = midprice(df, 40)
        features.append(val_MIDPRICE_40[-1] if isinstance(val_MIDPRICE_40, np.ndarray) else val_MIDPRICE_40)
        val_NATR_10 = natr(df, 10)
        features.append(val_NATR_10[-1] if isinstance(val_NATR_10, np.ndarray) else val_NATR_10)
        val_SMA_80 = sma(df, 80)
        features.append(val_SMA_80[-1] if isinstance(val_SMA_80, np.ndarray) else val_SMA_80)
        val_EMA_15 = ema(df, 15)
        features.append(val_EMA_15[-1] if isinstance(val_EMA_15, np.ndarray) else val_EMA_15)

        # Check for NaNs
        X_input = np.array([features])
        if np.isnan(X_input).any():
            return

        # Predict
        # Assumption: Model returns probabilities [prob_0, prob_1, prob_2]
        # Adjust mapping based on training labels
        
        try:
            # Use predict_proba for confidence threshold
            probs = self.model.predict_proba(X_input)[0]
            pred = np.argmax(probs)
            max_prob = probs[pred]
        except AttributeError:
            # Fallback if model doesn't support predict_proba (old models)
            pred = self.model.predict(X_input)[0]
            max_prob = 1.0
        
        self.write_log(f"Model Prediction: {pred}, Prob: {max_prob:.2f}, Current HoldingCount: {self.holding_count}")
        
        # Map Prediction to Target Position
        target = 0
        
        # Label Mapping: 0: Short, 1: Neutral, 2: Long
        if pred == 2 and max_prob >= self.confidence_threshold:
            target = self.fixed_size
            self.holding_count = self.holding_window
            self.write_log(f"Long Signal (Conf={max_prob:.2f}): Reset HoldingCount to {self.holding_window}")
        elif pred == 0 and max_prob >= self.confidence_threshold:
            target = -self.fixed_size
            self.holding_count = self.holding_window
            self.write_log(f"Short Signal (Conf={max_prob:.2f}): Reset HoldingCount to {self.holding_window}")
        else:
            # Neutral/Hold logic or Low Confidence
            if max_prob < self.confidence_threshold and pred != 1:
                 self.write_log(f"Signal Filtered (Low Conf={max_prob:.2f} < {self.confidence_threshold})")
                 
            self.holding_count -= 1
            if self.holding_count <= 0:
                target = 0
                self.write_log("Holding Period Ended: Closing Position")
            else:
                target = self.target_pos
                self.write_log(f"Holding Period Active: Keeping TargetPos {target}, HoldingCount {self.holding_count}")
        
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