# CTA Live Deploy — 实盘部署仓库规范

> 本仓库是 CTA 策略**唯一实盘部署来源**。交易服务器从此仓库拉取策略代码与账户配置。

## 目录结构

```
cta_live_deploy/
├── strategies/                  # 经校验的策略代码（唯一实盘来源）
├── configs/
│   ├── accounts.yaml            # 账户定义
│   ├── nav/cta_strategy_setting.json
│   └── luyl/cta_strategy_setting.json
├── scripts/
│   ├── build_deploy.py          # 构建部署包（核心脚本）
│   ├── validate_settings.py     # 配置校验
│   └── sync_strategies.py       # 策略代码同步
└── deploy-manifest.json         # 部署清单
```

## 铁律

1. **策略代码唯一来源**：`strategies/` 目录中的代码必须从 `cta_developer/cta/strategies/` 复制，禁止手工修改。
2. **配置唯一来源**：`configs/*/cta_strategy_setting.json` 必须通过 `build_deploy.py` 生成，禁止手工编辑。
3. **禁止提交敏感信息**：API Key、账户密码、Bot Token 等不得进入本仓库。
4. **每次部署必须有 manifest**：`deploy-manifest.json` 记录来源 commit、生成时间、校验和。
5. **交易服务器只读**：交易服务器仅从本仓库拉取，禁止在交易服务器上修改文件后回传。

## Git 提交规范

- `config: deploy crypto strategies for nav/luyl (YYYY-MM-DD)` — 常规部署
- `feat: add NewStrategy to live deploy` — 新增策略
- `fix: correct fixed_size for XauIntradayStrategy_XAU` — 修复参数
- `chore: sync strategy code from cta_developer@abc1234` — 同步代码

:root/quant/cta_live_deploy/AGENTS.md

## 跨项目协作

| 动作 | 源头 | 目标 | 说明 |
|------|------|------|------|
| 策略代码同步 | cta_developer/cta/strategies/ | cta_live_deploy/strategies/ | 通过 sync_strategies.py |
| 单策略参数 | cta_developer/cta_strategy_setting_*.json | build_deploy.py 输入 | 最优参数 |
| 组合权重 | portfolio_optimizer/data/portfolio_opt_*.json | build_deploy.py 输入 | 风险平价结果 |
| 账户定义 | tracker/config/accounts.yaml | cta_live_deploy/configs/accounts.yaml | 同步 |
| 实盘部署 | cta_live_deploy/ | 交易服务器 | git pull |

## 与其他 Agent 的协作

| 协作对象 | 关系 | 说明 |
|---------|------|------|
| `cta_developer` | 上游 | 策略源码和单策略参数来源 |
| `portfolio_optimizer` | 上游 | 组合权重和账户配置来源 |
| `tracker` | 上游 | 账户定义来源 |
| `spread_trader` | 平行 | 套利策略实盘部署不经过本仓库 |
| `data_operator` | 依赖 | 若发现实盘数据异常，转交 data_operator |

**边界说明**：本仓库是 CTA 策略**唯一实盘部署来源**，不处理套利策略实盘部署。
