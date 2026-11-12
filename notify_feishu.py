"""飞书自定义机器人推送层（多账号）。

作为 vendor/signin.py 的外部包装器运行：调用上游脚本、消费其单行 JSON 输出、
转换为飞书消息。signin.py 本身保持零改动。

多账号模型
----------
上游脚本完全无状态（不写缓存、不刷新 token、不回写凭据文件），因此账号隔离
只需隔离「凭据文件」与「我们自己的状态文件」：

  ~/.workbuddy/
    feishu.conf             全局 webhook 与签名密钥（600）
    run.log                 全局运行日志（每行带账号名前缀）
    accounts/
      <名字>.info           该账号凭据（600），文件名即飞书消息里的显示名
      <名字>/               该账号的独立状态目录
        daily-ran-today     当日是否已签到
        jitter-seed         今日选定的签到小时
        jitter-seed.date    抖动 seed 的日期
        token-warned-for    已告警过的到期时间
        signin-result.log   上游 silent 模式写出的 JSON

设计要点（见 openspec design.md 决策 3、4、5）：
  * 签名密钥存于 feishu.conf，绝不出现在代码或日志中
  * 推送失败 MUST NOT 影响领取结论，也不改变原始退出码语义
  * 上游 JSON 结构变化时降级为通用文本，而非抛异常
  * 每个账号单独推送一条消息，消息首行标明账号，避免归属不清
"""

import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

# cron / 最小环境下 HOME 可能未设置（os.path.expanduser 依赖它），
# 兜底为当前用户家目录，避免因环境差异导致整轮失败。
_HOME = os.environ.get("HOME") or os.path.expanduser("~") or "/root"

WORKBUDDY_HOME = os.environ.get("WORKBUDDY_HOME") or os.path.join(_HOME, ".workbuddy")
ACCOUNTS_DIR = os.path.join(WORKBUDDY_HOME, "accounts")
FEISHU_CONF = os.environ.get("WORKBUDDY_FEISHU_CONF") or os.path.join(
    WORKBUDDY_HOME, "feishu.conf"
)
RUN_LOG = os.path.join(WORKBUDDY_HOME, "run.log")
VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "signin.py")
# 解释器路径：cron 的 PATH 不含 nvm，sys.executable 在部分调用方式下可能为空，
# 故显式回退到系统 python3 的绝对路径。
PYTHON = sys.executable or "/usr/bin/python3"

# 未做 accounts/ 迁移时，用环境变量指向的单个凭据文件，视为名为 default 的账号。
LEGACY_ACCOUNT = "default"

# 结果分类：哪些属于「空跑」，巡检时不应打扰用户
IDLE_RESULTS = {"ALREADY", "IDLE", "TRAVELING", "LIMIT_REACHED", "INACTIVE"}
# 需要人工介入的结果
ATTENTION_RESULTS = {"AUTH_REJECTED", "NO_AUTH", "NO_SESSION", "AUTH_ERROR",
                     "FORBIDDEN", "NETWORK", "TIMEOUT", "ERROR", "DECRYPT_FAILED"}
# 真实入账的两种表述：中文「+8 积分」「+共 300 积分」与原始「+credit300」
GAIN_RE = re.compile(r"\+\s*(?:共\s*)?\d+\s*积分|\+credit\d+")

# 认证类失败的可操作指引（替代上游原文中的 HTTP 细节）
AUTH_HELP = {
    "AUTH_REJECTED": "登录凭据已失效，服务端拒绝了本次请求。\n"
                     "这通常意味着 accessToken 已过期，需要重新登录并提供新的凭据文件。\n"
                     "重新获取步骤见 README「凭据过期怎么办」。",
    "AUTH_ERROR": "登录凭据无法解析或解密，需要重新提供凭据。\n"
                  "若 doctor 显示 sym-v1（加密凭据），Linux 无法解密，"
                  "需改回桌面端定时执行，详见 README。",
    "NO_AUTH": "未找到登录凭据文件，请确认已放置到 accounts/<名字>.info。",
    "NO_SESSION": "本地登录会话缺少必要字段，请在客户端重新登录后再提供凭据。",
}


