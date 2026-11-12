"""凭据有效期检查（多账号）。

按凭据自身声明的 expiresAt 与当前时间求差，SHALL NOT 依赖硬编码固定日期，
以便凭据更新后逻辑自动适用（workbuddy-auto-signin spec）。

阈值通过环境变量覆盖，便于测试与调整：
  WORKBUDDY_TOKEN_WARN_DAYS   进入该天数以内即告警（默认 14）
  WORKBUDDY_TOKEN_CRIT_DAYS   进入该天数以内即紧急告警（默认 3）
"""

import json
import os
import sys
import time

_HOME = os.environ.get("HOME") or os.path.expanduser("~") or "/root"
WORKBUDDY_HOME = os.environ.get("WORKBUDDY_HOME") or os.path.join(_HOME, ".workbuddy")
ACCOUNTS_DIR = os.path.join(WORKBUDDY_HOME, "accounts")
# 未迁移到 accounts/ 时的单账号兜底路径
AUTH_FILE = os.environ.get("WORKBUDDY_AUTH_FILE") or os.path.join(
    WORKBUDDY_HOME, "auth", "workbuddy-desktop.info"
)
MARK_FILE = os.path.join(WORKBUDDY_HOME, "token-warned-for")

WARN_DAYS = int(os.environ.get("WORKBUDDY_TOKEN_WARN_DAYS", "14") or 14)
CRIT_DAYS = int(os.environ.get("WORKBUDDY_TOKEN_CRIT_DAYS", "3") or 3)


def resolve_paths(account=None):
    """返回 (凭据文件, 告警标记文件)。账号间必须隔离，否则只有一个账号会告警。"""
    if account:
        return (
            os.path.join(ACCOUNTS_DIR, account + ".info"),
            os.path.join(ACCOUNTS_DIR, account, "token-warned-for"),
        )
    return AUTH_FILE, MARK_FILE


def days_left(auth):
    """返回 accessToken 剩余天数（float）。无法判断时返回 None。

    同时优先使用 expiresAt；缺失时退回 iat + expiresIn（秒）。
    """
    now = time.time()
    exp = auth.get("expiresAt")
    if isinstance(exp, (int, float)) and exp > 0:
        # 该字段为毫秒时间戳
        if exp > 1e11:
            exp = exp / 1000.0
        return (exp - now) / 86400.0

    expires_in = auth.get("expiresIn")
    issued = auth.get("lastRefreshTime") or auth.get("issued_at")
    if isinstance(expires_in, (int, float)) and isinstance(issued, (int, float)):
        base = issued / 1000.0 if issued > 1e11 else issued
        return (base + expires_in - now) / 86400.0
    return None


def load_auth(path=AUTH_FILE):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def evaluate(auth, warn_days=WARN_DAYS, crit_days=CRIT_DAYS):
    """返回 (level, days_left)。level: ok | warn | critical | unknown"""
    auth = (auth or {}).get("auth") or {}
    left = days_left(auth)
    if left is None:
        return "unknown", None
    if left <= 0:
        return "critical", left
    if left <= crit_days:
        return "critical", left
    if left <= warn_days:
        return "warn", left
    return "ok", left


def mark_path_for(auth):
    """用 token 的到期时间作为标记键：换凭据后自动重置告警，无需改配置。"""
    exp = (auth or {}).get("auth", {}).get("expiresAt", "")
    return "%s" % exp


def should_alert(level, auth, mark_file=MARK_FILE):
    """同一到期时间只告警一次，避免每日重复打扰；换凭据后自动重新告警。"""
    if level in ("ok", "unknown"):
        return False
    key = mark_path_for(auth)
    try:
        with open(mark_file, "r", encoding="utf-8") as f:
            if f.read().strip() == key:
                return False
    except OSError:
        pass
    try:
        d = os.path.dirname(mark_file) or "."
        os.makedirs(d, mode=0o700, exist_ok=True)
        with open(mark_file, "w", encoding="utf-8") as f:
            f.write(key)
    except OSError:
        pass
    return True


def build_alert_text(level, left, account=None, auth_path=AUTH_FILE):
    """构造告警文案。给出可操作的重传指引，且不含 token 本身。"""
    head = "🔴 凭据已过期，签到已停止" if (
        level == "critical" and left is not None and left <= 0
    ) else ("🔴 凭据即将过期" if level == "critical" else "🟡 凭据即将过期")
    if account:
        head = "【%s】%s" % (account, head)
    lines = [head]
    if left is not None:
        if left <= 0:
            lines.append("已过期 %.1f 天，该账号所有领取动作将返回认证失败。" % abs(left))
        else:
            lines.append("剩余 %.1f 天，请在过期前重新提供该账号的登录态。" % left)
    lines.append("")
    lines.append("重新获取步骤：")
    lines.append("  1. 在已登录 WorkBuddy 的电脑上取登录态文件：")
    lines.append("     macOS   ~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info")
    lines.append("     Windows %LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\workbuddy-desktop.info")
    lines.append("  2. 传到服务器对应账号位置（文件名即账号名）：")
    lines.append("     scp <你的文件> root@<服务器>:%s" % (
        auth_path if os.path.isabs(auth_path) else
        os.path.join(ACCOUNTS_DIR, "%s.info" % (account or "<账号名>"))))
    lines.append("  3. 修正权限并自检：")
    lines.append("     chmod 600 %s" % os.path.join(
        ACCOUNTS_DIR, "%s.info" % (account or "<账号名>")))
    lines.append("     WORKBUDDY_AUTH_FILE=%s \\" % os.path.join(
        ACCOUNTS_DIR, "%s.info" % (account or "<账号名>")))
    lines.append("       python3 /root/work/workbuddy-auto-sign/vendor/signin.py doctor")
    lines.append("  看到 credential_format 为 plaintext 即成功。")
    lines.append("")
    lines.append("注意：一台机器同时只能登录一个账号，多账号需逐个登录后分别拷出，")
    lines.append("详见 README「多账号」。若 doctor 显示 sym-v1（加密凭据），Linux")
    lines.append("无法解密，需改回桌面端定时执行。")
    return "\n".join(lines)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    account = None
    i = 0
    while i < len(argv):
        if argv[i] == "--account" and i + 1 < len(argv):
            account = argv[i + 1]
            i += 2
        elif argv[i].startswith("--account="):
            account = argv[i].split("=", 1)[1]
            i += 1
        else:
            i += 1

    auth_path, mark_file = resolve_paths(account)
    auth = load_auth(auth_path)
    level, left = evaluate(auth)
    who = "【%s】" % account if account else ""
    print("%slevel=%s days_left=%s" % (
        who, level, ("%.2f" % left) if left is not None else "unknown"))
    if should_alert(level, auth, mark_file):
        print(build_alert_text(level, left, account=account, auth_path=auth_path))
        return 10
    return 0


if __name__ == "__main__":
    sys.exit(main())
