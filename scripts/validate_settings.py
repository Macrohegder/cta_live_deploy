#!/usr/bin/env python3
"""
CTA 实盘配置校验脚本 — 部署仓库版

校验规则：
- R1 完整性: setting 必须包含 strategy_class.parameters 的每一个参数名
- R2 类型一致性: 每个参数值的类型必须与策略类默认值的类型一致
- R3 bool 规范: bool 参数必须使用 JSON true/false，禁止字符串
- R4 策略存在性: class_name 必须能在 strategies/ 目录找到对应定义
- R5 品种格式: vt_symbol 必须符合 {SYMBOL}_{TYPE}_{EXCHANGE}.{MARKET} 格式

用法:
    python scripts/validate_settings.py --config configs/nav/cta_strategy_setting.json
    python scripts/validate_settings.py --all  # 校验所有账户配置
"""
import argparse
import importlib.util
import inspect
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Type

ROOT = Path(__file__).resolve().parents[1]
STRATEGIES_DIR = ROOT / "strategies"


def _load_strategy_class(class_name: str) -> Optional[Type]:
    """从 strategies/ 目录加载策略类"""
    if not STRATEGIES_DIR.exists():
        raise FileNotFoundError(f"策略目录不存在: {STRATEGIES_DIR}")

    for py_file in STRATEGIES_DIR.glob("*.py"):
        if py_file.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                py_file.stem, str(py_file)
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for _name, obj in inspect.getmembers(module, inspect.isclass):
                if _name == class_name:
                    return obj
        except Exception:
            continue
    return None


def _scan_strategy_classes() -> Dict[str, Path]:
    """扫描 strategies/ 目录，返回 {class_name: file_path}"""
    result = {}
    if not STRATEGIES_DIR.exists():
        return result
    for py_file in STRATEGIES_DIR.glob("*.py"):
        if py_file.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                py_file.stem, str(py_file)
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for _name, obj in inspect.getmembers(module, inspect.isclass):
                if hasattr(obj, "author"):  # vnpy CtaTemplate 特征
                    result[_name] = py_file
        except Exception:
            continue
    return result


def validate_vt_symbol(vt_symbol: str) -> bool:
    """校验 vt_symbol 格式: SYMBOL_TYPE_EXCHANGE.MARKET 或 SYMBOL.EXCHANGE"""
    if not vt_symbol:
        return False
    parts = vt_symbol.split(".")
    if len(parts) != 2:
        return False
    symbol_part, market = parts
    if not symbol_part or not market:
        return False
    # 允许常见格式
    return True


def validate_cta_config(config_path: Path, verbose: bool = True) -> bool:
    """
    校验 CTA 配置 JSON。
    致命错误通过 raise 异常阻断。
    """
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    data = json.loads(config_path.read_text(encoding="utf-8"))

    if not data:
        print("⚠️ 配置文件为空，无需校验")
        return True

    ok_count = 0
    warn_count = 0
    scanned_classes = _scan_strategy_classes()

    for key, cfg in data.items():
        class_name = cfg.get("class_name", "")
        setting = cfg.get("setting", {})
        vt_symbol = cfg.get("vt_symbol", "")

        if verbose:
            print(f"\n🔍 校验: {key} | class={class_name} | symbol={vt_symbol}")

        # === R4: 策略存在性 ===
        strategy_cls = _load_strategy_class(class_name)
        if strategy_cls is None:
            found_in = scanned_classes.get(class_name)
            if found_in:
                strategy_cls = _load_strategy_class(class_name)
            else:
                raise ValueError(
                    f"[CTA_CONFIG_RULE_R4] {key}: 策略类 {class_name} "
                    f"在 strategies/ 目录中找不到对应定义"
                )

        parameters = getattr(strategy_cls, "parameters", [])

        # === R1: 完整性 ===
        missing = [p for p in parameters if p not in setting]
        if missing:
            raise ValueError(
                f"[CTA_CONFIG_RULE_R1] {key}: 策略 {class_name} 缺失必填参数 {missing}"
            )

        # === R2: 类型一致性 + R3: bool 规范 ===
        for p in parameters:
            expected = getattr(strategy_cls, p)
            expected_type = type(expected)
            actual = setting[p]
            actual_type = type(actual)

            if expected_type != actual_type:
                raise TypeError(
                    f"[CTA_CONFIG_RULE_R2] {key}: 参数 {p} 类型不匹配。"
                    f"期望 {expected_type.__name__}（默认值 {expected!r}），"
                    f"实际 {actual_type.__name__}（值 {actual!r}）"
                )

            if expected_type is bool and isinstance(actual, str):
                raise TypeError(
                    f"[CTA_CONFIG_RULE_R3] {key}: 参数 {p} 为 bool 类型，"
                    f"禁止使用字符串 '{actual}'，请使用 JSON true/false"
                )

        # === R5: 品种格式 ===
        if not validate_vt_symbol(vt_symbol):
            raise ValueError(
                f"[CTA_CONFIG_RULE_R5] {key}: vt_symbol '{vt_symbol}' 格式不正确"
            )

        # === 非策略参数白名单（警告级别）===
        extra = [k for k in setting if k not in parameters]
        if extra:
            if verbose:
                print(f"  ⚠️ 包含非策略参数 {extra}，请确认是否为引擎需要")
            warn_count += 1

        if verbose:
            print(f"  ✅ R1-R5 通过 | 参数数: {len(parameters)}")
        ok_count += 1

    print(f"\n{'='*60}")
    print(f"CTA 配置校验完成: 通过 {ok_count} 条, 警告 {warn_count} 条")
    print(f"{'='*60}")
    return True


def validate_all(configs_dir: Path, verbose: bool = True) -> bool:
    """校验 configs/ 下所有账户配置"""
    all_ok = True
    for account_dir in sorted(configs_dir.iterdir()):
        if not account_dir.is_dir():
            continue
        config_file = account_dir / "cta_strategy_setting.json"
        if not config_file.exists():
            continue
        print(f"\n{'#'*60}")
        print(f"# 账户: {account_dir.name}")
        print(f"{'#'*60}")
        try:
            validate_cta_config(config_file, verbose=verbose)
        except Exception as e:
            print(f"\n❌ [VALIDATION_FAILED] {e}")
            all_ok = False
    return all_ok


def main():
    parser = argparse.ArgumentParser(description="CTA 实盘配置校验工具（部署仓库版）")
    parser.add_argument("--config", type=str, help="cta_strategy_setting.json 路径")
    parser.add_argument("--all", action="store_true", help="校验 configs/ 下所有账户")
    parser.add_argument("--quiet", action="store_true", help="静默模式，仅输出错误")
    args = parser.parse_args()

    verbose = not args.quiet

    try:
        if args.all:
            ok = validate_all(ROOT / "configs", verbose=verbose)
            sys.exit(0 if ok else 1)
        elif args.config:
            config_path = Path(args.config)
            validate_cta_config(config_path, verbose=verbose)
            sys.exit(0)
        else:
            parser.print_help()
            sys.exit(1)
    except Exception as e:
        print(f"\n❌ [CTA_CONFIG_VALIDATION_FAILED] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
