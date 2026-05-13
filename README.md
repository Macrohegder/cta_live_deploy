# CTA Live Deploy — 实盘部署仓库

> CTA 策略**唯一实盘部署来源**。交易服务器从此仓库拉取策略代码与账户配置。

## 快速开始

### 研发服务器：构建并推送部署

```bash
cd /root/quant/cta_live_deploy

# 构建部署包（dry-run 预览）
python3 scripts/build_deploy.py --assets crypto --accounts nav,luyl --dry-run

# 正式构建并推送
python3 scripts/build_deploy.py --assets crypto --accounts nav,luyl --push
```

### 交易服务器：拉取并校验

```bash
cd /opt/cta_live_deploy

# 手动执行部署检查
bash scripts/trading_server_deploy.sh

# 或添加到 crontab（每5分钟检查）
echo "*/5 * * * * root /opt/cta_live_deploy/scripts/trading_server_deploy.sh" > /etc/cron.d/cta-deploy
```

## 目录结构

```
cta_live_deploy/
├── strategies/                  # 经校验的策略代码（唯一实盘来源）
├── configs/
│   ├── accounts.yaml            # 账户定义
│   ├── nav/cta_strategy_setting.json
│   └── luyl/cta_strategy_setting.json
├── scripts/
│   ├── build_deploy.py          # 构建部署包（研发端）
│   ├── validate_settings.py     # 配置校验（两端共用）
│   ├── sync_strategies.py       # 策略代码同步
│   └── trading_server_deploy.sh # 交易服务器部署脚本
└── deploy-manifest.json         # 部署清单
```

## 数据流

```
cta_developer 批量回测 → 单策略最优参数
         ↓
tracker 组合优化 → portfolio_opt_{account}.json（风险平价权重）
         ↓
build_deploy.py → 合并生成账户级配置 + 同步策略代码
         ↓
git commit + push → 远程仓库
         ↓
交易服务器 git pull → validate → reload
```

## 配置规则

1. **单策略参数**：来自 `cta_developer/cta_strategy_setting_{asset}.json`（OAT/GA 优化后的最优参数）
2. **组合权重**：来自 `tracker/data/portfolio_opt_{account}.json`（风险平价 + 目标波动率）
3. **账户定义**：来自 `tracker/config/accounts.yaml`（基础仓位、倍数）
4. **最终仓位**：`fixed_size = base_size × final_allocation`

## 校验规则（R1-R5）

- **R1 完整性**：setting 必须包含 strategy_class.parameters 的每一个参数名
- **R2 类型一致性**：每个参数值的类型必须与策略类默认值的类型一致
- **R3 bool 规范**：bool 参数必须使用 JSON `true`/`false`，禁止字符串
- **R4 策略存在性**：class_name 必须能在 `strategies/` 目录找到对应定义
- **R5 品种格式**：vt_symbol 必须符合 `{SYMBOL}_{TYPE}_{EXCHANGE}.{MARKET}` 格式

## 常见问题

### Q: cta_developer 中找不到某策略的优化参数？
A: 脚本会自动 fallback 到 tracker 的 `accounts.yaml` base_setting，并打印警告。建议将该策略纳入 cta_developer 批量回测以获取最优参数。

### Q: 策略代码在 cta_developer 和 tracker 中不一致？
A: `sync_strategies.py` 优先从 `cta_developer/cta/strategies/` 复制，找不到则 fallback 到 `tracker/strategies/`。长期建议对齐两个仓库的策略代码。

### Q: 如何回滚到上一版本？
A: 研发端：`git revert HEAD` + `git push`。交易端：脚本在校验失败时会自动 `git reset --hard`，也可手动执行。