def log(msg, account=None):
    """写运行日志。只记录非敏感字段，token 永不入日志。"""
    try:
        os.makedirs(WORKBUDDY_HOME, mode=0o700, exist_ok=True)
        who = "[%s] " % account if account else ""
        with open(RUN_LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), who, msg))
    except OSError:
        pass


def discover_accounts():
    """发现账号：accounts/*.info，文件名去掉 .info 即账号名（兼显示名）。

    未迁移时回退到 WORKBUDDY_AUTH_FILE 指定的单个文件，视为 default 账号。
    返回排序后的名字列表，保证推送顺序稳定。
    """
    names = []
    try:
        for fn in sorted(os.listdir(ACCOUNTS_DIR)):
            if not fn.endswith(".info"):
                continue
            if os.path.isfile(os.path.join(ACCOUNTS_DIR, fn)):
                names.append(fn[: -len(".info")])
    except OSError:
        pass
    if names:
        return names

    legacy = os.environ.get("WORKBUDDY_AUTH_FILE")
    if legacy and os.path.exists(legacy):
        return [LEGACY_ACCOUNT]
    return []


def account_paths(name):
    """返回 (凭据文件, 状态目录)。状态目录按账号隔离，避免串号。"""
    if name == LEGACY_ACCOUNT and not os.path.exists(
        os.path.join(ACCOUNTS_DIR, name + ".info")
    ):
        legacy = os.environ.get("WORKBUDDY_AUTH_FILE")
        state = os.path.join(WORKBUDDY_HOME, "state", name)
        return legacy, state
    return (
        os.path.join(ACCOUNTS_DIR, name + ".info"),
        os.path.join(ACCOUNTS_DIR, name),
    )


