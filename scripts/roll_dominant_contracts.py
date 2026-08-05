#!/usr/bin/env python3
"""
按米筐（RQData）主力合约规则自动换月。

扫描 cta_live_deploy/configs/*/cta_strategy_setting.json，
将各实例的 vt_symbol 与 rqdatac.futures.get_dominant(root, rank=1) 对比，
不一致则备份并更新为最新主力合约，随后运行 validate_settings.py 校验。

用法：
    python3 roll_dominant_contracts.py [--dry-run] [--account <account_id>]

cron 建议（开盘前检查）：
    45 8 * * * cd /root/quant/cta_live_deploy && /usr/bin/python3 scripts/roll_dominant_contracts.py >> logs/roll_dominant_cron.log 2>&1
"""
import argparse
import json
import logging
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

DEPLOY_ROOT = Path('/root/quant/cta_live_deploy')
CONFIG_ROOT = DEPLOY_ROOT / 'configs'
LOG_DIR = DEPLOY_ROOT / 'logs'
VALIDATE_SCRIPT = DEPLOY_ROOT / 'scripts' / 'validate_settings.py'

# vt_symbol 形如 IF2608.CFFEX / T2609.CFFEX / AU2608.SHFE
VT_SYMBOL_RE = re.compile(r'^([A-Z]+)(\d{3,4})\.([A-Z]+)$')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger('roll_dominant')


def init_rqdata():
    """初始化 RQData datafeed（复用 rq_data/futures_update.py 的模式）。"""
    from vnpy_rqdata.rqdata_datafeed import RqdataDatafeed
    datafeed = RqdataDatafeed()
    datafeed.init()
    import rqdatac  # noqa: F401  # init 后 rqdatac 即可用
    return rqdatac


def get_dominant_map(rqdatac, roots: set) -> dict:
    """查询每个品种根的当前主力合约，如 {'IF': 'IF2608'}。"""
    result = {}
    for root in sorted(roots):
        try:
            dom = rqdatac.futures.get_dominant(root, rank=1)
            if dom is None:
                logger.warning('主力合约无数据: %s，跳过', root)
                continue
            # get_dominant 返回 Series 或标量，取最后一个
            if hasattr(dom, 'iloc'):
                dom = dom.iloc[-1]
            result[root] = str(dom)
        except Exception as e:
            logger.error('查询主力合约失败: %s | %s', root, e)
    return result


def roll_account(config_path: Path, dominant_map: dict, dry_run: bool) -> bool:
    """对单个账户配置执行换月。返回是否有变更。"""
    with open(config_path) as f:
        setting = json.load(f)

    changed = False
    for name, inst in setting.items():
        vt_symbol = inst.get('vt_symbol', '')
        m = VT_SYMBOL_RE.match(vt_symbol)
        if not m:
            logger.warning('[%s] 无法解析 vt_symbol: %s，跳过', config_path.parent.name, vt_symbol)
            continue
        root, _month, exchange = m.groups()
        dominant = dominant_map.get(root)
        if not dominant:
            continue
        new_vt_symbol = f'{dominant}.{exchange}'
        if new_vt_symbol != vt_symbol:
            logger.info('[%s] %s: %s -> %s', config_path.parent.name, name, vt_symbol, new_vt_symbol)
            inst['vt_symbol'] = new_vt_symbol
            changed = True

    if not changed:
        logger.info('[%s] 全部实例已是最新主力合约，无需换月', config_path.parent.name)
        return False

    if dry_run:
        logger.info('[%s] dry-run，不写盘', config_path.parent.name)
        return True

    # 备份后写盘
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup = config_path.with_suffix(f'.json.bak_{ts}')
    backup.write_text(json.dumps(setting, indent=2, ensure_ascii=False))
    with open(config_path, 'w') as f:
        json.dump(setting, f, indent=2, ensure_ascii=False)
    logger.info('[%s] 已更新配置，备份: %s', config_path.parent.name, backup.name)

    # 校验
    if VALIDATE_SCRIPT.exists():
        r = subprocess.run(
            [sys.executable, str(VALIDATE_SCRIPT), '--config', str(config_path)],
            capture_output=True, text=True, cwd=str(DEPLOY_ROOT.parent),
        )
        tail = (r.stdout or '') + (r.stderr or '')
        logger.info('[%s] 校验结果:\n%s', config_path.parent.name, tail.strip()[-500:])
        if r.returncode != 0:
            logger.error('[%s] 换月后校验失败！请人工检查: %s', config_path.parent.name, config_path)
    return True


def main():
    parser = argparse.ArgumentParser(description='按 RQData 主力合约规则自动换月')
    parser.add_argument('--dry-run', action='store_true', help='只打印变更，不写盘')
    parser.add_argument('--account', help='只处理指定账户目录名')
    args = parser.parse_args()

    LOG_DIR.mkdir(exist_ok=True)
    fh = logging.FileHandler(LOG_DIR / 'roll_dominant.log')
    fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logging.getLogger().addHandler(fh)

    if args.account:
        config_paths = [CONFIG_ROOT / args.account / 'cta_strategy_setting.json']
    else:
        config_paths = sorted(
            p for p in CONFIG_ROOT.glob('*/cta_strategy_setting.json')
            if not p.parent.name.startswith('_')
        )
    config_paths = [p for p in config_paths if p.exists()]
    if not config_paths:
        logger.error('未找到任何 cta_strategy_setting.json')
        return 1

    # 收集全部品种根
    roots = set()
    for p in config_paths:
        with open(p) as f:
            for inst in json.load(f).values():
                m = VT_SYMBOL_RE.match(inst.get('vt_symbol', ''))
                if m:
                    roots.add(m.group(1))

    logger.info('扫描账户: %d 个，品种根: %s', len(config_paths), sorted(roots))
    rqdatac = init_rqdata()
    dominant_map = get_dominant_map(rqdatac, roots)
    logger.info('当前主力合约: %s', dominant_map)

    n_changed = 0
    for p in config_paths:
        try:
            if roll_account(p, dominant_map, args.dry_run):
                n_changed += 1
        except Exception:
            logger.exception('处理失败: %s', p)

    logger.info('完成: %d/%d 个账户发生换月', n_changed, len(config_paths))
    return 0


if __name__ == '__main__':
    sys.exit(main())
