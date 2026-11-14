#!/usr/bin/env python3
"""每日热榜抓取。

两种模式：
  1) 全站榜（默认）：Reddit r/all + kworb YouTube 全球榜 + 关注频道更新
  2) 话题模式：指定若干话题，每个话题抓「最新新闻 + Reddit 讨论 + YouTube 最新视频」

话题从哪来（优先级从高到低）：
  - 环境变量 TOPICS，逗号/空格/顿号分隔
  - 仓库里的 config/topics.txt，一行一个（# 开头是注释）

指定话题的三种用法：
  - 想让每天的推送都跟你的话题走：改 config/topics.txt 并提交
  - 临时看一次：手动触发 workflow，把话题填进 topics 输入框（mode 选 adhoc）
  - 本机一条命令：node trigger-daily-hot.mjs --topics "AI 编程,游戏"

设计用于 GitHub Actions（runner 本身在境外，不需要任何代理）。
本地调试时设代理：set SCRAPE_PROXY=http://127.0.0.1:7890
本地调试想跳过推送：不要设 SERVERCHAN_KEY 即可。

只依赖 Python 标准库，无需 pip install。
"""

import os
import re
import html
import json
import sys
import time
import socket
import datetime
import pathlib
import urllib.request
import urllib.parse
import urllib.error

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
PROXY = os.environ.get("SCRAPE_PROXY", "").strip()


def _port_open(host, port, timeout=0.4):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


# 启动时先探一下代理端口。Clash 没开时的老行为是：海外源「连代理失败 → 退回直连 →
# 被墙 → 20s 超时 ×2 重试」，一个 URL 就能耗掉 40s，一轮 200s 以上甚至拖过 10 分钟窗口。
# 探到端口不通就别再试海外源，直接跳过并在报告里写明。
PROXY_DEAD = False
if PROXY:
    _hp = PROXY.split("//")[-1]
    _ph, _, _pp = _hp.partition(":")
    PROXY_DEAD = not _port_open(_ph or "127.0.0.1", int(_pp or "80"))
    if PROXY_DEAD:
        print("[warn] 代理 %s 连不上：海外源（Google News / YouTube）本轮跳过，国内榜走直连" % _hp)

# 单次请求超时 / 重试次数。云端 runner 直连、抖动少，用默认 40s×3 稳；
# 本地每 10 分钟跑一次时要压在 2~3 分钟内完成，所以看板那条链路会调小（见 hot-board.cmd）。
FETCH_TIMEOUT = int(os.environ.get("FETCH_TIMEOUT", "").strip() or "40")
FETCH_RETRIES = int(os.environ.get("FETCH_RETRIES", "").strip() or "3")

# ---- 模式与话题 ----
# daily = 每天那次，受「今天已跑过」守卫约束，写 data/<date>.md 并更新 latest.md
# adhoc = 临时查询，不受守卫约束，只写 data/adhoc/ 不覆盖当日文件，永远推送
MODE = (os.environ.get("MODE", "").strip().lower() or "daily")
TOPICS_ENV = os.environ.get("TOPICS", "").strip()
# 话题模式下是否仍然附带全站榜（1/true 开启）。
# 每日那次由 workflow 兜底设为 true —— 即「加话题」= 全站榜之外再追加，
# 不是把全站榜换掉；手动触发时以勾选框为准。
KEEP_GLOBAL = os.environ.get("KEEP_GLOBAL", "").strip().lower() in ("1", "true", "yes", "on")

# 中文话题在 Reddit 这类英文源上几乎搜不到东西（实测「科技」近 24 小时 0 条），
# 所以给常见话题配一个英文搜索词，**只用于 Reddit 段**；
# 新闻（Google News 中文版）和 YouTube 仍用原词。
#
# 用 `subreddit:xxx` 版块限定写法，比裸关键词准得多 —— 实测同一个话题：
#   `us stocks`            → 6 条全是噪声（猫、ADHD 药、食品银行）
#   `subreddit:stocks`     → 6 条全是 r/stocks 的真股票讨论 ✅
# 版块名如果写错也别怕：reddit_search() 会退回用版块名当关键词再搜一次。
# 想覆盖：环境变量 TOPIC_ALIAS="科技=technology, 数码=gadgets"
TOPIC_ALIASES = {
    "科技": "subreddit:technology",
    "AI": "subreddit:artificial",
    "人工智能": "subreddit:artificial",
    "编程": "subreddit:programming",
    "游戏": "subreddit:gaming",
    "电竞": "subreddit:esports",
    "美股": "subreddit:stocks",
    "港股": "subreddit:HKstocks",
    "A股": "subreddit:China_Stock",
    "财经": "subreddit:economics",
    "加密货币": "subreddit:CryptoCurrency",
    "比特币": "subreddit:Bitcoin",
    "手机": "subreddit:smartphones",
    "数码": "subreddit:gadgets",
    "汽车": "subreddit:cars",
    "新能源": "subreddit:electricvehicles",
    "机器人": "subreddit:robotics",
    "航天": "subreddit:space",
    "军事": "subreddit:Military",
    "影视": "subreddit:movies",
    "综艺": "subreddit:television",
    "音乐": "subreddit:music",
    "体育": "subreddit:sports",
    "足球": "subreddit:soccer",
    "篮球": "subreddit:nba",
    "健身": "subreddit:fitness",
    "旅行": "subreddit:travel",
    "美食": "subreddit:food",
    "健康": "subreddit:health",
    "教育": "subreddit:education",
    "宠物": "subreddit:aww",
    "时尚": "subreddit:femalefashionadvice",
}


def _load_alias_overrides():
    out = {}
    for pair in os.environ.get("TOPIC_ALIAS", "").split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


ALIAS_OVERRIDES = _load_alias_overrides()


def topic_query(topic):
    """话题在英文源（Reddit）上搜索时用的词。"""
    return ALIAS_OVERRIDES.get(topic) or TOPIC_ALIASES.get(topic, topic)

# ---- 中文圈热点（国内榜单）----
# 实测 2026-09-22：这四个接口在**海外出口**（GitHub runner 的美国 IP）下也能拿到数据：
#   微博热搜 52 条 / 抖音热榜 50 条 / B站排行 100 条 / 头条热榜 50 条
# 但 B站 会间歇性返回 code=-352（风控），所以逐源容错：单源失败只跳过它自己，不影响整体。
CN_HOT = (os.environ.get("CN_HOT", "").strip().lower() or "on") not in ("0", "false", "no", "off")
CN_HOT_LIMIT = int(os.environ.get("CN_HOT_LIMIT", "").strip() or "10")

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TOPICS_FILE = REPO_ROOT / "config" / "topics.txt"

