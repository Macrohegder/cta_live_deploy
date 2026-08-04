# CTA Live Deploy — 实盘部署仓库规范

> 本仓库是 CTA 策略**唯一实盘部署来源**。交易服务器从此仓库拉取策略代码与账户配置。

## 目录结构

```
cta_live_deploy/
├── strategies/                  # 经校验的策略代码（唯一实盘来源）
├── configs/
│   ├── accounts.yaml            # 账户定义（由 tracker 维护，本仓库只读使用）
│   ├── nav/cta_strategy_setting.json
│   └── luyl/cta_strategy_setting.json
├── scripts/
│   ├── build_deploy.py          # 构建部署包（唯一核心脚本）
│   ├── validate_settings.py     # 配置校验
│   ├── sync_strategies.py       # 策略代码同步
│   └── .DEPRECATED_generate_setting_from_deploy_config.py_BANNED  # 已废弃的第二套部署路径
└── deploy-manifest.json         # 部署清单
```

## 铁律

1. **策略代码唯一来源**：`strategies/` 目录中的代码必须从 `cta_developer/cta/strategies/` 原样复制，禁止手工修改，禁止自动改写 import 路径。
2. **配置唯一来源**：`configs/*/cta_strategy_setting.json` 必须通过 `build_deploy.py` 生成，禁止手工编辑。
3. **单一部署入口**：实盘部署配置必须通过 `build_deploy.py` 生成；`generate_setting_from_deploy_config.py` 已废弃并标记为 `.DEPRECATED_*_BANNED`。
4. **市场元数据不外泄**：合约乘数、收盘时间等市场元数据必须从 `data_operator` 共享配置或 `cta_engine` 读取；脚本中的硬编码默认值必须标注 `TODO` 待替换。
5. **禁止提交敏感信息**：API Key、账户密码、Bot Token 等不得进入本仓库。
6. **每次部署必须有 manifest**：`deploy-manifest.json` 记录来源 commit、生成时间、校验和。
7. **交易服务器只读**：交易服务器仅从本仓库拉取，禁止在交易服务器上修改文件后回传。

## Git 提交规范

- `config: deploy crypto strategies for nav/luyl (YYYY-MM-DD)` — 常规部署
- `feat: add NewStrategy to live deploy` — 新增策略
- `fix: correct fixed_size for XauIntradayStrategy_XAU` — 修复参数
- `chore: sync strategy code from cta_developer@abc1234` — 同步代码

:root/quant/cta_live_deploy/AGENTS.md

## 跨项目协作

| 动作 | 源头 | 目标 | 说明 |
|------|------|------|------|
| 策略代码同步 | cta_developer/cta/strategies/ | cta_live_deploy/strategies/ | 通过 sync_strategies.py 原样复制 |
| 单策略参数 | cta_developer/cta_strategy_setting_*.json | build_deploy.py 输入 | 最优参数 |
| 组合权重 | portfolio_optimizer/data/portfolio_opt_*.json | build_deploy.py 输入 | 风险平价结果 |
| 账户定义 | tracker/config/accounts.yaml | cta_live_deploy/configs/accounts.yaml | 由 tracker 维护，本仓库读取 |
| 实盘部署 | cta_live_deploy/ | 交易服务器 | git pull |

## 与其他 Agent 的协作

| 协作对象 | 关系 | 说明 |
|---------|------|------|
| `cta_developer` | 上游 | 策略源码和单策略参数来源 |
| `portfolio_optimizer` | 上游 | 组合权重来源 |
| `tracker` | 上游 | 账户定义（`accounts.yaml`）与绩效数据来源 |
| `spread_trader` | 平行 | 套利策略实盘部署不经过本仓库 |
| `data_operator` | 依赖 | 市场元数据与实盘数据异常处理 |

**边界说明**：本仓库是 CTA 策略**唯一实盘部署来源**，只负责构建、校验、同步部署包，不处理套利策略实盘部署，不独立生成账户配置或策略源码。

## 主力合约自动换月

- 脚本：`scripts/roll_dominant_contracts.py`（`--dry-run` 只看不动，`--account <id>` 指定账户）。
- 规则：以 `rqdatac.futures.get_dominant(root, rank=1)`（米筐主力规则）为准，扫描 `configs/*/cta_strategy_setting.json`，合约不一致则备份（`*.bak_<时间戳>`）后改写，并自动跑 `validate_settings.py` 校验。
- 调度：系统 crontab 每日 08:45（开盘前）执行，日志 `logs/roll_dominant_cron.log` 与 `logs/roll_dominant.log`。
- 2026-07-20 建立；当日首次全量执行，13 个账户从 2607 系列换月到 2608（股指期货 2607 于 07-17 到期）。

## 组合策略 paper 运行器（paper_cmd42_lf）

- 脚本：`scripts/run_portfolio_paper.py`（PAPER ONLY，无 gateway、无实盘下单路径），首个 `vnpy_portfoliostrategy` paper 运行器。
- 形态：日度批处理重放（与 paper_ldt / paper_rsimr_ih 同一惯例），非常驻进程；`FakeStrategyEngine` 从 ClickHouse 回放 326 根日线（889/888 连续合约），逐日调 `on_bars` 得当日目标手数。
- 配置：`configs/paper_cmd42_lf/runner_config.json`（仅 runner 层设置）；策略参数唯一来源 = `/root/quant/portfolio_strategy/configs/futures_carver_cmd42_lf.json`（只读注入，禁止复制进本仓库）。
- 校验：`scripts/validate_portfolio_settings.py configs/paper_cmd42_lf/runner_config.json`（独立于 validate_settings.py）。
- 换月：`--update-contracts` 用 rqdatac 主力规则生成 `configs/paper_cmd42_lf/live_contracts.json`（crontab 每周一 08:50）。
- 日志：`logs/portfolio_cmd42_lf/target_<date>.json`；crontab `40 23 * * 1-5` 每日运行（rq_data 23:00 更新之后）。
- 2026-08-04 建立；部署报告 `/root/quant/tasks/results/2026-08-04-cta_live_deploy-cmd42-lf-paper-deploy.md`。
