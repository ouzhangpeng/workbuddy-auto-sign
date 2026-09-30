"""飞书自定义机器人推送层。

作为 vendor/signin.py 的外部包装器运行：调用上游脚本、消费其单行 JSON 输出、
转换为飞书消息。signin.py 本身保持零改动。

设计要点（见 openspec design.md 决策 3、4、5）：
  * 签名密钥存于 ~/.workbuddy/feishu.conf，绝不出现在代码或日志中
  * 推送失败 MUST NOT 影响领取结论，也不改变原始退出码语义
  * 上游 JSON 结构变化时降级为通用文本，而非抛异常
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

STATE_DIR = os.environ.get("WORKBUDDY_STATE_DIR") or os.path.join(_HOME, ".workbuddy")
FEISHU_CONF = os.path.join(STATE_DIR, "feishu.conf")
RUN_LOG = os.path.join(STATE_DIR, "run.log")
VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "signin.py")
AUTH_FILE = os.path.join(STATE_DIR, "auth", "workbuddy-desktop.info")
# 解释器路径：cron 的 PATH 不含 nvm，sys.executable 在部分调用方式下可能为空，
# 故显式回退到系统 python3 的绝对路径。
PYTHON = sys.executable or "/usr/bin/python3"

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
    "NO_AUTH": "未找到登录凭据文件，请确认已放置并设置 WORKBUDDY_AUTH_FILE。",
    "NO_SESSION": "本地登录会话缺少必要字段，请在客户端重新登录后再提供凭据。",
}


def log(msg):
    """写运行日志。只记录非敏感字段，token 永不入日志。"""
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        with open(RUN_LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


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


def push(text, conf=None):
    """推送一条纯文本消息。返回 True/False，永不抛异常。"""
    conf = conf or load_feishu_conf()
    if not conf:
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
        log("飞书返回非零：code=%s msg=%s" % (body.get("code"), body.get("msg")))
        return False
    except (urllib.error.URLError, OSError, ValueError) as e:
        log("飞书推送失败：%s" % e)
        return False


def build_text(result, kind):
    """把 signin.py 的 JSON 转成人话。

    签到与猫猫旅行分别呈现，不混为一句笼统结论（feishu-notify spec）。
    对缺字段/结构变化的输入降级为通用文本，绝不抛异常。
    """
    if not isinstance(result, dict):
        return "WorkBuddy 运行输出无法解析：%r" % (result,)

    parts = []
    label = "每日签到" if kind == "daily" else "成长中心巡检"

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
    return "\n".join(parts)


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
    此时退出码为 0 属正常空跑，不应推送（否则每天会收到 3 条「无法解析」）。
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


def run_signin(mode):
    """调用上游 signin.py，返回 (exit_code, parsed_json_or_None)。

    上游 silent* 模式把 JSON 写入日志文件而非 stdout（见其 emit()：pythonw 下
    sys.stdout 为 None，打到 stdout 等于扔进黑洞）。故对 silent 模式需读取
    WORKBUDDY_SIGNIN_LOG 中本次新增的行。
    """
    env = dict(os.environ)
    env.setdefault("WORKBUDDY_AUTH_FILE", AUTH_FILE)

    # silent 模式：记录日志文件当前偏移，只读本次新增的内容
    log_path = env.get("WORKBUDDY_SIGNIN_LOG") or os.path.join(
        STATE_DIR, "signin-result.log"
    )
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
        log("signin.py %s 超时" % mode)
        return 124, None

    out = (proc.stdout or "").strip()
    if not out and silent:
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                f.seek(offset)
                out = f.read().strip()
        except OSError as e:
            log("读取 signin 日志失败：%s" % e)
            out = ""

    if not out:
        log("signin.py %s 无输出，stderr=%s" % (mode, (proc.stderr or "")[:300]))
        return proc.returncode, None
    try:
        # 日志行形如 "[2026-09-30 16:19:04] {...}"，取末行并剥掉前缀
        last = out.splitlines()[-1]
        if last.startswith("["):
            last = last[last.index("]") + 1:].strip()
        return proc.returncode, json.loads(last)
    except (ValueError, IndexError):
        # 结构变化或非 JSON：保留原文片段供排查，不抛异常
        log("signin.py %s 输出无法解析：%s" % (mode, out[:300]))
        return proc.returncode, None


def run_token_check():
    """凭据有效期检查。进入告警阈值时推送提醒（同一到期时间只推一次）。

    返回值仅用于日志；任何异常都不得影响签到本身。
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import check_token
    except ImportError as e:
        log("无法加载 check_token：%s" % e)
        return
    try:
        auth = check_token.load_auth()
        level, left = check_token.evaluate(auth)
        log("token-check level=%s days_left=%s" % (
            level, ("%.2f" % left) if left is not None else "unknown"))
        if not check_token.should_alert(level, auth):
            log("token-check 无需告警")
            return
        text = check_token.build_alert_text(level, left)
        if push(text):
            log("凭据告警已推送（level=%s）" % level)
        else:
            log("凭据告警推送失败")
    except Exception as e:  # noqa: BLE001 - 告警失败绝不阻断签到
        log("token-check 异常（已忽略）：%s: %s" % (type(e).__name__, e))


def main():
    kind = sys.argv[1] if len(sys.argv) > 1 else "daily"

    if kind == "token-check":
        run_token_check()
        return 0

    mode = {"daily": "auto", "poll": "silent-poll"}.get(kind, "auto")

    code, result = run_signin(mode)
    log("mode=%s exit=%s result=%s" % (mode, code, (result or {}).get("result")))

    if should_push(result, kind, code):
        text = build_text(result, kind)
        if push(text):
            log("已推送（%s）" % kind)
        else:
            # 推送失败不改变原始退出码语义
            log("推送失败，但保留原始退出码 %s" % code)
    else:
        log("空跑，按粒度策略不推送（%s）" % kind)

    sys.exit(code)


if __name__ == "__main__":
    main()