# ---- 微信推送（Server酱）----
# 在 GitHub 仓库 Settings → Secrets and variables → Actions 里配 SERVERCHAN_KEY。
# 本地调试可以临时 set SERVERCHAN_KEY=xxx。留空则跳过推送。
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "").strip()
# 单条消息正文上限（Server酱 Turbo 是 32KB，留点余量）
MAX_PUSH_BYTES = 30000

# ---- 本地看板（2026-11-13 新增）----
# 设了 BOARD_PATH 就不再推送微信，改成把报告渲染成一个自动刷新的 HTML 页面。
# 用途：人守在电脑前时看热点 —— 没有条数额度、不会被 32KB 截断、能点链接。
# 由计划任务每 10 分钟重跑一次覆盖它，页面自己每 60 秒 reload。
BOARD_PATH = os.environ.get("BOARD_PATH", "").strip()

# 冗余触发的守卫：workflow 在同一小时内排了多个时间点互为备份（GitHub 的 cron
# 在整点高负载时会被延迟甚至直接丢弃）。设 FORCE=1 忽略"今天已跑过"的检查强制重跑。
FORCE = os.environ.get("FORCE", "").strip().lower() in ("1", "true", "yes", "on")

# 本地跑的时候，Clash 的出口 IP 被 Reddit 硬限流（实测每条都是 429，
# 换 old.reddit 也是 302 跳走），重试只会浪费 20s+40s 的退避。
# 所以给一个开关：SKIP_REDDIT=1 时整段跳过，报告里写明原因。
# 云端 runner（美国 IP）不需要这个开关。
SKIP_REDDIT = os.environ.get("SKIP_REDDIT", "").strip().lower() in ("1", "true", "yes", "on")

# ---- 高频模式（每 10 分钟一档，2026-11-13 新增）----
# 与 daily 的三点区别：
#   1) 不走「当天已跑过」守卫 —— 每档都要真跑
#   2) 推送压成「速报体」（单条装得下）。全量报告约 33KB 会被拆成两条，
#      一天 144 档 × 2 条 = 288 条，纯属浪费额度
#   3) 只写 data/frequent.md 一份覆盖文件，不写按日文件、不动 latest.md，
#      免得把 daily 的守卫状态弄脏
FREQUENT = MODE == "frequent"

# 静默时段：默认北京 8 点到 23 点。凌晨每 10 分钟一条微信没人受得了。
# 格式 "8-23"；想全天就把 ACTIVE_HOURS 设成 "0-24"。
ACTIVE_HOURS_RAW = os.environ.get("ACTIVE_HOURS", "").strip() or "8-23"


def bj_now():
    """runner 上系统时区是 UTC，所有对用户可见的时间都换算成北京时间。"""
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)


_T0 = time.time()


def plog(msg):
    """带累计耗时的进度行。高频档每 10 分钟一轮，
    没有进度打印的话「卡在哪儿」只能靠猜 —— 这一步是给排查用的。"""
    print("[%6.1fs] %s" % (time.time() - _T0, msg), flush=True)


def in_active_hours():
    try:
        a, b = [int(x) for x in ACTIVE_HOURS_RAW.split("-")]
    except Exception:
        a, b = 8, 23
    h = bj_now().hour
    return a <= h < b

# ---- 想追的 Reddit 版块（仅全站榜模式用）----
# 注意：Reddit 对未认证请求限流很紧（同一 IP 约每分钟 1 次）。
# 每多写一个版块，就要把间隔调大，否则会 429。
# r/all 本身已覆盖全站热门，一般一个就够。
SUBREDDITS = [] if SKIP_REDDIT else ["all"]

# ---- 想追的 YouTube 频道：填 channel_id（形如 UCxxxxxxxxxxxxxxxxxxxxxx）----
# 获取办法：打开频道的"关于"页 → 分享 → 复制频道 ID；
# 或打开频道页源码搜 "channelId"
CHANNELS = {
    # "MrBeast": "UCX6OQ3DkcsbYNE6H8uQQuVA",
}

# 话题模式下每个话题取多少条
TOPIC_NEWS_LIMIT = 6
TOPIC_REDDIT_LIMIT = 6
TOPIC_YT_LIMIT = 6
# 同一个话题内、以及话题之间，给 Reddit 留的间隔（未认证请求限流很紧）
REDDIT_GAP = int(os.environ.get("REDDIT_GAP", "").strip() or "20")
# YouTube「最新视频」怎么取：sp 参数是 YouTube 的搜索筛选位。
# 实测（2026-09-22 逐个对照）：
#   CAI=            想按上传日期排序 → **被忽略**，中文关键词下返回一堆几年前的老教程
#   EgIIAg==        今天     → 真的只有当天
#   EgIIAw==        本周     → 真的只有一周内
#   EgIIBA==        本月
#   CAISBAgDEAE=    本周 + 按上传日期排序 → 最符合「最新」，首选
# 所以按「时间窗从紧到松」依次试，哪一档拿到足够条数就用哪一档，
# 而不是只用单一参数（之前的写法就是这么被老视频混进来的）。
YT_SP_TIERS = [
    ("CAISBAgDEAE=", 7),   # 本周 + 按日期排序
    ("EgIIBA==", 30),      # 本月
    ("", 30),              # 不设筛选，靠下面的年龄过滤兜底
]
YT_MIN_ITEMS = 3           # 某一档拿到这么多条就算够用


def make_opener():
    if PROXY:
        host = PROXY.split("//")[-1]
        handler = urllib.request.ProxyHandler(
            {"http": "http://" + host, "https": "http://" + host}
        )
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()


OPENER = make_opener()
# 直连兜底：本地跑的时候代理（Clash）可能没开，此时国内四榜本来能直连，
# 不该因为代理挂着就整页抓空。云端 runner 上 PROXY 为空，这两个是同一个，不会多打一次。
#
# 注意 ProxyHandler({}) 这个空表**不能省**：build_opener() 不带参数时会读
# HTTP_PROXY / HTTPS_PROXY 环境变量，而宿主进程（WorkBuddy）恰好把这两个变量
# 指向它自己的内部代理端口。那个端口会随会话变化、随时失效，于是"直连兜底"
# 实际是去连另一个死代理 —— 表现为国内四个榜单全挂、单轮跑 200~300 秒。
OPENER_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def open_bytes(req, timeout=FETCH_TIMEOUT):
    """先走代理（配了的话），失败退回直连；代理已探到没开就直接直连。

    所有出网请求都该走这里 —— 国内四个榜单原本只走代理、没有兜底，
    Clash 一关就整片「抓取失败 / 403」，明明直连是通的。"""
    if OPENER is OPENER_DIRECT or PROXY_DEAD:
        with OPENER_DIRECT.open(req, timeout=timeout) as resp:
            return resp.read()
    try:
        with OPENER.open(req, timeout=timeout) as resp:
            return resp.read()
    except Exception:
        req2 = urllib.request.Request(req.full_url, headers=dict(req.header_items()))
        with OPENER_DIRECT.open(req2, timeout=timeout) as resp:
            return resp.read()


