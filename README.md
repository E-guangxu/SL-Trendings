# 每日热榜 · 云端自动抓取

每早 8 点（北京时间）自动抓取 Reddit 热榜和 YouTube 全球热门榜，结果自动提交回本仓库。

**不需要你的电脑开机，也不需要任何代理** —— 任务在 GitHub 的服务器上跑，那台机器本身就在墙外。

> **状态：已上线，微信通道已验证打通；定时触发于 2026-09-22 修复**
> 仓库 https://github.com/E-guangxu/SL-Trendings
> 2026-09-21 验证：run #1 抓取成功（32 秒）；run #2 端到端成功（13 秒），
> 日志中 `[notify] 微信推送 成功`，微信实际收到榜单。
>
> ⚠️ **2026-09-22 事故**：当天 08:00 没有推送。原因不是微信通道，而是**定时任务根本没被触发**——
> 原 cron `0 0 * * *` 落在 UTC 整点，这是 GitHub Actions 全球最拥堵的时刻，
> 官方明确说明此时任务会被延迟、负载过高时会被**直接丢弃**。修复见下节「定时为什么会漏跑」。

---

## 为什么不用本机跑

本机的定时任务有三道硬门槛，缺一个就跑不了：

| 条件 | 说明 |
|---|---|
| 电脑必须开机 | WorkBuddy 没有注册系统计划任务，也没有后台服务，纯靠应用进程调度 |
| WorkBuddy 必须运行 | 它不在开机启动项里，需要你自己打开 |
| 代理必须开着 | 代理是本机 `127.0.0.1:7890`，软件没启动时这个端口根本没人监听 |

而且本地数据库里**没有"补跑"机制**——错过的运行就是错过了，不会在你下次开机时补上。

放到 GitHub 上跑就完全绕开了这三条。

---

## 用法

**已经部署好了，日常什么都不用做。** 每早 8 点结果自动出现在 `data/latest.md`。

想手动跑一次：

1. 打开 https://github.com/E-guangxu/SL-Trendings/actions
2. 左侧选 `daily-hot` → 右侧 **Run workflow** → **Run workflow**
3. 约 30 秒后刷新，会出现一次新的运行记录，结果同时提交到 `data/`

### 换一台机器 / 重新部署

```bash
git clone https://github.com/E-guangxu/SL-Trendings.git
cd SL-Trendings
# 改完 scripts/hot.py 后
git add -A
git commit -m "chore: 调整抓取目标"
git push
```

推代码时的 Git 凭据存在 **Windows 凭据管理器**里（`git:https://github.com`），
不用每次输密码，也没有明文 token 文件。

---

## 改抓取目标

编辑 `scripts/hot.py` 顶部三个配置：

```python
SUBREDDITS = ["all"]              # Reddit 版块
CHANNELS = {}                     # YouTube 频道（channel_id → 显示名）
```

加 YouTube 频道时填 channel_id：

```python
CHANNELS = {
    "MrBeast": "UCX6OQ3DkcsbYNE6H8uQQuVA",
    "影视飓风": "UCxxxxxxxxxxxxxxxxxxxxxx",
}
```

channel_id 的拿法：打开频道页 → 查看网页源代码 → 搜 `channelId`（形如 `UC` 开头 24 位）。

> **注意**：Reddit 对未认证请求限流很紧，同一 IP 约每分钟只能请求一次。
> 每多写一个版块，就要把 `build_report()` 里的 `time.sleep(60)` 调得更大，否则后面的版块会返回 429。

---

## 指定话题

每天推送的结构是 **「全站榜 ＋ 你加的话题」**：先 Reddit r/all + YouTube 全球热门，再按每个话题追加一段。

> ⚠️ 语义是**添加**，不是**替换**。想只要话题、不要全站榜：手动触发时取消勾选 `keep_global`。

每个话题三段：

| 段落 | 来源 | 特点 |
|---|---|---|
| 最新新闻 | Google News RSS 搜索 | 无需 key、最稳，标题自带来源，用原话题词（中文照搜） |
| Reddit 讨论（近 24 小时） | Reddit 搜索 RSS | 中文话题自动换英文搜索词；排序必须用 `top`（实测 `hot`/`relevance` 全是小版块梗图），无结果退到「近一周 + top」 |
| YouTube 最新视频 | 搜索结果页 + 时间窗 | 日期排序参数实测不可靠，改为按时间窗从紧到松试，默认只留 30 天内（`YT_MAX_AGE_DAYS` 可调） |

**中文话题在 Reddit 上基本搜不到东西**（实测「科技」近 24 小时 0 条），所以常见中文话题内置了英文搜索词
（`科技→technology`、`美股→us stocks`、`游戏→gaming`……，共 30 个），**只作用于 Reddit 段**。
要临时加词：本机跑时设 `TOPIC_ALIAS="数码=gadgets, 摄影=photography"`。

### 用法 1：每天固定多跟几个话题（最常用）

编辑仓库里的 `config/topics.txt`，一行一个（`#` 是注释）：

```
科技
游戏
```

