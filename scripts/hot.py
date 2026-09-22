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

# 冗余触发的守卫：workflow 在同一小时内排了多个时间点互为备份（GitHub 的 cron
# 在整点高负载时会被延迟甚至直接丢弃）。设 FORCE=1 忽略"今天已跑过"的检查强制重跑。
FORCE = os.environ.get("FORCE", "").strip().lower() in ("1", "true", "yes", "on")

# ---- 想追的 Reddit 版块（仅全站榜模式用）----
# 注意：Reddit 对未认证请求限流很紧（同一 IP 约每分钟 1 次）。
# 每多写一个版块，就要把间隔调大，否则会 429。
# r/all 本身已覆盖全站热门，一般一个就够。
SUBREDDITS = ["all"]

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
REDDIT_GAP = 20
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


def fetch(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with OPENER.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def fetch_retry(url, retries=3, backoff=4, timeout=40):
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


def load_topics():
    """话题来源：环境变量 TOPICS 优先，其次 config/topics.txt。"""
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
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


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
        with OPENER.open(req, timeout=20) as resp:
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
    with OPENER.open(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


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
        try:
            for i, (t, link, meta) in enumerate(_cn_items(src, CN_HOT_LIMIT), 1):
                if link:
                    lines.append("%d. [%s](%s)%s" % (i, t, link, (" · " + meta) if meta else ""))
                else:
                    lines.append("%d. %s%s" % (i, t, (" · " + meta) if meta else ""))
        except Exception as exc:
            lines.append("(抓取失败: %s)" % exc)
        lines.append("")


def topic_sections(lines, topics):
    """话题模式：每个话题 = 最新新闻 + Reddit 讨论 + YouTube 最新视频。"""
    for idx, topic in enumerate(topics):
        if idx:
            time.sleep(REDDIT_GAP)
        lines.append("## 话题：%s" % topic)
        lines.append("")

        lines.append("### 最新新闻")
        try:
            items = gnews_topic(topic, TOPIC_NEWS_LIMIT)
            if items:
                for i, (t, link, pub) in enumerate(items, 1):
                    lines.append("%d. [%s](%s) · %s" % (i, t, link, pub))
            else:
                lines.append("(无数据)")
        except Exception as exc:
            lines.append("(抓取失败: %s)" % exc)
        lines.append("")

        time.sleep(REDDIT_GAP)
        rq = topic_query(topic)
        lines.append(
            "### Reddit 讨论（近 24 小时）" + ("" if rq == topic else "　搜索词：%s" % rq)
        )
        try:
            items = reddit_search(rq, TOPIC_REDDIT_LIMIT)
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
        try:
            items, note = yt_search_recent(topic, TOPIC_YT_LIMIT)
            for i, (t, link, pub, views) in enumerate(items, 1):
                meta = " · ".join(x for x in (pub, views) if x)
                lines.append("%d. [%s](%s)%s" % (i, t, link, (" · " + meta) if meta else ""))
            if note:
                lines.append("")
                lines.append("(%s)" % note)
        except Exception as exc:
            lines.append("(抓取失败: %s)" % exc)
        lines.append("")


def build_report(topics):
    today = datetime.date.today().isoformat()
    lines = ["# 每日热榜 · %s" % today, ""]

    if topics:
        lines.append("> 话题：%s" % " / ".join(topics))
        lines.append("")

    # 中文圈热点放最前：国内榜单是每天第一眼要看的东西。
    # 临时查询（adhoc + 指定话题）时不带 —— 那次的目的就是看这个话题，别塞一堆大盘进来。
    if CN_HOT and not (MODE == "adhoc" and topics):
        cn_sections(lines)

    if topics:
        topic_sections(lines, topics)
        if not KEEP_GLOBAL:
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
    today = datetime.date.today().isoformat()
    topics = load_topics()
    adhoc = MODE == "adhoc"

    print("[mode] %s | 话题 %s" % (MODE, ("= " + " / ".join(topics)) if topics else "= (无，走全站榜)"))

    # 守卫：同一天只真正执行一次。workflow 里排了多个时间点做冗余，
    # 被丢弃的那次由下一次补上；补上的那次看到文件已存在就安静跳过。
    # adhoc（临时查询）不走守卫，否则想看话题时会被"今天已经跑过"挡住。
    if not adhoc and (data_dir / ("%s.md" % today)).exists() and not FORCE:
        print("[skip] %s 今天已经跑过了，跳过（这是冗余触发的正常行为，不是错误）" % today)
        return

    report = build_report(topics)

    # 全军覆没要报错，不能静默变成"今天没热点"。
    # 所有来源都失败时让 job 变红，触发 workflow 里的失败告警步骤。
    n_items = len(re.findall(r"^\d+\. ", report, re.M))
    if n_items == 0:
        n_fail = report.count("(抓取失败")
        msg = "所有来源都抓取失败（失败段数 %d）。请查看 Actions 日志。" % n_fail
        print("[error] " + msg)
        notify_serverchan("【抓取失败】每日热榜", msg)
        sys.exit(1)

    if adhoc:
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
    notify_serverchan(title, report)


if __name__ == "__main__":
    main()