def fetch(url, timeout=FETCH_TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return open_bytes(req, timeout).decode("utf-8", "ignore")


def fetch_retry(url, retries=FETCH_RETRIES, backoff=4, timeout=FETCH_TIMEOUT):
    """带重试的 fetch。Google News 会偶发 SSL 中断
    （实测 `[SSL: UNEXPECTED_EOF_WHILE_READING]`），重试基本就好，
    别让一次抖动把整段新闻变成「抓取失败」。"""
    last = None
    for attempt in range(retries):
        try:
            return fetch(url, timeout=timeout)
        except Exception as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    raise last


# ---- 话题档位：主 / 辅 ----
# topics.txt 里可以给话题标档位：`AI@主`、`科技@辅`。
#   主 = 排在版面最前（在「中文圈热点」之前），且每个来源多取一些
#   辅 = 排在后面做补充，条数减半
# 不标注 = 老行为（各 6 条），位置跟以前的顺序一致。
# 为什么要分档：光改顺序不够 —— 4 个话题各 6 条时，AI 只占四分之一版面，
# 说是「为主」但看起来不为主。档位把顺序和篇幅一起改了。
TIER_ORDER = {"主": 0, "辅": 1, "": 2}
TIER_LIMITS = {
    "主": {"news": 8, "reddit": 6, "yt": 8},
    "辅": {"news": 4, "reddit": 3, "yt": 4},
    "": {"news": TOPIC_NEWS_LIMIT, "reddit": TOPIC_REDDIT_LIMIT, "yt": TOPIC_YT_LIMIT},
}


def _parse_topic_token(token):
    """把 `AI@主` / `科技:辅` / `游戏` 拆成 (名称, 档位)。"""
    t = token.strip()
    m = re.match(r"^(.*?)\s*[@:：]\s*(主|辅|primary|secondary)$", t, re.I)
    if not m:
        return t, ""
    tier = {"primary": "主", "secondary": "辅"}.get(m.group(2).lower(), m.group(2))
    return m.group(1).strip(), tier


def load_topic_specs():
    """话题来源：环境变量 TOPICS 优先，其次 config/topics.txt。

    返回 [{"name": "AI", "tier": "主"}, ...]，主档排前面，同档内保持原顺序。"""
    parts = []
    if TOPICS_ENV:
        parts = re.split(r"[,，、;；\s]+", TOPICS_ENV)
    else:
        try:
            raw = TOPICS_FILE.read_text(encoding="utf-8")
        except Exception:
            return []
        for line in raw.splitlines():
            line = line.split("#")[0].strip()
            if line:
                parts.extend(re.split(r"[,，、;；]+", line))
    seen, out = set(), []
    for p in parts:
        name, tier = _parse_topic_token(p)
        if name and name not in seen:
            seen.add(name)
            out.append({"name": name, "tier": tier})
    out.sort(key=lambda s: TIER_ORDER.get(s["tier"], 2))
    return out


def load_topics():
    """只要名字的老接口（从 specs 派生，保持向后兼容）。"""
    return [s["name"] for s in load_topic_specs()]


def reddit_entries(xml, limit):
    """Reddit 的 .rss 条目解析（热榜和搜索结果格式相同）。"""
    out = []
    for entry in re.findall(r"<entry>([\s\S]*?)</entry>", xml)[:limit]:
        t = re.search(r"<title>([\s\S]*?)</title>", entry)
        l = re.search(r'<link href="([^"]*)"', entry)
        title = html.unescape(t.group(1).strip()) if t else "(无标题)"
        out.append((title, l.group(1) if l else ""))
    return out


def _reddit_fetch(url, retries=3):
    """Reddit 对未认证请求限流很紧，被抓到就退避重试。注意必须带浏览器 UA，
    否则 403（实测：带 UA 200 / 不带 403）。"""
    xml, last_exc = None, None
    for attempt in range(retries):
        try:
            xml = fetch(url)
            break
        except Exception as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(20 * (attempt + 1))
    if xml is None:
        raise last_exc
    return xml


def reddit_hot(sub, limit=10):
    """Reddit 版块热榜。注意：.json 已被反爬，必须走 .rss。"""
    return reddit_entries(_reddit_fetch("https://www.reddit.com/r/%s/hot/.rss" % sub), limit)


def reddit_search(query, limit=TOPIC_REDDIT_LIMIT):
    """按话题搜 Reddit，先「近一天 + 票数最高(top)」，没结果退到「近一周 + top」。
    实测（2026-09-22）：
      sort=top / t=day    → r/technology 的真实热点，可用 ✅
      sort=hot / t=day    → 全是小版块的梗图 ❌
      sort=relevance/t=day→ 杂（股票版、游戏版都混进来）❌
    话题词会被 topic_query() 换成英文或 `subreddit:xxx` 版块限定（中文词在 Reddit 上搜不到）。"""

    def grab(sort, t, q, n):
        url = "https://www.reddit.com/search.rss?" + urllib.parse.urlencode(
            {"q": q, "sort": sort, "t": t, "limit": n}
        )
        items = reddit_entries(_reddit_fetch(url), n)
        # search.rss 会把版块首页（/r/xxx/，不含 /comments/）也当成结果塞进来，
        # 那不是帖子，滤掉。
        return [it for it in items if "/comments/" in it[1]][:limit]

    n = max(limit * 2, 10)
    items = grab("top", "day", query, n)
    if not items:
        time.sleep(REDDIT_GAP)
        items = grab("top", "week", query, n)
    if not items and query.startswith("subreddit:"):
        # 版块名可能不存在（或该版块近一周没热帖），退一步拿版块名当关键词搜
        time.sleep(REDDIT_GAP)
        items = grab("top", "week", query.split(":", 1)[1], n)
    return items


def kworb_trending(limit=15):
    """YouTube 全球热门榜。聚合站是纯 HTML，无需登录，最稳"""
    page = fetch_retry("https://kworb.net/youtube/trending_overall.html", retries=2)
    rows = re.findall(
        r'href="/youtube/trending/video/([A-Za-z0-9_\-]{6,})\.html">([^<]+)</a>', page
    )
    out, seen = [], set()
    for vid, title in rows:
        if vid in seen:
            continue
        seen.add(vid)
        out.append((html.unescape(title).strip(), "https://youtu.be/" + vid))
        if len(out) >= limit:
            break
    return out


def yt_channel_latest(channel_id, limit=5):
    """频道最新视频。官方 RSS 不需要登录，比 yt-dlp 稳"""
    xml = fetch("https://www.youtube.com/feeds/videos.xml?channel_id=%s" % channel_id)
    out = []
    for entry in re.findall(r"<entry>([\s\S]*?)</entry>", xml)[:limit]:
        t = re.search(r"<title>([\s\S]*?)</title>", entry)
        l = re.search(r'<link rel="alternate" href="([^"]*)"', entry)
        p = re.search(r"<published>([^<]*)</published>", entry)
        out.append(
            (
                html.unescape(t.group(1).strip()) if t else "(无标题)",
                l.group(1) if l else "",
                p.group(1)[:10] if p else "",
            )
        )
    return out


def gnews_topic(query, limit=TOPIC_NEWS_LIMIT):
    """Google News RSS 搜索。无需 key、无需登录，是最稳的话题新闻源。
    标题形如「标题 - 来源」，保持原样更有信息量。"""
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, "hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"}
    )
    xml = fetch_retry(url)
    out = []
    for item in re.findall(r"<item>([\s\S]*?)</item>", xml)[:limit]:
        t = re.search(r"<title>([\s\S]*?)</title>", item)
        l = re.search(r"<link>([\s\S]*?)</link>", item)
        d = re.search(r"<pubDate>([\s\S]*?)</pubDate>", item)
        title = html.unescape(t.group(1).strip()) if t else "(无标题)"
        pub = d.group(1).strip()[:16] if d else ""
        out.append((title, l.group(1).strip() if l else "", pub))
    return out


