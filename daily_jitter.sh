#!/bin/bash
# 每日签到抖动包装器。
#
# crontab 以 5 9,10,11 * * * 调用本脚本（每天 3 次触发，实际执行 1 次）。
# 首次触发时选定今天的目标小时（9/10/11）并记入 seed 文件，后续触发读标记，
# 到点才执行，否则立即退出。
#
# 降级方向：随机源不可用或 seed 写入失败时，首次触发**立即执行**，
# 使抖动失效时退化为「固定 9 点跑一次」（无害），而非当日漏跑。
#
# 详见 openspec design.md 决策 6。

# 本脚本依赖 bash（$RANDOM 与间接展开 ${!var} 在 dash 下均不可用）。
# 若被 /bin/sh 调用则自动重新以 bash 执行，避免因调用方式错误而整轮漏跑。
if [ -z "${BASH_VERSION:-}" ]; then
    exec /bin/bash "$0" "$@"
fi

set -uo pipefail

# cron 环境下 HOME 通常存在，但 set -u 下未定义即报错；此处兜底为当前用户家目录，
# 避免因环境差异导致整轮静默失败。
: "${HOME:=$(cd ~ 2>/dev/null && pwd)}"
: "${HOME:=/root}"

STATE_DIR="${WORKBUDDY_STATE_DIR:-$HOME/.workbuddy}"
SEED_FILE="$STATE_DIR/jitter-seed"
SEED_DATE="$STATE_DIR/jitter-seed.date"
RUN_MARK="$STATE_DIR/daily-ran-today"
LOG="$STATE_DIR/run.log"
VALID_HOURS="9 10 11"
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
NOTIFY="$(dirname "$SELF")/notify_feishu.py"

mkdir -p "$STATE_DIR" 2>/dev/null || true
chmod 700 "$STATE_DIR" 2>/dev/null || true

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >>"$LOG" 2>/dev/null; }

TODAY="$(date +%Y-%m-%d)"
CURRENT_HOUR="$(date +%-H)"

# 当日已执行过 -> 直接退出
if [ -f "$RUN_MARK" ] && [ "$(cat "$RUN_MARK" 2>/dev/null)" = "$TODAY" ]; then
    log "daily: 当日已执行（标记 $TODAY），跳过"
    exit 0
fi

pick_hour() {
    # $RANDOM 在 dash 下为空（/bin/sh -> dash），故此处显式用 bash 语义；
    # 调用方已用 /bin/bash 执行，这里做二次保险。
    #
    # 注意：不得使用 `set -- $VALID_HOURS` 展开位置参数——它会覆盖 $0，
    # 在经 `exec /bin/bash "$0"` 重新进入的路径下会使 ${!idx} 取到脚本名而非小时。
    local r="${RANDOM:-}"
    if [ -z "$r" ]; then
        # 随机源不可用 -> 返回空，交由首次立即执行的降级逻辑处理
        return 1
    fi
    case $(( r % 3 )) in
        0) echo 9  ;;
        1) echo 10 ;;
        2) echo 11 ;;
    esac
    return 0
}

TARGET=""
if [ -f "$SEED_FILE" ] && [ -f "$SEED_DATE" ] && [ "$(cat "$SEED_DATE")" = "$TODAY" ]; then
    TARGET="$(cat "$SEED_FILE" 2>/dev/null)"
fi

if [ -z "$TARGET" ]; then
    # 首次触发：选目标小时
    TARGET="$(pick_hour || true)"
    if [ -z "$TARGET" ]; then
        log "daily: 随机源不可用，降级为立即执行"
        echo "$TODAY" >"$RUN_MARK" 2>/dev/null || log "daily: 写标记失败（次日可能重复执行）"
        exec python3 "$NOTIFY" daily
        exit $?
    fi
    if printf '%s' "$TODAY" >"$SEED_DATE" 2>/dev/null && \
       printf '%s' "$TARGET" >"$SEED_FILE" 2>/dev/null; then
        log "daily: 选定今日目标小时 $TARGET"
    else
        # seed 写不进去 -> 不等待，立即执行
        log "daily: seed 写入失败，降级为立即执行"
        echo "$TODAY" >"$RUN_MARK" 2>/dev/null || true
        exec python3 "$NOTIFY" daily
        exit $?
    fi
fi

if [ "$CURRENT_HOUR" -ge "$TARGET" ]; then
    echo "$TODAY" >"$RUN_MARK" 2>/dev/null || log "daily: 写标记失败"
    log "daily: 到点（$CURRENT_HOUR >= $TARGET），执行签到"
    # 先查凭据有效期（进入阈值时推送提醒，失败不影响签到），再执行签到
    /usr/bin/python3 "$NOTIFY" token-check
    /usr/bin/python3 "$NOTIFY" daily
    exit $?
fi

log "daily: 未到点（$CURRENT_HOUR < $TARGET），跳过"
exit 0
