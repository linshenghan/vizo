#!/bin/bash
# ===== Claude Code Router 版本管理工具 =====
# 用法: ./git_helper.sh [命令]

set -e
cd /home/linshengbing/.claude-code-router

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# 项目验证函数
check_project_files() {
    python3 ~/.claude-code-router/v3/git_commit_guard.py /home/linshengbing/.claude-code-router
    return $?
}

case "$1" in
    save)
        MSG="${2:-自动保存}"
        TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
        git add -A

        # 提交前检查项目归属
        if ! check_project_files 2>/dev/null; then
            echo -e "${RED}提交已取消：检测到跨项目文件${NC}"
            exit 1
        fi

        git commit -m "[$TIMESTAMP] $MSG" || echo -e "${YELLOW}没有需要提交的更改${NC}"
        echo -e "${GREEN}✅ 已保存: $MSG${NC}"
        ;;
    list)
        echo -e "${GREEN}===== 提交历史（最近 20 条）=====${NC}"
        git log --oneline -20 --decorate
        ;;
    diff)
        echo -e "${GREEN}===== 未提交的改动 =====${NC}"
        git status --short
        echo ""
        git diff --stat
        ;;
    rollback)
        N=${2:-1}
        echo -e "${YELLOW}⚠️ 即将回滚到前 $N 个版本${NC}"
        echo "当前版本: $(git log -1 --oneline)"
        TARGET=$(git log --oneline -$((N+1)) | tail -1)
        echo "目标版本: $TARGET"
        echo ""
        read -p "确认回滚? (y/N): " confirm
        if [[ "$confirm" == "y" || "$confirm" == "Y" ]]; then
            BACKUP_BRANCH="backup_$(date +%Y%m%d_%H%M%S)"
            git branch "$BACKUP_BRANCH"
            echo -e "${GREEN}已创建备份分支: $BACKUP_BRANCH${NC}"
            git reset --hard HEAD~$N
            echo -e "${GREEN}✅ 回滚完成${NC}"
            echo "当前版本: $(git log -1 --oneline)"
        else
            echo "已取消"
        fi
        ;;
    rollback-to)
        if [ -z "$2" ]; then
            echo -e "${RED}请指定提交 hash${NC}"
            exit 1
        fi
        HASH="$2"
        echo -e "${YELLOW}⚠️ 即将回滚到指定版本${NC}"
        echo "当前版本: $(git log -1 --oneline)"
        echo "目标版本: $(git log -1 --oneline $HASH)"
        read -p "确认回滚? (y/N): " confirm
        if [[ "$confirm" == "y" || "$confirm" == "Y" ]]; then
            BACKUP_BRANCH="backup_$(date +%Y%m%d_%H%M%S)"
            git branch "$BACKUP_BRANCH"
            git reset --hard "$HASH"
            echo -e "${GREEN}✅ 回滚完成${NC}"
        else
            echo "已取消"
        fi
        ;;
    status)
        echo -e "${GREEN}===== 当前状态 =====${NC}"
        echo "分支: $(git branch --show-current)"
        echo "最新提交: $(git log -1 --oneline 2>/dev/null || echo '无提交')"
        echo ""
        git status --short
        ;;
    check)
        echo -e "${GREEN}===== 项目文件检查 =====${NC}"
        check_project_files
        ;;
    help|*)
        echo "Claude Code Router 版本管理工具"
        echo ""
        echo "  save [描述]        保存当前状态"
        echo "  list               查看提交历史"
        echo "  diff               查看未提交改动"
        echo "  rollback [n]       回滚到前 n 个版本"
        echo "  rollback-to [hash] 回滚到指定提交"
        echo "  status             查看当前状态"
        echo "  check              检查暂存区文件是否属于本项目"
        ;;
esac
