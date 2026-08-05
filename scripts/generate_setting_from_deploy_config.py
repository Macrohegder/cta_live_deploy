#!/usr/bin/env python3
"""
[DEPRECATED] 第二套部署路径已废弃。

本脚本与 scripts/build_deploy.py / scripts/sync_strategies.py 高度重复，
并存在硬编码合约乘数 / 收盘时间、自动改写策略 import 路径等问题。

已重命名为 .DEPRECATED_generate_setting_from_deploy_config.py_BANNED。

实盘部署唯一入口：scripts/build_deploy.py
策略代码同步：scripts/sync_strategies.py（保持原样复制，不修改 import）
"""
import sys


def main():
    print("=" * 80)
    print("[BANNED] generate_setting_from_deploy_config.py")
    print("=" * 80)
    print("本脚本已废弃。请使用标准部署流程：")
    print("  python3 scripts/build_deploy.py --assets crypto --dry-run")
    print("  python3 scripts/sync_strategies.py --manifest deploy-manifest.json")
    print("=" * 80)
    sys.exit(1)


if __name__ == "__main__":
    main()