# 发布时间文本 → 天数。YouTube 会按界面语言给「17 小時前」或「17 hours ago」，
# 繁体/简体都可能出现，所以单位要覆盖全。
_AGE_UNITS = [
    ("秒", 1.0 / 86400), ("second", 1.0 / 86400),
    ("分鐘", 1.0 / 1440), ("分钟", 1.0 / 1440), ("分", 1.0 / 1440), ("minute", 1.0 / 1440),
    ("小時", 1.0 / 24), ("小时", 1.0 / 24), ("hour", 1.0 / 24),
    ("天", 1.0), ("day", 1.0),
    ("週", 7.0), ("周", 7.0), ("week", 7.0),
    ("個月", 30.0), ("个月", 30.0), ("month", 30.0),
    ("年", 365.0), ("year", 365.0),
]


def age_days(text):
    """把「1 天前」「4 年前」解析成天数；解析不出来返回 None。"""
    if not text:
        return None
    m = re.search(
        r"(\d+)\s*(秒|分鐘|分钟|分|小時|小时|天|週|周|個月|个月|年|second|minute|hour|day|week|month|year)",
        text,
        re.I,
    )
    if not m:
        return None
    n = int(m.group(1))
    u = m.group(2).lower()
    for key, days in _AGE_UNITS:
        if key in u:
            return n * days
    return None


def _runs_text(node):
    """ytInitialData 里的文本有两种形态：runs[{text}] 或 simpleText。"""
    if not isinstance(node, dict):
        return ""
    if node.get("runs"):
        return "".join(r.get("text", "") for r in node["runs"])
    return node.get("simpleText", "") or ""


def _yt_search_raw(query, sp, cap):
    """抓一页搜索结果，返回 [(title, url, pub, views)]。官方没有话题 RSS，
    只能解析搜索结果页里的 ytInitialData。"""
    params = {"search_query": query}
    if sp:
        params["sp"] = sp
    page = fetch_retry("https://www.youtube.com/results?" + urllib.parse.urlencode(params), retries=2)
    m = re.search(r"var ytInitialData = (\{[\s\S]*?\});</script>", page)
    if not m:
        raise RuntimeError("结果页里找不到 ytInitialData（可能被换了模板或弹出同意页）")
    data = json.loads(m.group(1))

    raw = []

    def walk(node):
        if len(raw) >= cap:
            return
        if isinstance(node, dict):
            vr = node.get("videoRenderer")
            if isinstance(vr, dict):
                vid = vr.get("videoId") or ""
                title = _runs_text(vr.get("title"))
                if vid and title:
                    raw.append(
                        (
                            title,
                            "https://youtu.be/" + vid,
                            _runs_text(vr.get("publishedTimeText")),
                            _runs_text(vr.get("viewCountText")),
                        )
                    )
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data.get("contents", {}))
    if not raw:
        raise RuntimeError("ytInitialData 里没有 videoRenderer")
    return raw


def yt_search_recent(query, limit=TOPIC_YT_LIMIT):
    """按话题找「最新视频」。

    返回 (items, note)。items 每项 (title, url, pub, views)。
    note 是人类可读的说明，写进报告里，让你知道这一段的时效范围。
    发布时间解析不出数字的一律保留（宁多勿漏）。
    """
    cap = max(limit * 4, 20)
    best = None       # 任何一档里筛出来的最好结果
    last_raw = None   # 全都筛空时，退回到原始结果
    last_err = None

    for sp, max_age in YT_SP_TIERS:
        try:
            raw = _yt_search_raw(query, sp, cap)
        except Exception as exc:
            last_err = exc
            continue
        last_raw = raw
        fresh = [it for it in raw if age_days(it[2]) is None or age_days(it[2]) <= max_age]
        if len(fresh) >= YT_MIN_ITEMS:
            return fresh[:limit], "近 %d 天内" % max_age
        if best is None or len(fresh) > len(best[0]):
            best = (fresh, max_age)

    if best and best[0]:
        return best[0][:limit], "近 %d 天内（该话题近期视频较少）" % best[1]
    if last_raw:
        return last_raw[:limit], "未找到近期视频，以下为相关度排序（可能较旧）"
    raise last_err or RuntimeError("YouTube 搜索没拿到任何结果")


def global_sections(lines):
    """全站榜：Reddit r/all + YouTube 全球热门 + 关注频道。"""
    for idx, sub in enumerate(SUBREDDITS):
        if idx:
            time.sleep(60)  # Reddit 限流，版块之间必须留足间隔
        lines.append("## Reddit · r/%s" % sub)
        try:
            items = reddit_hot(sub, 10)
            if items:
                for i, (t, link) in enumerate(items, 1):
                    lines.append("%d. [%s](%s)" % (i, t, link))
            else:
                lines.append("(无数据)")
        except Exception as exc:
            lines.append("(抓取失败: %s)" % exc)
        lines.append("")

    lines.append("## YouTube 全球热门")
    if PROXY_DEAD:
        lines.append("(代理未开，海外源已跳过)")
    else:
        try:
            items = kworb_trending(15)
            for i, (t, link) in enumerate(items, 1):
                lines.append("%d. [%s](%s)" % (i, t, link))
        except Exception as exc:
            lines.append("(抓取失败: %s)" % exc)
    lines.append("")

    if CHANNELS:
        lines.append("## 关注频道更新")
        for name, cid in CHANNELS.items():
            lines.append("### %s" % name)
            try:
                for i, (t, link, pub) in enumerate(yt_channel_latest(cid), 1):
                    lines.append("%d. %s · %s — %s" % (i, pub, t, link))
            except Exception as exc:
                lines.append("(抓取失败: %s)" % exc)
            lines.append("")


