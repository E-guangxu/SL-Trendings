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

## 定时为什么会漏跑（重要）

GitHub Actions 的 `schedule` **不是精确调度器，是"尽力而为"**。官方原文：

> The `schedule` event can be delayed during periods of high loads of GitHub Actions workflow runs.
> High load times include the start of every hour. **If the load is sufficiently high enough,
> some queued jobs may be dropped.**

翻译过来就是：**整点最堵；堵到一定程度，排队的任务会被直接丢掉**，而且**不会补跑**。

而 `0 0 * * *`（UTC 零点）恰好是全世界最多人用的时段——几乎所有人的"每日任务"都写在这个点上。

### 本项目的三重防护

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

三次触发里**任意一次成功就够**，成功后其余的会打印 `[skip] ... 今天已经跑过了` 安静退出。

> 手动触发（Run workflow）默认带 `FORCE=true`，会忽略这个守卫强制重跑，方便测试。

### 还嫌不稳？用外部定时器

如果某天要求"必须准点"（比如要抢时效性内容），可以挂一个外部定时器去打 `workflow_dispatch` API。
`workflow_dispatch` 走的是实时事件通道，不经过那个会丢任务的批量 cron 调度器，秒级触发：

```bash
curl -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer <你的 token>" \
  https://api.github.com/repos/E-guangxu/SL-Trendings/actions/workflows/daily-hot.yml/dispatches \
  -d '{"ref":"main"}'
```

免费方案：cron-job.org、Cloudflare Worker 的 cron trigger。本项目目前没接，靠上面三重防护。
（顺带一提，`gh.mjs dispatch daily-hot.yml` 就是干这件事的。）

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
