#!/usr/bin/env python3
"""每日热榜抓取：Reddit 热榜 + kworb YouTube 全球榜 + 指定 YouTube 频道最新

设计用于 GitHub Actions（runner 本身在境外，不需要任何代理）。
本地调试时设代理：set SCRAPE_PROXY=http://127.0.0.1:7890

只依赖 Python 标准库，无需 pip install。
"""

import os
import re
import html
import sys
import time
import datetime
import pathlib
import urllib.request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
PROXY = os.environ.get("SCRAPE_PROXY", "").strip()

# ---- 想追的 Reddit 版块 ----
# 注意：Reddit 对未认证请求限流很紧（同一 IP 约每分钟 1 次）。
# 每多写一个版块，就要在 build_report 里把间隔调大，否则会 429。
# r/all 本身已覆盖全站热门，一般一个就够。
SUBREDDITS = ["all"]

# ---- 想追的 YouTube 频道：填 channel_id（形如 UCxxxxxxxxxxxxxxxxxxxxxx）----
# 获取办法：打开频道的"关于"页 → 分享 → 复制频道 ID；
# 或打开频道页源码搜 "channelId"
CHANNELS = {
    # "MrBeast": "UCX6OQ3DkcsbYNE6H8uQQuVA",
}


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


def reddit_hot(sub, limit=10, retries=3):
    """Reddit 热榜。注意：.json 已被反爬，必须走 .rss。
    连续请求会被限流（429），所以要退避重试。"""
    xml = None
    last_exc = None
    for attempt in range(retries):
        try:
            xml = fetch("https://www.reddit.com/r/%s/hot/.rss" % sub)
            break
        except Exception as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(20 * (attempt + 1))
    if xml is None:
        raise last_exc

    out = []
    for entry in re.findall(r"<entry>([\s\S]*?)</entry>", xml)[:limit]:
        t = re.search(r"<title>([\s\S]*?)</title>", entry)
        l = re.search(r'<link href="([^"]*)"', entry)
        title = html.unescape(t.group(1).strip()) if t else "(无标题)"
        out.append((title, l.group(1) if l else ""))
    return out


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


def build_report():
    today = datetime.date.today().isoformat()
    lines = ["# 每日热榜 · %s" % today, ""]

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

    return "\n".join(lines)


def main():
    report = build_report()
    data_dir = pathlib.Path("data")
    data_dir.mkdir(exist_ok=True)
    today = datetime.date.today().isoformat()
    (data_dir / ("%s.md" % today)).write_text(report, encoding="utf-8")
    (data_dir / "latest.md").write_text(report, encoding="utf-8")
    sys.stdout.write(report + "\n")


if __name__ == "__main__":
    main()
