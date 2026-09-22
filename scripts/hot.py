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
# 想加词或覆盖：环境变量 TOPIC_ALIAS="科技=tech, 数码=gadgets"
TOPIC_ALIASES = {
    "科技": "technology",
    "人工智能": "artificial intelligence",
    "编程": "programming",
    "游戏": "gaming",
    "电竞": "esports",
    "美股": "us stocks",
    "港股": "hong kong stocks",
    "A股": "china stocks",
    "财经": "finance",
    "加密货币": "crypto",
    "比特币": "bitcoin",
    "手机": "smartphone",
    "数码": "gadgets",
    "汽车": "cars",
    "新能源": "electric vehicles",
    "机器人": "robotics",
    "航天": "space",
    "军事": "military",
    "影视": "movies",
    "综艺": "tv shows",
    "音乐": "music",
    "体育": "sports",
    "足球": "football",
    "篮球": "nba",
    "健身": "fitness",
    "旅行": "travel",
    "美食": "food",
    "健康": "health",
    "教育": "education",
    "宠物": "pets",
    "时尚": "fashion",
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
    实测（2026-09-22，话题 科技→technology）：
      sort=top / t=day    → 6 条里 4 条来自 r/technology，是真实科技热点 ✅
      sort=hot / t=day    → 全是小版块的梗图、子版块首页，基本不可用 ❌
      sort=relevance/t=day→ 杂（IndianStreetBets、Helldivers 都混进来）❌
    中文关键词在 day 窗口下经常一条都搜不到，所以调用方先用 topic_query() 换英文词。"""

    def grab(sort, t):
        n = max(limit * 2, 10)
        url = "https://www.reddit.com/search.rss?" + urllib.parse.urlencode(
            {"q": query, "sort": sort, "t": t, "limit": n}
        )
        items = reddit_entries(_reddit_fetch(url), n)
        # search.rss 会把版块首页（/r/xxx/，不含 /comments/）也当成结果塞进来，
        # 那不是帖子，滤掉。
        return [it for it in items if "/comments/" in it[1]][:limit]

    items = grab("top", "day")
    if not items:
        time.sleep(REDDIT_GAP)
        items = grab("top", "week")
    return items


def kworb_trending(limit=15):
    """YouTube 全球热门榜。聚合站是纯 HTML，无需登录，最稳"""
    page = fetch("https://kworb.net/youtube/trending_overall.html")
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
    xml = fetch(url)
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
    page = fetch("https://www.youtube.com/results?" + urllib.parse.urlencode(params))
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
        topic_sections(lines, topics)
        if not KEEP_GLOBAL:
            return "\n".join(lines)

    global_sections(lines)
    return "\n".join(lines)


def notify_serverchan(title, body):
    """把报告推到微信（Server酱）。设计上永不抛异常 —— 推送失败不能拖垮抓取任务。"""
    if not SERVERCHAN_KEY:
        print("[notify] 未配置 SERVERCHAN_KEY，跳过微信推送")
        return False

    raw_bytes = body.encode("utf-8")
    if len(raw_bytes) > MAX_PUSH_BYTES:
        body = raw_bytes[:MAX_PUSH_BYTES].decode("utf-8", "ignore") + "\n\n...(内容过长已截断)"
        print("[notify] 正文超长，已截断")

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
