#!/usr/bin/env python3
"""
从 cta_developer 的 final_deploy_config.json 生成 vn.py 实盘 strategy_setting.json。

用法:
    # 生成默认账户配置（使用 final_deploy_config 中最后一个窗口的参数）
    python3 scripts/generate_setting_from_deploy_config.py \
        --deploy-config /root/quant/cta_developer/configs/rsi_mean_reversion_ih_t_final_deploy_config.json \
        --account cn_futures

    # 指定品种映射（连续合约 -> 具体月份合约）
    python3 scripts/generate_setting_from_deploy_config.py \
        --deploy-config /root/quant/cta_developer/configs/rsi_mean_reversion_ih_t_final_deploy_config.json \
        --account cn_futures \
        --symbol-map "IH889.CFFEX=IH2607.CFFEX,T889.CFFEX=T2609.CFFEX"

    # 使用指定窗口的参数
    python3 scripts/generate_setting_from_deploy_config.py \
        --deploy-config /root/quant/cta_developer/configs/rsi_mean_reversion_ih_t_final_deploy_config.json \
        --account cn_futures \
        --window-index 3

    # 打印需要部署的策略源码文件（不写入配置）
    python3 scripts/generate_setting_from_deploy_config.py \
        --deploy-config /root/quant/cta_developer/configs/rsi_mean_reversion_ih_t_final_deploy_config.json \
        --print-source-files

    # 生成配置并自动同步策略代码到 cta_live_deploy/strategies/
    python3 scripts/generate_setting_from_deploy_config.py \
        --deploy-config /root/quant/cta_developer/configs/rsi_mean_reversion_ih_t_final_deploy_config.json \
        --account cn_futures \
        --sync-strategies

    # dry-run 预览
    python3 scripts/generate_setting_from_deploy_config.py \
        --deploy-config /root/quant/cta_developer/configs/rsi_mean_reversion_ih_t_final_deploy_config.json \
        --account cn_futures \
        --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

ROOT = Path(__file__).resolve().parents[1]
CTA_DEV_ROOT = Path("/root/quant/cta_developer")
CTA_DEV_STRATEGIES = CTA_DEV_ROOT / "cta" / "strategies"

# 品种默认合约乘数（实盘前务必与券商确认）
DEFAULT_CONTRACT_SIZE = {
    "IH889.CFFEX": 300,
    "IF888.CFFEX": 300,
    "IC888.CFFEX": 200,
    "IM888.CFFEX": 200,
    "T889.CFFEX": 10000,
    "TF888.CFFEX": 10000,
    "TS888.CFFEX": 20000,
    "TL888.CFFEX": 10000,
}

# 品种默认收盘时间（用于 auto_daily_end）
DEFAULT_DAILY_END = {
    "IH889.CFFEX": (15, 0),
    "IF888.CFFEX": (15, 0),
    "IC888.CFFEX": (15, 0),
    "IM888.CFFEX": (15, 0),
    "T889.CFFEX": (15, 15),
    "TF888.CFFEX": (15, 15),
    "TS888.CFFEX": (15, 15),
    "TL888.CFFEX": (15, 15),
}


def parse_symbol_map(raw: Optional[str]) -> Dict[str, str]:
    """解析品种映射字符串，如 'IH889.CFFEX=IH2607.CFFEX,T889.CFFEX=T2609.CFFEX'。"""
    out = {}
    if not raw:
        return out
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise ValueError(f"品种映射格式错误，应为 'A=B': {pair}")
        src, dst = pair.split("=", 1)
        out[src.strip()] = dst.strip()
    return out


def find_strategy_file(class_name: str, strategies_dir: Path = CTA_DEV_STRATEGIES) -> Optional[Path]:
    """在策略目录中查找包含指定 class 定义的 .py 文件。"""
    for py_file in sorted(strategies_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        content = py_file.read_text(encoding="utf-8")
        if f"class {class_name}" in content:
            return py_file
    return None


def collect_strategy_dependencies(
    class_name: str,
    strategies_dir: Path = CTA_DEV_STRATEGIES,
    visited: Optional[Set[str]] = None,
) -> List[Path]:
    """
    递归收集策略源码文件及其 cta.strategies 内部依赖。

    返回按依赖顺序排列的文件路径列表（被依赖的文件在前）。
    """
    if visited is None:
        visited = set()

    if class_name in visited:
        return []
    visited.add(class_name)

    py_file = find_strategy_file(class_name, strategies_dir)
    if py_file is None:
        raise FileNotFoundError(f"找不到策略类 {class_name} 的源码文件")

    content = py_file.read_text(encoding="utf-8")

    # 匹配 from cta.strategies.xxx import ... 或 import cta.strategies.xxx
    deps: List[Path] = []
    patterns = [
        r"from\s+cta\.strategies\.(\w+)\s+import",
        r"import\s+cta\.strategies\.(\w+)",
    ]
    dep_module_names: Set[str] = set()
    for pat in patterns:
        for m in re.finditer(pat, content):
            dep_module_names.add(m.group(1))

    for dep_name in sorted(dep_module_names):
        dep_file = strategies_dir / f"{dep_name}.py"
        if not dep_file.exists():
            continue
        # 递归收集依赖的依赖
        # 需要先读取该文件中的 class 名，才能继续递归
        dep_classes = []
        for line in dep_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("class ") and "Strategy" in line:
                dep_class = line.split("class ")[1].split("(")[0].split(":")[0].strip()
                dep_classes.append(dep_class)
        for dep_class in dep_classes:
            deps.extend(collect_strategy_dependencies(dep_class, strategies_dir, visited))

    # 去重并保持顺序
    seen: Set[Path] = set()
    unique_deps: List[Path] = []
    for f in deps:
        if f not in seen:
            seen.add(f)
            unique_deps.append(f)

    unique_deps.append(py_file)
    return unique_deps


def find_window_params(symbol_cfg: dict, window_index: Optional[int] = None) -> dict:
    """找到指定窗口的参数。默认使用最后一个窗口。"""
    windows = symbol_cfg.get("ensemble_params_by_window", [])
    if not windows:
        raise ValueError(f"品种 {symbol_cfg.get('symbol')} 没有 ensemble_params_by_window")

    if window_index is None:
        return windows[-1]

    for w in windows:
        if w.get("window_index") == window_index:
            return w
    raise ValueError(f"找不到 window_index={window_index}，可用: {[w.get('window_index') for w in windows]}")


def build_setting(symbol_cfg: dict, window_params: dict, vt_symbol: str) -> dict:
    """构建 vn.py strategy setting。"""
    symbol = symbol_cfg["symbol"]
    ensemble_params = window_params.get("ensemble_params", [])
    if not ensemble_params:
        raise ValueError(f"品种 {symbol} window {window_params.get('window_index')} 没有 ensemble_params")

    # 基础参数
    contract_size = DEFAULT_CONTRACT_SIZE.get(symbol, 1)
    daily_end_hour, daily_end_minute = DEFAULT_DAILY_END.get(symbol, (14, 59))

    return {
        "class_name": "RsiMeanReversionEnsembleStrategy",
        "vt_symbol": vt_symbol,
        "setting": {
            "rsi_window": 14,
            "atr_window": 14,
            "sl_atr_multiplier": 2.0,
            "risk_percent": 0.02,
            "capital": int(symbol_cfg.get("capital", 5_000_000)),
            "contract_size": contract_size,
            "auto_daily_end": True,
            "daily_end_hour": daily_end_hour,
            "daily_end_minute": daily_end_minute,
            "ensemble_mode": symbol_cfg["ensemble_mode"],
            "max_lots": int(symbol_cfg["max_lots"]),
            "ensemble_params": json.dumps(ensemble_params, ensure_ascii=False),
        },
    }


def generate_setting(
    deploy_config_path: Path,
    output_path: Path,
    symbol_map: Dict[str, str],
    window_index: Optional[int] = None,
    dry_run: bool = False,
    print_source_files: bool = False,
    sync_strategies: bool = False,
) -> Dict[str, List[Path]]:
    """
    生成 strategy_setting.json。

    返回: {strategy_key: [依赖的源码文件路径列表]}
    """
    with open(deploy_config_path) as f:
        deploy_cfg = json.load(f)

    account_capital = deploy_cfg.get("account_capital", 5_000_000)
    setting: Dict[str, dict] = {}
    source_files_by_strategy: Dict[str, List[Path]] = {}

    for symbol_cfg in deploy_cfg.get("symbols", []):
        symbol = symbol_cfg["symbol"]
        vt_symbol = symbol_map.get(symbol, symbol)

        window_params = find_window_params(symbol_cfg, window_index)
        strategy_key = f"cn_{vt_symbol.replace('.', '_')}_RSIMEANREVERSION_daily"

        cfg = build_setting(symbol_cfg, window_params, vt_symbol)
        setting[strategy_key] = cfg

        # 收集策略源码依赖
        source_files = collect_strategy_dependencies(cfg["class_name"])
        source_files_by_strategy[strategy_key] = source_files

        print(f"\n[{strategy_key}] {symbol} -> {vt_symbol}")
        print(f"  class_name={cfg['class_name']}")
        print(f"  ensemble_mode={cfg['setting']['ensemble_mode']}")
        print(f"  max_lots={cfg['setting']['max_lots']}")
        print(f"  window_index={window_params['window_index']}")
        print(f"  ensemble_params_count={len(window_params['ensemble_params'])}")

        if print_source_files or sync_strategies:
            print(f"  source_files:")
            for f in source_files:
                print(f"    - {f}")

    if dry_run:
        print("\n[Dry-run] 生成的配置如下：")
        print(json.dumps(setting, indent=2, ensure_ascii=False))
        return source_files_by_strategy

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(setting, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"\nSaved strategy setting: {output_path}")
    print(f"Account capital: {account_capital:,.0f} CNY")
    print(f"Total strategies: {len(setting)}")

    # 自动同步策略代码
    if sync_strategies:
        print("\n[Sync] 开始同步策略代码到 cta_live_deploy/strategies/ ...")
        all_files: Set[Path] = set()
        for files in source_files_by_strategy.values():
            all_files.update(files)

        deploy_strategies_dir = ROOT / "strategies"
        deploy_strategies_dir.mkdir(parents=True, exist_ok=True)

        # 用于改写 import 的正则
        from_import_re = re.compile(r"from\s+cta\.strategies\.(\w+)\s+import")
        plain_import_re = re.compile(r"import\s+cta\.strategies\.(\w+)")

        for src in sorted(all_files):
            dst = deploy_strategies_dir / src.name
            content = src.read_text(encoding="utf-8")
            # 改写 cta.strategies.xxx 为 cta_live_deploy 包内绝对导入 strategies.xxx
            new_content = from_import_re.sub(r"from strategies.\1 import", content)
            new_content = plain_import_re.sub(lambda m: f"import strategies.{m.group(1)}", new_content)

            if dst.exists():
                old_content = dst.read_text(encoding="utf-8")
                if old_content == new_content:
                    print(f"  ➖ 无变化: {src.name}")
                    continue
                print(f"  🔄 更新: {src.name}")
            else:
                print(f"  ➕ 新增: {src.name}")
            dst.write_text(new_content, encoding="utf-8")
        print(f"[Sync] 完成，共同步 {len(all_files)} 个文件")
        print("[Sync] 注意：交易服务器需把 cta_live_deploy/strategies/ 加入 PYTHONPATH，")
        print("      或在 vn.py 启动前设置 PYTHONPATH=/opt/cta_live_deploy:$PYTHONPATH")

    return source_files_by_strategy


def main():
    parser = argparse.ArgumentParser(description="从 final_deploy_config.json 生成 vn.py 实盘 strategy_setting.json")
    parser.add_argument("--deploy-config", required=True, help="final_deploy_config.json 路径")
    parser.add_argument("--account", default="cn_futures", help="账户名，用于生成输出路径")
    parser.add_argument("--output-dir", default=str(ROOT / "configs"), help="输出目录")
    parser.add_argument("--symbol-map", default="", help="品种映射，如 'IH889.CFFEX=IH2607.CFFEX,T889.CFFEX=T2609.CFFEX'")
    parser.add_argument("--window-index", type=int, default=None, help="使用第几个窗口的参数，默认最后一个")
    parser.add_argument("--print-source-files", action="store_true", help="打印每个策略依赖的源码文件")
    parser.add_argument("--sync-strategies", action="store_true", help="自动把策略源码同步到 cta_live_deploy/strategies/")
    parser.add_argument("--dry-run", action="store_true", help="只打印不写入")
    args = parser.parse_args()

    deploy_config_path = Path(args.deploy_config)
    output_path = Path(args.output_dir) / args.account / "cta_strategy_setting.json"
    symbol_map = parse_symbol_map(args.symbol_map)

    generate_setting(
        deploy_config_path=deploy_config_path,
        output_path=output_path,
        symbol_map=symbol_map,
        window_index=args.window_index,
        dry_run=args.dry_run,
        print_source_files=args.print_source_files,
        sync_strategies=args.sync_strategies,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
