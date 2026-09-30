# Design

## Context

见 proposal.md 的 Why。此处只记录影响技术选型的既成事实与约束。

**已在本机实测确认的事实**（非推断）：

```
$ python3 signin.py doctor
{"result": "AUTH_READY", "credential_format": "plaintext"}      <- 明文，无需解密运行时

$ python3 signin.py status
HTTP 200  today_checked_in / streak_days / daily_credit / end_time

成长中心五个端点全部 200：
  buddy/travel/status  buddy/quota  lottery/chances  streak  tasks
```

**关键约束：**

1. **鉴权是单一 Bearer token，不是两套。** 早期基于第三方博客的判断（签到走
   `copilot.tencent.com` + Bearer、派猫走 `www.workbuddy.cn` + Cookie）是**错误的**。
   该博客从浏览器 DevTools 取样，把「浏览器如何发请求」误当作「API 如何鉴权」。
   `signin.py:1046` 证实全部 18 个端点同域：`copilot.tencent.com/v2/*`。
   本设计不依赖该博客。

2. **凭据形态决定了落点。** `credential_format` 为 `plaintext` 意味着 Linux 上无需
   WorkBuddy 运行时即可解密。若将来变为 `sym-v1` 信封，`find_workbuddy_runtime()`
   在 Linux 返回空，本方案立即失效，须迁回桌面端。

3. **零第三方依赖可行。** `signin.py` 与飞书推送层均仅用 Python 标准库。
   解释器 `/usr/bin/python3` 在 cron 非交互环境中可见（已实测）。

4. **Node 在 cron 中不可见**（`.bashrc` 的 nvm 仅对交互式 shell 生效），但本方案
   不需要 Node。这条约束仅对 OpenSpec CLI 的 cron 化有影响，对运行时无影响。

5. **本期活动 `end_time` 为 2026-10-15**，届时服务端将返回非活动期状态。

## Goals / Non-Goals

**Goals:**

- 复用上游领取逻辑，本项目只做「接线 + 告警」，零逻辑重写
- 凭据与密钥永不出现在仓库、日志、进程参数中
- 常见故障（凭据过期、活动结束、网络抖动）对使用者可见且可自行处置
- 全部使用 Python 标准库，无 pip install

**Non-Goals（design 层边界）：**

- 不实现 refresh token 自动续期（理由见决策 5）
- 不改动上游 `signin.py` 任何一行，包括其 bug
- 不做多账号支持
- 不保证在活动结束后继续工作（活动是外部约束）

## Decisions

### 决策 1：vendored 引入而非 pip 依赖或 submodule

**选择**：`git clone` 到项目内固定目录，作为 vendored 依赖；记录上游 commit hash。

**理由**：上游是单文件脚本、无 package 定义、MIT 协议。submodule 在服务器上会引入
额外管理负担（服务器不需要改代码，只需要能跑）。

**备选**：pip install → 该项目未发布为包。运行时 clone → 每次执行依赖网络，不可取。

**代价**：上游更新需要手动同步。缓解：README 已声明接口可能变更；本方案把「接口失效」
表现为飞书上的明确错误，而非静默失败（见决策 4）。

### 决策 2：凭据存放于 `~/.workbuddy/auth/`，与代码目录分离

**选择**：凭据与飞书配置统一放 `~/.workbuddy/`，权限 `0600`，通过 `WORKBUDDY_AUTH_FILE`
与自定义环境变量指向。目录 `700`。

**理由**：物理隔离比依赖 `.gitignore` 可靠——`.gitignore` 只在文件未被 track 时有效，
一旦误提交就形同虚设。当前凭据文件位于项目目录内（用户放置），需迁出。

**备选**：留在项目目录 + `.gitignore` → 已在设计中否决，因上游 `.gitignore` 未覆盖
`*.info`（只排除了 `.workbuddy/` 与 `signin.log`）。

**必做**：本项目 `.gitignore` 显式补 `*.info`、`*.env`、`secrets/`，形成第二道防线。

### 决策 3：飞书密钥移出代码，采用签名而非仅关键词

**选择**：`webhook` 与 `secret` 存于 `~/.workbuddy/feishu.conf`（`0600`），
脚本读取后计算 HMAC-SHA256 签名。

**理由**：本机有公网 IP 并运行 frps，webhook 泄漏后任何人都能向群内刷垃圾。
签名是飞书官方推荐的三种安全机制中最可靠的一种，且实现仅需 3 行标准库。
关键词机制仅匹配 `text` 与 `title` 字段，对富文本无效。

