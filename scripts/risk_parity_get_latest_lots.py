"""Compute latest ERC weights, vol-adjusted weights, and tradable lots.

Usage:
    python get_latest_lots.py
    python get_latest_lots.py --target-vol 0.15 --capital 20000000
    python get_latest_lots.py --target-vol 0.12 --erc-lookback-days 126
"""


# Ensure risk_parity_strategy modules are importable when running from another agent.
import sys
from pathlib import Path
_RPS_ROOT = Path(__file__).resolve().parents[2] / "risk_parity_strategy"
if str(_RPS_ROOT) not in sys.path:
    sys.path.insert(0, str(_RPS_ROOT))
import argparse
import warnings

import ffn
import numpy as np
import pandas as pd
from sklearn.covariance import LedoitWolf

from config.settings import CONTRACT_MULTIPLIERS
from data.data_loader import load_vnpy_prices

warnings.filterwarnings("ignore")


def parse_args():
    parser = argparse.ArgumentParser(
        description="计算最新 ERC 权重、波动率调整后权重和实际交易手数",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python get_latest_lots.py
  python get_latest_lots.py --target-vol 0.15
  python get_latest_lots.py --target-vol 0.12 --capital 20000000
  python get_latest_lots.py --erc-lookback-days 126 --start-date 2022-01-01
        """,
    )
    parser.add_argument("--target-vol", type=float, default=0.10, help="目标年化波动率（默认 0.10=10%%）")
    parser.add_argument("--capital", type=float, default=10_000_000, help="资金规模（默认 1000万）")
    parser.add_argument(
        "--erc-lookback-days", type=int, default=252, help="ERC 权重计算回望期（交易日，默认 252≈12个月）"
    )
    parser.add_argument("--start-date", type=str, default="2020-01-01", help="数据起始日期（默认 2020-01-01）")
    parser.add_argument("--vol-clip-max", type=float, default=5.0, help="波动率调整因子上限（默认 5.0）")
    return parser.parse_args()


def main():
    args = parse_args()

    # 1. Load latest data
    price, _ = load_vnpy_prices(start_date=args.start_date, end_date=None)
    price = price.ffill().bfill()
    for col in price.columns:
        price.loc[price[col] <= 0, col] = np.nan
    price = price.dropna()

    print(f"数据范围: {price.index[0].date()} ~ {price.index[-1].date()}")
    print(f"最新价格 ({price.index[-1].date()}):")
    latest_prices = price.iloc[-1]
    for asset, p in latest_prices.items():
        print(f"  {asset}: {p:.2f}")
    print()

    # 2. Compute returns for ERC lookback
    returns = price.pct_change().dropna()
    recent_returns = returns.tail(args.erc_lookback_days)

    # 3. ERC weights using ffn
    erc_weights = ffn.calc_erc_weights(recent_returns)
    print(f"=== ERC 权重 ({args.erc_lookback_days}日回望) ===")
    for asset, w in erc_weights.items():
        print(f"  {asset}: {w:.4f} ({w * 100:.2f}%)")
    print(f"  权重总和: {erc_weights.sum():.4f}")
    print()

    # 4. Portfolio volatility (annualized)
    lw = LedoitWolf()
    cov_matrix = pd.DataFrame(
        lw.fit(recent_returns.values).covariance_,
        index=recent_returns.columns,
        columns=recent_returns.columns,
    )
    portfolio_variance = np.dot(erc_weights.values, np.dot(cov_matrix.values, erc_weights.values))
    portfolio_vol = np.sqrt(portfolio_variance * 252)
    print(f"组合年化波动率: {portfolio_vol:.4f} ({portfolio_vol * 100:.2f}%)")

    # 5. Volatility adjustment
    target_vol = args.target_vol
    vol_adjustment = target_vol / portfolio_vol if portfolio_vol > 0 else 1.0
    vol_adjustment = np.clip(vol_adjustment, 0.1, args.vol_clip_max)
    print(f"目标波动率: {target_vol * 100:.0f}%")
    print(f"波动率调整因子: {vol_adjustment:.4f}")
    print()

    # 6. Adjusted weights
    adjusted_weights = erc_weights * vol_adjustment
    print("=== 波动率调整后权重 ===")
    for asset, w in adjusted_weights.items():
        print(f"  {asset}: {w:.4f} ({w * 100:.2f}%)")
    print(f"  权重总和: {adjusted_weights.sum():.4f}")
    print()

    # 7. Calculate lots
    capital = args.capital
    print(f"=== 实际交易手数 (资金 {capital:,.0f}) ===")
    total_nominal = 0
    for asset, weight in adjusted_weights.items():
        multiplier = CONTRACT_MULTIPLIERS.get(asset, 1)
        price_val = latest_prices[asset]
        theoretical_lots = (capital * weight) / (price_val * multiplier)
        actual_lots = max(1, round(theoretical_lots))
        nominal_value = actual_lots * price_val * multiplier
        total_nominal += nominal_value
        print(f"  {asset}:")
        print(f"    理论手数: {theoretical_lots:.2f}")
        print(f"    实际手数: {actual_lots}")
        print(f"    合约乘数: {multiplier}")
        print(f"    名义价值: {nominal_value:,.0f} ({nominal_value / capital * 100:.2f}% of capital)")
        print()

    print(f"总名义价值: {total_nominal:,.0f} ({total_nominal / capital * 100:.2f}% of capital)")
    print(f"实际杠杆率: {total_nominal / capital:.2f}x")


if __name__ == "__main__":
    main()