保存提交即可，下一次 08:05 的推送就变成「全站榜 ＋ 科技 ＋ 游戏」。
**文件里全是注释 = 只推全站榜。**

### 用法 2：临时看一次（不影响每天那次）

网页上：Actions → `daily-hot` → **Run workflow**，填两个框：

- `topics`：`AI 编程,游戏`（逗号分隔，可留空）
- `mode`：选 **adhoc**
- `keep_global`：临时查询默认不勾（只看话题）；勾上就再带一份全站榜

### 用法 3：本机一条命令

```bash
node C:\Users\13710\.workbuddy\bin\trigger-daily-hot.mjs --topics "AI 编程,游戏"
```

参数：

| 参数 | 作用 |
|---|---|
| `--topics "A,B"` | 指定话题（逗号/空格/顿号分隔） |
| `--mode daily\|adhoc` | 不给 `--topics` 时默认 `daily`；给了话题则默认 `adhoc` |
| `--keep-global` | 临时查询时也连带全站榜（**每日那次本来就带**，不用加） |
| `--no-global` | 只要话题，不要全站榜 |
| `--force` | 忽略「今天已跑过」，强制重跑 |

### `daily` 与 `adhoc` 的区别

|  | `daily`（每天那次） | `adhoc`（临时查询） |
|---|---|---|
| 受「今天已跑过」守卫约束 | 是 | **否**，想看就能看 |
| 写入位置 | `data/<日期>.md` + 更新 `latest.md` | `data/adhoc/<日期>-<话题>.md` |
| 会覆盖当日文件吗 | 会 | **不会**（所以不会把守卫状态弄脏） |
| 一定推送微信吗 | 是 | 是 |

> ⚠️ 临时查询**必须走 `adhoc`**。如果拿 `daily` 去查话题，当天早上已经跑过，会被守卫直接跳过，你会以为什么都没发生。

### 话题模式的已知限制

- **中文话题在 Reddit 上命中率低**，经常是「(无数据)」。想提升命中率就用英文关键词（`AI` 比 `AI 编程` 好）。新闻和 YouTube 两段不受影响。
- **YouTube 那个「按上传日期排序」参数（`sp=CAI=`）是坏的** —— 实测它被忽略，中文关键词下会返回一堆几年前的老教程。
  脚本改成按「时间窗从紧到松」依次试：**本周+按日期 → 本月 → 不筛**，哪一档够 3 条就用哪一档，
  并且拉回来后再按发布时间自己核一遍。报告末尾会注明这一段用的是哪个时间窗。
  档位定义在 `scripts/hot.py` 的 `YT_SP_TIERS`，想只看今天就把 `EgIIAg==`(今天) 加到最前面。
- **Google News 给的是 `news.google.com/rss/articles/...` 跳转链接**，点开会二次跳转，链接比较长。
- **每个话题要花约 30–90 秒**（主要耗在给 Reddit 留的限流间隔上），3 个话题约 3–5 分钟。话题别一次给太多。

---

## 推送到微信

每天早上抓完，除了写进 `data/`，还会把整份榜单**直接推到你微信**（走 Server酱）。

**已配置完成**：仓库 Secret `SERVERCHAN_KEY` 已写入，无需再操作。

如果要换 key 或在新仓库重新配置，在 **Settings → Secrets and variables → Actions → New repository secret** 里加一个名为 `SERVERCHAN_KEY` 的 Secret，值填你的 Server酱 SendKey。
（本机也可以用 `~/.workbuddy/bin/gh.mjs setsecret SERVERCHAN_KEY <keyfile>` 直接写，Secret 会自动做 libsodium 加密。）

- 没配这个 Secret 也能正常跑，只是不发微信（日志里会打 `[notify] 未配置 SERVERCHAN_KEY，跳过微信推送`）
- **推送失败不会让任务变红** —— 代码里做了兜底，抓到的数据照样提交。微信收不到请看 Actions 日志里的 `[notify]` 行
- Server酱免费版每天额度有限（约 5 条），本任务每天只发 1 条，够用

SendKey 的拿法：打开 https://sct.ftqq.com → 微信扫码登录 → 「SendKey」页面复制。

---

## 定时为什么会漏跑（重要，2026-09-22 实测）

有两层原因，第二层是决定性的。

### 第一层：GitHub 的 cron 本身就不精确

GitHub Actions 的 `schedule` **不是精确调度器，是"尽力而为"**。官方原文：

> The `schedule` event can be delayed during periods of high loads of GitHub Actions workflow runs.
> High load times include the start of every hour. **If the load is sufficiently high enough,
> some queued jobs may be dropped.**

**整点最堵；堵到一定程度任务被直接丢弃，而且不补跑。** 而 `0 0 * * *`（UTC 零点）
恰好是全世界最多人用的时段。

### 第二层（决定性）：GitHub 当前的 schedule 派发故障

2026-09-22 实测发现：**这个仓库的 cron 从来没派发过任何一次运行。**

