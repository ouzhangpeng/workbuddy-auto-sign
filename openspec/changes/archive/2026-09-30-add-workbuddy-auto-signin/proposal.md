# Proposal

## Why

WorkBuddy「Buddy 加油站」的积分（每日签到 100 分、派 Buddy 旅行 5~10 分）必须手动逐项点击，
漏一天就是实打实的损失，且连签天数会因此清零。这台服务器 7×24 在线、正好适合承接这类
「零散、高频、无需人工判断」的重复动作。

社区项目 [88lin/workbuddy-auto-signin](https://github.com/88lin/workbuddy-auto-signin)
（MIT，874 star）已经把全部领取逻辑逆向并实现，且在 Linux 上有用户实测通过。凭据已在本机
验证可用，接口全部返回 200。现在缺的只是：把脚本接上定时、接上飞书通知、并在凭据过期前
主动告知使用者。

## What Changes

- 引入上游 `signin.py` 作为**外部依赖直接复用，不修改、不重写任何领取逻辑**
- 新增飞书推送层：消费 `signin.py` 输出的单行 JSON，转成飞书消息
  - 签到任务（每日一次）：成功或失败**必推**
  - 巡检任务（每 4 小时）：**仅在真正领到积分或发生错误时推送**，空跑不打扰
- 新增双定时（crontab）：每日签到 + 每 4 小时成长中心巡检
- 新增凭据生命周期管理：
  - 每日检查 `accessToken` 剩余有效期，临近过期时推送飞书告警并给出重传指引
  - 凭据过期导致认证失败时，推送**明确的「需重新登录」**而非原始 HTTP 错误
- 新增活动结束识别：本期活动 `end_time` 为 2026-10-15，结束时推送明确说明而非报错
- 安全加固：`.gitignore` 排除 `*.info`（上游未覆盖此项）；webhook 与签名密钥移出仓库至
  `0600` 权限文件；凭据文件与代码目录物理隔离

**非目标**：不实现任何领取逻辑、不安装 CodeBuddy CLI、不实现 refresh token 自动续期
（凭据约 30 天过期，由使用者在 60 天宽限期内手动重传）。

## Capabilities

### New Capabilities
- `workbuddy-auto-signin`: 定时领取 WorkBuddy 加油站及成长中心积分，含每日签到、
  成长中心巡检（旅行领奖/派发、任务领奖、断登补签、连登兑换、抽奖、能量盲盒），
  以及飞书结果通知与凭据过期告警
- `feishu-notify`: 飞书自定义机器人签名推送能力，含消息构造、签名计算、
  推送粒度策略与失败降级

### Modified Capabilities
（无 —— 本项目当前没有任何既有 spec）

## Impact

**上游依赖**
- `88lin/workbuddy-auto-signin`：作为 vendored 依赖引入，其接口为命令 + 单行 JSON 输出
- 上游若因腾讯接口变更而失效，本方案的领取能力随之失效（README 已声明此风险）

**凭据**
- `workbuddy-desktop.info`：明文 JWT，已验证 `credential_format: plaintext`
- `accessToken` 有效期至 2026-10-30，`refreshToken` 至 2026-11-29
- 若未来客户端改为 `$wbEncrypted` 信封，Linux 无解密运行时，本方案需迁回桌面端

**外部服务**
- `copilot.tencent.com`：18 个内部接口（非公开、无文档）
- `open.feishu.cn`：自定义机器人 webhook（带签名校验）

**运行环境**
- 本机为无头 Linux 容器，Python 3.12.3（`/usr/bin/python3`，cron 环境同样可见），无 WorkBuddy 桌面端
- 零第三方依赖：`signin.py` 与飞书推送层均只用 Python 标准库