def load_feishu_conf():
    """读取 webhook 与 secret。

    文件缺失、权限不足或格式错误时返回 None —— 调用方据此降级为仅写日志，
    MUST NOT 中断领取动作（design.md 风险缓解）。
    """
    try:
        st = os.stat(FEISHU_CONF)
        if st.st_mode & 0o077:
            log("feishu.conf 权限过宽（%o），已拒绝使用" % (st.st_mode & 0o777))
            return None
        conf = {}
        with open(FEISHU_CONF, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip()
        if not conf.get("FEISHU_WEBHOOK"):
            log("feishu.conf 缺少 FEISHU_WEBHOOK")
            return None
        return conf
    except OSError as e:
        log("读取 feishu.conf 失败：%s" % e)
        return None


def sign(secret, timestamp):
    """HMAC-SHA256 签名：Base64(HMAC-SHA256(key="ts\\n"+secret, msg=b""))"""
    string_to_sign = "%s\n%s" % (timestamp, secret)
    return base64.b64encode(
        hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    ).decode("utf-8")


def push(text, conf=None, account=None):
    """推送一条纯文本消息。返回 True/False，永不抛异常。"""
    conf = conf or load_feishu_conf()
    if not conf:
        log("跳过推送：feishu.conf 不可用", account)
        return False
    ts = str(int(time.time()))
    payload = {"msg_type": "text", "content": {"text": text}}
    if conf.get("FEISHU_SECRET"):
        payload["timestamp"] = ts
        payload["sign"] = sign(conf["FEISHU_SECRET"], ts)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        conf["FEISHU_WEBHOOK"], data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("code") == 0 or body.get("StatusCode") == 0:
            return True
        log("飞书返回非零：code=%s msg=%s" % (body.get("code"), body.get("msg")), account)
        return False
    except (urllib.error.URLError, OSError, ValueError) as e:
        log("飞书推送失败：%s" % e, account)
        return False


def build_text(result, kind, account=None):
    """把 signin.py 的 JSON 转成人话。

    首行标明账号，使「每账号一条消息」时归属清晰。
    签到与猫猫旅行分别呈现，不混为一句笼统结论（feishu-notify spec）。
    对缺字段/结构变化的输入降级为通用文本，绝不抛异常。
    """
    label = "每日签到" if kind == "daily" else "成长中心巡检"
    head = "【%s】%s" % (account, label) if account else label

    if not isinstance(result, dict):
        return "%s\n运行输出无法解析：%r" % (head, result)

    parts = []
    # 签到
    res = result.get("result")
    credit = result.get("credit")
    if res:
        seg = "[签到] %s" % res
        if isinstance(credit, int):
            seg += "（+%d 积分）" % credit
        if result.get("streak_days"):
            seg += "，连续 %s 天" % result["streak_days"]
        if result.get("trigger") == "poll":
            seg += "（补签）"
        parts.append(seg)

    # 猫猫旅行 / 成长中心
    growth = result.get("growth") or result.get("growth_result")
    if growth:
        parts.append("[猫猫旅行] %s" % growth)

    if not parts:
        # 上游结构可能已变化，至少保留原始结果标识
        parts.append("[%s] 结果：%s" % (label, res or "UNKNOWN"))

    if result.get("needs_attention") or res in ATTENTION_RESULTS:
        parts.append("⚠️ 需要你处理")

    # 认证类失败改写为可操作指引，不直接暴露原始 HTTP 状态码或接口返回细节
    if res in ("AUTH_REJECTED", "AUTH_ERROR", "NO_AUTH", "NO_SESSION"):
        parts.append(AUTH_HELP.get(res, "登录态不可用，需重新登录后重试。"))
        # 丢弃上游原文中的 HTTP 细节，避免误导排查方向
        parts = [p for p in parts if "HTTP" not in p or "重新登录" in p]
    elif isinstance(result.get("report"), str) and result["report"]:
        parts.append(result["report"])
    return "\n".join([head] + parts)


def has_gain(result):
    """判断本轮是否真的入账。

    不能只看 result：巡检补签时 result 为 ALREADY（当日已签过），但成长中心
    仍可能领到任务奖或旅行奖励 —— 上游实测输出即为此形态（result=ALREADY
    且 growth 含「+credit300」）。反之 result 非空闲值时也可能什么都没领到。
    故以「实际入账迹象」为准。
    """
    if not isinstance(result, dict):
        return False
    credit = result.get("credit")
    if isinstance(credit, int) and credit > 0:
        return True
    for field in ("growth", "report"):
        value = result.get(field)
        if isinstance(value, str) and GAIN_RE.search(value):
            return True
    return False


def should_push(result, kind, exit_code=0):
    """daily 无条件推送；poll 仅在领到积分或出错时推送。

    result 为 None 表示上游完全无输出：silent-poll 的空跑刻意不落盘，
    此时退出码为 0 属正常空跑，不应推送（否则每账号每天会收到 3 条「无法解析」）。
    """
    if kind == "daily":
        return True
    if exit_code != 0:
        return True
    if result is None:
        return False
    if not isinstance(result, dict):
        return True
    if result.get("needs_attention") or result.get("result") in ATTENTION_RESULTS:
        return True
    if has_gain(result):
        return True
    return False


def run_signin(mode, account=None):
    """调用上游 signin.py，返回 (exit_code, parsed_json_or_None)。

    上游 silent* 模式把 JSON 写入日志文件而非 stdout（见其 emit()：pythonw 下
    sys.stdout 为 None，打到 stdout 等于扔进黑洞）。故对 silent 模式需读取
    WORKBUDDY_SIGNIN_LOG 中本次新增的行。

    signin-result.log 必须按账号隔离：多账号共享同一文件时，两个进程的追加
    会让「记录偏移量后读新增行」解析到别的账号的输出。
    """
    auth_file, state_dir = account_paths(account) if account else (
        os.environ.get("WORKBUDDY_AUTH_FILE"),
        os.environ.get("WORKBUDDY_STATE_DIR") or WORKBUDDY_HOME,
    )

    env = dict(os.environ)
    if auth_file:
        env["WORKBUDDY_AUTH_FILE"] = auth_file
    else:
        # 不传 null 给 subprocess（env 值必须是 str），也让上游走自己的探测逻辑
        env.pop("WORKBUDDY_AUTH_FILE", None)
    try:
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
    except OSError:
        pass

    log_path = os.path.join(state_dir, "signin-result.log")
    silent = mode.startswith("silent")
    offset = 0
    if silent:
        env["WORKBUDDY_SIGNIN_LOG"] = log_path
        try:
            offset = os.path.getsize(log_path)
        except OSError:
            offset = 0

    try:
        proc = subprocess.run(
            [PYTHON, VENDOR, mode],
            capture_output=True, text=True, env=env, timeout=600,
        )
    except subprocess.TimeoutExpired:
        log("signin.py %s 超时" % mode, account)
        return 124, None

    out = (proc.stdout or "").strip()
    if not out and silent:
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                f.seek(offset)
                out = f.read().strip()
        except OSError as e:
            log("读取 signin 日志失败：%s" % e, account)
            out = ""

    if not out:
        log("signin.py %s 无输出，stderr=%s" % (mode, (proc.stderr or "")[:300]), account)
        return proc.returncode, None
    try:
        # 日志行形如 "[2026-09-30 16:19:04] {...}"，取末行并剥掉前缀
        last = out.splitlines()[-1]
        if last.startswith("["):
            last = last[last.index("]") + 1:].strip()
        return proc.returncode, json.loads(last)
    except (ValueError, IndexError):
        # 结构变化或非 JSON：保留原文片段供排查，不抛异常
        log("signin.py %s 输出无法解析：%s" % (mode, out[:300]), account)
        return proc.returncode, None


def run_token_check(account=None):
    """凭据有效期检查。进入告警阈值时推送提醒（同一到期时间只推一次）。

    返回值仅用于日志；任何异常都不得影响签到本身。
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import check_token
    except ImportError as e:
        log("无法加载 check_token：%s" % e, account)
        return
    try:
        auth_path, mark_file = check_token.resolve_paths(account)
        auth = check_token.load_auth(auth_path)
        level, left = check_token.evaluate(auth)
        log("token-check level=%s days_left=%s" % (
            level, ("%.2f" % left) if left is not None else "unknown"), account)
        if not check_token.should_alert(level, auth, mark_file):
            log("token-check 无需告警", account)
            return
        text = check_token.build_alert_text(level, left, account=account,
                                            auth_path=auth_path)
        if push(text, account=account):
            log("凭据告警已推送（level=%s）" % level, account)
        else:
            log("凭据告警推送失败", account)
    except Exception as e:  # noqa: BLE001 - 告警失败绝不阻断签到
        log("token-check 异常（已忽略）：%s: %s" % (type(e).__name__, e), account)


def run_one(kind, account):
    """对单个账号执行一次 daily 或 poll，返回退出码。"""
    mode = {"daily": "auto", "poll": "silent-poll"}.get(kind, "auto")
    code, result = run_signin(mode, account)
    log("mode=%s exit=%s result=%s" % (mode, code, (result or {}).get("result")), account)

    if should_push(result, kind, code):
        text = build_text(result, kind, account)
        if push(text, account=account):
            log("已推送（%s）" % kind, account)
        else:
            # 推送失败不改变原始退出码语义
            log("推送失败，但保留原始退出码 %s" % code, account)
    else:
        log("空跑，按粒度策略不推送（%s）" % kind, account)
    return code


def parse_argv(argv):
    """解析 --account NAME。返回 (kind, account_or_None)。"""
    kind = argv[0] if argv else "daily"
    account = None
    rest = argv[1:]
    i = 0
    while i < len(rest):
        if rest[i] == "--account" and i + 1 < len(rest):
            account = rest[i + 1]
            i += 2
        elif rest[i].startswith("--account="):
            account = rest[i].split("=", 1)[1]
            i += 1
        else:
            i += 1
    return kind, account


def main():
    kind, only = parse_argv(sys.argv[1:])

    # 供 shell 消费的账号清单：一行一个名字，便于 `while read` 遍历
    if kind == "accounts":
        for name in discover_accounts():
            print(name)
        return 0

    if only:
        accounts = [only]
    else:
        accounts = discover_accounts()

    if not accounts:
        log("未发现任何账号：%s 下没有 *.info" % ACCOUNTS_DIR)
        return 0

    if kind == "token-check":
        for name in accounts:
            run_token_check(name)
        return 0

    rc = 0
    for name in accounts:
        code = run_one(kind, name)
        # 多账号时任一失败即视为整轮失败，便于 cron 日志与外部监控发现
        if code != 0 and rc == 0:
            rc = code
    return rc


if __name__ == "__main__":
    sys.exit(main())