def _cn_num(v):
    """把热度数字写成「320.9万」这种可读形式。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ""
    if v >= 1e8:
        return "%.1f亿" % (v / 1e8)
    if v >= 1e4:
        return "%.0f万" % (v / 1e4)
    return str(int(v))


def _cn_weibo(j, limit):
    out = []
    for x in ((j.get("data") or {}).get("realtime") or []):
        word = (x.get("word") or "").strip()
        if not word:
            continue
        tag = x.get("label_name") or x.get("icon_desc") or ""
        num = x.get("num")
        meta = " · ".join(v for v in (tag, ("热 " + _cn_num(num)) if num else "") if v)
        out.append(
            (word, "https://s.weibo.com/weibo?q=%s" % urllib.parse.quote("#" + word + "#"), meta)
        )
        if len(out) >= limit:
            break
    return out


def _cn_douyin(j, limit):
    out = []
    for x in (j.get("word_list") or []):
        word = (x.get("word") or "").strip()
        if not word:
            continue
        hv = x.get("hot_value")
        out.append(
            (
                word,
                "https://www.douyin.com/search/%s" % urllib.parse.quote(word),
                ("热度 " + _cn_num(hv)) if hv else "",
            )
        )
        if len(out) >= limit:
            break
    return out


def _cn_bili(j, limit):
    out = []
    for x in ((j.get("data") or {}).get("list") or []):
        title = (x.get("title") or "").strip()
        if not title:
            continue
        owner = (x.get("owner") or {}).get("name") or ""
        views = (x.get("stat") or {}).get("view") or 0
        meta = " · ".join(v for v in (owner, ("播放 " + _cn_num(views)) if views else "") if v)
        bvid = x.get("bvid") or ""
        out.append(
            (title, "https://www.bilibili.com/video/%s" % bvid if bvid else "", meta)
        )
        if len(out) >= limit:
            break
    return out


def _cn_toutiao(j, limit):
    out = []
    for x in (j.get("data") or []):
        title = (x.get("Title") or "").strip()
        if not title:
            continue
        hv = x.get("HotValue")
        # 头条给的 URL 后面挂着一长串跟踪参数（实测每条 700+ 字节，
        # 10 条就把消息撑大 7KB），只保留 /trending/<id>/ 这一段。
        link = (x.get("Url") or "").split("?")[0]
        out.append((title, link, ("热度 " + _cn_num(hv)) if hv else ""))
        if len(out) >= limit:
            break
    return out


# 顺序就是推送里的顺序：微博 → 抖音 → B站 → 头条
CN_SOURCES = [
    {
        "label": "微博热搜",
        "url": "https://weibo.com/ajax/side/hotSearch",
        "referer": "https://weibo.com/",
        "parse": _cn_weibo,
    },
    {
        "label": "抖音热榜",
        "url": "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/",
        "referer": "https://www.douyin.com/",
        "parse": _cn_douyin,
    },
    {
        "label": "B站全站排行",
        "url": "https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all",
        # ranking 接口会间歇性返回 code=-352（风控，实测约一半概率中招）。
        # 两道保险：① 先访问主页拿 cookie 带上（不带时几乎必挂）
        #           ② 仍失败就退到「热门视频」接口（返回结构一样，共用同一个解析器）
        "referer": "https://www.bilibili.com/",
        "parse": _cn_bili,
        "cookie": True,
        "fallback": "https://api.bilibili.com/x/web-interface/popular?ps=20&pn=1",
    },
    {
        "label": "今日头条热榜",
        "url": "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
        "referer": "https://www.toutiao.com/",
        "parse": _cn_toutiao,
    },
]

_bili_cookie_cache = {"v": None}


def _bili_cookie():
    """访问一次 B站主页，把它发的 cookie 原样带上再调接口。"""
    if _bili_cookie_cache["v"] is not None:
        return _bili_cookie_cache["v"]
    jar = ""
    try:
        req = urllib.request.Request(
            "https://www.bilibili.com/",
            headers={"User-Agent": UA, "Accept": "text/html,*/*"},
        )
        # 这里要读响应头里的 Set-Cookie，所以不能直接用 open_bytes
        if PROXY_DEAD or OPENER is OPENER_DIRECT:
            resp = OPENER_DIRECT.open(req, timeout=20)
        else:
            try:
                resp = OPENER.open(req, timeout=20)
            except Exception:
                resp = OPENER_DIRECT.open(req, timeout=20)
        with resp:
            jar = "; ".join(
                c.split(";")[0].strip() for c in (resp.headers.get_all("Set-Cookie") or [])
            )
    except Exception as exc:
        print("[cn] B站 cookie 预热失败: %s" % exc)
    _bili_cookie_cache["v"] = jar
    return jar


def _cn_json(url, referer, cookie=None):
    headers = {
        "User-Agent": UA,
        "Accept": "application/json,text/html,*/*",
        "Referer": referer,
    }
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    return json.loads(open_bytes(req, timeout=30).decode("utf-8", "ignore"))


def _cn_items(src, limit, retries=2):
    """抓单个国内榜单。主接口失败会退到 fallback（配了的话）。
    每个 URL 重试 retries 次 —— B站 的 -352 是概率性的，重试常有奇效。"""
    urls = [src["url"]] + ([src["fallback"]] if src.get("fallback") else [])
    last = None
    for url in urls:
        for attempt in range(retries):
            try:
                cookie = _bili_cookie() if src.get("cookie") else None
                j = _cn_json(url, src["referer"], cookie)
                if isinstance(j, dict) and j.get("code") not in (None, 0):
                    raise RuntimeError("接口返回 code=%s" % j.get("code"))
                items = src["parse"](j, limit)
                if not items:
                    raise RuntimeError("解析为空（接口可能改版）")
                return items
            except Exception as exc:
                last = exc
                if attempt < retries - 1:
                    time.sleep(3)
    raise last


def cn_sections(lines):
    """中文圈热点：国内四个榜单，逐源容错 —— 挂一个不影响其他。"""
    lines.append("## 中文圈热点")
    lines.append("")
    for src in CN_SOURCES:
        lines.append("### %s" % src["label"])
        n = 0
        try:
            for i, (t, link, meta) in enumerate(_cn_items(src, CN_HOT_LIMIT), 1):
                n = i
                if link:
                    lines.append("%d. [%s](%s)%s" % (i, t, link, (" · " + meta) if meta else ""))
                else:
                    lines.append("%d. %s%s" % (i, t, (" · " + meta) if meta else ""))
        except Exception as exc:
            lines.append("(抓取失败: %s)" % exc)
        plog("中文圈 · %s：%s" % (src["label"], ("%d 条" % n) if n else "失败"))
        lines.append("")


def topic_sections(lines, specs):
    """话题段落：每个话题 = 最新新闻 + Reddit 讨论 + YouTube 最新视频。

    specs 是 load_topic_specs() 的返回（带档位）；主档每段多取几条。"""
    for idx, spec in enumerate(specs):
        topic, tier = spec["name"], spec["tier"]
        lim = TIER_LIMITS.get(tier, TIER_LIMITS[""])
        if idx:
            time.sleep(REDDIT_GAP)
        plog("开始话题 %s（%s）…" % (topic, tier or "常"))
        lines.append("## 话题：%s%s" % (topic, ("（%s）" % tier) if tier else ""))
        lines.append("")

        lines.append("### 最新新闻")
        if PROXY_DEAD:
            lines.append("(代理未开，海外源已跳过)")
        else:
            try:
                items = gnews_topic(topic, lim["news"])
                if items:
                    for i, (t, link, pub) in enumerate(items, 1):
                        lines.append("%d. [%s](%s) · %s" % (i, t, link, pub))
                else:
                    lines.append("(无数据)")
            except Exception as exc:
                lines.append("(抓取失败: %s)" % exc)
        lines.append("")

        if not SKIP_REDDIT:
            time.sleep(REDDIT_GAP)
        rq = topic_query(topic)
        lines.append(
            "### Reddit 讨论（近 24 小时）" + ("" if rq == topic else "　搜索词：%s" % rq)
        )
        try:
            if SKIP_REDDIT:
                raise RuntimeError("已跳过：本地出口被 Reddit 限流 429，云端版照常抓")
            items = reddit_search(rq, lim["reddit"])
            if items:
                for i, (t, link) in enumerate(items, 1):
                    lines.append("%d. [%s](%s)" % (i, t, link))
            else:
                lines.append("(无数据)")
        except Exception as exc:
            lines.append("(抓取失败: %s)" % exc)
        lines.append("")

        time.sleep(5)
        lines.append("### YouTube 最新视频")
        if PROXY_DEAD:
            lines.append("(代理未开，海外源已跳过)")
        else:
            try:
                items, note = yt_search_recent(topic, lim["yt"])
                for i, (t, link, pub, views) in enumerate(items, 1):
                    meta = " · ".join(x for x in (pub, views) if x)
                    lines.append("%d. [%s](%s)%s" % (i, t, link, (" · " + meta) if meta else ""))
                if note:
                    lines.append("")
                    lines.append("(%s)" % note)
            except Exception as exc:
                lines.append("(抓取失败: %s)" % exc)
        lines.append("")
        plog("话题 %s 完成" % topic)


def build_report(specs):
    today = datetime.date.today().isoformat()
    lines = ["# 每日热榜 · %s" % today, ""]

    if specs:
        label = " / ".join(
            ("%s（%s）" % (s["name"], s["tier"])) if s["tier"] else s["name"] for s in specs
        )
        lines.append("> 关注范围：%s" % label)
        lines.append("")

    primary = [s for s in specs if s["tier"] == "主"]
    others = [s for s in specs if s["tier"] != "主"]

    # 主话题顶到最前面：一眼先看到 AI，而不是先划过大盘榜单才找到。
    if primary:
        topic_sections(lines, primary)

    # 中文圈热点：国内榜单是每天第二眼要看的东西。
    # 临时查询（adhoc + 指定话题）时不带 —— 那次的目的就是看这个话题，别塞一堆大盘进来。
    if CN_HOT and not (MODE == "adhoc" and specs):
        cn_sections(lines)

    # 辅话题跟在中文圈后面，只做补充。
    if others:
        topic_sections(lines, others)

    if specs and not KEEP_GLOBAL:
        return "\n".join(lines)

    global_sections(lines)
    return "\n".join(lines)


def split_report(body, limit):
    """把报告切成若干块，每块 ≤ limit 字节，切点尽量落在 `## ` 段边界上。

    为什么要切：内容全开（中文圈热点 + 多话题 + 全站榜）实测约 33KB，
    超过 Server酱 单条正文上限，截断会把最后几段整段丢掉。
    宁可多发一条，也别丢内容。"""
    blocks, cur = [], []
    for line in body.split("\n"):
        if line.startswith("## ") and cur:
            blocks.append("\n".join(cur))
            cur = [line]
        else:
            cur.append(line)
    if cur:
        blocks.append("\n".join(cur))

    chunks, buf = [], ""
    for b in blocks:
        cand = (buf + "\n" + b) if buf else b
        if buf and len(cand.encode("utf-8")) > limit:
            chunks.append(buf)
            buf = b
        else:
            buf = cand
    if buf:
        chunks.append(buf)
    return chunks


def compact_report(body, keep=4, keep_main=None):
    """把完整报告压成「速报体」：每个清单只留前 keep 条。

    只是**展示层**裁剪 —— 数据仍然是全量抓的，只是不全推。
    目的：让一条消息装得下。否则全量 33KB 每档拆成两条，一天 288 条，
    既浪费额度又把手机刷屏。

    keep_main：主话题段（`## 话题：AI（主）`）单独放宽。
    既然 AI 是「为主」，就不能和配角的段落一样只留 4 条。"""
    keep_main = keep if keep_main is None else keep_main
    out, cur = [], keep
    for line in body.split("\n"):
        m = re.match(r"^##\s+(.*)", line)
        if m:
            cur = keep_main if "（主）" in m.group(1) else keep
            out.append(line)
            continue
        m = re.match(r"^(\d+)\. ", line)
        if m and int(m.group(1)) > cur:
            continue
        out.append(line)
    return "\n".join(out)


def _md_inline(text):
    s = html.escape(text, quote=False)
    s = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        lambda m: '<a href="%s" target="_blank" rel="noopener">%s</a>'
        % (m.group(2), m.group(1)),
        s,
    )
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    return s


# ---- 看板：新增内容高亮 ----
# 每轮抓完和上一轮对比，这一轮才出现的条目在看板上高亮。
# 状态文件放在 board.html 旁边（board.html.state.json），不入库。
BOARD_STATE_PATH = os.environ.get("BOARD_STATE", "").strip()


def board_state_file(board_path):
    if BOARD_STATE_PATH:
        return pathlib.Path(BOARD_STATE_PATH)
    return pathlib.Path(str(board_path) + ".state.json")


def _item_title(line):
    """从 `1. [标题](链接) · 元信息` 里抠出标题本身，用来做跨轮对比。"""
    m = re.match(r"^\d+\.\s+\[([^\]]+)\]", line)
    if m:
        return m.group(1).strip()
    m = re.match(r"^\d+\.\s+(.*)$", line)
    if m:
        return re.sub(r"\s+·\s+.*$", "", m.group(1)).strip()
    return ""


def scan_items(report):
    """扫描报告里的条目，返回 (行号->(段名, 标题), {段名: [标题...]})。

    段名用「二级 > 三级」做键，这样不同话题下同名的「最新新闻」不会互相串。"""
    h2 = h3 = ""
    rows, sections = {}, {}
    for idx, line in enumerate(report.split("\n")):
        m2 = re.match(r"^##\s+(.*)", line)
        if m2:
            h2, h3 = m2.group(1).strip(), ""
            continue
        m3 = re.match(r"^###\s+(.*)", line)
        if m3:
            h3 = m3.group(1).strip()
            continue
        if re.match(r"^\d+\.\s+", line):
            title = _item_title(line)
            if not title:
                continue
            key = "%s > %s" % (h2, h3) if h3 else h2
            rows[idx] = (key, title)
            sections.setdefault(key, []).append(title)
    return rows, sections


def diff_board_state(report, state_path):
    """和上一轮的状态比，找出本轮才出现的条目。

    返回 (新增行号集合, 新增条数, 是否有基线, 本轮 sections)。
    某个段落第一次出现时没有基线 —— 那种情况不标（否则第一次跑就满屏 NEW）。"""
    rows, sections = scan_items(report)
    prev = {}
    try:
        prev = json.loads(state_path.read_text(encoding="utf-8")).get("sections") or {}
    except Exception:
        prev = {}

    new_idx = set()
    for idx, (key, title) in rows.items():
        base = prev.get(key)
        if base is None:
            continue
        if title not in base:
            new_idx.add(idx)
    return new_idx, len(new_idx), bool(prev), sections


def save_board_state(state_path, stamp, sections):
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps({"updated": stamp, "sections": sections},
                       ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    except Exception as exc:
        print("[warn] 写状态文件失败（下一轮会把全部条目当新增）: %s" % exc)


def render_board(report, stamp, interval=60, new_idx=None, new_count=0, has_baseline=True):
    """把 Markdown 报告渲染成一个自包含的 HTML 看板。

    不用第三方库：报告结构是固定的（#/##/###/引用/有序列表），正则够用。
    页面自己每 interval 秒整页 reload —— 计划任务每 10 分钟覆盖文件，
    所以最坏情况下内容最多晚 60 秒才显示出来。

    new_idx 是「本轮新增」的行号集合，命中的 li 会加 .is-new 高亮。"""
    body, in_list = [], False
    new_idx = new_idx or set()

    def close_list():
        nonlocal in_list
        if in_list:
            body.append("</ol>")
            in_list = False

    for ln, raw in enumerate(report.split("\n")):
        line = raw.rstrip()
        if not line.strip():
            close_list()
            continue
        m = re.match(r"^###\s+(.*)", line)
        if m:
            close_list()
            body.append("<h3>%s</h3>" % _md_inline(m.group(1)))
            continue
        m = re.match(r"^##\s+(.*)", line)
        if m:
            close_list()
            body.append("<h2>%s</h2>" % _md_inline(m.group(1)))
            continue
        m = re.match(r"^#\s+(.*)", line)
        if m:
            close_list()
            body.append("<h1>%s</h1>" % _md_inline(m.group(1)))
            continue
        m = re.match(r"^>\s*(.*)", line)
        if m:
            close_list()
            body.append('<p class="note">%s</p>' % _md_inline(m.group(1)))
            continue
        m = re.match(r"^\d+\.\s+(.*)", line)
        if m:
            if not in_list:
                body.append("<ol>")
                in_list = True
            cls = ' class="is-new"' if ln in new_idx else ""
            body.append("<li%s>%s</li>" % (cls, _md_inline(m.group(1))))
            continue
        close_list()
        body.append("<p>%s</p>" % _md_inline(line))
    close_list()

    if not has_baseline:
        chip = '<span class="chip chip-base">首轮建立基线，下一轮起标记新增</span>'
    elif new_count:
        chip = '<span class="chip chip-new">本轮新增 %d 条（黄色高亮）</span>' % new_count
    else:
        chip = '<span class="chip">本轮无新增</span>'

    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="%(iv)d">
<title>热点看板 · %(stamp)s</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 28px 20px 80px;
    background: #f7f8fa; color: #1f2328;
    font: 15px/1.7 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
  }
  main { max-width: 1080px; margin: 0 auto; }
  .topbar {
    position: sticky; top: 0; z-index: 9;
    display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    margin: -28px -20px 22px; padding: 12px 20px;
    background: rgba(255,255,255,.92); backdrop-filter: blur(8px);
    border-bottom: 1px solid #e6e8eb;
    font-size: 13px; color: #57606a;
  }
  .dot { width: 8px; height: 8px; border-radius: 50%%; background: #1a7f37;
         box-shadow: 0 0 0 3px #dafbe1; }
  .topbar b { color: #1f2328; font-weight: 600; }
  h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: .3px; }
  h2 { font-size: 17px; margin: 30px 0 10px; padding-left: 10px;
       border-left: 3px solid #0969da; }
  h3 { font-size: 14px; margin: 18px 0 6px; color: #424a53;
       font-weight: 600; letter-spacing: .2px; }
  .note { margin: 6px 0 0; color: #57606a; font-size: 13px; }
  ol { margin: 6px 0 14px; padding-left: 24px; }
  li { margin: 4px 0; }
  li::marker { color: #8c959f; font-variant-numeric: tabular-nums; }
  /* 本轮新增：黄色底 + 左侧金色竖条 + NEW 角标 */
  li.is-new {
    background: linear-gradient(90deg, #fff8c5 0%%, rgba(255,248,197,0) 74%%);
    border-left: 3px solid #d4a72c; border-radius: 3px;
    margin-left: -8px; padding: 2px 10px 2px 9px;
  }
  li.is-new::marker { color: #9a6700; font-weight: 700; }
  li.is-new::after {
    content: "NEW"; margin-left: 8px; padding: 0 5px;
    font-size: 10px; font-weight: 700; letter-spacing: .6px;
    color: #7d4e00; background: #ffd33d; border-radius: 3px;
    vertical-align: 1px;
  }
  .chip { padding: 2px 9px; border-radius: 999px;
          border: 1px solid #e6e8eb; background: #f6f8fa; color: #57606a; }
  .chip-new { background: #fff8c5; border-color: #e9c352; color: #7d4e00; font-weight: 600; }
  .chip-base { background: #eef4ff; border-color: #c7d8f7; color: #0a4ea8; }
  a { color: #0969da; text-decoration: none; }
  a:hover { text-decoration: underline; }
  main > p { color: #57606a; }
</style></head>
<body><main>
<div class="topbar">
  <span class="dot"></span>
  <span>更新于 <b>%(stamp)s</b></span>
  <span>·</span>
  <span>每 10 分钟重抓，页面 <b id="cd">%(iv)d</b> 秒后自动刷新</span>
  %(chip)s
</div>
%(body)s
</main>
<script>
  var left = %(iv)d;
  setInterval(function () {
    left = left > 0 ? left - 1 : %(iv)d;
    document.getElementById('cd').textContent = left;
  }, 1000);
</script>
</body></html>""" % {"iv": interval, "stamp": stamp, "body": "\n".join(body), "chip": chip}


