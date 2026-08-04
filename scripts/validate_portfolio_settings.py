#!/usr/bin/env python3
"""
validate_portfolio_settings.py — paper_cmd42_lf 组合策略 paper 配置最小校验器

独立于现有 validate_settings.py（后者面向 CTA 单策略 cta_strategy_setting.json）。

用法：
    python3 scripts/validate_portfolio_settings.py configs/paper_cmd42_lf/runner_config.json

任一 FAIL 则 exit 1；WARN 不阻断。
"""
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

DEPLOY_ROOT = Path("/root/quant/cta_live_deploy")

REQUIRED_RUNNER_KEYS = [
    "account", "mode", "strategy_config_path", "strategy_module_path",
    "live_contracts_path", "log_dir", "warmup_trading_days", "stale_data_warn_days",
]

STRATEGY_PARAM_KEYS = [
    "fast_windows", "slow_windows", "scalars", "vol_window", "fdm", "idm",
    "vol_target", "forecast_cap", "long_flat", "max_leverage",
    "max_lots_per_symbol", "capital", "carry_weight", "carry_scalar",
    "carry_symbols", "carry_dir", "rule_fdm",
]

# 本部署（cmd42_lf）的预期关键参数
EXPECTED = {"capital": 10_000_000, "max_leverage": 5.0, "idm": 2.1284, "long_flat": 1}

CONTINUOUS_RE = re.compile(r"^[A-Z]{1,2}88[89]\.(SHFE|DCE|CZCE|INE)$")
LIVE_SYMBOL_RE = re.compile(r"^[A-Z]{1,2}\d{3,4}$")

results: list = []  # (level, msg)


def ok(msg: str) -> None:
    results.append(("OK", msg))


def warn(msg: str) -> None:
    results.append(("WARN", msg))


def fail(msg: str) -> None:
    results.append(("FAIL", msg))


def main() -> int:
    if len(sys.argv) != 2:
        print("用法: python3 scripts/validate_portfolio_settings.py <runner_config.json>")
        return 1

    config_path = Path(sys.argv[1])
    if not config_path.is_absolute():
        config_path = DEPLOY_ROOT / config_path

    # ---- 1. runner_config 必需键 + strategy_config_path 可读 ----
    if not config_path.exists():
        fail(f"runner_config 不存在: {config_path}")
        return report()
    with open(config_path) as f:
        runner = json.load(f)
    missing_keys = [k for k in REQUIRED_RUNNER_KEYS if k not in runner]
    if missing_keys:
        fail(f"runner_config 缺必需键: {missing_keys}")
    else:
        ok(f"runner_config 必需键齐全（{len(REQUIRED_RUNNER_KEYS)} 个）")

    sc_path = Path(runner.get("strategy_config_path", ""))
    if not sc_path.exists():
        fail(f"strategy_config_path 不存在: {sc_path}")
        return report()
    with open(sc_path) as f:
        sc = json.load(f)
    ok(f"strategy_config_path 可读: {sc_path}")

    # ---- 2. 策略参数键 + 关键参数一致性 ----
    missing_params = [k for k in STRATEGY_PARAM_KEYS if k not in sc]
    if missing_params:
        warn(f"源配置缺策略参数键（将落策略类默认值）: {missing_params}")
    else:
        ok(f"源配置覆盖全部 {len(STRATEGY_PARAM_KEYS)} 个策略参数键")
    for key, expected in EXPECTED.items():
        actual = sc.get(key)
        if actual == expected or (isinstance(actual, (int, float)) and float(actual) == float(expected)):
            ok(f"关键参数 {key}={actual}（预期 {expected}）")
        else:
            fail(f"关键参数 {key}={actual}，与预期 {expected} 不一致")

    # ---- 3. vt_symbols 格式 + sizes/priceticks/slippages 覆盖 ----
    vt_symbols = sc.get("vt_symbols", [])
    bad = [s for s in vt_symbols if not CONTINUOUS_RE.match(s)]
    if len(vt_symbols) != 42:
        fail(f"vt_symbols 数量={len(vt_symbols)}，预期 42")
    elif bad:
        fail(f"vt_symbols 格式非法: {bad}")
    else:
        ok("42 个 vt_symbols 格式合法（889/888 连续合约）")
    for dict_key in ("sizes", "priceticks", "slippages"):
        d = sc.get(dict_key, {})
        missing = [s for s in vt_symbols if s not in d]
        if missing:
            fail(f"{dict_key} 缺 {len(missing)} 项: {missing[:5]}{'...' if len(missing) > 5 else ''}")
        else:
            ok(f"{dict_key} 覆盖全部 42 项")

    # ---- 4. live_contracts.json ----
    live_path = DEPLOY_ROOT / runner.get("live_contracts_path", "")
    if not live_path.exists():
        fail(f"live_contracts.json 缺失: {live_path}（请跑 --update-contracts）")
    else:
        with open(live_path) as f:
            live = json.load(f)
        contracts = live.get("contracts", {})
        missing_live = [s for s in vt_symbols if s not in contracts]
        if missing_live:
            fail(f"live_contracts.json 缺 {len(missing_live)} 项: {missing_live[:5]}")
        else:
            ok("live_contracts.json 覆盖全部 42 项")
        for s in vt_symbols:
            entry = contracts.get(s)
            if not entry:
                continue
            root_expected = re.match(r"^([A-Z]{1,2})88[89]", s.split(".")[0]).group(1)
            if entry.get("root") != root_expected:
                fail(f"{s}: root={entry.get('root')} 与连续合约根 {root_expected} 不一致")
            if not LIVE_SYMBOL_RE.match(entry.get("live_symbol", "")):
                fail(f"{s}: live_symbol 格式非法: {entry.get('live_symbol')}")
        gen_at = datetime.fromisoformat(live["generated_at"])
        age_days = (datetime.now() - gen_at).days
        if age_days > 7:
            warn(f"live_contracts.json 生成于 {age_days} 天前（>7 天），建议跑 --update-contracts")
        else:
            ok(f"live_contracts.json 生成于 {age_days} 天前（≤7 天）")

    # ---- 5. ClickHouse 日线数据可订阅性 ----
    try:
        from vnpy.trader.constant import Exchange, Interval
        from vnpy.trader.database import get_database
        db = get_database()
        probe_start = datetime.now() - timedelta(days=15)
        probe_end = datetime.now() + timedelta(days=1)
        stale_days = int(runner.get("stale_data_warn_days", 5))
        for s in vt_symbols:
            symbol, exch = s.split(".")
            bars = db.load_bar_data(symbol, Exchange(exch), Interval.DAILY, probe_start, probe_end)
            if not bars:
                fail(f"ClickHouse 无日线数据: {s}")
                continue
            last_date = bars[-1].datetime.date()
            age = (datetime.now().date() - last_date).days
            if age > stale_days:
                warn(f"{s} 最新日线 {last_date}（{age} 天前，>{stale_days} 天）")
        n_fail_data = sum(1 for lv, m in results if lv == "FAIL" and "ClickHouse 无日线" in m)
        if n_fail_data == 0:
            ok(f"ClickHouse 42 个连续合约均有日线数据（新鲜度阈值 {stale_days} 天）")
    except Exception as e:
        fail(f"ClickHouse 数据检查异常: {e}")

    return report()


def report() -> int:
    n_fail = 0
    for level, msg in results:
        print(f"[{level}] {msg}")
        if level == "FAIL":
            n_fail += 1
    n_warn = sum(1 for lv, _ in results if lv == "WARN")
    print(f"\n汇总: {n_fail} FAIL / {n_warn} WARN / {len(results) - n_fail - n_warn} OK")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
