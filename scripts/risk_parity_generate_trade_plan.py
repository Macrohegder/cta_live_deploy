"""
Generate QMT trade basket CSV from risk parity backtest weights.

This module was extracted from risk_parity_strategy/risk_parity_strategy_erc_vol_target_etf.py
as part of the 2026-07-07 Agent boundary cleanup.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Tuple

import pandas as pd


def _get_qmt_market(code: str) -> str:
    """Return QMT market code: Shanghai for codes starting with 5 or 11, Shenzhen otherwise."""
    if code.startswith("5") or code.startswith("11"):
        return "SH"
    return "SZ"


def generate_trade_plan(
    res: Any,
    strategy_name: str,
    price_data: pd.DataFrame,
    universe: Dict[str, Tuple[str, str]],
    capital: float,
    output_dir: str,
) -> pd.DataFrame:
    """Generate the latest target position and QMT basket import files.

    Two CSVs are produced:
    1. ``qmt_trade_plan.csv`` - human-readable trade plan.
    2. ``qmt_basket.csv`` - comma-separated basket file for QMT auto-import.

    Assumes a fresh portfolio with zero existing holdings.
    """
    weights = res.get_security_weights(strategy_name)
    if weights is None or weights.empty:
        raise RuntimeError(f"No weight data for strategy {strategy_name}")

    latest_date = weights.index[-1]
    latest_weights = weights.loc[latest_date]
    latest_prices = price_data.loc[latest_date]

    readable_rows: List[Dict[str, object]] = []
    basket_rows: List[Dict[str, object]] = []

    for code in latest_weights.index:
        if code not in universe or code not in latest_prices:
            continue

        target_weight = float(latest_weights[code])
        if target_weight <= 0:
            continue

        price = float(latest_prices[code])
        name = universe[code][0]
        market = _get_qmt_market(code)

        target_mv = target_weight * capital
        # ETF lot size = 100 shares
        raw_shares = int(target_mv / price)
        shares = (raw_shares // 100) * 100
        actual_mv = shares * price

        readable_rows.append(
            {
                "证券代码": code,
                "证券名称": name,
                "买卖方向": "买入" if shares > 0 else "",
                "委托数量": shares,
                "委托价格": round(price, 4),
                "目标权重": round(target_weight, 6),
                "目标市值(元)": round(target_mv, 2),
                "实际投入(元)": round(actual_mv, 2),
                "备注": f"最新权重日期 {latest_date.strftime('%Y-%m-%d')}",
            }
        )

        basket_rows.append(
            {
                "代码": code,
                "市场": market,
                "数量": shares,
                "相对权重": 0,
                "方向": 0,
                "量比": 0,
            }
        )

    os.makedirs(output_dir, exist_ok=True)

    readable_df = pd.DataFrame(readable_rows)
    readable_path = os.path.join(output_dir, "qmt_trade_plan.csv")
    readable_df.to_csv(readable_path, index=False, encoding="utf-8-sig")

    basket_df = pd.DataFrame(basket_rows)
    basket_path = os.path.join(output_dir, "qmt_basket.csv")
    basket_df.to_csv(basket_path, index=False, encoding="utf-8-sig")

    return readable_df
