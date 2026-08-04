#!/usr/bin/env python3
"""
run_portfolio_paper.py — vnpy_portfoliostrategy 日度批处理 paper 运行器

PAPER ONLY — 无实盘下单路径。
本脚本不连接任何 gateway，不产生任何真实委托；所有"成交"均为内存中的即时模拟
（用于推进 pos_data 以便逐日重放），最终只输出目标手数表与 JSON 日志。

运行形态（与 paper_ldt / paper_rsimr_ih 同一惯例）：
- 非常驻进程，cron 日度批处理（收盘后、rq_data 23:00 更新之后）；
- 用 FakeStrategyEngine 从本机 ClickHouse 回放 warmup+当日 日线，
  逐日调 strategy.on_bars，得到当日目标手数；
- 信号层使用 889/888 连续合约（与回测一致），live 合约映射仅用于手数落地展示。

用法：
    python3 scripts/run_portfolio_paper.py --config configs/paper_cmd42_lf/runner_config.json [--date YYYY-MM-DD] [--dry-run] [--update-contracts]

cron 建议（不要由本脚本安装，手工加入 crontab）：
    40 23 * * 1-5 cd /root/quant/cta_live_deploy && /usr/bin/python3 scripts/run_portfolio_paper.py --config configs/paper_cmd42_lf/runner_config.json >> logs/portfolio_cmd42_lf/cron.log 2>&1
"""
import argparse
import importlib.util
import json
import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from vnpy.trader.constant import Direction, Exchange, Interval, Offset
from vnpy.trader.database import get_database
from vnpy.trader.object import BarData
from vnpy_portfoliostrategy.base import EngineType

DEPLOY_ROOT = Path("/root/quant/cta_live_deploy")

# 策略参数白名单（与 FuturesCarverMaStrategy.parameters 对齐；
# 源配置缺哪个就用策略类默认值，capital 必须显式注入）
STRATEGY_PARAM_KEYS = [
    "fast_windows", "slow_windows", "scalars", "vol_window", "fdm", "idm",
    "vol_target", "forecast_cap", "long_flat", "max_leverage",
    "max_lots_per_symbol", "capital", "carry_weight", "carry_scalar",
    "carry_symbols", "carry_dir", "rule_fdm",
]

CONTINUOUS_RE = re.compile(r"^([A-Z]{1,2})88[89]\.(SHFE|DCE|CZCE|INE)$")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("portfolio_paper")


