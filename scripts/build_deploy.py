#!/usr/bin/env python3
"""
CTA 实盘部署构建脚本 — 核心脚本

功能：
1. 读取 cta_developer 单策略最优参数
2. 读取 tracker 组合优化结果（风险平价权重）
3. 读取 tracker 账户定义（accounts.yaml）
4. 合并生成账户级 cta_strategy_setting.json
5. 同步策略代码
6. 生成 deploy-manifest.json
7. 自动 git commit + push

用法:
    # 完整构建并推送
    python scripts/build_deploy.py --assets crypto --push

    # 仅生成配置（dry-run，不提交）
    python scripts/build_deploy.py --assets crypto --dry-run

    # 指定账户
    python scripts/build_deploy.py --assets crypto --accounts nav,luyl
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import yaml

ROOT = Path(__file__).resolve().parents[1]
CTA_DEV_ROOT = Path("/root/quant/cta_developer")
TRACKER_ROOT = Path("/root/quant/tracker")
CTA_DEV_STRATEGIES = CTA_DEV_ROOT / "cta" / "strategies"
TRACKER_STRATEGIES = TRACKER_ROOT / "strategies"

# 默认配置
DEFAULT_ASSETS = ["crypto"]
DEFAULT_ACCOUNTS = ["nav", "luyl"]


def _load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: Path, data: dict, indent: int = 2):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False, sort_keys=True)
        f.write("\n")


def _run_cmd(cmd: List[str], cwd: Path = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, check=check)


def _get_git_commit(repo: Path) -> str:
    try:
        result = _run_cmd(["git", "rev-parse", "--short", "HEAD"], cwd=repo, check=False)
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _get_git_status(repo: Path) -> bool:
    """检查仓库是否有未提交的变更"""
    result = _run_cmd(["git", "status", "--porcelain"], cwd=repo, check=False)
    return result.returncode == 0 and bool(result.stdout.strip())


def load_cta_developer_settings(asset: str) -> Dict[str, dict]:
    """
    读取 cta_developer 生成的单策略最优配置
    返回: {(class_name, vt_symbol): setting_dict}
    """
    setting_path = CTA_DEV_ROOT / f"cta_strategy_setting_{asset}.json"
    if not setting_path.exists():
        # fallback 到合并版
        setting_path = CTA_DEV_ROOT / "cta_strategy_setting.json"
        if not setting_path.exists():
            raise FileNotFoundError(f"cta_developer 配置不存在: {setting_path}")

    data = _load_json(setting_path)
    out = {}
    for key, cfg in data.items():
        # 只处理指定 asset 的策略（如果读取的是合并版）
        if asset != "all" and not key.startswith(f"{asset}_"):
            continue
        class_name = cfg.get("class_name", "")
        vt_symbol = cfg.get("vt_symbol", "")
        setting = cfg.get("setting", {})
        if class_name and vt_symbol:
            out[(class_name, vt_symbol)] = setting
    return out


def load_tracker_accounts() -> Tuple[Dict, Dict]:
    """
    读取 tracker 的账户配置
    返回: (base_strategies, accounts_config)
    """
    accounts_path = TRACKER_ROOT / "config" / "accounts.yaml"
    if not accounts_path.exists():
        raise FileNotFoundError(f"tracker 账户配置不存在: {accounts_path}")

    raw = _load_yaml(accounts_path)
    base_strategies = raw.get("base_strategies", {})
    accounts = raw.get("accounts", {})
    return base_strategies, accounts


def load_portfolio_opt(account_id: str) -> Optional[dict]:
    """读取 tracker 的组合优化结果"""
    opt_path = TRACKER_ROOT / "data" / f"portfolio_opt_{account_id}.json"
    if not opt_path.exists():
        return None
    return _load_json(opt_path)


def _load_strategy_class_from_dir(class_name: str, search_dirs: List[Path]) -> Optional[type]:
    """在指定目录中查找并加载策略类"""
    import importlib.util
    import inspect
    for directory in search_dirs:
        if not directory.exists():
            continue
        for py_file in directory.glob("*.py"):
            if py_file.name.startswith("_"):
                continue
            content = py_file.read_text(encoding="utf-8")
            if f"class {class_name}" in content:
                try:
                    spec = importlib.util.spec_from_file_location(py_file.stem, str(py_file))
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    for name, obj in inspect.getmembers(module, inspect.isclass):
                        if name == class_name and hasattr(obj, "parameters"):
                            return obj
                except Exception:
                    continue
    return None


def _get_strategy_defaults(class_name: str) -> dict:
    """获取策略类的默认参数值"""
    # 优先从 cta_developer 加载，其次 tracker
    strategy_cls = _load_strategy_class_from_dir(class_name, [CTA_DEV_STRATEGIES, TRACKER_STRATEGIES])
    if strategy_cls is None:
        return {}
    defaults = {}
    for p in getattr(strategy_cls, "parameters", []):
        if hasattr(strategy_cls, p):
            defaults[p] = getattr(strategy_cls, p)
    return defaults


def merge_strategy_setting(
    base_strategy: dict,
    cta_dev_setting: Optional[dict],
    allocation: float,
    account_id: str,
) -> dict:
    """
    合并生成最终的策略 setting

    规则:
    1. 以策略类默认参数为基底
    2. 用 accounts.yaml 的 base_setting 覆盖
    3. 用 cta_developer 的优化参数覆盖非仓位参数（bar_window, boll_window 等）
    4. 用 portfolio_opt 的 final_allocation 计算 fixed_size
    5. capital 保持 accounts.yaml 中的值（账户级别）
    """
    class_name = base_strategy.get("class_name", "")

    # 1. 策略类默认值
    setting = _get_strategy_defaults(class_name)

    # 2. accounts.yaml base_setting 覆盖
    for k, v in base_strategy.get("base_setting", {}).items():
        setting[k] = v

    # 3. cta_developer 优化参数覆盖（排除仓位相关参数）
    if cta_dev_setting:
        for k, v in cta_dev_setting.items():
            if k not in ("fixed_size", "capital"):
                setting[k] = v

    # 4. 计算 fixed_size（如果策略类支持）
    base_size = base_strategy.get("base_setting", {}).get("fixed_size", 10)
    optimized_size = int(base_size * allocation)
    optimized_size = max(1, optimized_size)
    setting["fixed_size"] = optimized_size

    # 5. 确保 capital 存在
    if "capital" not in setting:
        setting["capital"] = base_strategy.get("base_setting", {}).get("capital", 100000)

    return setting


def generate_account_config(
    account_id: str,
    base_strategies: dict,
    cta_dev_settings: Dict[Tuple[str, str], dict],
    asset: str,
) -> Optional[dict]:
    """
    生成单个账户的 cta_strategy_setting.json
    """
    opt_result = load_portfolio_opt(account_id)
    if opt_result is None:
        print(f"  ⚠️ 账户 {account_id} 无组合优化结果，跳过")
        return None

    final_allocation = opt_result.get("final_allocation", {})
    if not final_allocation:
        print(f"  ⚠️ 账户 {account_id} 的 final_allocation 为空，跳过")
        return None

    # 读取账户策略清单
    accounts_path = TRACKER_ROOT / "config" / "accounts.yaml"
    raw = _load_yaml(accounts_path)
    account_def = raw.get("accounts", {}).get(account_id, {})
    account_strategies = account_def.get("strategies", {})

    strategy_config = {}
    deployed_strategies = []

    for strategy_short_name, multiplier in account_strategies.items():
        base_key = strategy_short_name
        if base_key not in base_strategies:
            print(f"  ⚠️ 跳过未知策略: {base_key}")
            continue

        base_config = base_strategies[base_key]
        class_name = base_config.get("class_name", "")
        vt_symbol = base_config.get("vt_symbol", "")

        # 从组合优化结果中获取 allocation
        allocation = final_allocation.get(base_key, 0)
        if allocation <= 0:
            print(f"  ⏭️ 策略 {base_key} allocation={allocation:.4f}，不纳入配置")
            continue

        # 查找 cta_developer 中的优化参数
        cta_dev_setting = cta_dev_settings.get((class_name, vt_symbol))
        if cta_dev_setting is None:
            print(f"  ⚠️ 在 cta_developer 中找不到 {class_name} @ {vt_symbol} 的优化参数，使用 base_setting")

        # 合并生成最终 setting
        final_setting = merge_strategy_setting(base_config, cta_dev_setting, allocation, account_id)

        # key 命名：沿用 tracker 的简洁风格
        config_key = base_key

        strategy_config[config_key] = {
            "class_name": class_name,
            "vt_symbol": vt_symbol,
            "setting": final_setting,
        }

        deployed_strategies.append({
            "key": config_key,
            "class_name": class_name,
            "vt_symbol": vt_symbol,
            "fixed_size": final_setting.get("fixed_size"),
            "allocation": allocation,
        })

        print(f"  ✓ {config_key}: fixed_size={final_setting.get('fixed_size')} (allocation={allocation:.2f}x)")

    return {
        "config": strategy_config,
        "strategies": deployed_strategies,
        "source": {
            "portfolio_opt_date": opt_result.get("timestamp", ""),
            "multiplier": opt_result.get("multiplier", 0),
            "expected_vol": opt_result.get("expected_vol", 0),
        }
    }


def sync_strategy_code(deployed_classes: Set[str], dry_run: bool = False) -> dict:
    """调用 sync_strategies.py 同步策略代码"""
    sync_script = ROOT / "scripts" / "sync_strategies.py"
    cmd = [sys.executable, str(sync_script), "--classes"] + sorted(deployed_classes)
    if dry_run:
        cmd.append("--dry-run")

    result = subprocess.run(cmd, capture_output=True, text=True)
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr)
        raise RuntimeError("策略代码同步失败")

    # 解析报告（简化：直接返回类名集合）
    return {"synced_classes": sorted(deployed_classes)}


def generate_manifest(
    asset: str,
    accounts: List[str],
    account_results: Dict[str, dict],
    cta_dev_commit: str,
    tracker_commit: str,
) -> dict:
    """生成部署清单"""
    manifest = {
        "version": "1.0.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "asset": asset,
        "sources": {
            "cta_developer_commit": cta_dev_commit,
            "tracker_commit": tracker_commit,
        },
        "accounts": {},
        "checksums": {},
    }

    for account_id in accounts:
        result = account_results.get(account_id)
        if result is None:
            continue
        cfg = result["config"]
        manifest["accounts"][account_id] = {
            "strategy_count": len(cfg),
            "strategies": [
                {
                    "key": s["key"],
                    "class_name": s["class_name"],
                    "vt_symbol": s["vt_symbol"],
                    "fixed_size": s["fixed_size"],
                    "allocation": s["allocation"],
                }
                for s in result["strategies"]
            ],
            "portfolio_multiplier": result["source"]["multiplier"],
            "expected_volatility": result["source"]["expected_vol"],
        }
        # 计算配置 checksum
        cfg_json = json.dumps(cfg, sort_keys=True, ensure_ascii=False)
        manifest["checksums"][account_id] = hashlib.sha256(cfg_json.encode()).hexdigest()[:16]

    return manifest


def git_commit_and_push(manifest: dict, dry_run: bool = False) -> bool:
    """git add → commit → push"""
    if dry_run:
        print("\n[DRY-RUN] 跳过 git 操作")
        return True

    # 检查是否有变更
    result = _run_cmd(["git", "status", "--porcelain"], cwd=ROOT, check=False)
    if not result.stdout.strip():
        print("\n⏭️ 无文件变更，跳过 git 提交")
        return True

    # git add
    _run_cmd(["git", "add", "."], cwd=ROOT)

    # git commit
    today = datetime.now().strftime("%Y-%m-%d")
    msg = f"config: deploy {manifest['asset']} strategies for {','.join(manifest['accounts'].keys())} ({today})"
    result = _run_cmd(["git", "commit", "-m", msg], cwd=ROOT, check=False)
    if result.returncode != 0:
        print(f"\n⚠️ git commit 失败: {result.stderr}")
        return False

    print(f"\n✅ git commit: {msg}")

    # git push
    result = _run_cmd(["git", "push", "origin", "main"], cwd=ROOT, check=False)
    if result.returncode != 0:
        print(f"\n❌ git push 失败: {result.stderr}")
        return False

    print("✅ git push 成功")
    return True


def build_deploy(asset: str, accounts: List[str], dry_run: bool = False, push: bool = False) -> bool:
    """
    构建部署包的主函数
    """
    print(f"{'='*70}")
    print(f"🚀 CTA 实盘部署构建")
    print(f"   资产: {asset}")
    print(f"   账户: {', '.join(accounts)}")
    print(f"{'='*70}")

    # 1. 读取 cta_developer 单策略配置
    print(f"\n📡 步骤1: 读取 cta_developer 单策略最优参数")
    try:
        cta_dev_settings = load_cta_developer_settings(asset)
        print(f"   共 {len(cta_dev_settings)} 条策略配置")
    except Exception as e:
        print(f"   ❌ 失败: {e}")
        return False

    # 2. 读取 tracker 账户配置
    print(f"\n📡 步骤2: 读取 tracker 账户定义")
    try:
        base_strategies, accounts_def = load_tracker_accounts()
        print(f"   基础策略: {len(base_strategies)} 个")
        print(f"   账户: {list(accounts_def.keys())}")
    except Exception as e:
        print(f"   ❌ 失败: {e}")
        return False

    # 3. 生成各账户配置
    print(f"\n📡 步骤3: 生成账户级配置")
    account_results = {}
    all_deployed_classes = set()

    for account_id in accounts:
        print(f"\n   📊 账户: {account_id}")
        print(f"   {'-'*60}")
        result = generate_account_config(account_id, base_strategies, cta_dev_settings, asset)
        if result and result["config"]:
            account_results[account_id] = result
            # 保存配置
            config_dir = ROOT / "configs" / account_id
            config_dir.mkdir(parents=True, exist_ok=True)
            config_path = config_dir / "cta_strategy_setting.json"
            if not dry_run:
                _save_json(config_path, result["config"])
                print(f"   💾 配置已保存: {config_path}")
            else:
                print(f"   💾 [dry-run] 配置将保存到: {config_path}")
            for s in result["strategies"]:
                all_deployed_classes.add(s["class_name"])
        elif result:
            print(f"   ⚠️ 生成配置为空")

    if not account_results:
        print("\n❌ 无有效配置生成，终止部署")
        return False

    # 4. 同步策略代码
    print(f"\n📡 步骤4: 同步策略代码 ({len(all_deployed_classes)} 个)")
    try:
        sync_report = sync_strategy_code(all_deployed_classes, dry_run=dry_run)
    except Exception as e:
        print(f"   ❌ 失败: {e}")
        return False

    # 5. 生成 manifest
    print(f"\n📡 步骤5: 生成部署清单")
    cta_dev_commit = _get_git_commit(CTA_DEV_ROOT)
    tracker_commit = _get_git_commit(TRACKER_ROOT)
    manifest = generate_manifest(asset, accounts, account_results, cta_dev_commit, tracker_commit)
    manifest_path = ROOT / "deploy-manifest.json"
    if not dry_run:
        _save_json(manifest_path, manifest)
        print(f"   💾 manifest: {manifest_path}")
    else:
        print(f"   💾 [dry-run] manifest 将保存到: {manifest_path}")

    # 6. 校验配置
    print(f"\n📡 步骤6: 校验生成的配置")
    if dry_run:
        print("   [dry-run] 跳过文件校验（配置未写入磁盘）")
    else:
        validate_script = ROOT / "scripts" / "validate_settings.py"
        for account_id in accounts:
            if account_id not in account_results:
                continue
            config_path = ROOT / "configs" / account_id / "cta_strategy_setting.json"
            result = subprocess.run(
                [sys.executable, str(validate_script), "--config", str(config_path), "--quiet"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                print(f"   ✅ {account_id}: 校验通过")
            else:
                print(f"   ❌ {account_id}: 校验失败")
                print(result.stdout)
                print(result.stderr)
                return False

    # 7. Git 提交与推送
    if push:
        print(f"\n📡 步骤7: Git 提交与推送")
        ok = git_commit_and_push(manifest, dry_run=dry_run)
        if not ok:
            return False

    # 汇总
    print(f"\n{'='*70}")
    print(f"✅ 部署构建完成")
    print(f"{'='*70}")
    print(f"   资产: {asset}")
    for account_id, result in account_results.items():
        print(f"   {account_id}: {len(result['config'])} 个策略")
    print(f"   cta_developer@{cta_dev_commit}")
    print(f"   tracker@{tracker_commit}")
    print(f"{'='*70}")

    return True


def main():
    parser = argparse.ArgumentParser(description="CTA 实盘部署构建脚本")
    parser.add_argument("--assets", type=str, default="crypto", help="资产类别，逗号分隔 (crypto,cn,etf)")
    parser.add_argument("--accounts", type=str, default="nav,luyl", help="账户，逗号分隔")
    parser.add_argument("--dry-run", action="store_true", help="只检查生成，不写入文件/不提交")
    parser.add_argument("--push", action="store_true", help="构建后自动 git push")
    args = parser.parse_args()

    assets = [a.strip() for a in args.assets.split(",") if a.strip()]
    accounts = [a.strip() for a in args.accounts.split(",") if a.strip()]

    all_ok = True
    for asset in assets:
        ok = build_deploy(asset, accounts, dry_run=args.dry_run, push=args.push)
        if not ok:
            all_ok = False

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