def _push_once(title, body):
    url = "https://sctapi.ftqq.com/%s.send" % SERVERCHAN_KEY
    payload = urllib.parse.urlencode({"title": title, "desp": body}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"User-Agent": UA})
    try:
        with OPENER.open(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "ignore")
        code = json.loads(raw).get("code")
        print("[notify] 微信推送 %s: %s" % ("成功" if code == 0 else "失败", raw[:200]))
        return code == 0
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        print("[notify] 微信推送 HTTP %s: %s" % (exc.code, detail or exc))
        return False
    except Exception as exc:
        print("[notify] 微信推送异常: %s" % exc)
        return False


def notify_serverchan(title, body):
    """把报告推到微信（Server酱）。设计上永不抛异常 —— 推送失败不能拖垮抓取任务。
    内容过长时自动按段拆成多条发送（单条上限见 MAX_PUSH_BYTES）。"""
    if not SERVERCHAN_KEY:
        print("[notify] 未配置 SERVERCHAN_KEY，跳过微信推送")
        return False

    chunks = split_report(body, MAX_PUSH_BYTES)
    total = len(chunks)
    ok = True
    for idx, chunk in enumerate(chunks, 1):
        part = title if total == 1 else "%s（%d/%d）" % (title, idx, total)
        if idx > 1:
            time.sleep(3)  # 连着发太快会被限流
        if len(chunk.encode("utf-8")) > MAX_PUSH_BYTES:
            chunk = (
                chunk.encode("utf-8")[:MAX_PUSH_BYTES].decode("utf-8", "ignore")
                + "\n\n...(过长已截断)"
            )
            print("[notify] 第 %d 段仍超长，已截断" % idx)
        ok = _push_once(part, chunk) and ok
    print("[notify] 共 %d 条（%s）" % (total, "内容分段，避免截断" if total > 1 else "单条发完"))
    return ok