| 检查项 | 结果 |
|---|---|
| 工作流 state | `active` |
| 文件在默认分支 main | ✅ |
| 仓库被禁用 / 归档 | 否 |
| `workflow_dispatch` 手动触发 | ✅ 3 秒内启动 |
| **`event=schedule` 的累计运行数** | **0** |
| 用 `*/5`（每 5 分钟）实测 35 分钟 | **仍然是 0** |

配置全对、手动能跑，但**定时这条链路完全不工作**。这与 GitHub 社区正在处理的故障一致
（[讨论 #207211](https://github.com/orgs/community/discussions/207211)，2026-09-08：
最小 `*/5` 工作流在多个仓库都完全不产生运行；有人撰文记录了这轮故障，
并说明自己的解法是"把时钟移出 GitHub"）。注意 GitHub 状态页当时仍显示 Actions "Normal"。

### 本项目的三步防护

```yaml
schedule:
  - cron: '7 0 * * *'    # 北京 08:07
  - cron: '23 0 * * *'   # 北京 08:23（备用）
  - cron: '41 0 * * *'   # 北京 08:41（备用）
```

| 防护 | 做法 | 解决什么 |
|---|---|---|
| 错峰 | 用 `:07` 而不是 `:00` | 避开最拥堵的分钟槽 |
| 冗余 | 同一小时内排三次 | 前面被丢弃，后面自动补上 |
| 不重复 | `hot.py` 里检查 `data/{今天}.md` 是否已存在，存在就跳过 | 冗余触发不会重复推送、重复提交 |

三次里**任意一次成功就够**，成功后其余的会打印 `[skip] ... 今天已经跑过了` 安静退出。

> 手动触发（Run workflow）默认带 `FORCE=true`，忽略守卫强制重跑，方便测试。
> 想确认某次运行是被什么触发的，看日志里「触发信息（诊断用）」那一步的 `event_name`。

### ✅ 真正的时钟：外部定时器（推荐）

因为上面的第二层原因，**cron 在当前不可依赖**。可靠做法是把"时钟"挪到 GitHub 之外——
用外部定时器打 `workflow_dispatch` API。这条路**实测 3 秒内启动**：

```bash
curl -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer <你的 token>" \
  -H "Content-Type: application/json" \
  https://api.github.com/repos/E-guangxu/SL-Trendings/actions/workflows/daily-hot.yml/dispatches \
  -d '{"ref":"main","inputs":{"force":"false"}}'
```

> `force: false` 很关键：这样**外部触发和 cron 谁先跑通都行**，后到的那个会自动跳过，
> 不会重复推送。

**三种挂法**：

| 方式 | 免开机 | 需要什么 |
|---|---|---|
| cron-job.org（免费）| ✅ | 注册账号，把上面的 URL + token 填进去 |
| Cloudflare Worker cron（免费）| ✅ | Cloudflare 账号 |
| 本机 Windows 计划任务 | ❌ 需开机 | 已配好：`~/.workbuddy/bin/trigger-daily-hot.cmd` |

本机那个脚本已写好并实测通过（`node ~/.workbuddy/bin/trigger-daily-hot.mjs`，
3 秒内启动运行，日志写到 `~/.workbuddy/logs/trigger-daily-hot.log`）。
它**不需要代理、也不需要 WorkBuddy 在运行**——因为 GitHub API 可以直连。唯一前提是电脑开着。

> **安全提示**：给外部服务的 token 建议用 **fine-grained token，只授权这一个仓库的
> `Actions: read and write`**，而不是全权 token。这样即使泄露，最坏情况也只是有人能触发
> 这个公开仓库的任务。

---

## 失败会主动告诉你

任务出错时会**主动推一条微信**，而不是让你自己发现今天没收到榜单：

- `scripts/hot.py` 里：所有来源都抓失败时会 `exit(1)` 让任务变红，并先推一条告警
- `daily-hot.yml` 里的 `失败时微信告警` 步骤：`if: failure()`，任何步骤出错都会推送告警（含运行链接）

这样**"没收到消息"和"任务失败"是两件事**：前者要查推送，后者微信会直接告诉你。

---

## 已知限制

- **Reddit 限流**：默认只抓一个版块（`r/all` 本身已覆盖全站热门），避免 429
- **YouTube 视频下载**：本方案只抓榜单标题和链接，不下载视频。真要在云端下载视频，yt-dlp 可能被 YouTube 的机器人检测拦下（需要 cookies 或住宅代理）；抓榜单/RSS 不受影响
- **定时精度**：GitHub 的定时任务高峰期可能延迟，极端情况下会被**直接丢弃**（详见上文「定时为什么会漏跑」）。已用错峰 + 三次冗余缓解，但做不到严格准点。需要准点就挂外部定时器
- **仓库活跃度**：GitHub 会在仓库 60 天无提交后暂停定时任务。本方案每天都提交，所以不会触发

---

## 免费额度

| 仓库类型 | Actions 额度 |
|---|---|
| Public | 完全免费，无分钟数限制 |
| Private | 每月 2000 分钟免费 |

本任务每次运行约 1 分钟，即使每天跑也只占 30 分钟/月。
