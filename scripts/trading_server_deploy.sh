#!/bin/bash
# =============================================================================
# 交易服务器部署脚本
# 功能：定时拉取 cta_live_deploy 仓库，校验配置，通知重启
#
# 部署方式：
#   1. 复制本脚本到交易服务器 /opt/cta_live_deploy/scripts/
#   2. 添加到 crontab: */5 * * * * /opt/cta_live_deploy/scripts/trading_server_deploy.sh
#   3. 或配置为 systemd service
# =============================================================================

set -euo pipefail

DEPLOY_DIR="/opt/cta_live_deploy"
LOG_FILE="${DEPLOY_DIR}/logs/deploy.log"
TELEGRAM_BOT="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT="${TELEGRAM_CHAT_ID:-}"
REMOTE_BRANCH="origin/main"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log() {
    echo -e "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

send_telegram() {
    local msg="$1"
    if [[ -n "$TELEGRAM_BOT" && -n "$TELEGRAM_CHAT" ]]; then
        curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT}/sendMessage" \
            -d "chat_id=${TELEGRAM_CHAT}" \
            -d "text=${msg}" \
            -d "parse_mode=HTML" > /dev/null || true
    fi
}

check_and_deploy() {
    cd "$DEPLOY_DIR"
    mkdir -p logs

    log "🔍 检查远程更新..."

    # fetch 最新状态
    git fetch "$REMOTE_BRANCH" 2>/dev/null || {
        log "${RED}❌ git fetch 失败${NC}"
        send_telegram "⚠️ <b>交易服务器</b>\ngit fetch 失败，请检查网络或仓库配置"
        return 1
    }

    LOCAL=$(git rev-parse HEAD)
    REMOTE=$(git rev-parse "$REMOTE_BRANCH")

    if [[ "$LOCAL" == "$REMOTE" ]]; then
        log "✅ 已是最新版本 ($LOCAL)"
        return 0
    fi

    log "${YELLOW}🔄 发现更新: $LOCAL → $REMOTE${NC}"

    # 记录变更摘要
    CHANGE_SUMMARY=$(git log --oneline "$LOCAL..$REMOTE" | head -5)
    log "变更摘要:\n$CHANGE_SUMMARY"

    # 拉取更新
    git pull "$REMOTE_BRANCH" 2>&1 | tee -a "$LOG_FILE"
    log "${GREEN}✅ git pull 完成${NC}"

    # 校验配置
    log "🔍 校验配置..."
    VALIDATE_OK=true
    for cfg in configs/*/cta_strategy_setting.json; do
        if [[ ! -f "$cfg" ]]; then
            continue
        fi
        ACCOUNT=$(basename "$(dirname "$cfg")")
        if python3 scripts/validate_settings.py --config "$cfg" --quiet 2>>"$LOG_FILE"; then
            log "  ✅ $ACCOUNT: 校验通过"
        else
            log "  ${RED}❌ $ACCOUNT: 校验失败${NC}"
            VALIDATE_OK=false
        fi
    done

    if [[ "$VALIDATE_OK" != "true" ]]; then
        log "${RED}❌ 配置校验失败，执行回滚...${NC}"
        git reset --hard "$LOCAL"
        send_telegram "🚨 <b>CTA 部署失败</b>\n配置校验未通过，已自动回滚到上一版本\n\n变更:\n<pre>$CHANGE_SUMMARY</pre>"
        return 1
    fi

    log "${GREEN}✅ 配置校验全部通过${NC}"

    # 读取 manifest 信息
    MANIFEST="${DEPLOY_DIR}/deploy-manifest.json"
    if [[ -f "$MANIFEST" ]]; then
        ACCOUNTS=$(python3 -c "import json; d=json.load(open('$MANIFEST')); print(','.join(d.get('accounts', {}).keys()))")
        STRATEGY_COUNT=$(python3 -c "import json; d=json.load(open('$MANIFEST')); print(sum(a.get('strategy_count',0) for a in d.get('accounts',{}).values()))")
        log "📋 manifest: $ACCOUNTS | 共 $STRATEGY_COUNT 个策略"
    fi

    # 通知（不自动重启，由人工或 systemd 处理）
    send_telegram "✅ <b>CTA 配置已更新</b>\n交易服务器已拉取最新部署\n\n版本: <code>${REMOTE:0:8}</code>\n账户: $ACCOUNTS\n策略数: $STRATEGY_COUNT\n\n请根据情况重启策略引擎。"

    log "${GREEN}🚀 部署完成，请根据需要重启策略引擎${NC}"
    return 0
}

# 主入口
check_and_deploy