**备选**：仅关键词 → 泄露后仍可被任意调用。IP 白名单 → 本机出口 IP 可能变动，
且飞书侧需长期维护。

### 决策 4：推送层为独立包装器，不侵入 signin.py

**选择**：写一个 `notify_feishu.py` 包装器，调用 `signin.py`、捕获其单行 JSON、
转换为飞书消息。`signin.py` 保持零改动。

**理由**：上游 1742 行且持续更新（37 次提交）。任何 fork 都会在下次同步时产生冲突。
包装器把耦合面压缩到一个进程的 stdin/stdout。

**备选**：修改 signin.py 内置推送 → fork 成本，且违背「零改动」原则。

**推论**：包装器必须容忍 `signin.py` 的 JSON 结构变化（字段缺失时降级为通用文本，
而非抛异常）。

### 决策 5：凭据轮换采用人工重传，不实现自动 refresh

**选择**：每日比对 `accessToken.expiresAt` 与当前时间，剩余天数进入阈值时推送告警并
给出重传指引。认证失败时推送明确的「需重新登录」。

**理由**：凭据中确实存在 `refreshToken`（有效期至 2026-11-29）且 JWT 含
`offline_access` scope，CLI 代码中也有 `POST /v2{prefix}/auth/token/refresh`
（需 `X-Refresh-Token` 与 `X-Auth-Refresh-Source: plugin` 两个头）。理论上可自动续期。

但三点使其不适合作为主路径：
- 刷新响应是否回写 `.info` 文件**未经验证**；若仅更新内存，则「自动续期」在服务器上
  根本不成立（无客户端进程持有该状态）
- `X-Auth-Refresh-Source: plugin` 表明这是客户端插件身份的调用路径，服务器侧复用的
  合法性未知
- 频率极低：accessToken 约 30 天过期，refreshToken 另有约 60 天宽限期

**代价**：约每 30 天需人工介入一次。这与「60 天重传一次」的原始约定一致，且在
飞书告警中会被明确提示，不会静默中断。

**未来可升级**：若将来验证 refresh 可用且回写文件，可在不改动 specs 的前提下将
告警替换为自动刷新。

### 决策 6：签到时刻每日抖动，巡检时刻固定

**选择**：

```
5 9,10,11 * * *    daily-wrapper      <- 每日签到，3 次触发，实际执行 1 次
5 12,16,20 * * *   poll-wrapper       <- 成长中心巡检 + 签到兜底，3 次，固定时刻
```

`daily-wrapper` 的抖动逻辑：首次被触发时用 `$RANDOM` 选定今天的目标小时（9/10/11），
写入 `~/.workbuddy/jitter-seed`；后续触发读取该标记，到点则执行，否则立即退出。
当日实际执行时刻因此在 09:05 / 10:05 / 11:05 间近似均匀分布，逐日不同。

**理由（时区前提已实测）**：服务器时区为 `Asia/Shanghai (+0800)`，与腾讯按北京时间跨天
记账一致，故 9~11 点窗口内任意时刻都属当日，不存在跨天风险。

**为何用 3 次触发而非「单次触发 + 内部 sleep」**：cron 不会二次唤醒。若 09:05 触发后
wrapper 判定「还早」并退出，则当日再无执行机会。上游 `install-windows.ps1:120-122`
已实测过同类问题——`RepetitionInterval` 的重复实例错过即永久跳过，
`StartWhenAvailable` 不补跑。故必须以多个独立触发点承载抖动逻辑。

**为何把起点从 00:05 移到 09:05**：一是语义上更接近人工行为，二是避开整点的接口压力
（飞书官方文档亦建议不要在整点/半点集中请求）。延后至上午同时消除了「刚跨天、服务器
网络尚未就绪」这一在 00:05 概率最高的失败诱因。

**为何巡检时刻保持固定**：巡检由服务端状态驱动且完全幂等，固定时刻便于日志定位；
抖动对幂等操作无收益。签到抖动、巡检固定的分工是有意的。

**关键澄清——抖动不是为了防封号**：服务端可观测的只有请求时间戳与频率。一天一次的
签到在风控视角下，无论固定时刻还是抖动 150 分钟，都属「低频稀疏」同类。真正决定风险
的是请求总量，而本方案的空跑巡检仅发 1 次 GET，量级已接近人工手动。**抖动带来的是
容错而非安全**：签到主体落在 9~11 点窗口，加上 3 次固定巡检每次都会先查状态补签，
当日机会从「1 次」增至「4 次」，比固定单次触发更不容易断签。

**备选与否决理由**：

