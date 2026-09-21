# 每日热榜 · 云端自动抓取

每早 8 点（北京时间）自动抓取 Reddit 热榜和 YouTube 全球热门榜，结果自动提交回本仓库。

**不需要你的电脑开机，也不需要任何代理** —— 任务在 GitHub 的服务器上跑，那台机器本身就在墙外。

> **状态：已上线** · 仓库 https://github.com/E-guangxu/SL-Trendings
> 2026-09-21 手动触发验证通过（run #1，32 秒，success，结果已自动提交回仓库）。

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

## 已知限制

- **Reddit 限流**：默认只抓一个版块（`r/all` 本身已覆盖全站热门），避免 429
- **YouTube 视频下载**：本方案只抓榜单标题和链接，不下载视频。真要在云端下载视频，yt-dlp 可能被 YouTube 的机器人检测拦下（需要 cookies 或住宅代理）；抓榜单/RSS 不受影响
- **定时精度**：GitHub 的定时任务高峰期可能延迟几分钟到半小时，这不是故障
- **仓库活跃度**：GitHub 会在仓库 60 天无提交后暂停定时任务。本方案每天都提交，所以不会触发

---

## 免费额度

| 仓库类型 | Actions 额度 |
|---|---|
| Public | 完全免费，无分钟数限制 |
| Private | 每月 2000 分钟免费 |

本任务每次运行约 1 分钟，即使每天跑也只占 30 分钟/月。
