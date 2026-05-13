#!/usr/bin/env python3
"""
策略代码同步脚本
从 cta_developer/cta/strategies/ 复制策略代码到 cta_live_deploy/strategies/

用法:
    python scripts/sync_strategies.py --list       # 列出 cta_developer 中所有策略
    python scripts/sync_strategies.py --all        # 同步所有策略（不推荐，只同步部署列表）
    python scripts/sync_strategies.py --manifest deploy-manifest.json
    python scripts/sync_strategies.py --classes CincoStrategy Nr7BreakoutStrategy
"""
import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import List, Set

ROOT = Path(__file__).resolve().parents[1]
CTA_DEV_STRATEGIES = Path("/root/quant/cta_developer/cta/strategies")
TRACKER_STRATEGIES = Path("/root/quant/tracker/strategies")
DEPLOY_STRATEGIES = ROOT / "strategies"


def _find_strategy_file(class_name: str, source_dir: Path) -> Path:
    """在 source_dir 中查找包含指定 class_name 的 .py 文件"""
    for py_file in sorted(source_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        content = py_file.read_text(encoding="utf-8")
        if f"class {class_name}" in content:
            return py_file
    return None


def _file_checksum(path: Path) -> str:
    """计算文件 MD5"""
    return hashlib.md5(path.read_bytes()).hexdigest()


def sync_strategies(class_names: List[str], dry_run: bool = False) -> dict:
    """
    同步指定策略类到部署目录
    优先从 cta_developer 复制，找不到则 fallback 到 tracker/strategies/
    返回同步报告
    """
    if not CTA_DEV_STRATEGIES.exists():
        raise FileNotFoundError(f"cta_developer 策略目录不存在: {CTA_DEV_STRATEGIES}")

    DEPLOY_STRATEGIES.mkdir(parents=True, exist_ok=True)
    
    report = {
        "synced": [],
        "missing": [],
        "unchanged": [],
        "fallback": [],
        "errors": []
    }

    for class_name in sorted(set(class_names)):
        src = _find_strategy_file(class_name, CTA_DEV_STRATEGIES)
        source_desc = "cta_developer"
        
        if src is None:
            # fallback 到 tracker/strategies/
            if TRACKER_STRATEGIES.exists():
                src = _find_strategy_file(class_name, TRACKER_STRATEGIES)
                if src:
                    source_desc = "tracker"
        
        if src is None:
            report["missing"].append(class_name)
            print(f"  ❌ 找不到策略: {class_name}（cta_developer 和 tracker 均无）")
            continue

        dst = DEPLOY_STRATEGIES / src.name

        if dst.exists():
            if _file_checksum(src) == _file_checksum(dst):
                report["unchanged"].append(class_name)
                print(f"  ➖ 无变化: {class_name} ({src.name})")
                continue
            else:
                print(f"  🔄 更新: {class_name} ({src.name})")
        else:
            if source_desc == "tracker":
                print(f"  ➕ 新增(fallback): {class_name} ({src.name}) [来自 tracker]")
                report["fallback"].append(class_name)
            else:
                print(f"  ➕ 新增: {class_name} ({src.name})")

        if not dry_run:
            try:
                shutil.copy2(str(src), str(dst))
                report["synced"].append(class_name)
            except Exception as e:
                report["errors"].append({"class": class_name, "error": str(e)})
                print(f"  ❌ 复制失败: {e}")
        else:
            report["synced"].append(class_name)

    return report


def scan_cta_developer_strategies() -> List[str]:
    """扫描 cta_developer 中的所有策略类名"""
    classes = []
    for py_file in sorted(CTA_DEV_STRATEGIES.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        content = py_file.read_text(encoding="utf-8")
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("class ") and "Strategy" in line:
                # 提取类名
                name = line.split("class ")[1].split("(")[0].split(":")[0].strip()
                classes.append(name)
    return sorted(set(classes))


def extract_classes_from_manifest(manifest_path: Path) -> Set[str]:
    """从 deploy-manifest.json 中提取需要同步的策略类名"""
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    classes = set()
    for account_info in data.get("accounts", {}).values():
        for strategy in account_info.get("strategies", []):
            classes.add(strategy.get("class_name", ""))
    return classes


def extract_classes_from_config(config_path: Path) -> Set[str]:
    """从 cta_strategy_setting.json 中提取策略类名"""
    data = json.loads(config_path.read_text(encoding="utf-8"))
    return {cfg.get("class_name", "") for cfg in data.values()}


def main():
    parser = argparse.ArgumentParser(description="策略代码同步工具")
    parser.add_argument("--classes", nargs="+", help="指定同步的策略类名")
    parser.add_argument("--manifest", type=str, help="从 deploy-manifest.json 提取策略列表")
    parser.add_argument("--config", type=str, help="从 cta_strategy_setting.json 提取策略列表")
    parser.add_argument("--all", action="store_true", help="同步所有策略（慎用）")
    parser.add_argument("--list", action="store_true", help="列出 cta_developer 中所有策略")
    parser.add_argument("--dry-run", action="store_true", help="只检查，不实际复制")
    args = parser.parse_args()

    if args.list:
        classes = scan_cta_developer_strategies()
        print(f"cta_developer 中共有 {len(classes)} 个策略类:")
        for c in classes:
            print(f"  - {c}")
        sys.exit(0)

    class_names = []
    if args.all:
        class_names = scan_cta_developer_strategies()
    elif args.classes:
        class_names = args.classes
    elif args.manifest:
        class_names = list(extract_classes_from_manifest(Path(args.manifest)))
    elif args.config:
        class_names = list(extract_classes_from_config(Path(args.config)))
    else:
        parser.print_help()
        sys.exit(1)

    if not class_names:
        print("⚠️ 没有需要同步的策略")
        sys.exit(0)

    print(f"🔄 准备同步 {len(class_names)} 个策略...")
    if args.dry_run:
        print("   (dry-run 模式，不实际复制文件)")

    report = sync_strategies(class_names, dry_run=args.dry_run)

    print(f"\n{'='*60}")
    print(f"同步完成: 新增/更新 {len(report['synced'])}, 无变化 {len(report['unchanged'])}, 缺失 {len(report['missing'])}, 错误 {len(report['errors'])}")
    print(f"{'='*60}")

    if report["missing"]:
        print(f"\n❌ 缺失策略（请在 cta_developer 中检查）:")
        for c in report["missing"]:
            print(f"   - {c}")
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