class FakeStrategyEngine:
    """
    StrategyTemplate 所需引擎方法的最小 paper 实现。

    已对照 vnpy_portfoliostrategy/template.py 核实被调用的引擎方法：
    get_size / get_pricetick / load_bars / send_order / cancel_order /
    put_strategy_event / send_notification / sync_strategy_data /
    write_log / get_engine_type。
    """

    def __init__(self, strategy_config: dict, end_date, stale_warn_days: int) -> None:
        self.sizes: dict = strategy_config["sizes"]
        self.priceticks: dict = strategy_config["priceticks"]
        self.end_date = end_date              # date 对象，回放截止日
        self.stale_warn_days = stale_warn_days

        self.db = get_database()
        self.current_dt = None                # 回放当前日（date）
        self.blotter: list = []               # 全部模拟成交
        self.last_bars: dict = {}             # 最终日各合约 BarData
        self.data_last_dates: dict = {}       # 各合约数据库最新日线日期
        self._order_seq: int = 0

    # ---- 合约元数据（来自源策略配置 dict，唯一来源原则） ----

    def get_size(self, strategy, vt_symbol: str):
        size = self.sizes.get(vt_symbol)
        if size is None:
            raise KeyError(f"源配置 sizes 缺少 {vt_symbol}")
        return size

    def get_pricetick(self, strategy, vt_symbol: str):
        return self.priceticks.get(vt_symbol)

    # ---- 历史数据回放 ----

    def load_bars(self, strategy, days: int, interval) -> None:
        """从 ClickHouse 取日线，逐日对齐回放 strategy.on_bars。

        日历起点按 days×1.75+60 天缓冲往前取，每合约截最近 days 根；
        按交易日并集对齐，缺失日用前收填充（OHLC=前收, volume=0）。
        """
        assert interval == Interval.DAILY, "本 runner 只支持日线"
        end_dt = datetime.combine(self.end_date, datetime.max.time())
        start_dt = end_dt - timedelta(days=int(days * 1.75) + 60)

        series: dict = {}     # vt_symbol -> {date: BarData}（已截最近 days 根）
        for vt_symbol in strategy.vt_symbols:
            symbol, exch = vt_symbol.split(".")
            bars = self.db.load_bar_data(
                symbol, Exchange(exch), Interval.DAILY, start_dt, end_dt
            )
            if not bars:
                logger.warning("ClickHouse 无日线数据: %s（回放窗口内）", vt_symbol)
                series[vt_symbol] = {}
                continue
            bars = bars[-days:]
            series[vt_symbol] = {b.datetime.date(): b for b in bars}
            self.data_last_dates[vt_symbol] = max(series[vt_symbol])

        # 交易日并集（已按各合约最近 days 根截断）
        calendar = sorted({d for s in series.values() for d in s})
        if not calendar:
            raise RuntimeError("回放窗口内没有任何日线数据")

        last_close: dict = {}
        started: set = set()
        for d in calendar:
            self.current_dt = d
            bars: dict = {}
            for vt_symbol in strategy.vt_symbols:
                bar = series[vt_symbol].get(d)
                if bar is not None:
                    bars[vt_symbol] = bar
                    last_close[vt_symbol] = bar.close_price
                    started.add(vt_symbol)
                elif vt_symbol in started:
                    # 缺失日前收填充
                    symbol, exch = vt_symbol.split(".")
                    c = last_close[vt_symbol]
                    bars[vt_symbol] = BarData(
                        symbol=symbol,
                        exchange=Exchange(exch),
                        datetime=datetime.combine(d, datetime.min.time()),
                        interval=Interval.DAILY,
                        volume=0,
                        turnover=0,
                        open_interest=0,
                        open_price=c,
                        high_price=c,
                        low_price=c,
                        close_price=c,
                        gateway_name="PAPER_FFILL",
                    )
            strategy.on_bars(bars)
            self.last_bars = bars

        logger.info("回放完成: %d 个交易日，截止 %s", len(calendar), calendar[-1])

    # ---- 委托（即时模拟成交，无任何 gateway 路径） ----

    def send_order(self, strategy, vt_symbol: str, direction: Direction,
                   offset: Offset, price: float, volume: float,
                   lock: bool, net: bool) -> list:
        self._order_seq += 1
        vt_orderid = f"PAPER.{strategy.strategy_name}.{self._order_seq}"

        if direction == Direction.LONG:
            strategy.pos_data[vt_symbol] += int(volume)
        else:
            strategy.pos_data[vt_symbol] -= int(volume)

        self.blotter.append({
            "date": self.current_dt.isoformat() if self.current_dt else None,
            "vt_symbol": vt_symbol,
            "direction": direction.value,
            "offset": offset.value,
            "volume": int(volume),
            "price": price,
            "vt_orderid": vt_orderid,
        })
        return [vt_orderid]

    def cancel_order(self, strategy, vt_orderid: str) -> None:
        """no-op：即时成交无挂单；从活动委托集合移除即可。"""
        strategy.active_orderids.discard(vt_orderid)

    # ---- 事件 / 持久化 / 日志 ----

    def put_strategy_event(self, strategy) -> None:
        """no-op：批处理模式无事件推送。"""

    def send_notification(self, msg: str, strategy=None) -> None:
        """no-op：批处理模式不推送通知。"""

    def sync_strategy_data(self, strategy) -> None:
        """no-op：状态由 target_<date>.json 日志承担。"""

    def write_log(self, msg: str, strategy=None) -> None:
        name = strategy.strategy_name if strategy else "engine"
        logger.info("[%s] %s", name, msg)

    def get_engine_type(self) -> EngineType:
        return EngineType.LIVE


def init_rqdata():
    """初始化 RQData datafeed（与 roll_dominant_contracts.py 同模式）。"""
    from vnpy_rqdata.rqdata_datafeed import RqdataDatafeed
    datafeed = RqdataDatafeed()
    datafeed.init()
    import rqdatac  # noqa: F401
    return rqdatac


