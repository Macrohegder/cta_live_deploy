import pickle
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

def adx(df: pd.DataFrame, window: int) -> np.ndarray:
    """计算ADX指标"""
    return ta.ADX(df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy(), window)

class Kbins_ADX_90_30_I45_long_short(TargetPosTemplate):
    """
    基于ADX_90特征和30根bar持仓周期的KBinsovernight策略
    
    使用TargetPosTemplate重构：
    1. 简化交易逻辑：只需设置self.target_pos，模板自动处理下单和追单。
    2. 提高健壮性：内置了撤单和追单逻辑，应对成交不完全的情况。
    3. 兼容性：保持了原有信号逻辑和风控参数。
    """

    author = "KBins策略生成器"

    # 策略参数
    long_label = 3  # 做多标签
    short_label = 6  # 做空标签
    holding_window = 30  # 持仓周期
    fixed_size = 1  # 每次交易数量
    payup = 3 # 默认超价tick数量，保证成交
    pos_tolerance = 1e-6

    # 策略变量
    feature_value = 0  # 特征值
    feature_label = 0  # 特征标签
    holding_count = 0  # 持仓计数
    
    base_position = 0 #0:无底仓 1：有底仓
    target_pos = 0 # 目标仓位

    parameters = ["long_label", "short_label", "holding_window", "fixed_size", "payup"]

    
    variables = ["feature_value", "feature_label", "holding_count", "target_pos"]
    

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        """构造函数"""
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        self.windows = [90]

        self.bg = BarGenerator(self.on_bar, 45, self.on_window_bar)

        size = 100
        if len(self.windows) != 0:
            # 确保ArrayManager足够大
            size = max(self.windows) * 3
        self.am = ArrayManager(size=size)

        # 加载预训练模型和特征计算函数
        with open("strategies/Kbins_ADX_90_B10_L3_S6_H30_I45_long_short_ETHUSDT_SWAP_OKX_GLOBAL.model", "rb") as f:
            self.est = pickle.load(f)
        self.feature_func = adx

    def on_init(self):
        """初始化回调"""
        self.write_log("策略初始化")
        self.load_bar(15)
        
        # 设置TargetPosTemplate的tick_add
        # payup为tick数量，需要乘以pricetick得到价格偏移量
        self.tick_add = self.payup * self.get_pricetick()

    def on_start(self):
        """启动回调"""
        self.write_log("策略启动")

    def on_stop(self):
        """停止回调"""
        self.write_log("策略停止")

    def on_tick(self, tick: TickData):
        """Tick更新回调"""
        # 必须先调用父类方法以更新last_tick，供TargetPosTemplate计算价格
        super().on_tick(tick)
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData):
        """Bar更新回调"""
        # 必须先调用父类方法以更新last_bar
        super().on_bar(bar)
        self.bg.update_bar(bar)
        
        # 补单机制：检查仓位是否一致，如果不一致且无活动订单，则补单
        if abs(self.pos - self.target_pos) > self.pos_tolerance and not self.active_orderids:
            self.trade()

    def on_window_bar(self, bar: BarData):
        """窗口Bar回调"""
        # TargetPosTemplate会自动管理订单，无需手动cancel_all
        
        am = self.am
        am.update_bar(bar)
        if not am.inited:
            return

        # 准备数据
        df = pd.DataFrame({
            'open': am.open,
            'high': am.high,
            'low': am.low,
            'close': am.close,
            'volume': am.volume
        })
        
        # 获取指标计算函数的源代码

        result = self.feature_func(df, *self.windows)  # 添加windows作为参数
        self.feature_value = float(result[-1] if isinstance(result, np.ndarray) else result)

        if np.isnan(self.feature_value):
            return

        # 使用模型预测标签
        output = self.est.transform(np.array([[self.feature_value]]))
        self.feature_label = int(output[0][0])
              
        target = 0
        
        
        
        # 生成交易信号并设置目标仓位
        if self.feature_label == self.long_label:
            
            # 多头信号 + 无底仓 + 可做多 -> 1倍仓位
            target = self.fixed_size
            self.holding_count = self.holding_window
                            
        elif self.feature_label == self.short_label:
            
            # 空头信号 + 无底仓 + 可做空 -> -1倍仓位
            target = -self.fixed_size
            self.holding_count = self.holding_window
            
                           
        else:
            # 无信号，处理持仓周期
            self.holding_count -= 1
            if self.holding_count <= 0:
                
                target = 0 # 持有期结束，空仓
                
            else:
                # 保持当前目标
                target = self.target_pos
                
        self.set_target_pos(target)
        self.put_event()

    def trade(self):
        if abs(self.pos - self.target_pos) < self.pos_tolerance:
            return
        super().trade()

    def on_order(self, order: OrderData):
        """订单回调"""
        super().on_order(order)

    def on_trade(self, trade: TradeData):
        """成交回调"""
        self.write_log(f"交易执行 - 方向: {'多' if trade.direction == Direction.LONG else '空'}, 开平: {trade.offset}, 价格: {trade.price:.4f}, 数量: {trade.volume}, 当前持仓: {self.pos}")
        if abs(self.pos) <= 1e-7:
            self.write_log("持仓量存在7位及以上小数，调整为0")
            self.pos = 0
        self.put_event()

    def on_stop_order(self, stop_order: StopOrder):
        """停止单回调"""
        pass