- *每天仅 1 次触发 + `sleep` 抖动* → 进程最长挂 2.5 小时，且当日无二次唤醒，必漏（已否决）
- *24 小时每小时触发、每日随机命中 1 次* → 触发面过宽，与本方案「9~11 点窗口」的
  可控边界相比无额外收益
- *巡检时刻也随机* → 对幂等操作无收益，反而损害日志可读性（已否决）

**时间预算约束**：cron 环境下 `/usr/bin/python3` 可见（已实测），但 `.bashrc` 的 nvm
不生效，故 `PATH` 一律用绝对路径。$RANDOM 依赖 bash，crontab 的 `/bin/sh` 不保证支持，
故 cron 行须显式指定 `/bin/bash` 或由 wrapper 自行取熵。

### 决策 7：巡检空跑不落盘、不推送

**选择**：沿用上游 `silent-poll` 语义——仅在领到积分或出错时记录。

**理由**：一天 7 次轮询，若每次都推送或写日志，真实信号会被淹没。

**代价**：无法从日志确认「今天确实跑过了」。缓解：投递结果另有 cron 日志与文件 mtime
可供排查；`WORKBUDDY_GROWTH_LOG_EMPTY=1` 可临时打开全量日志。

## Risks / Trade-offs

**[上游接口随时失效]** → 上游 README 已声明接口系从桌面端逆向、腾讯改版即可能失效。
缓解：失效表现为飞书上的明确错误而非静默；`validate`/状态查询与签到在同一轮内执行，
接口全挂时仍会推送告警，不会无声消失。

**[客户端改为加密凭据]** → `credential_format` 从 `plaintext` 变为 `sym-v1` 时，
Linux 无解密运行时，方案立即失效。缓解：部署后每周检查一次 `doctor` 输出并记录；
凭据更新时必然重新执行 `doctor`，可及早发现。

**[活动于 2026-10-15 结束]** → 服务端届时返回非活动期。缓解：识别为「活动已结束」并
推送明确说明，判定为正常状态而非故障。需注意：活动结束后本项目的签到能力即失效，
除非腾讯开启新一期。

**[webhook 泄漏]** → 缓解：签名校验 + 密钥移出仓库 + 不打印。webhook 本身在对话中
已明文出现过，若担心可随时在飞书群设置中重置。

**[轮换密钥误操作]** → `~/.workbuddy/feishu.conf` 权限不足或格式错误时，推送层 SHALL
降级为写日志而不中断领取（决策 4 的推论）。

**[cron 环境 PATH 缺失]** → 已实测 `/usr/bin/python3` 在 cron 中可见，Node 不可见但
不需要。缓解：crontab 中使用绝对路径，不依赖 PATH。

**[本机无 GUI，登录需浏览器]** → 本方案不涉及登录，凭据由使用者在桌面端获取后传入。
若未来需要在本机刷新凭据，将无法完成（这是选择人工重传而非自动续期的原因之一）。

**[抖动机制自身故障]** → 若 `jitter-seed` 写入失败或读取异常，可能导致当日三次触发
全部跳过。缓解：wrapper 在「首次触发且标记缺失」时**默认立即执行**（而非等待到点），
使抖动失效时退化为「固定 9 点跑一次」，与原方案等价而非漏跑。巡检的 3 次补签为第二层保障。

**[$RANDOM 在 /bin/sh 下不可用]** → Debian 的 `/bin/sh` 为 dash，不支持 `$RANDOM`。
缓解：crontab 中显式以 `/bin/bash` 调用 wrapper；wrapper 内对随机源不可用的情况
提供固定偏移兜底。

## Migration Plan

1. 将凭据从项目目录迁至 `~/.workbuddy/auth/`（`chmod 600`），设 `WORKBUDDY_AUTH_FILE`
2. 创建 `~/.workbuddy/feishu.conf`（`chmod 600`），填入 webhook 与 secret
3. 落地 `.gitignore`（含 `*.info`）
4. 编写 `notify_feishu.py` 包装器
5. 手动跑一次 `daily`，确认飞书收到消息
6. 安装 crontab 双任务，观察首日 7 次轮询
7. 确认无误后 `git init` 并提交（此时凭据已在仓库外）

**回滚**：删除 crontab 两行即可恢复原状；`signin.py` 未被修改，无代码回滚成本。

## Open Questions

- 上游 `signin.py` 的 JSON 输出字段在未来版本是否稳定（包装器已做降级防护，
  但若结构剧变需调整映射）
- 活动结束后腾讯是否开启新一期（属外部未知，不影响本设计）
