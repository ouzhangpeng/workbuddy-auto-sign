# WorkBuddy 自动签到

在无头 Linux 服务器上自动领取 WorkBuddy「Buddy 加油站」及成长中心积分，结果推送到飞书。

领取逻辑来自上游开源项目 [88lin/workbuddy-auto-signin](https://github.com/88lin/workbuddy-auto-signin)（MIT），
本仓库只做三件事：**接定时、接飞书、加告警**，不重写任何领取逻辑。

## 覆盖范围

一条 `auto` 命令即覆盖全部 18 个接口：

| 能力 | 说明 |
|------|------|
| 每日签到 | 100 分/天，先查状态后领取，幂等 |
| 派 Buddy 旅行 | 领归来奖励 + 派出新一趟，服务端每日放行一次 |
| 任务领奖 | 接单 + 领奖（任务奖励排在抽奖前，因其产出的抽奖机会/能量立即可用） |
| 断登补登 | 有补登卡时自动补，保住连登 |
| 连登兑换 | 入门/进阶/巅峰三档 |
| 抽奖 | 消耗抽奖机会 |
| 能量盲盒 | 能量攒够即开 |

## 架构

```
  crontab
    |
    +-- 5 9,10,11 * * *   daily_jitter.sh           遍历 accounts/*.info
    |     |                 (每个账号独立掷骰子选 9/10/11 点中之一)
    |     +-- notify_feishu.py token-check --account <名字>   凭据过期告警
    |     +-- notify_feishu.py daily --account <名字> --> signin.py auto --> 飞书
    |
    +-- 5 12,16,20 * * *  notify_feishu.py poll     遍历 accounts/*.info
          (固定时刻；兼作签到兜底：每轮先查状态，未签则补)
```

账号数由 `~/.workbuddy/accounts/*.info` 决定，**加账号只需放文件，不用改 crontab**。
每个账号单独推送一条消息，首行标注 `【账号名】`。

## 多账号

### 目录结构

```
~/.workbuddy/
  feishu.conf                全局 webhook 与签名密钥（600）
  run.log                    全局日志（每行带 [账号名] 前缀）
  accounts/
    账号1.info               凭据（600），**文件名即飞书消息里的显示名**
    账号1/                   该账号的独立状态目录
      daily-ran-today        当日是否已签到
      jitter-seed            今日选定的签到小时
      jitter-seed.date       抖动 seed 的日期
      token-warned-for       已告警过的到期时间
      signin-result.log      上游 silent 模式写出的 JSON
    账号2.info
    账号2/
```

状态必须按账号隔离：若共用 `daily-ran-today`，账号1 跑完后账号2 当天永远不跑；
若共用 `signin-result.log`，巡检按偏移量读新增行时会解析到别的账号的输出。

### 为什么需要「逐个登录再拷出」

CodeBuddy CLI 在一台机器上只有**一个固定凭据路径**
（Linux 为 `~/.local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info`），
登录第二个账号会**覆盖**第一个。所以收割流程是：

```bash
# 账号1：登录后拷出
codebuddy                      # 以账号1登录（首次交互登录）
mkdir -p ~/.workbuddy/accounts
cp ~/.local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info \
   ~/.workbuddy/accounts/账号1.info
chmod 600 ~/.workbuddy/accounts/账号1.info

# 账号2：退出后换账号登录，再拷出
# （退出/切换方式随 CLI 版本而异，通常是清掉上面的凭据文件后重启 codebuddy 重新登录）
codebuddy
cp ~/.local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info \
   ~/.workbuddy/accounts/账号2.info
chmod 600 ~/.workbuddy/accounts/账号2.info
```

拷出来的 `.info` 彼此独立，可长期共存。上游 `signin.py` **只读**凭据、不刷新
token、不回写文件，所以不依赖 CLI 会话仍然有效。

> 每个账号的 accessToken 仍约 30 天过期，收割流程需按账号重复。

### 加号 / 删号 / 改名

```bash
# 加号：放文件即可，下一轮 cron 自动纳入
cp <新账号.info> ~/.workbuddy/accounts/账号3.info && chmod 600 ~/.workbuddy/accounts/账号3.info

# 改名：移动凭据与状态目录（显示名随之改变）
mv ~/.workbuddy/accounts/账号3.info ~/.workbuddy/accounts/工作号.info
mv ~/.workbuddy/accounts/账号3       ~/.workbuddy/accounts/工作号

# 删号：两者一起删，避免残留状态
rm ~/.workbuddy/accounts/账号3.info && rm -rf ~/.workbuddy/accounts/账号3

# 确认当前识别到哪些账号
python3 notify_feishu.py accounts
```

账号名不要含空格（消息与日志里用作标识）。

## 文件位置

| 路径 | 用途 | 是否入仓库 |
|------|------|-----------|
| `notify_feishu.py` | 飞书推送包装器（本仓库） | 是 |
| `daily_jitter.sh` | 签到时刻抖动（本仓库） | 是 |
| `check_token.py` | 凭据有效期检查（本仓库） | 是 |
| `crontab.txt` | crontab 配置模板（本仓库） | 是 |
| `vendor/signin.py` | 上游领取逻辑 | 是（vendored） |
| `vendor/UPSTREAM_COMMIT` | 上游版本锚点 | 是 |
| `~/.workbuddy/accounts/<名字>.info` | 各账号登录凭据 | **否** |
| `~/.workbuddy/accounts/<名字>/` | 各账号状态 | **否** |
| `~/.workbuddy/feishu.conf` | webhook 与签名密钥 | **否** |
| `~/.workbuddy/run.log` | 运行日志 | 否 |

## 部署

### 1. 放置凭据（每个账号一份）

在已登录 WorkBuddy 的电脑上取登录态文件：

- macOS：`~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info`
- Windows：`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info`
- Linux：`~/.local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info`

传到服务器，**文件名即账号显示名**：

```bash
mkdir -p ~/.workbuddy/accounts && chmod 700 ~/.workbuddy ~/.workbuddy/accounts
scp workbuddy-desktop.info root@<服务器>:/root/.workbuddy/accounts/账号1.info
chmod 600 /root/.workbuddy/accounts/账号1.info
```

多个账号见上一节「多账号」。


### 2. 配置飞书

飞书群 → 群设置 → 群机器人 → 添加机器人 → 自定义机器人，复制 Webhook 地址，
并在「安全设置」中开启**签名校验**（会给你一个密钥）。

```bash
cat > ~/.workbuddy/feishu.conf <<'EOF'
FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/<你的token>
FEISHU_SECRET=<你的签名密钥>
EOF
chmod 600 ~/.workbuddy/feishu.conf
```

### 3. 安装定时任务

```bash
crontab /root/work/workbuddy-auto-sign/crontab.txt
crontab -l        # 确认两行任务
```

### 4. 手动验证

```bash
python3 notify_feishu.py accounts        # 应列出识别到的账号名
WORKBUDDY_AUTH_FILE=/root/.workbuddy/accounts/账号1.info \
  python3 vendor/signin.py doctor        # 期望 AUTH_READY / credential_format: plaintext
python3 notify_feishu.py daily --account 账号1   # 应在飞书收到以【账号1】开头的消息
python3 notify_feishu.py poll            # 遍历全部账号；仅在有入账或出错时推送
```

## 凭据过期怎么办

每个账号的凭据约 30 天过期。临近过期（默认 14 天内）飞书会自动收到告警，
**每个账号独立告警、消息带【账号名】**，无需盯时间。

告警文案里带完整重传步骤。手动处理（以「账号1」为例）：

```bash
# 1. 按上面「部署」第 1 步重新取文件并 scp 上来，覆盖同名文件
scp workbuddy-desktop.info root@<服务器>:/root/.workbuddy/accounts/账号1.info
chmod 600 /root/.workbuddy/accounts/账号1.info

# 2. 自检
WORKBUDDY_AUTH_FILE=/root/.workbuddy/accounts/账号1.info \
  python3 vendor/signin.py doctor
```

看到 `credential_format: plaintext` 即成功，告警阈值会按新凭据的 `expiresAt`
自动重算，**不需要改任何配置**。多个账号逐个重复即可。

### 如果 doctor 显示 sym-v1

说明新版客户端把 token 加密成了 `$wbEncrypted` 信封。Linux 上没有解密运行时
（上游脚本的 `find_workbuddy_runtime()` 在 Linux 返回空），本方案会报
`RUNTIME_NOT_FOUND`。

此时只能改回桌面端执行：用上游自带的 `workbuddy-auto-signin.plist.example`（macOS）
或 `install-windows.ps1`（Windows）配置定时，飞书通知仍可复用本仓库的
`notify_feishu.py`（把 `vendor/signin.py` 换成桌面端路径即可）。

## 飞书消息的含义

| 消息 | 含义 | 该做什么 |
|------|------|---------|
| `[签到] CLAIMED（+100 积分），连续 N 天` | 签到成功 | 不用管 |
| `[签到] ALREADY` | 当日已签过 | 不用管 |
| `[签到] ALREADY ... （补签）` | 巡检补签成功 | 不用管，说明 9~11 点那次没跑成 |
| `[签到] INACTIVE` | **签到活动已结束** | 等腾讯开新期，本方案会一直这样提示 |
| `⚠️ 需要你处理` + 登录凭据已失效 | 凭据过期 | 按上一节重传 |
| `⚠️ 需要你处理` + 网络不可达 | 断网 | 等下次自动重试，持续失败再查 |
| `🟡/🔴 凭据即将过期` | 提前告警 | 按上一节重传 |

推送粒度：**签到任务每账号每次都推**；**巡检只在某账号真领到积分或出错时推**，
「已签过 / 在路上 / 今日名额已用完」这类空跑不会打扰你。

多账号时每条消息首行带 `【账号名】`，例如：

```
【账号1】每日签到
[签到] CLAIMED（+8 积分），连续 3 天
[猫猫旅行] 领取任务奖励 +credit300
```

## 上游同步

`vendor/` 是**普通文件目录**（不是 submodule），因此克隆本仓库即可直接运行，无需
`git submodule update`。

比对当前 vendored 版本：

```bash
cat vendor/UPSTREAM_COMMIT
```

查看上游是否有新提交，并把更新同步进来：

```bash
# 1. 看上游变更
git clone --depth 50 https://github.com/88lin/workbuddy-auto-signin /tmp/wb-upstream
git -C /tmp/wb-upstream log --oneline

# 2. 同步文件（保留我们自己的 UPSTREAM_COMMIT）
cp /tmp/wb-upstream/signin.py vendor/signin.py
cp /tmp/wb-upstream/LICENSE vendor/LICENSE

# 3. 更新版本锚点
git -C /tmp/wb-upstream rev-parse HEAD > vendor/UPSTREAM_COMMIT

# 4. 跑上游测试确认无回归
pip3 install --target=/tmp/cbc-pytest pytest
cd vendor && PYTHONPATH=/tmp/cbc-pytest python3 -m pytest tests/ -q
```

必须在 `vendor/` 目录内运行上游测试 —— 它用裸 `import signin`，依赖当前目录在
`sys.path` 上。当前基线：**37 passed, 12 skipped**。

同步后务必跑一次 `python3 vendor/signin.py doctor`，确认接口契约未变。

> 注：早期版本曾在 `vendor/` 内保留 `.git` 以便 `git fetch`，但那会让 git 把 `vendor/`
> 当作 gitlink，克隆本仓库后将得到一个**空目录**（没有 `signin.py`），部署直接失败。
> 故改为普通文件 + `UPSTREAM_COMMIT` 锚点。

### 什么时候该怀疑上游

以下任一现象出现时，先去 [上游 issue 区](https://github.com/88lin/workbuddy-auto-signin/issues)
看是否已有报告：

- 飞书报「脚本运行异常」或所有结果都是 `ERROR`
- `doctor` 通过但所有请求都返回 `AUTH_REJECTED`（凭据没问题的情况下）
- 领取接口返回 404（说明接口路径已变）

上游 README 明确声明：签到接口系从桌面端逆向得到，腾讯改版即可能失效。

## 风险提示

- **活动期有限**：本期（第 10 期）`end_time` 为 **2026-10-15 23:59:59**。
  之后服务端返回非活动期，脚本会推送「活动已结束」而非报错 —— 这是正常状态。
- **凭据形态可能变化**：目前是明文（plaintext）。若腾讯改为加密，本方案在 Linux
  上立即失效，需迁回桌面端（见上文 sym-v1 一节）。
- **接口非公开**：全部为逆向得到的内部接口，随时可能变动。
- **个人自用**：不要用于多账号批量领取，可能触发风控。
- **webhook 泄漏**：已在飞书侧开启签名校验。即便如此，若怀疑泄漏请在群设置中重置密钥，
  然后更新 `~/.workbuddy/feishu.conf`。