def update_contracts(runner_config: dict, strategy_config: dict) -> int:
    """用 rqdatac 主力规则生成 889/888 → live 合约映射文件。"""
    rqdatac = init_rqdata()
    contracts: dict = {}
    failed: list = []
    for vt_symbol in strategy_config["vt_symbols"]:
        m = CONTINUOUS_RE.match(vt_symbol)
        if not m:
            logger.error("无法解析连续合约代码: %s", vt_symbol)
            failed.append(vt_symbol)
            continue
        root, exchange = m.groups()
        continuous_code = vt_symbol.split(".")[0]
        try:
            dom = rqdatac.futures.get_dominant(root, rank=1)
            if dom is None:
                raise ValueError("get_dominant 返回 None")
            if hasattr(dom, "iloc"):
                dom = dom.iloc[-1]
            live_symbol = str(dom)
        except Exception as e:
            logger.error("查询主力合约失败: %s | %s", root, e)
            failed.append(vt_symbol)
            continue
        contracts[vt_symbol] = {
            "root": root,
            "exchange": exchange,
            "continuous_code": continuous_code,
            "live_symbol": live_symbol,
            "live_vt_symbol": f"{live_symbol}.{exchange}",
        }
        logger.info("%s -> %s", vt_symbol, contracts[vt_symbol]["live_vt_symbol"])

    out_path = DEPLOY_ROOT / runner_config["live_contracts_path"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "rqdatac futures.get_dominant(rank=1)",
        "contracts": contracts,
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    logger.info("已写入 %s（%d 项，失败 %d 项）", out_path, len(contracts), len(failed))
    return 1 if failed else 0


def load_strategy_class(module_path: str):
    """从策略源码目录加载 FuturesCarverMaStrategy（只读引用，不复制代码）。"""
    file_path = Path(module_path) / "strategies" / "futures_carver_ma_strategy.py"
    spec = importlib.util.spec_from_file_location("futures_carver_ma_strategy", file_path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, module_path)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(module_path)
    return module.FuturesCarverMaStrategy


def determine_end_date(db, vt_symbols: list, arg_date: str | None):
    """回放截止日：--date 指定，或数据库最新交易日（42 合约最后日期的最大值）。"""
    if arg_date:
        return datetime.strptime(arg_date, "%Y-%m-%d").date()
    probe_start = datetime.now() - timedelta(days=15)
    probe_end = datetime.now() + timedelta(days=1)
    last_dates = []
    for vt_symbol in vt_symbols:
        symbol, exch = vt_symbol.split(".")
        bars = db.load_bar_data(symbol, Exchange(exch), Interval.DAILY, probe_start, probe_end)
        if bars:
            last_dates.append(bars[-1].datetime.date())
    if not last_dates:
        raise RuntimeError("数据库近 15 天无任何日线数据")
    return max(last_dates)


def run_paper(runner_config: dict, arg_date: str | None, dry_run: bool) -> int:
    strategy_config_path = Path(runner_config["strategy_config_path"])
    with open(strategy_config_path) as f:
        strategy_config = json.load(f)
    vt_symbols = strategy_config["vt_symbols"]

    stale_warn_days = int(runner_config.get("stale_data_warn_days", 5))

    # live 合约映射（缺失或超 7 天仅警告，不阻断）
    live_map: dict = {}
    live_path = DEPLOY_ROOT / runner_config["live_contracts_path"]
    if live_path.exists():
        with open(live_path) as f:
            live_payload = json.load(f)
        live_map = live_payload.get("contracts", {})
        gen_at = datetime.fromisoformat(live_payload["generated_at"])
        if (datetime.now() - gen_at).days > 7:
            logger.warning("live_contracts.json 生成于 %s（>7 天），建议跑 --update-contracts", gen_at.date())
    else:
        logger.warning("live_contracts.json 缺失（%s），live 映射列将显示 '-'；请跑 --update-contracts", live_path)

    # 截止日
    tmp_db = get_database()
    end_date = determine_end_date(tmp_db, vt_symbols, arg_date)
    logger.info("回放截止交易日: %s", end_date)

    stale = (datetime.now().date() - end_date).days > stale_warn_days
    if stale:
        logger.warning("STALE: 数据最新日线 %s 距今超过 %d 个日历日", end_date, stale_warn_days)

    # 构造引擎与策略
    engine = FakeStrategyEngine(strategy_config, end_date, stale_warn_days)
    strategy_cls = load_strategy_class(runner_config["strategy_module_path"])

    setting = {k: strategy_config[k] for k in STRATEGY_PARAM_KEYS if k in strategy_config}
    if "capital" not in setting:
        setting["capital"] = strategy_config.get("capital", 10_000_000)
    capital = float(setting["capital"])

    strategy_name = strategy_config.get("strategy_name", "FuturesCarverMaStrategy")
    strategy = strategy_cls(engine, strategy_name, vt_symbols, setting)

    # paper 批处理：直接置 trading=True，使 send_order 走模拟成交路径
    strategy.trading = True
    strategy.on_init()
    strategy.on_start()

    # 汇总
    sizes = strategy_config["sizes"]
    rows: list = []
    gross = 0.0
    final_date = engine.current_dt
    for vt_symbol in vt_symbols:
        target = strategy.get_target(vt_symbol)
        bar = engine.last_bars.get(vt_symbol)
        close = bar.close_price if bar else None
        notional = abs(target) * close * sizes[vt_symbol] if close else 0.0
        weight = notional / capital if capital else 0.0
        gross += weight
        live = live_map.get(vt_symbol, {})
        rows.append({
            "continuous": vt_symbol,
            "live_vt_symbol": live.get("live_vt_symbol", "-"),
            "target_lots": target,
            "close": close,
            "notional": round(notional, 2),
            "weight": round(weight, 6),
        })

    nonzero = [r for r in rows if r["target_lots"] != 0]
    final_blotter = [t for t in engine.blotter if t["date"] == (final_date.isoformat() if final_date else None)]

    # 控制台输出
    print("=" * 96)
    print(f"PAPER 目标手数表  account={runner_config['account']}  date={final_date}  capital={capital:,.0f}")
    print("=" * 96)
    print(f"{'continuous':<14}{'live':<14}{'target':>8}{'close':>12}{'notional':>16}{'weight':>9}")
    for r in rows:
        print(f"{r['continuous']:<14}{r['live_vt_symbol']:<14}{r['target_lots']:>8}"
              f"{r['close']:>12}{r['notional']:>16,.0f}{r['weight']:>9.2%}")
    print("-" * 96)
    print(f"非零持仓: {len(nonzero)}/42   gross_exposure={gross:.3f}x   当日模拟成交 {len(final_blotter)} 笔（全程 {len(engine.blotter)} 笔）")
    if stale:
        print(f"*** STALE WARNING: 数据最新日线 {end_date} 距今超过 {stale_warn_days} 个日历日 ***")

    # JSON 日志
    payload = {
        "account": runner_config["account"],
        "date": final_date.isoformat() if final_date else None,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "capital": capital,
        "gross_exposure": round(gross, 6),
        "stale": stale,
        "data_last_dates": {k: v.isoformat() for k, v in engine.data_last_dates.items()},
        "targets": rows,
        "blotter_final_day": final_blotter,
        "blotter_total_fills": len(engine.blotter),
        "current_holdings": strategy.current_holdings,
    }
    if not dry_run:
        log_dir = DEPLOY_ROOT / runner_config["log_dir"]
        log_dir.mkdir(parents=True, exist_ok=True)
        out_path = log_dir / f"target_{final_date.isoformat()}.json"
        with open(out_path, "w") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        logger.info("已写入 %s", out_path)
    else:
        logger.info("dry-run：不写 JSON 日志")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="vnpy_portfoliostrategy 日度批处理 paper 运行器（PAPER ONLY）")
    parser.add_argument("--config", required=True, help="runner_config.json 路径")
    parser.add_argument("--date", default=None, help="回放截止交易日 YYYY-MM-DD（默认数据库最新）")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写 JSON 日志")
    parser.add_argument("--update-contracts", action="store_true", help="用 rqdatac 更新主力合约映射后退出")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = DEPLOY_ROOT / config_path
    with open(config_path) as f:
        runner_config = json.load(f)

    with open(runner_config["strategy_config_path"]) as f:
        strategy_config = json.load(f)

    if args.update_contracts:
        return update_contracts(runner_config, strategy_config)
    return run_paper(runner_config, args.date, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