def slugify(text):
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", text).strip("-")
    return s[:40] or "topic"


def main():
    data_dir = pathlib.Path("data")
    today = bj_now().date().isoformat()
    specs = load_topic_specs()
    topics = [s["name"] for s in specs]
    adhoc = MODE == "adhoc"

    scope = " / ".join(
        ("%s（%s）" % (s["name"], s["tier"])) if s["tier"] else s["name"] for s in specs
    )
    print("[mode] %s | 话题 %s" % (MODE, ("= " + scope) if specs else "= (无，走全站榜)"))

    # 高频档：先看静默时段。放在最前面，省得凌晨白抓一遍白占 runner。
    if FREQUENT and not in_active_hours():
        print("[skip] 不在 ACTIVE_HOURS=%s 的有效时段内，跳过" % ACTIVE_HOURS_RAW)
        return

    # 守卫：同一天只真正执行一次。workflow 里排了多个时间点做冗余，
    # 被丢弃的那次由下一次补上；补上的那次看到文件已存在就安静跳过。
    # adhoc（临时查询）和 frequent（每 10 分钟）都不走守卫。
    if not adhoc and not FREQUENT and (data_dir / ("%s.md" % today)).exists() and not FORCE:
        print("[skip] %s 今天已经跑过了，跳过（这是冗余触发的正常行为，不是错误）" % today)
        return

    report = build_report(specs)

    if FREQUENT:
        # 速报体：普通段留 4 条，主话题段留 6 条（AI 要压得住版面）
        stamp = bj_now().strftime("%m-%d %H:%M")
        report = compact_report(report, keep=4, keep_main=6)
        report = re.sub(
            r"^# 每日热榜 · \S+", "# 热点速报 · %s" % stamp, report, count=1, flags=re.M
        )

    # 全军覆没要报错，不能静默变成"今天没热点"。
    # 所有来源都失败时让 job 变红，触发 workflow 里的失败告警步骤。
    n_items = len(re.findall(r"^\d+\. ", report, re.M))
    if n_items == 0:
        n_fail = report.count("(抓取失败")
        msg = "所有来源都抓取失败（失败段数 %d）。请查看 Actions 日志。" % n_fail
        print("[error] " + msg)
        if FREQUENT:
            # 高频档静默失败即可：一天 144 次里偶发一次抓空很正常，
            # 每次都告警会把微信刷爆，也把 job 刷红。等下一档自然会恢复。
            return
        notify_serverchan("【抓取失败】每日热榜", msg)
        sys.exit(1)

    # 看板模式：只写 HTML，不推送、不提交、不写 data/。
    # 放在守卫之后、归档之前 —— 它就是「本地一直看着」这条用法的主路径。
    if BOARD_PATH:
        target = pathlib.Path(BOARD_PATH)
        target.parent.mkdir(parents=True, exist_ok=True)
        stamp = bj_now().strftime("%m-%d %H:%M:%S")
        state_file = board_state_file(target)
        new_idx, n_new, has_base, sections = diff_board_state(report, state_file)
        target.write_text(
            render_board(report, stamp, new_idx=new_idx,
                         new_count=n_new, has_baseline=has_base),
            encoding="utf-8",
        )
        save_board_state(state_file, stamp, sections)
        print("[board] 已更新 %s（%d 条，新增 %d 条，%d 字节）"
              % (target, n_items, n_new, len(report.encode("utf-8"))))
        return

    if FREQUENT:
        # 只留一份覆盖文件，不写按日文件、不动 latest.md
        data_dir.mkdir(exist_ok=True)
        (data_dir / "frequent.md").write_text(report, encoding="utf-8")
        title = "热点速报 · %s" % bj_now().strftime("%m-%d %H:%M")
        print("[frequent] 写入 data/frequent.md（速报体 %d 条）" % n_items)
    elif adhoc:
        # 临时查询：单独归档，绝不写当日文件、也不覆盖 latest.md，
        # 免得把"今天已经跑过"的守卫状态弄脏。
        target = data_dir / "adhoc" / ("%s-%s.md" % (today, slugify("+".join(topics) or "global")))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(report, encoding="utf-8")
        title = "每日热榜 · %s · %s" % (today, " / ".join(topics) or "全站榜")
        print("[adhoc] 写入 %s" % target)
    else:
        data_dir.mkdir(exist_ok=True)
        (data_dir / ("%s.md" % today)).write_text(report, encoding="utf-8")
        (data_dir / "latest.md").write_text(report, encoding="utf-8")
        title = "每日热榜 · %s" % today

    sys.stdout.write(report + "\n")

    # 高频档不推送：它的消费方是本地看板（BOARD_PATH）或 data/frequent.md。
    # 一天 144 条推微信既费额度又刷屏，2026-11-13 起改成「抓取归抓取、推送归每日那次」。
    if FREQUENT:
        print("[frequent] 高频档只抓不发（看板/文件即为产出）")
        return

    notify_serverchan(title, report)


if __name__ == "__main__":
    main()
