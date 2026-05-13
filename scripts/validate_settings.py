#!/usr/bin/env python3
"""
CTA 实盘配置校验脚本 -- 部署仓库版
"""
import argparse
import importlib.util
import inspect
import json
import sys
from pathlib import Path
from typing import Dict, Optional, Type

ROOT = Path(__file__).resolve().parents[1]
STRATEGIES_DIR = ROOT / "strategies"


def _load_strategy_class(class_name: str) -> Optional[Type]:
    if not STRATEGIES_DIR.exists():
        raise FileNotFoundError(f"策略目录不存在: {STRATEGIES_DIR}")

    candidates = []
    for py_file in STRATEGIES_DIR.glob("*.py"):
        if py_file.name.startswith("_"):
            continue
        content = py_file.read_text(encoding="utf-8")
        if f"class {class_name}" in content:
            candidates.append((py_file, class_name))
            continue
        if class_name.startswith(("Kbins_", "Xgb_")):
            name_prefix = class_name.split("_")[:3]
            file_stem = py_file.stem
            if all(part in file_stem for part in name_prefix):
                for line in content.splitlines():
                    line = line.strip()
                    if line.startswith("class ") and ("Strategy" in line or "_long_short" in line or "_Multi_" in line):
                        actual_name = line.split("class ")[1].split("(")[0].split(":")[0].strip()
                        candidates.append((py_file, actual_name))
                        break

    if not candidates:
        return None

    for py_file, actual_name in candidates:
        if actual_name == class_name:
            return _exec_module_and_get_class(py_file, class_name)

    py_file, actual_name = candidates[0]
    return _exec_module_and_get_class(py_file, actual_name)


def _exec_module_and_get_class(py_file: Path, class_name: str) -> Optional[Type]:
    try:
        module_name = f"live_strategies.{py_file.stem}"
        spec = importlib.util.spec_from_file_location(module_name, str(py_file))
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        for name, obj in inspect.getmembers(module, inspect.isclass):
            if name == class_name:
                return obj
    except Exception:
        pass
    return None


def validate_vt_symbol(vt_symbol: str) -> bool:
    if not vt_symbol:
        return False
    parts = vt_symbol.split(".")
    return len(parts) == 2 and all(parts)


def validate_cta_config(config_path: Path, verbose: bool = True) -> bool:
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not data:
        print("[WARN] 配置文件为空")
        return True

    ok_count = 0
    warn_count = 0

    for key, cfg in data.items():
        class_name = cfg.get("class_name", "")
        setting = cfg.get("setting", {})
        vt_symbol = cfg.get("vt_symbol", "")

        if verbose:
            print(f"\n[CHECK] {key} | class={class_name}")

        strategy_cls = _load_strategy_class(class_name)
        if strategy_cls is None:
            raise ValueError(f"[R4] {key}: 策略类 {class_name} 找不到定义")

        parameters = getattr(strategy_cls, "parameters", [])

        missing = [p for p in parameters if p not in setting]
        if missing:
            raise ValueError(f"[R1] {key}: 缺失必填参数 {missing}")

        for p in parameters:
            expected = getattr(strategy_cls, p)
            expected_type = type(expected)
            actual = setting[p]
            actual_type = type(actual)

            if expected_type != actual_type:
                raise TypeError(
                    f"[R2] {key}: 参数 {p} 类型不匹配。"
                    f"期望 {expected_type.__name__}，实际 {actual_type.__name__}"
                )

            if expected_type is bool and isinstance(actual, str):
                raise TypeError(f"[R3] {key}: 参数 {p} 为 bool 类型，禁止使用字符串")

        if not validate_vt_symbol(vt_symbol):
            raise ValueError(f"[R5] {key}: vt_symbol '{vt_symbol}' 格式不正确")

        extra = [k for k in setting if k not in parameters]
        if extra:
            if verbose:
                print(f"  [WARN] 非策略参数 {extra}")
            warn_count += 1

        if verbose:
            print(f"  [OK] R1-R5 通过 | 参数数: {len(parameters)}")
        ok_count += 1

    print(f"\n{'='*60}")
    print(f"校验完成: 通过 {ok_count} 条, 警告 {warn_count} 条")
    print(f"{'='*60}")
    return True


def validate_all(configs_dir: Path, verbose: bool = True) -> bool:
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
            print(f"\n[FAIL] {e}")
            all_ok = False
    return all_ok


def main():
    parser = argparse.ArgumentParser(description="CTA 实盘配置校验工具")
    parser.add_argument("--config", type=str, help="cta_strategy_setting.json 路径")
    parser.add_argument("--all", action="store_true", help="校验 configs/ 下所有账户")
    parser.add_argument("--quiet", action="store_true", help="静默模式")
    args = parser.parse_args()

    verbose = not args.quiet
    try:
        if args.all:
            ok = validate_all(ROOT / "configs", verbose=verbose)
            sys.exit(0 if ok else 1)
        elif args.config:
            validate_cta_config(Path(args.config), verbose=verbose)
            sys.exit(0)
        else:
            parser.print_help()
            sys.exit(1)
    except Exception as e:
        print(f"\n[FAIL] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
