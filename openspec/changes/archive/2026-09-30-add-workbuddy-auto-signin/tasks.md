# Tasks

## 1. 安全基线与凭据隔离

- [x] 1.1 创建 `.gitignore`，包含 `*.info`、`*.env`、`secrets/`、`__pycache__/`、
      `*.pyc`、`signin.log`；验证 `git check-ignore workbuddy-desktop.info` 命中该文件
- [x] 1.2 创建 `~/.workbuddy/`（权限 `700`）及 `~/.workbuddy/auth/` 子目录；
      验证 `stat -c '%a' ~/.workbuddy` 为 `700`
- [x] 1.3 将 `workbuddy-desktop.info` 从项目目录迁至 `~/.workbuddy/auth/`
      （权限 `600`）；验证项目目录下不再存在 `*.info`，且 `python3 signin.py doctor`
      以 `WORKBUDDY_AUTH_FILE` 指向新路径后仍返回 `credential_format: plaintext`
- [x] 1.4 创建 `~/.workbuddy/feishu.conf`（权限 `600`），写入 webhook 与 secret；
      验证文件权限为 `600`，且在项目目录内 grep webhook 与 secret 的实际取值均无命中

## 2. 上游依赖引入

- [x] 2.1 将 `88lin/workbuddy-auto-signin` clone 到 `vendor/` 并记录当前 commit hash
      至 `vendor/UPSTREAM_COMMIT`；验证 `git -C vendor rev-parse HEAD` 与记录一致
- [x] 2.2 验证 `python3 vendor/signin.py doctor` 返回 `AUTH_READY`、
      `python3 vendor/signin.py status` 返回 HTTP 200；
      确认全程无第三方依赖缺失报错（`python3 -c "import urllib.request, hmac, base64"` 通过）

## 3. 飞书推送层

- [x] 3.1 实现签名计算与 webhook 调用（`vendor_notify.py`）：以
      `timestamp + "\n" + secret` 做 HMAC-SHA256 再 Base64，POST JSON；
      验证对无效签名飞书返回非 0 code 且被记为推送失败而非异常抛出
- [x] 3.2 实现凭据/配置读取：从 `~/.workbuddy/feishu.conf` 读 webhook 与 secret，
      文件缺失或权限不足时降级为写日志并继续，**不中断领取动作**；
      验证 `chmod 000` 该文件后运行，领取结果仍如实产出且日志含失败原因
- [x] 3.3 实现消息构造：分别呈现签到与猫猫旅行状态，`needs_attention` 为真时明确标注；
      验证对 `CLAIMED` / `ALREADY` / `AUTH_REJECTED` / `INACTIVE` 四种 JSON 输入
      各产出可读文本，且不含任何 token 片段
- [x] 3.4 实现 JSON 结构降级：`signin.py` 输出缺字段或非 JSON 时，产出通用文本而非抛异常；
      验证以 `echo 'garbage'` 与 `echo '{}'` 分别喂入，wrapper 仍退出码 0 并记录降级原因
- [x] 3.5 实现推送粒度：`daily` 无条件推送；`poll` 仅在领到积分或出错时推送；
      验证 poll 传入「已签过 / 在路上 / 名额已用尽」三种结果时飞书均未收到消息
- [x] 3.6 端到端验证：对真实 webhook 发送一条测试消息；
      验证返回 `{"code":0,"msg":"success"}` 且飞书群内实际收到

## 4. 签到抖动与巡检兜底

- [x] 4.1 实现 `daily-wrapper`：首次触发时用 `$RANDOM` 选定目标小时（9/10/11）写入
      `~/.workbuddy/jitter-seed`，后续触发读标记到点才执行；
      验证同一天连续手动调用 3 次仅执行 1 次，且跨天重置
- [x] 4.2 实现随机源不可用时的降级：`$RANDOM` 为空（dash 环境）或 seed 写入失败时，
      首次触发**立即执行**而非等待到点；验证 `dash -c 'echo $RANDOM'` 为空的情况下
      首次调用即执行，且当日不重复
- [x] 4.3 验证签到兜底：手动删除当日签到状态后运行 `poll` 包装器，
      确认其补签成功且推送中带有补签标识
- [x] 4.4 验证活动结束识别：以构造的 `end_time` 已过期状态运行，
      确认推送为「活动已结束」且退出码为 0（不标记为故障）

## 5. 定时任务安装

- [x] 5.1 写入 crontab：以 `/bin/bash` 显式调用（`/bin/sh` 为 dash，无 `$RANDOM`），
      签到 `5 9,10,11 * * *`、巡检 `5 12,16,20 * * *`，解释器用绝对路径 `/usr/bin/python3`；
      验证 `crontab -l` 显示五行且无 PATH 依赖
- [x] 5.2 验证抖动生效：连续 3 天检查 `~/.workbuddy/run.log` 中实际执行时刻，
      确认落在 9/10/11 点中且逐日不同
- [x] 5.3 验证空跑无副作用：确认 poll 空跑时不写日志、不推送（除
      `WORKBUDDY_GROWTH_LOG_EMPTY=1` 外），且每日网络请求总量不高于人工手动量级

## 6. 凭据过期告警

- [x] 6.1 实现有效期检查：读取 `accessToken.expiresAt` 与当前时间求差，
      **相对阈值判断，不硬编码日期**；验证将 `expiresAt` 改为 10 天后触发告警推送，
      改回 60 天后不再推送
- [x] 6.2 实现认证失败的明确提示：接口返回 401 时推送「凭据已过期，需重新登录」
      及重传指引，而非原始 HTTP 错误；验证推送文本不含 token 与完整响应体
- [x] 6.3 验证凭据更新后自动恢复：替换为新凭据后，确认告警阈值按新 `expiresAt`
      重新计算，无需修改任何配置

## 7. 文档

- [x] 7.1 编写 `README.md`：部署步骤、crontab 说明、凭据重传流程（含具体命令与
      `WORKBUDDY_AUTH_FILE` 写法）、飞书告警的含义与处置方式；
      验证文档中每条命令均可在本机复制执行
- [x] 7.2 记录上游同步方式（依据 `vendor/UPSTREAM_COMMIT`）与失效判据
      （接口报错时先查上游 issue）；验证按文档步骤可完成一次上游版本比对
- [x] 7.3 记录本期活动 `end_time` 为 2026-10-15，并说明结束后预期行为
      （推送「活动已结束」而非报错）；验证该日期与 `status` 接口返回值一致
