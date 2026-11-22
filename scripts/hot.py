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
import concurrent.futures

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

# ==================== 范围过滤与涨速引擎（2026-11-21）====================
# 用户诉求：① 只要 AI / 科技，无关的一律不进版；② 不要平台热搜排行罗列；
# ③ 要「一段时间内浏览/热度激增」的具体帖子与新闻。
# 所以整份报告的骨架从「按平台列榜」改成「按热度涨速排序」。
AI_ONLY = (os.environ.get("AI_ONLY", "").strip().lower() or "on") not in ("0", "false", "no", "off")

# 中文/通用白名单：命中即收。国内热榜噪声极大（娱乐、社会新闻占九成），
# 必须用「白名单命中」而不是「黑名单剔除」，否则永远滤不干净。
AI_TERMS_CN = (
    "人工智能", "大模型", "大语言模型", "多模态", "智能体", "算法", "算力", "芯片",
    "半导体", "光刻", "晶圆", "GPU", "CPU", "NPU", "TPU", "数据中心", "液冷", "光模块",
    "内存", "闪存", "固态", "显卡", "服务器", "封装", "制程",
    "云计算", "边缘计算", "量子计算", "量子芯片", "核聚变", "机器人", "人形机器人",
    "具身智能", "自动驾驶", "无人驾驶", "智能驾驶", "无人机", "脑机", "神经网络",
    "机器学习", "深度学习", "强化学习", "开源", "编程", "程序员", "开发者", "代码",
    "操作系统", "数据库", "网络安全", "黑客", "漏洞", "稀土", "光伏", "储能", "电池",
    "科创板", "科技", "硬科技", "黑科技", "数码", "智能手机", "折叠屏", "卫星", "航天",
    "火箭", "星链", "元宇宙", "虚拟现实", "增强现实", "空间计算", "智能眼镜",
    "AI", "AGI", "AIGC", "LLM", "GPT", "ChatGPT", "OpenAI", "Anthropic", "Claude",
    "Gemini", "Llama", "DeepSeek", "Qwen", "Kimi", "Sora", "Midjourney", "Copilot",
    "NVIDIA", "英伟达", "AMD", "英特尔", "Intel", "台积电", "ASML", "特斯拉", "Tesla",
    "SpaceX", "苹果", "Apple", "华为", "小米", "字节", "腾讯", "阿里", "百度", "Meta",
    "微软", "Microsoft", "谷歌", "Google", "三星", "Samsung", "高通", "联发科",
)
# 这些是「看着像科技、其实是消费/娱乐」的高频误命中，命中就丢掉。
# 比如「苹果」既是公司也是水果，「特斯拉」也会出现在车祸社会新闻里。
AI_TERMS_BLOCK = (
    "苹果价格", "苹果滞销", "苹果丰收", "吃苹果", "苹果园", "卖苹果",
    "特斯拉车祸", "特斯拉自燃", "小米粥", "小米辣", "小米椒", "小米种植",
    "华为手机壳", "明星", "恋情", "结婚", "离婚", "分手", "出轨", "塌房",
    "综艺", "电视剧", "电影票房", "演唱会", "选秀", "网红", "直播带货",
    "世界杯", "奥运会", "足球", "篮球", "彩票", "星座", "算命",
    # 含 AI 但本质是娱乐二次创作：鬼畜、AI 翻唱、AI MV 大赛。
    # 这类在国内视频平台上量很大，不挡的话 B 站段会被它们占满。
    "鬼畜", "AI翻唱", "AI MV", "MV大赛", "整活", "沙雕", "二创", "同人",
    "AI续写", "全民制作人",
)

# 英文术语必须自己划边界：Python 的 \b 把中文也算 \w，
# 所以 "AI技术" 里 `\bAI\b` 匹配不到。用显式前后否定更可靠。
_EN_TERM_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:AI|AGI|AIGC|LLM|GPT|GPU|CPU|NPU|TPU|API|SDK|RAG|"
    r"OpenAI|Anthropic|Claude|Gemini|Llama|DeepSeek|Qwen|Kimi|Sora|Midjourney|"
    r"Copilot|NVIDIA|AMD|Intel|Tesla|SpaceX|Meta|Microsoft|Google|Samsung|"
    r"Apple|AWS|Azure|Kubernetes|Linux|Rust|Python|GitHub)(?![A-Za-z0-9])",
    re.I,
)
_CN_TERM_SET = tuple(t for t in AI_TERMS_CN if t.isascii() is False)
_EN_TERM_SET = tuple(t for t in AI_TERMS_CN if t.isascii())


# 这几个词太泛：热榜里「某科技公司发布会」「新款数码产品」大多是消费导购，
# strict 模式下不算命中，用于国内榜单这种必须从严的源。
_WEAK_TERMS = ("科技", "数码", "黑科技", "硬科技", "智能手机",
               # 公司名单独出现不算数：「多啃苹果牙齿能变整齐」里的苹果是水果，
               # 「华为子品牌开卖」是导购稿。标题里另外还有芯片/发布/系统这类
               # 语境词时，会命中别的白名单词，照样能进。
               "苹果", "华为", "三星", "小米", "谷歌", "微软", "腾讯", "百度")

# 娱乐内容的强特征。必须单独拦一道：像「黄俊捷出道十周年苹果烟花」这种，
# 靠「苹果」命中了公司白名单，但整条是粉丝应援，和科技毫无关系。
# 黑名单式短语（"明星""恋情"）盖不住这种变体，只能认「出道/应援/专辑」这类词。
_ENTERTAIN_MARK = (
    "出道", "应援", "粉丝", "新歌", "专辑", "杀青", "番位", "塌房", "打榜",
    "演唱会", "追星", "爱豆", "剧组", "预告片",
)


def is_ai_tech(text, strict=False):
    """判断一条内容是否属于 AI / 科技范围。

    strict=True 用于国内热榜、YouTube 搜索这类噪声源：命中黑名单直接否，
    且「科技/数码」这种泛词不算数 —— 否则一条手机促销也能混进来。
    HN / 官方博客这类源天然就是科技内容，不必过滤。
    """
    if not AI_ONLY:
        return True
    s = (text or "").strip()
    if not s:
        return False
    # 先把「长得像白名单词、其实不是」的说法摘掉：
    # 「三星系统」是天文（一颗恒星带两颗伴星），不是三星电子；
    # 「三星堆」是考古。不清掉的话这类纯科学/考古条目会靠一个词混进来。
    for trap in ("三星系统", "三星堆", "三星伴月", "北斗七星", "人造卫星轨迹"):
        s = s.replace(trap, "")
    for bad in AI_TERMS_BLOCK:
        if bad in s:
            return False
    if strict and any(w in s for w in _ENTERTAIN_MARK):
        return False
    if _EN_TERM_RE.search(s):
        return True
    for t in _CN_TERM_SET:
        if t in s:
            if strict and t in _WEAK_TERMS:
                continue
            return True
    # 英文术语只能走上面的边界正则 —— 拿 "AI" 当子串搜会命中 said/trail/email，
    # 那是把整条时间线都放进来，等于没过滤。
    return False


# ---- 涨速：每小时热度 ----
# 两种算法，取决于这个源能不能给出「绝对热度 + 存在时长」：
#   A) 绝对速度（HN 分数、YouTube 播放量）：vph = heat / age_h
#   B) 跨轮增量（微博/抖音/头条/B站 只给当前热度值）：vph = Δheat / Δt
SURGE_WINDOW_H = float(os.environ.get("SURGE_WINDOW_H", "").strip() or "6")
# 新条目至少要观测这么久才算得出可信涨速（不然 30 分钟内的小波动会被放大成天文数字）
SURGE_MIN_SPAN_H = float(os.environ.get("SURGE_MIN_SPAN_H", "").strip() or "0.4")
# 「本轮最猛」摘要取几条
SURGE_TOP_N = int(os.environ.get("SURGE_TOP_N", "").strip() or "6")


def vph_abs(heat, age_h):
    """绝对速度：把「当前热度」摊到它的存在时长上。
    下限 0.5 小时，避免刚发布 3 分钟的帖子算出荒唐的天文数字。"""
    try:
        heat = float(heat)
    except (TypeError, ValueError):
        return 0.0
    return heat / max(float(age_h or 0), 0.5)


_SURGE_STATE_DEFAULT = ""


def surge_state_path(board_path=""):
    """涨速历史单独存一个文件 —— 看板的 sections 状态每轮全量覆盖，
    没有「上一次的热度值」，不能拿它算增量。"""
    env = os.environ.get("SURGE_STATE", "").strip()
    if env:
        return pathlib.Path(env)
    base = board_path or BOARD_PATH or str(REPO_ROOT / "data" / "surge.html")
    return pathlib.Path(str(base) + ".surge.json")


def load_surge(path):
    try:
        raw = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        items = raw.get("items")
        return items if isinstance(items, dict) else {}
    except Exception:
        return {}


def bump_surge(prev, key, heat, now_ts):
    """把本条的最新热度并入历史，返回 (新记录, 增量信息或 None)。

    记录结构 {first:[热度,时间], last:[热度,时间]}：
    first 是窗口起点，超过 SURGE_WINDOW_H 就往后挪一格，
    这样算出来的是「最近最多 6 小时的平均涨速」，单轮抓取失败也不会丢基线。
    """
    try:
        heat = float(heat)
    except (TypeError, ValueError):
        return None, None
    rec = prev.get(key)
    if not isinstance(rec, dict) or not rec.get("first"):
        return {"first": [heat, now_ts], "last": [heat, now_ts]}, None
    first_h, first_t = rec["first"]
    last_h, last_t = rec.get("last") or [first_h, first_t]
    # 起点太旧：把上一轮的观测值提为新起点，重新起算窗口
    if now_ts - first_t > SURGE_WINDOW_H * 3600:
        first_h, first_t = last_h, last_t
        if now_ts - first_t > SURGE_WINDOW_H * 3600:
            first_h, first_t = heat, now_ts
    span_h = (now_ts - first_t) / 3600.0
    delta = heat - first_h
    info = None
    if span_h >= SURGE_MIN_SPAN_H:
        # 注意这里**不要求 delta > 0**：热度回落的条目也要返回信息，
        # 否则调用方会把「明明有基线、只是没涨」误判成「还没建立基线」。
        info = {"delta": delta, "span_h": span_h, "vph": delta / span_h,
                "base": first_h, "heat": heat}
    return {"first": [first_h, first_t], "last": [heat, now_ts]}, info


def prune_surge(items, now_ts):
    """只留 24 小时内还在更新的条目，防止状态文件无限膨胀。"""
    out = {}
    for k, rec in items.items():
        try:
            last_t = (rec.get("last") or rec.get("first"))[1]
        except Exception:
            continue
        if now_ts - last_t <= 24 * 3600:
            out[k] = rec
    return out


def save_surge(path, items, now_ts):
    try:
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(path).write_text(
            json.dumps({"updated": int(now_ts), "items": items},
                       ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    except Exception as exc:
        print("[warn] 写涨速状态失败（本轮无法算增量）: %s" % exc)


def fmt_num(v):
    """热度数字人读友好：12345 → 1.2万。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ""
    if v >= 1e8:
        return "%.1f亿" % (v / 1e8)
    if v >= 1e4:
        return "%.1f万" % (v / 1e4)
    if v >= 1000:
        return "%.0f" % v
    return "%.0f" % v


def speed_text(vph, delta=None, span_h=None, unit=""):
    """涨速的展示文案。
    绝对速度：`148分/h`；跨轮增量：`+12.4万 / 2.1h（5.9万/h）`。"""
    if vph is None:
        return ""
    if delta is not None and span_h:
        # 不足 1 小时就说「25 分钟」，比 "0.4h" 直观得多
        span = ("%.1fh" % span_h) if span_h >= 1 else ("%d 分钟" % max(1, round(span_h * 60)))
        return "+%s / %s（%s%s/h）" % (fmt_num(delta), span, fmt_num(vph), unit)
    return "%s%s/h" % (fmt_num(vph), unit)


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
# 由计划任务每 30 分钟重跑一次覆盖它，页面自己定时 reload。
# BOARD_EVERY_MIN 只影响页面上那行说明文字（抓取节奏由计划任务决定，代码改不了它），
# 改计划任务间隔时顺手把它一起改，免得页面上的字骗人。
BOARD_PATH = os.environ.get("BOARD_PATH", "").strip()
BOARD_EVERY_MIN = int(os.environ.get("BOARD_EVERY_MIN", "").strip() or "30")
BOARD_RELOAD_SEC = int(os.environ.get("BOARD_RELOAD_SEC", "").strip() or "300")
# 页面顶栏那行说明文字。云端是每天跑一次，写死"每 N 分钟"会骗人，所以允许整句覆盖。
BOARD_EVERY_TEXT = os.environ.get("BOARD_EVERY_TEXT", "").strip()
# BOARD_ALSO=1：写完看板后不要退出，继续走当日归档 + 推送。
# 云端每日那次用它，一份数据同时产出 data/<date>.md（给微信/仓库）和 docs/index.html（给网页）。
BOARD_ALSO = os.environ.get("BOARD_ALSO", "").strip().lower() in ("1", "true", "yes", "on")

# 冗余触发的守卫：workflow 在同一小时内排了多个时间点互为备份（GitHub 的 cron
# 在整点高负载时会被延迟甚至直接丢弃）。设 FORCE=1 忽略"今天已跑过"的检查强制重跑。
FORCE = os.environ.get("FORCE", "").strip().lower() in ("1", "true", "yes", "on")
# 手动点「立即抓取」时由本地服务置 1：绕过 GitHub 的 1 小时缓存。
# 定时轮次不设，走缓存 —— 星数是慢变量，重复抓只是白烧 API 配额。
FORCE_GITHUB = os.environ.get("FORCE_GITHUB", "").strip().lower() in ("1", "true", "yes", "on")

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


def fetch(url, timeout=FETCH_TIMEOUT, direct=False):
    """direct=True 强制直连：国内源绕代理是「本机→代理→国内」，实测慢 3~5 倍。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if direct:
        with OPENER_DIRECT.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "ignore")
    return open_bytes(req, timeout).decode("utf-8", "ignore")


def fetch_retry(url, retries=FETCH_RETRIES, backoff=4, timeout=FETCH_TIMEOUT, direct=False):
    """带重试的 fetch。Google News 会偶发 SSL 中断
    （实测 `[SSL: UNEXPECTED_EOF_WHILE_READING]`），重试基本就好，
    别让一次抖动把整段新闻变成「抓取失败」。"""
    last = None
    for attempt in range(retries):
        try:
            return fetch(url, timeout=timeout, direct=direct)
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


# 资讯的时间窗。原来是 12 小时，但垂直媒体（量子位、The Decoder）一天只发
# 几条，12 小时窗口里经常整段空着 —— 那「来源多」就只是名义上的。放到 24 小时，
# 每条仍标着「几小时前」，旧的自然看得出旧。
NEWS_WINDOW_H = float(os.environ.get("NEWS_WINDOW_H", "").strip() or "24")

# 中文科技新闻里一大半是活动稿和政务通稿：「XX 论坛圆满收官」「XX 座谈会召开」
# 「某地擘画 AI 产业蓝图」。它们含 AI/科技关键词，但没有任何前沿信息 ——
# 这是「前沿资讯」里最该滤掉的一类，比娱乐八卦更耗注意力。
NEWS_TITLE_BLOCK = (
    "论坛", "峰会", "揭牌", "签约", "圆满", "共话", "擘画", "调研", "座谈",
    "走访", "宣讲", "党建", "主题教育", "开幕式", "闭幕式", "发布会现场",
    # 电商促销稿：「XX 开卖」「到手价」「直降」是导购，不是前沿
    "开卖", "到手价", "直降", "券后", "预售开启",
)


def fmt_age(pub):
    """把 RSS 的 pubDate 换算成「几分钟前」。看板上「什么时候发生的」
    比「Sat, 21 Nov 2026」有用得多。

    时间格式三种都有：RFC822（大多数 RSS）、ISO8601（Atom / The Verge）、
    `2026-11-21 20:44:52  +0800`（36氪，两个空格）。统一交给 _rss_dt 认。
    """
    dt = _rss_dt(pub)
    if dt is None:
        return (pub or "")[:16]
    h = (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds() / 3600.0
    if h < 0:
        return "刚刚"
    if h < 1:
        return "%d 分钟前" % max(1, int(h * 60))
    if h < 24:
        return "%.0f 小时前" % h
    return "%.0f 天前" % (h / 24)


def _pub_ts(text):
    """RSS pubDate → epoch 秒；解析不了给 0（排序时沉底）。"""
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(text or "")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def gnews_topic(query, limit=TOPIC_NEWS_LIMIT, hours=12):
    """Google News RSS 搜索，**限定时间窗**。

    `when:Nh` 是这次改造的关键 —— 不加时间窗时返回的是「相关度排序」，
    里面混着两周前的旧闻；而「前沿资讯」的价值全在「刚刚发生」。
    同一条新闻会被几十家媒体转载，按标题归并后用转载家数当热度信号：
    3 家同时报的，比只有 1 家报的更值得看。
    """
    q = query if not hours else "%s when:%dh" % (query, int(hours))
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"}
    )
    xml = fetch_retry(url)

    buckets, order = {}, []
    for item in re.findall(r"<item>([\s\S]*?)</item>", xml)[: max(limit * 5, 25)]:
        t = re.search(r"<title>([\s\S]*?)</title>", item)
        l = re.search(r"<link>([\s\S]*?)</link>", item)
        d = re.search(r"<pubDate>([\s\S]*?)</pubDate>", item)
        if not t:
            continue
        full = clean_title(t.group(1))
        # Google News 的标题统一是「正文标题 - 媒体名」。多数用 " - "，
        # 但不少中文源省略空格写成 "标题-媒体名"，两种都要认。
        m = re.search(r"\s+-\s+([^-]{2,24})$", full) or re.search(r"-([^-]{2,12})$", full)
        head, src = (full[: m.start()].strip(), m.group(1).strip()) if m else (full, "")
        key = re.sub(r"[\s\W_]+", "", head)[:44]
        if not key:
            continue
        if key in buckets:
            buckets[key]["n"] += 1
            continue
        pub_raw = d.group(1).strip() if d else ""
        buckets[key] = {
            "title": head,
            "url": l.group(1).strip() if l else "",
            "age": fmt_age(pub_raw),
            "ts": _pub_ts(pub_raw),
            "src": src,
            "n": 1,
        }
        order.append(key)

    out = [buckets[k] for k in order]
    # 转载家数一样的（绝大多数），按发生时间新→旧。前沿资讯里「最新」本身就是信息。
    out.sort(key=lambda x: (-x["n"], -x["ts"]))
    return out[:limit]


# ---- Hacker News（海外科技圈最快、最干净的激增信号）----
# Algolia 一次请求就把 30 条首页帖连同 points / 发布时间 / 评论数一起返回，
# 比官方 API 逐个拉 item 快一个数量级（后者 60 条要 20s+）。
# 分数 ÷ 存在时长 = 「每小时涨多少分」，这是最接近「热度激增」的量化指标。
HN_MAX_AGE_H = float(os.environ.get("HN_MAX_AGE_H", "").strip() or "24")
HN_MIN_SCORE = int(os.environ.get("HN_MIN_SCORE", "").strip() or "30")
# HN 首页偶尔会漂上来音乐归档、隐私政策这种和科技无关的帖子。不做白名单过滤
# （那会把「黎曼猜想证明」这类没有科技关键词的前沿数学一并误杀），
# 只用一小组精确短语挡掉明显跑题的。
HN_TITLE_BLOCK = (
    "privacy policy", "terms of service", "cookie policy", "at collection",
    "live archive", "who is hiring", "who wants to be hired", "unemployed",
)


def hn_stories(limit=12):
    """返回 HN 首页里「新且涨得猛」的帖子，按每小时得分排序。"""
    raw = fetch("https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=50",
                timeout=25)
    data = json.loads(raw)
    now = time.time()
    rows = []
    for h in data.get("hits") or []:
        title = (h.get("title") or "").strip()
        if not title:
            continue
        low = title.lower()
        if any(b in low for b in HN_TITLE_BLOCK):
            continue
        pts = h.get("points") or 0
        ts = h.get("created_at_i") or 0
        age_h = (now - ts) / 3600.0 if ts else 99
        if age_h > HN_MAX_AGE_H or pts < HN_MIN_SCORE:
            continue
        oid = h.get("objectID") or ""
        rows.append({
            "title": title,
            "url": h.get("url") or "https://news.ycombinator.com/item?id=%s" % oid,
            "hn_url": "https://news.ycombinator.com/item?id=%s" % oid,
            "heat": float(pts),
            "heat_text": "%d 分" % pts,
            "comments": h.get("num_comments") or 0,
            "age_h": age_h,
            "vph": vph_abs(pts, age_h),
            "source": "Hacker News",
        })
    rows.sort(key=lambda r: -r["vph"])
    return rows[:limit]


# ---- GitHub 热榜（新项目的涨星速度）----
# github.com/trending 的 HTML 在本机出口抓不动（22s 后连接被中断，exit 56），
# 云端虽然能通但解析 HTML 太脆。改用官方 Search API：一次请求就带回
# created_at 和 stargazers_count，相除即得「每天涨多少星」——
# 和 HN 的「分/小时」、YouTube 的「播放/小时」是同一套口径，可横向比较。
# 只统计近 GITHUB_WINDOW_D 天新建的仓库，所以榜单天然是「最近在爆的」。
GITHUB_WINDOW_D = int(os.environ.get("GITHUB_WINDOW_D", "").strip() or "7")
GITHUB_LIMIT = int(os.environ.get("GITHUB_LIMIT", "").strip() or "8")
# 星/天下限。低于这个数的项目，热度曲线已经平了，不叫「激增」。
GITHUB_MIN_SPD = float(os.environ.get("GITHUB_MIN_SPD", "").strip() or "120")
# 缓存有效期（小时）。GitHub 的星数是慢变量：同一批仓库半小时也就涨 2~5 星，
# 每 30 分钟重抓一次等于 12 次里有 11 次在做无用功，还占未认证 API 的配额。
# 设 0 关闭缓存（云端每天只跑一次，缓存反而会让它拿到陈旧数据）。
GITHUB_CACHE_H = float(os.environ.get("GITHUB_CACHE_H", "").strip() or "1")
# 新项目榜里刷星营销号和资源合集占比很高，这些不是「前沿」，先挡掉。
GITHUB_BLOCK = (
    "awesome-", "awesome_", "interview", "cheat", "crack", "keygen",
    "tutorial", "roadmap", "面试", "教程", "破解", "题库", "学习路线",
    "刷星", "引流", "接码", "爬虫池", "优惠券",
    # 凭据提取 / 设备解锁这类工具在榜上常冲到前排，但不是前沿，是灰产。
    "凭据提取", "密码提取", "提取工具", "unlock",
    # 2026-11-22：一批「游戏外挂 / 盗版密钥」仓库靠刷星挤进前排，特征是
    # 仓库名带 `portable-x64`、描述里写 preactivated / multi-instance，
    # 语言标 Jupyter Notebook（其实根本没代码），一天涨一千多星。
    "preactivated", "portable-x", "portable x64", "multi-instance",
    "green-timed", "uncensored", "hypixel", "skyblock", "roblox",
    "war thunder", "emulator keys", "nsp-box", "xci/nsp", "one-click ready",
)
# 没写描述的仓库一律跳过：真项目都会写一句干什么用，
# 空描述 + 高星速基本就是刷量空壳（实测 google-ultra 2555★、google-one-tools 466★ 都是空的）。


def _github_cache_path():
    """缓存放在看板旁边，和 .state / .surge 同一套路。
    没有看板路径（adhoc 等模式）时落到 data/ 下。"""
    if BOARD_PATH:
        p = pathlib.Path(BOARD_PATH)
        return p.with_name(p.name + ".github.json")
    return REPO_ROOT / "data" / ".github_cache.json"


def _github_apply_age(rows, now_ts):
    """按当前时间重算 age_h 与 ★/天。
    缓存命中也走这一遍 —— 星数没变但仓库又老了几十分钟，
    直接沿用缓存里的 spd 会让数字虚高。"""
    for r in rows:
        age_h = max((now_ts - r["created_ts"]) / 3600.0, 0.5)
        r["age_h"] = age_h
        r["spd"] = r["stars"] / (age_h / 24.0)
    return rows


def github_trending(limit=GITHUB_LIMIT, force=False):
    """返回近 GITHUB_WINDOW_D 天新建、涨星最快的仓库，按 ★/天 排序。

    force=True 绕过缓存，供「立即抓取」按钮用。"""
    cache_path = _github_cache_path()
    now_ts = time.time()

    if not force and GITHUB_CACHE_H > 0 and cache_path.is_file():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            ts = float(cache.get("ts") or 0)
            rows = cache.get("rows") or []
            if rows and ts and (now_ts - ts) < GITHUB_CACHE_H * 3600:
                _github_apply_age(rows, now_ts)
                age_min = int((now_ts - ts) / 60)
                for r in rows:
                    r["cache_age_min"] = age_min
                    r["cached"] = True
                rows.sort(key=lambda r: -r["spd"])
                plog("GitHub 命中缓存（%d 分钟前抓的，共 %d 条）" % (age_min, len(rows)))
                return rows[:limit]
        except Exception as exc:
            print("[github] 缓存读取失败，改为重新抓取: %s" % exc)

    since = (datetime.date.today() - datetime.timedelta(days=GITHUB_WINDOW_D)).isoformat()
    q = urllib.parse.urlencode({
        "q": "created:>%s" % since,
        "sort": "stars",
        "order": "desc",
        "per_page": "40",
    })
    raw = fetch_retry("https://api.github.com/search/repositories?" + q, retries=2, timeout=20)
    data = json.loads(raw)
    rows = []
    for it in data.get("items") or []:
        name = (it.get("full_name") or "").strip()
        if not name:
            continue
        blob = (name + " " + (it.get("description") or "")).lower()
        if any(b in blob for b in GITHUB_BLOCK):
            continue
        desc = (it.get("description") or "").strip()
        if not desc:
            continue
        stars = int(it.get("stargazers_count") or 0)
        if stars <= 0:
            continue
        try:
            t = datetime.datetime.strptime(
                it.get("created_at") or "", "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=datetime.timezone.utc)
        except Exception:
            continue
        rows.append({
            "title": name,
            "url": it.get("html_url") or "",
            "desc": desc,
            "stars": stars,
            "forks": int(it.get("forks_count") or 0),
            "lang": (it.get("language") or "").strip(),
            "created_ts": t.timestamp(),
        })

    _github_apply_age(rows, now_ts)
    rows = [r for r in rows if r["spd"] >= GITHUB_MIN_SPD]
    rows.sort(key=lambda r: -r["spd"])

    # 缓存存全量（不只是前 limit 条），下次命中时能直接排。
    if GITHUB_CACHE_H > 0:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            keep = ("title", "url", "desc", "stars", "forks", "lang", "created_ts")
            cache_path.write_text(
                json.dumps({"ts": now_ts, "rows": [{k: r[k] for k in keep} for r in rows]},
                           ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            print("[warn] 写 GitHub 缓存失败（不影响本轮）: %s" % exc)

    return rows[:limit]


# ---- AI 实验室官方博客（最前沿的一手信息，不需要猜）----
# 这些源更新慢（几天一条），所以按「近 N 小时」过滤，没有就整段不出现 ——
# 免得看板上永远是同一条旧新闻。
LAB_FEEDS = [
    ("OpenAI", "https://openai.com/news/rss.xml"),
    ("Google DeepMind", "https://deepmind.google/blog/rss.xml"),
    ("Hugging Face", "https://huggingface.co/blog/feed.xml"),
]
LAB_FRESH_H = float(os.environ.get("LAB_FRESH_H", "").strip() or "48")


def _lab_one(feed):
    """抓单个实验室博客。失败返回空表 —— 一家挂了不影响其他家。"""
    name, url = feed
    try:
        xml = fetch_retry(url, retries=2, timeout=25)
    except Exception as exc:
        print("[lab] %s 抓取失败: %s" % (name, exc))
        return []
    out = []
    for item in re.findall(r"<item>([\s\S]*?)</item>", xml)[:15]:
        t = re.search(r"<title>([\s\S]*?)</title>", item)
        l = re.search(r"<link>([\s\S]*?)</link>", item)
        d = re.search(r"<pubDate>([\s\S]*?)</pubDate>", item)
        if not t or not d:
            continue
        ts = _pub_ts(clean_title(d.group(1)))
        if not ts:
            continue
        age_h = (time.time() - ts) / 3600.0
        if age_h > LAB_FRESH_H:
            continue
        out.append({
            "title": clean_title(t.group(1)),
            "url": (l.group(1).strip() if l else ""),
            "age_h": age_h,
            "age": fmt_age(clean_title(d.group(1))),
            "source": name,
        })
    return out


def lab_posts(limit=8):
    """抓各家 AI 实验室博客的最新文章，只留近 LAB_FRESH_H 小时的。

    三个 feed 并行抓 —— OpenAI 的 RSS 带全文，单个就将近 1MB，
    串行跑光这一段就要 5 秒以上。"""
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(LAB_FEEDS)) as pool:
        for chunk in pool.map(_lab_one, LAB_FEEDS):
            rows.extend(chunk)
    rows.sort(key=lambda r: r["age_h"])
    return rows[:limit]


# ---- 资讯渠道：媒体 RSS ----
# 只有 Google News 一个源时，一小时内被同一批通讯社稿占满，「谁在报道」
# 其实只剩一个视角。这里补上各自的 RSS：
#   ai=True  垂直 AI 媒体，标题天然在范围内，不必再过关键词（过了反而会误杀，
#            比如「GPT-6.2 暴击具身模型」不含任何白名单词，但显然该收）
#   ai=False 综合科技/产业媒体，必须过 is_ai_tech，否则半版都是手机导购和财报
NEWS_FEEDS = [
    # 海外（走代理）
    {"name": "TechCrunch", "url": "https://techcrunch.com/category/artificial-intelligence/feed/",
     "cn": False, "ai": True},
    {"name": "The Decoder", "url": "https://the-decoder.com/feed/",
     "cn": False, "ai": True},
    {"name": "The Verge", "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
     "cn": False, "ai": True},
    {"name": "Techmeme", "url": "https://www.techmeme.com/feed.xml",
     "cn": False, "ai": False},
    {"name": "Ars Technica", "url": "https://feeds.arstechnica.com/arstechnica/index",
     "cn": False, "ai": False},
    {"name": "Simon Willison", "url": "https://simonwillison.net/atom/everything/",
     "cn": False, "ai": True},
    # 国内（直连，走代理反而慢）
    {"name": "量子位", "url": "https://www.qbitai.com/feed", "cn": True, "ai": True},
    {"name": "IT之家", "url": "https://www.ithome.com/rss/", "cn": True, "ai": False},
    {"name": "Solidot", "url": "https://www.solidot.org/index.rss", "cn": True, "ai": False},
    {"name": "Readhub", "url": "https://readhub.cn/rss", "cn": True, "ai": False},
    {"name": "雷锋网", "url": "https://www.leiphone.com/feed", "cn": True, "ai": False},
    {"name": "36氪", "url": "https://www.36kr.com/feed", "cn": True, "ai": False},
]

# 每个源最多留几条。不设上限时 IT之家（单次 60 条）会把整段吃光，
# 那就又变回「一个来源」了。
NEWS_PER_FEED = int(os.environ.get("NEWS_PER_FEED", "").strip() or "3")

# 「具身智能」段的判定词。标题统一转小写再比，所以英文词写小写就行。
# 包含 vla（Vision-Language-Action）和 manipulation —— 这两个是这条线
# 最常用的术语，光靠「具身」「机器人」会漏掉三分之一。
EMBODIED_TERMS = ("具身", "人形", "机器人", "robot", "embodied", "humanoid",
                  "vla", "manipulation", "teleop", "locomotion")

# 机器人表演赛/活动稿。这类新闻量大、上镜，但没有任何技术信息 ——
# 「格斗挑战赛」「拳击金腰带」本质是厂商营销，不是具身智能的进展。
EMBODIED_BLOCK = ("格斗", "拳击", "拔河", "跳舞", "舞蹈", "马拉松", "表演赛",
                  "剪彩", "打卡", "快闪", "巡演", "擂台", "赛事")


def _rss_dt(text):
    """RSS 用 RFC822（`Sun, 22 Nov 2026 03:21:10 GMT`），Atom 用 ISO8601。
    两种都要认，否则 Atom 源（Simon Willison）会全部因为「没时间」被丢掉。"""
    s = clean_title(text)
    if not s:
        return None
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(s)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt
    except Exception:
        pass
    try:
        # 36氪 给的是 `2026-11-21 20:44:52  +0800`（日期和时间之间两个空格），
        # fromisoformat 要求单个空格，先压一下。
        dt = datetime.datetime.fromisoformat(re.sub(r"\s+", " ", s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except Exception:
        return None


def _feed_one(feed, window_h):
    """抓单个媒体源。失败返回空表 —— 一家挂了不能拖垮整段。"""
    name = feed["name"]
    try:
        xml = fetch_retry(feed["url"], retries=2, timeout=20, direct=feed["cn"])
    except Exception as exc:
        print("[news] %s 抓取失败: %s" % (name, exc))
        return []

    blocks = re.findall(r"<item>([\s\S]*?)</item>", xml)
    atom = False
    if not blocks:
        blocks = re.findall(r"<entry>([\s\S]*?)</entry>", xml)
        atom = True
    if not blocks:
        print("[news] %s 不是 RSS/Atom（%d 字节）" % (name, len(xml)))
        return []

    now = datetime.datetime.now(datetime.timezone.utc)
    out = []
    for blk in blocks[:40]:
        t = re.search(r"<title[^>]*>([\s\S]*?)</title>", blk)
        if not t:
            continue
        title = clean_title(t.group(1))
        if not title:
            continue
        if atom:
            m = re.search(r'<link[^>]*href="([^"]+)"', blk)
            link = m.group(1).strip() if m else ""
        else:
            m = re.search(r"<link>([\s\S]*?)</link>", blk)
            link = clean_title(m.group(1)) if m else ""
        d = re.search(r"<(pubDate|published|updated|dc:date)>([\s\S]*?)</\1>", blk)
        dt = _rss_dt(d.group(2)) if d else None
        if dt is None:
            continue  # 没时间就没法判断「是不是这一小时的事」，宁可不要
        age_h = (now - dt).total_seconds() / 3600.0
        if age_h < 0:
            age_h = 0.0
        if age_h > window_h:
            continue
        if not feed["ai"] and not is_ai_tech(title, strict=True):
            continue
        if any(b in title for b in NEWS_TITLE_BLOCK):
            continue
        out.append({
            "title": title,
            "url": link,
            "ts": dt.timestamp(),
            "age_h": age_h,
            "age": fmt_age(d.group(2)),
            "source": name,
            "cn": feed["cn"],
            "n": 1,
        })
    out.sort(key=lambda r: -r["ts"])
    return out[:NEWS_PER_FEED]


def _interleave(rows):
    """按来源轮转取：每个源先出最新一条，再第二轮。

    纯按时间排会让 IT之家 + 36氪 把 8 个位置占满 —— 明明接了十来个渠道，
    看上去还是只有两个来源。轮转能把「渠道多」这件事直接体现在版面里。"""
    buckets, order = {}, []
    for r in rows:
        s = r.get("source") or ""
        if s not in buckets:
            buckets[s] = []
            order.append(s)
        buckets[s].append(r)
    out, i = [], 0
    while True:
        added = False
        for s in order:
            if i < len(buckets[s]):
                out.append(buckets[s][i])
                added = True
        if not added:
            break
        i += 1
    return out


_FEED_ROWS_CACHE = {}


def _all_feed_rows(window_h, skip_en=False):
    """所有媒体源抓一遍，结果缓存。

    「具身智能」段要在这批稿子里筛机器人的，同一轮里不能再抓一遍 ——
    12 个 feed 重抓一次就是 4 秒白花。"""
    key = (round(float(window_h), 1), bool(skip_en))
    if key in _FEED_ROWS_CACHE:
        return _FEED_ROWS_CACHE[key]
    feeds = [f for f in NEWS_FEEDS if not (skip_en and not f["cn"])]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(feeds)) as pool:
        results = list(pool.map(lambda f: _feed_one(f, window_h), feeds))
    rows = [r for chunk in results for r in chunk]
    _FEED_ROWS_CACHE[key] = rows
    return rows


def news_feed_rows(window_h, seen=None, skip_en=False):
    """所有媒体源并行抓，去重后分成海外/国内两组。"""
    seen = seen if seen is not None else set()
    en, cn = [], []
    for r in _all_feed_rows(window_h, skip_en):
        k = _dedup_key(r["title"])
        if not k or k in seen:
            continue
        seen.add(k)
        (cn if r["cn"] else en).append(r)
    en.sort(key=lambda r: -r["ts"])
    cn.sort(key=lambda r: -r["ts"])
    return _interleave(en), _interleave(cn)


# ---- Reddit 热点 ----
# 2026-11-22 实测：本机（含 Clash 7890）访问 reddit.com 一律 429，
# .json 返回 403 拦截页（189KB），redlib 镜像全挂，RSSHub 镜像也挂。
# 唯一能用的是 Arctic Shift 归档 API —— 但它只按 created_utc 排序，
# 且 score 是「入库那一刻」的快照：刚发的帖子永远是 1 分。
# 实测 12 小时以上的帖子会被重新扫描，分数才是真的（219 分、459 分）。
# 所以这一段本质是「过去一天 Reddit 最热的帖子」，不是「正在涨的」。
REDDIT_SUBS = ("LocalLLaMA", "MachineLearning", "LocalLLM", "singularity", "robotics")
REDDIT_MIN_SCORE = int(os.environ.get("REDDIT_MIN_SCORE", "").strip() or "40")
REDDIT_WINDOW_H = float(os.environ.get("REDDIT_WINDOW_H", "").strip() or "36")
REDDIT_LIMIT = int(os.environ.get("REDDIT_LIMIT", "").strip() or "8")
# 每个版块最多几条。不设上限时 r/singularity 一条 4000 分的梗图能把前八名
# 全占了 —— 那版块情绪浓度高、涨分快，但一半是「今天又发生了什么」的闲聊。
REDDIT_PER_SUB = int(os.environ.get("REDDIT_PER_SUB", "").strip() or "2")
ARCTIC_API = "https://arctic-shift.photon-reddit.com/api/posts/search"

# 高热但无信息量的帖式。这类帖分数极高（情绪共鸣），但对「AI 前沿」零价值。
REDDIT_BLOCK = (
    "average day", "final moments", "what did i miss", "am i the only one",
    "in this sub", "shitpost", "meme", "unpopular opinion", "rant",
    "just me?", "how do you feel", "anyone else",
)


def _reddit_archive(sub, window_h):
    """单个版块：取近 window_h 小时的帖子，带上分数。

    一次只能查一个版块 —— 用逗号拼多个实测返回 0 条。
    字段名写错（permalink / stickied）整个请求会 400，只能用白名单里的。"""
    now = datetime.datetime.now(datetime.timezone.utc)
    q = urllib.parse.urlencode({
        "subreddit": sub,
        "limit": "100",
        "after": (now - datetime.timedelta(hours=window_h)).strftime("%Y-%m-%dT%H:%M:%S"),
        "fields": "id,title,score,num_comments,created_utc,subreddit,url",
    })
    data = json.loads(fetch_retry(ARCTIC_API + "?" + q, retries=2, timeout=25))
    out = []
    for p in (data.get("data") or []):
        pid = (p.get("id") or "").strip()
        title = (p.get("title") or "").strip()
        if not pid or not title:
            continue
        out.append({
            "title": title,
            # /comments/<id> 会跳到原帖；用自帖链接的话点开是图片/外链，讨论反而丢了
            "url": "https://www.reddit.com/comments/%s" % pid,
            "score": int(p.get("score") or 0),
            "comments": int(p.get("num_comments") or 0),
            "ts": float(p.get("created_utc") or 0),
            "sub": (p.get("subreddit") or sub).strip(),
        })
    return out


def reddit_hot_posts(limit=REDDIT_LIMIT):
    """几个 AI / 机器人版块合并，按分数排，每个版块限量。"""
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(REDDIT_SUBS)) as pool:
        for chunk in pool.map(lambda s: _reddit_archive(s, REDDIT_WINDOW_H), REDDIT_SUBS):
            rows.extend(chunk)
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    picked, seen, per_sub = [], set(), {}
    rows.sort(key=lambda r: -r["score"])
    for r in rows:
        if r["score"] < REDDIT_MIN_SCORE:
            continue
        low = r["title"].lower()
        if any(b in low for b in REDDIT_BLOCK):
            continue
        k = _dedup_key(r["title"])
        if not k or k in seen:
            continue
        if per_sub.get(r["sub"], 0) >= REDDIT_PER_SUB:
            continue
        seen.add(k)
        per_sub[r["sub"]] = per_sub.get(r["sub"], 0) + 1
        r["age_h"] = max((now - r["ts"]) / 3600.0, 0.1)
        picked.append(r)
    return picked[:limit]


# ---- arXiv 机器人论文（具身智能真正的「前沿」在这）----
ARXIV_API = "https://export.arxiv.org/api/query"


def arxiv_robotics(limit=4, days=5):
    """cs.RO 最新投稿。

    用 API 而不是 rss.arxiv.org：后者只在工作日发公告，周日拿到的是空 feed
    （实测 892 字节、0 条），正好会是「周末这条永远空着」。"""
    q = urllib.parse.urlencode({
        "search_query": "cat:cs.RO",
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": "40",
    })
    xml = fetch_retry(ARXIV_API + "?" + q, retries=2, timeout=25)
    now = datetime.datetime.now(datetime.timezone.utc)
    out = []
    for blk in re.findall(r"<entry>([\s\S]*?)</entry>", xml):
        t = re.search(r"<title>([\s\S]*?)</title>", blk)
        i = re.search(r"<id>([\s\S]*?)</id>", blk)
        d = re.search(r"<published>([\s\S]*?)</published>", blk)
        if not (t and i and d):
            continue
        dt = _rss_dt(d.group(1))
        if dt is None:
            continue
        age_h = (now - dt).total_seconds() / 3600.0
        if age_h > days * 24:
            continue
        title = re.sub(r"\s+", " ", clean_title(t.group(1))).strip()
        if not title:
            continue
        out.append({
            "title": title,
            "url": clean_title(i.group(1)).replace("http://", "https://"),
            "age": fmt_age(d.group(1)),
            "age_h": age_h,
        })
    out.sort(key=lambda r: r["age_h"])
    return out[:limit]




# 发布时间文本 → 天数。YouTube 会按界面语言给「17 小時前」或「17 hours ago」，
# 繁体/简体都可能出现，所以单位要覆盖全。
#
# 2026-11-21 补：YouTube 实际返回的简体形式是 **`8h ago` / `4d ago` / `2m ago`**
# 这种单字母缩写，原来的表里只有 hour/day 全词，导致 age 全部解析失败、
# 播放量涨速算不出来（页面上只剩「总播放」）。单字母必须补上，
# 且 "mo" 要排在 "m" 前面，否则「3 months」会被当成 3 分钟。
_AGE_UNITS = [
    ("秒", 1.0 / 86400), ("second", 1.0 / 86400),
    ("分鐘", 1.0 / 1440), ("分钟", 1.0 / 1440), ("分", 1.0 / 1440),
    ("month", 30.0), ("mo", 30.0),
    ("minute", 1.0 / 1440), ("min", 1.0 / 1440), ("m", 1.0 / 1440),
    ("小時", 1.0 / 24), ("小时", 1.0 / 24), ("hour", 1.0 / 24), ("h", 1.0 / 24),
    ("天", 1.0), ("day", 1.0), ("d", 1.0),
    ("週", 7.0), ("周", 7.0), ("week", 7.0), ("w", 7.0),
    ("個月", 30.0), ("个月", 30.0), ("month", 30.0), ("mo", 30.0),
    ("年", 365.0), ("year", 365.0), ("y", 365.0),
]

_AGE_RE = re.compile(
    r"(\d+)\s*(秒|分鐘|分钟|分|小時|小时|天|週|周|個月|个月|年|"
    r"seconds?|minutes?|months?|hours?|days?|weeks?|years?|mo|[hdwmy])",
    re.I,
)


def age_days(text):
    """把「1 天前」「4d ago」解析成天数；解析不出来返回 None。"""
    if not text:
        return None
    m = _AGE_RE.search(text)
    if not m:
        return None
    n = int(m.group(1))
    u = m.group(2).lower()
    for key, days in _AGE_UNITS:
        if key in u:
            return n * days
    return None


def clean_title(text):
    """RSS 标题里偶尔带 CDATA 包装（OpenAI 的 feed 就是），
    不清掉会原样显示成 `<![CDATA[Combatting ...]]>`。"""
    s = re.sub(r"<!\[CDATA\[([\s\S]*?)\]\]>", r"\1", text or "")
    return html.unescape(s).strip()


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


def parse_views(text):
    """`12万次观看` / `1.2M views` / `3,456 views` → 数字。"""
    s = (text or "").replace(",", "").replace(" ", "")
    m = re.search(r"([\d.]+)\s*(亿|万|M|K|B)", s, re.I)
    if m:
        try:
            n = float(m.group(1))
        except ValueError:
            return 0.0
        mul = {"亿": 1e8, "万": 1e4, "M": 1e6, "K": 1e3, "B": 1e9}.get(m.group(2).upper(), 1)
        return n * mul
    m = re.search(r"(\d+)", s)
    return float(m.group(1)) if m else 0.0


# 搜索词。直接用话题名搜 "AI" 会回来一堆「零基础学 AI」的课程广告，
# 加限定词能把结果压到「新闻 / 发布会 / 实测」这类有信息量的视频上。
YT_QUERIES = {
    "AI": "AI 最新进展",
    "人工智能": "人工智能 最新",
    "科技": "科技 前沿",
}
YT_LIVE_H = float(os.environ.get("YT_LIVE_H", "").strip() or "14")
# YouTube 的时政/军事聚合号很爱在标题里挂「英伟达」「芯片」蹭流量，
# 光靠科技白名单挡不住 —— 这类标题的落点在人物和冲突上，不在技术上。
YT_TITLE_BLOCK = ("热搜", "时政", "大选", "普京", "泽连斯基", "军事", "六代机", "导弹",
                  "鬼畜", "赖清德", "蔡英文", "卖台", "中共", "FBI", "Trump")


def yt_search_recent(query, limit=TOPIC_YT_LIMIT):
    """按话题找「正在涨」的视频。

    这里做过一次方向修正：原来按「发布时间」排，只能说明它新，说明不了它火。
    现在解析播放量，用 **播放量 ÷ 发布时长 = 每小时播放** 排序 ——
    「刚发 6 小时已经 20 万播放」远比「发了 3 天 50 万播放」更值得看。
    时间窗仍卡在 YT_LIVE_H 小时内，否则老视频的累计播放量会把指标彻底拉爆。

    返回 (items, note)，items 每项是 dict。
    """
    cap = max(limit * 5, 25)
    last_err = None
    q = YT_QUERIES.get(query, query)

    for sp, max_age in YT_SP_TIERS:
        # 时间窗收得比比对参数更紧：这里要的是「最近在涨」，不是「本月还行」
        max_age = min(max_age, YT_LIVE_H)
        try:
            raw = _yt_search_raw(q, sp, cap)
        except Exception as exc:
            last_err = exc
            continue
        rows = []
        for title, url, pub, views in raw:
            # 中文 YouTube 搜索污染很重：「AI 最新进展」会捞回军事、农业、时政
            # 这类只是蹭到关键词的视频，必须按标题过一遍白名单和黑名单。
            if not is_ai_tech(title):
                continue
            if any(b in title for b in YT_TITLE_BLOCK):
                continue
            a = age_days(pub)
            if a is not None and a * 24 > max_age:
                continue
            v = parse_views(views)
            age_h = (a * 24) if a is not None else None
            rows.append({
                "title": title, "url": url,
                "heat": v, "heat_text": views,
                "age_h": age_h,
                "vph": vph_abs(v, age_h) if age_h else 0.0,
                "views": v,
            })
        if not rows:
            # 这一档在时间窗内确实没东西。**不要再试下一档**：max_age 已经被
            # YT_LIVE_H 封顶，换 sp 只是放宽取样范围，过滤标准一模一样，
            # 多试一次就是白等 3~5 秒。之前 46 秒一轮主要浪费在这里。
            return [], "近 %d 小时内没有找到符合条件的视频" % YT_LIVE_H
        # 有播放量的排前面；解析不出播放量的垫底但保留
        rows.sort(key=lambda r: -r["vph"])
        return rows[:limit], "近 %d 小时内发布，按每小时播放量排序" % max_age

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
        num = x.get("num") or 0
        meta = " · ".join(v for v in (tag, ("热 " + _cn_num(num)) if num else "") if v)
        out.append({
            "title": word,
            "url": "https://s.weibo.com/weibo?q=%s" % urllib.parse.quote("#" + word + "#"),
            "meta": meta,
            "heat": float(num or 0),
        })
        if len(out) >= limit:
            break
    return out


def _cn_douyin(j, limit):
    out = []
    for x in (j.get("word_list") or []):
        word = (x.get("word") or "").strip()
        if not word:
            continue
        hv = x.get("hot_value") or 0
        out.append({
            "title": word,
            "url": "https://www.douyin.com/search/%s" % urllib.parse.quote(word),
            "meta": ("热度 " + _cn_num(hv)) if hv else "",
            "heat": float(hv or 0),
        })
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
        out.append({
            "title": title,
            "url": "https://www.bilibili.com/video/%s" % bvid if bvid else "",
            "meta": meta,
            "heat": float(views or 0),
        })
        if len(out) >= limit:
            break
    return out


def _cn_toutiao(j, limit):
    out = []
    for x in (j.get("data") or []):
        title = (x.get("Title") or "").strip()
        if not title:
            continue
        hv = x.get("HotValue") or 0
        # 头条给的 URL 后面挂着一长串跟踪参数（实测每条 700+ 字节，
        # 10 条就把消息撑大 7KB），只保留 /trending/<id>/ 这一段。
        link = (x.get("Url") or "").split("?")[0]
        out.append({
            "title": title,
            "url": link,
            "meta": ("热度 " + _cn_num(hv)) if hv else "",
            "heat": float(hv or 0),
        })
        if len(out) >= limit:
            break
    return out


# 顺序就是推送里的顺序：微博 → 抖音 → B站 → 头条
CN_SOURCES = [
    {
        "label": "微博热搜",
        "short": "微博",
        "url": "https://weibo.com/ajax/side/hotSearch",
        "referer": "https://weibo.com/",
        "parse": _cn_weibo,
    },
    {
        "label": "抖音热榜",
        "short": "抖音",
        "url": "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/",
        "referer": "https://www.douyin.com/",
        "parse": _cn_douyin,
    },
    {
        "label": "B站全站排行",
        "short": "B站",
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
        "short": "头条",
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
        # 这里要读响应头里的 Set-Cookie，所以不能直接用 open_bytes。
        # 直连：B站是国内站，走代理只会更慢。
        with OPENER_DIRECT.open(req, timeout=20) as resp:
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
    # 国内接口一律直连。走 Clash 等于「本机 → 代理 → 国内」绕一圈，
    # 实测慢 3~5 倍；代理规则切到全局时还会因为出口 IP 变化被判风控。
    with OPENER_DIRECT.open(req, timeout=30) as resp:
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


CN_SURGE_LIMIT = int(os.environ.get("CN_SURGE_LIMIT", "").strip() or "10")


def cn_surge_section(lines, now_ts, prev_surge, new_surge):
    """国内四榜 → 只留 AI / 科技 → 用跨轮热度差算「每小时涨多少」。

    这是本次改造动作最大的一段。以前是把四个榜单原样倒出来，
    微博热搜前十里有八个是明星八卦 —— 那不是「前沿资讯」，是噪音。
    现在榜单只作为**原料**：滤掉非 AI/科技，剩下的按涨速排。
    第一轮没有历史基线，算不出涨速，就退化成「当前在榜」并说明原因。
    """
    lines.append("## 国内 · AI / 科技 热度激增")
    lines.append("")
    rows, pending, errors = [], [], []

    for src in CN_SOURCES:
        try:
            items = _cn_items(src, max(CN_HOT_LIMIT * 4, 40))
        except Exception as exc:
            errors.append("%s %s" % (src["short"], exc))
            continue
        kept = 0
        for it in items:
            title = it["title"]
            if not is_ai_tech(title, strict=True):
                continue
            kept += 1
            key = "cn|%s|%s" % (src["short"], title)
            rec, info = bump_surge(prev_surge, key, it["heat"], now_ts)
            if rec:
                new_surge[key] = rec
            row = dict(it)
            row["source"] = src["short"]
            if info and info["vph"] > 0:
                row.update(info)
                rows.append(row)
            else:
                # 两种都进 pending：还没攒够观测时间，或者热度在回落。
                # 下面那句提示对两种情况都成立，不用分开写。
                pending.append(row)
        plog("国内 · %s：%d 条命中 AI/科技" % (src["short"], kept))

    rows.sort(key=lambda r: -r.get("vph", 0))
    if rows:
        for i, r in enumerate(rows[:CN_SURGE_LIMIT], 1):
            lines.append("%d. [%s](%s) · %s · %s" % (
                i, r["title"], r["url"], r["source"],
                speed_text(r["vph"], r["delta"], r["span_h"]),
            ))
    elif pending:
        # 两种可能：还没攒够观测时间，或者热度持平/回落。
        # 老实说明，别把「当前在榜」伪装成「激增」。
        lines.append("本轮暂无上涨条目（观测不足 %d 分钟，或热度持平/回落）。当前在榜 %d 条：" % (
            int(SURGE_MIN_SPAN_H * 60), len(pending)))
        for i, r in enumerate(pending[:6], 1):
            lines.append("%d. [%s](%s) · %s%s" % (
                i, r["title"], r["url"], r["source"],
                (" · " + r["meta"]) if r.get("meta") else "",
            ))
    else:
        lines.append("本轮四个平台都没有 AI / 科技相关条目（娱乐与社会新闻已全部滤掉）。")

    if errors:
        lines.append("")
        lines.append("(抓取失败：%s)" % "；".join(errors))
    lines.append("")


def _dedup_key(text):
    return re.sub(r"[\s\W_]+", "", text or "")[:44]


def _news_queries(specs):
    """把话题名翻成 Google News 的查询词。

    「AI」这种两字母词单独搜会回来一堆不相关的缩写命中，
    所以换成「人工智能 / 大模型」这类指向明确的中文词。"""
    env = os.environ.get("NEWS_QUERIES", "").strip()
    if env:
        return [q.strip() for q in re.split(r"[,，、]+", env) if q.strip()][:4]
    names = [s["name"] for s in specs] or ["人工智能"]
    out = []
    for n in names:
        if n in ("AI", "人工智能", "ai"):
            out.extend(["人工智能", "大模型"])
        elif n == "科技":
            out.extend(["芯片 半导体", "科技公司"])
        else:
            out.append(n)
    seen, res = set(), []
    for q in out:
        if q not in seen:
            seen.add(q)
            res.append(q)
    return res[:4]


def hn_section(lines):
    """海外科技圈：按「每小时涨多少分」排。HN 本身就是 AI/科技浓度最高的地方，
    不需要再做关键词过滤。"""
    lines.append("## Hacker News（海外科技圈 · 按 分/小时）")
    lines.append("")
    try:
        rows = hn_stories(12)
    except Exception as exc:
        lines.append("(抓取失败: %s)" % exc)
        lines.append("")
        return
    if not rows:
        lines.append("(本轮没有分数达标的新帖)")
        lines.append("")
        return
    for i, r in enumerate(rows, 1):
        meta = "%s · %d 分 · %.0fh · %d 评论" % (
            speed_text(r["vph"], unit="分"), int(r["heat"]), r["age_h"], r["comments"])
        lines.append("%d. [%s](%s) · %s" % (i, r["title"], r["url"], meta))
    lines.append("")


def github_section(lines):
    """GitHub 新项目的涨星速度。看的是「这几天在爆什么」，不是总星数排行 ——
    总星数榜永远被那几个巨型仓库占着，看不出任何趋势。"""
    lines.append("## GitHub 热榜（近 %d 天新项目 · 按 ★/天）" % GITHUB_WINDOW_D)
    lines.append("")
    try:
        rows = github_trending(force=FORCE_GITHUB)
    except Exception as exc:
        lines.append("(抓取失败: %s)" % exc)
        lines.append("")
        return
    if not rows:
        lines.append("(本轮没有涨星速度达标的项目)")
        lines.append("")
        return
    for i, r in enumerate(rows, 1):
        meta = ["%s★" % fmt_num(r["stars"]), "%s★/天" % fmt_num(r["spd"])]
        meta.append("%.0fh" % r["age_h"] if r["age_h"] < 48 else "%.0f天" % (r["age_h"] / 24))
        if r["lang"]:
            meta.append(r["lang"])
        # 描述是仓库作者自己写的，里面常带 [wiki] / a|b 这类字符，
        # 直接塞进 markdown 会把链接和表格语法搞坏。
        desc = re.sub(r"[\[\]|`<>]", " ", r["desc"])
        desc = re.sub(r"\s{2,}", " ", desc).strip()
        if len(desc) > 60:
            desc = desc[:60].rstrip() + "…"
        head = "[%s](%s)" % (r["title"], r["url"])
        if desc:
            lines.append("%d. %s · %s · %s" % (i, head, desc, " · ".join(meta)))
        else:
            lines.append("%d. %s · %s" % (i, head, " · ".join(meta)))
    if rows and rows[0].get("cached"):
        lines.append("")
        lines.append(
            "(%d 小时更新一次；本段数据为 %d 分钟前抓取，点页面顶部「立即抓取」可强制刷新)"
            % (GITHUB_CACHE_H, rows[0].get("cache_age_min") or 0)
        )
    lines.append("")


def lab_section(lines):
    """AI 实验室官方博客。更新慢，但一条就是一条 —— 这是最一手的前沿。
    近 LAB_FRESH_H 小时内没有新文就整段不出现，免得永远挂着同一条旧闻。"""
    try:
        rows = lab_posts(8)
    except Exception as exc:
        print("[lab] 整段失败: %s" % exc)
        return
    if not rows:
        return
    lines.append("## 实验室动态（近 %d 小时）" % int(LAB_FRESH_H))
    lines.append("")
    for i, r in enumerate(rows, 1):
        lines.append("%d. [%s](%s) · %s · %s" % (
            i, r["title"], r["url"], r["source"], r["age"]))
    lines.append("")


def embodied_section(lines):
    """具身智能 / 人形机器人：单独成段。

    不塞进「前沿资讯」是因为这条线的信号结构和别的不一样 ——
    新闻在中文媒体里一天能出一堆，但真正的前沿在 arXiv 的 cs.RO，
    两部分要分开看：哪个是「谁又发了什么通稿」，哪个是「学术界在做什么」。"""
    lines.append("## 具身智能 · 人形机器人")
    lines.append("")

    # 1) 中文媒体动态（Google News 的中文版对英文查询处理很怪，
    #    "embodied AI" 会返回县城比赛新闻，所以只用中文查询词）
    rows, seen = [], set()
    if not PROXY_DEAD:
        for q in ("具身智能", "人形机器人"):
            try:
                for it in gnews_topic(q, 8, NEWS_WINDOW_H):
                    if any(b in it["title"] for b in NEWS_TITLE_BLOCK):
                        continue
                    if any(b in it["title"] for b in EMBODIED_BLOCK):
                        continue
                    k = _dedup_key(it["title"])
                    if not k or k in seen:
                        continue
                    seen.add(k)
                    rows.append(it)
            except Exception as exc:
                print("[embodied] %s 抓取失败: %s" % (q, exc))
        rows.sort(key=lambda x: (-x["n"], -x.get("ts", 0)))
    else:
        lines.append("(代理未开，Google News 已跳过)")
        lines.append("")

    # 2) 媒体 RSS 里命中机器人关键词的（复用资讯段已抓的 result，不重复请求）
    try:
        for r in _all_feed_rows(NEWS_WINDOW_H, PROXY_DEAD):
            low = r["title"].lower()
            if not any(t in low for t in EMBODIED_TERMS):
                continue
            k = _dedup_key(r["title"])
            if not k or k in seen:
                continue
            seen.add(k)
            row = dict(r)
            row["n"] = 1
            rows.append(row)
    except Exception as exc:
        print("[embodied] 媒体补充失败: %s" % exc)

    if rows:
        lines.append("### 动态（近 %d 小时）" % int(NEWS_WINDOW_H))
        lines.append("")
        for i, r in enumerate(rows[:8], 1):
            extra = []
            if r.get("n", 1) > 1:
                extra.append("%d 家转载" % r["n"])
            if r.get("age"):
                extra.append(r["age"])
            if r.get("source"):
                extra.append(r["source"])
            lines.append("%d. [%s](%s)%s" % (
                i, r["title"], r["url"], (" · " + " · ".join(extra)) if extra else ""))
        lines.append("")
    elif PROXY_DEAD:
        pass
    else:
        lines.append("(近 %d 小时没有抓到相关动态)" % int(NEWS_WINDOW_H))
        lines.append("")

    # 3) arXiv cs.RO 最新投稿 —— 「前沿」在这个位置上最准
    if not PROXY_DEAD:
        lines.append("### arXiv 机器人新论文（cs.RO）")
        lines.append("")
        try:
            papers = arxiv_robotics(4)
            if papers:
                for i, p in enumerate(papers, 1):
                    lines.append("%d. [%s](%s) · %s" % (i, p["title"], p["url"], p["age"]))
            else:
                lines.append("(近几天没有新投稿)")
        except Exception as exc:
            lines.append("(抓取失败: %s)" % exc)
        lines.append("")


def reddit_section(lines):
    """Reddit 热点。

    本机访问 reddit.com 是硬限流（429），只能走 Arctic Shift 归档，
    而归档里的分数只对 12 小时以上的帖子可信 —— 刚发的永远是 1 分。
    所以这段的定位是「过去一天 Reddit 在热什么」，标题里写清楚，别假装实时。"""
    lines.append("## Reddit 热帖（近 %d 小时 · 按分数）" % int(REDDIT_WINDOW_H))
    lines.append("")
    if PROXY_DEAD:
        lines.append("(代理未开，海外源已跳过)")
        lines.append("")
        return
    try:
        rows = reddit_hot_posts()
    except Exception as exc:
        lines.append("(抓取失败: %s)" % exc)
        lines.append("")
        return
    if not rows:
        lines.append("(本轮没有分数达标的帖子)")
        lines.append("")
        return
    for i, r in enumerate(rows, 1):
        lines.append("%d. [%s](%s) · %d 分 · %d 评论 · r/%s" % (
            i, r["title"], r["url"], r["score"], r["comments"], r["sub"]))
    lines.append("")
    lines.append("(分数来自归档接口，约滞后半天；新发的帖子在归档里还只有 1 分)")
    lines.append("")


def news_section(lines, specs):
    """前沿资讯：时间窗卡死，多渠道并收。

    原来只有 Google News 一个源，一小时内的稿子基本来自同一批通讯社，
    「几家转载」看着像多源印证，其实是同一篇通稿被转载。现在三块分开：
    多源印证（Google News 的转载计数）→ 海外媒体 → 国内媒体。"""
    lines.append("## 前沿资讯（近 %d 小时）" % int(NEWS_WINDOW_H))
    lines.append("")
    # 代理没开只影响海外那几块，国内 RSS 是直连的，照抓。
    # （原来这里直接 return，等于 Clash 一关整段都没了。）
    if PROXY_DEAD:
        lines.append("(代理未开，Google News 与海外媒体已跳过)")
        lines.append("")

    seen = set()

    # 1) Google News：唯一能给出「N 家转载」这个热度信号的源
    g_rows = []
    if not PROXY_DEAD:
        for q in _news_queries(specs):
            try:
                for it in gnews_topic(q, 8, NEWS_WINDOW_H):
                    if not is_ai_tech(it["title"]):
                        continue
                    if any(b in it["title"] for b in NEWS_TITLE_BLOCK):
                        continue
                    k = _dedup_key(it["title"])
                    if not k or k in seen:
                        continue
                    seen.add(k)
                    g_rows.append(it)
            except Exception as exc:
                print("[news] %s 抓取失败: %s" % (q, exc))
        g_rows.sort(key=lambda x: (-x["n"], -x.get("ts", 0)))
        # 只有一条媒体在发的稿子，媒体自己的 RSS 会收得更准（还带原文链接）。
        # 这里只留「两家以上同时报道」的 —— 那才是转载数想表达的信号，
        # 否则这一段会重新变成本地政务稿和活动通稿的堆放处。
        g_rows = [r for r in g_rows if r["n"] >= 2]

    # 2) 各家媒体的 RSS（并行，逐源容错）
    en_rows, cn_rows = news_feed_rows(NEWS_WINDOW_H, seen, skip_en=PROXY_DEAD)

    if not (g_rows or en_rows or cn_rows):
        lines.append("(近 %d 小时没有抓到相关资讯)" % int(NEWS_WINDOW_H))
        lines.append("")
        return

    # 把实际用到的渠道列在开头，一眼能看出「来源有多少」
    used = []
    if g_rows:
        used.append("Google News")
    for r in en_rows + cn_rows:
        if r["source"] not in used:
            used.append(r["source"])
    lines.append("渠道：%s" % " · ".join(used))
    lines.append("")

    def emit(rows, cap):
        for i, r in enumerate(rows[:cap], 1):
            extra = []
            if r.get("n", 1) > 1:
                extra.append("%d 家转载" % r["n"])
            if r.get("age"):
                extra.append(r["age"])
            if r.get("source"):
                extra.append(r["source"])
            lines.append("%d. [%s](%s)%s" % (
                i, r["title"], r["url"], (" · " + " · ".join(extra)) if extra else ""))
        lines.append("")

    if g_rows:
        lines.append("### 多源印证（多家同时报道）")
        lines.append("")
        emit(g_rows, 6)
    if en_rows:
        lines.append("### 海外媒体")
        lines.append("")
        emit(en_rows, 9)
    if cn_rows:
        lines.append("### 国内媒体")
        lines.append("")
        emit(cn_rows, 9)



def yt_section(lines, specs):
    """YouTube：按「播放量/小时」排。真正说明「这条正在被大量观看」的是
    播放增速，而不是发布时间。"""
    lines.append("## YouTube（近 %d 小时发布 · 按 播放/小时）" % int(YT_LIVE_H))
    lines.append("")
    if PROXY_DEAD:
        lines.append("(代理未开，海外源已跳过)")
        lines.append("")
        return
    rows, seen, note = [], set(), ""
    # 话题之间并行搜。单次搜索走代理要 6s 左右，两个话题串着跑就是 13s，
    # 这是整轮里最慢的一段。只有 2~3 个并发，不至于触发 YouTube 限流。
    names = ([s["name"] for s in specs] or ["AI"])[:3]

    def _one(name):
        try:
            return yt_search_recent(name, 8)
        except Exception as exc:
            print("[yt] %s 抓取失败: %s" % (name, exc))
            return [], ""

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(names))) as pool:
        results = list(pool.map(_one, names))
    for items, n in results:
        # 第二个查询常常一条都捞不到，别让它把前一个的有效说明覆盖成
        # 「没有找到新视频」—— 那和上面列出的视频自相矛盾。取第一个非空的。
        if items and n and not note:
            note = n
        for it in items:
            if it["url"] in seen:
                continue
            seen.add(it["url"])
            rows.append(it)
    if not rows:
        lines.append("(近 %d 小时没有抓到新视频)" % int(YT_LIVE_H))
        lines.append("")
        return
    rows.sort(key=lambda r: -r["vph"])
    for i, r in enumerate(rows[:12], 1):
        meta = []
        if r["vph"]:
            meta.append(speed_text(r["vph"], unit="播放"))
        if r["heat"]:
            meta.append("总 %s 播放" % fmt_num(r["heat"]))
        if r["age_h"]:
            meta.append("%.0fh" % r["age_h"])
        lines.append("%d. [%s](%s)%s" % (
            i, r["title"], r["url"], (" · " + " · ".join(meta)) if meta else ""))
    if note:
        lines.append("")
        lines.append("(%s)" % note)
    lines.append("")


def _seg(lines, fn, *args):
    """跑一个报告段落并记录耗时。哪一段拖慢整轮，日志里一眼能看出来。"""
    t0 = time.time()
    fn(lines, *args)
    plog("段 %s：%.1fs" % (fn.__name__.replace("_section", ""), time.time() - t0))


def build_report(specs, now_ts, prev_surge, new_surge):
    """报告骨架：从「按平台列榜」改成「按热度涨速」。

    段序就是重要性排序 —— 先海外最硬的技术讨论，再一手实验室动态，
    然后才是新闻、视频、国内。国内放最后是因为它的信号最软
    （平台热度指数，不是真实浏览量），但它是唯一能反映中文舆论的。
    """
    today = bj_now().date().isoformat()
    scope = " / ".join(
        ("%s（%s）" % (s["name"], s["tier"])) if s["tier"] else s["name"] for s in specs
    )
    lines = ["# AI · 科技前沿 · %s" % today, ""]
    lines.append("> 范围：%s｜只收 AI / 科技，无关话题已滤除；各段按热度涨速排序。" % (scope or "AI / 科技"))
    lines.append("")

    _seg(lines, hn_section)
    _seg(lines, github_section)
    _seg(lines, embodied_section)
    _seg(lines, lab_section)
    _seg(lines, news_section, specs)
    _seg(lines, reddit_section)
    _seg(lines, yt_section, specs)
    if CN_HOT:
        cn_surge_section(lines, now_ts, prev_surge, new_surge)
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


def render_board(report, stamp, interval=None, new_idx=None, new_count=0, has_baseline=True):
    """把 Markdown 报告渲染成一个自包含的 HTML 看板。

    不用第三方库：报告结构是固定的（#/##/###/引用/有序列表），正则够用。
    页面自己每 interval 秒整页 reload —— 计划任务每 30 分钟覆盖文件，
    所以最坏情况下内容最多晚 interval 秒才显示出来。

    new_idx 是「本轮新增」的行号集合，命中的 li 会加 .is-new 高亮。"""
    if interval is None:
        interval = BOARD_RELOAD_SEC
    every_text = BOARD_EVERY_TEXT or ("每 %d 分钟重抓" % BOARD_EVERY_MIN)
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
  .btn {
    margin-left: auto; padding: 4px 14px; border-radius: 6px;
    border: 1px solid #d0d7de; background: #fff; color: #1f2328;
    font-size: 13px; font-weight: 600; font-family: inherit; cursor: pointer;
  }
  .btn:hover { background: #f3f4f6; border-color: #afb8c1; }
  .btn:disabled { opacity: .5; cursor: progress; }
  #rf-tip { color: #57606a; }
  #rf-tip.err { color: #cf222e; }
  a { color: #0969da; text-decoration: none; }
  a:hover { text-decoration: underline; }
  main > p { color: #57606a; }
</style></head>
<body><main>
<div class="topbar">
  <span class="dot"></span>
  <span>更新于 <b>%(stamp)s</b></span>
  <span>·</span>
  <span>%(every_text)s，页面 <b id="cd">%(iv)d</b> 秒后自动刷新</span>
  %(chip)s
  <button id="rf" class="btn" type="button">立即抓取</button>
  <span id="rf-tip"></span>
</div>
%(body)s
</main>
<script>
(function () {
  var IV = %(iv)d, left = IV, busy = false;
  var cd = document.getElementById('cd');
  var btn = document.getElementById('rf');
  var tip = document.getElementById('rf-tip');

  // 按钮要打本地服务。页面由服务提供（同端口）时用相对路径；用 file:// 双击
  // 打开、或在别的端口预览时，指向 8765。线上（github.io）没有服务，隐藏按钮。
  var host = location.hostname;
  var isLocal = host === '127.0.0.1' || host === 'localhost';
  var API = (isLocal && location.port === '8765') ? '' : 'http://127.0.0.1:8765';
  if (!isLocal && location.protocol !== 'file:') {
    btn.style.display = 'none';
  }

  // 不用 meta refresh：它在抓取过程中也可能触发整页重载，
  // 把正在等待的请求连同按钮状态一起清掉。
  setInterval(function () {
    if (busy) return;
    left -= 1;
    if (left <= 0) { location.reload(); return; }
    cd.textContent = left;
  }, 1000);

  btn.addEventListener('click', function () {
    if (busy) return;
    busy = true;
    btn.disabled = true;
    btn.textContent = '抓取中…';
    tip.className = '';
    tip.textContent = '约 15 秒';
    fetch(API + '/api/refresh', { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j || !j.ok) throw new Error((j && j.error) || '抓取失败');
        tip.textContent = '完成，正在刷新…';
        setTimeout(function () { location.reload(); }, 400);
      })
      .catch(function (e) {
        busy = false;
        btn.disabled = false;
        btn.textContent = '立即抓取';
        tip.className = 'err';
        tip.textContent = (e.message === 'Failed to fetch')
          ? '连不上服务，双击桌面「启动看板服务」'
          : e.message;
      });
  });
})();
</script>
</body></html>""" % {"iv": interval, "stamp": stamp, "body": "\n".join(body), "chip": chip,
       "every_text": every_text}


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

    now_ts = time.time()
    surge_file = surge_state_path()
    prev_surge = load_surge(surge_file)
    new_surge = {}
    report = build_report(specs, now_ts, prev_surge, new_surge)
    # 合并而不是覆盖：某一轮国内源全挂时，上一轮的基线要留着，否则下一轮又从零开始。
    save_surge(surge_file, prune_surge({**prev_surge, **new_surge}, now_ts), now_ts)
    plog("涨速基线：上轮 %d 条 → 本轮更新 %d 条" % (len(prev_surge), len(new_surge)))

    if FREQUENT:
        # 只换标题，不再压缩。高频档已经不发微信了（消费方是看板），
        # 压缩的唯一理由（一条消息装得下）早就没了，留着只会让看板少看内容。
        stamp = bj_now().strftime("%m-%d %H:%M")
        report = re.sub(
            r"^# [^\n]*", "# AI · 科技前沿 · %s" % stamp, report, count=1, flags=re.M
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
        if FREQUENT or not BOARD_ALSO:
            return

    if FREQUENT:
        # 只留一份覆盖文件，不写按日文件、不动 latest.md
        data_dir.mkdir(exist_ok=True)
        (data_dir / "frequent.md").write_text(report, encoding="utf-8")
        title = "AI 科技前沿 · %s" % bj_now().strftime("%m-%d %H:%M")
        print("[frequent] 写入 data/frequent.md（速报体 %d 条）" % n_items)
    elif adhoc:
        # 临时查询：单独归档，绝不写当日文件、也不覆盖 latest.md，
        # 免得把"今天已经跑过"的守卫状态弄脏。
        target = data_dir / "adhoc" / ("%s-%s.md" % (today, slugify("+".join(topics) or "global")))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(report, encoding="utf-8")
        title = "AI 科技前沿 · %s · %s" % (today, " / ".join(topics) or "AI/科技")
        print("[adhoc] 写入 %s" % target)
    else:
        data_dir.mkdir(exist_ok=True)
        (data_dir / ("%s.md" % today)).write_text(report, encoding="utf-8")
        (data_dir / "latest.md").write_text(report, encoding="utf-8")
        title = "AI 科技前沿 · %s" % today

    sys.stdout.write(report + "\n")

    # 高频档不推送：它的消费方是本地看板（BOARD_PATH）或 data/frequent.md。
    # 一天 144 条推微信既费额度又刷屏，2026-11-13 起改成「抓取归抓取、推送归每日那次」。
    if FREQUENT:
        print("[frequent] 高频档只抓不发（看板/文件即为产出）")
        return

    notify_serverchan(title, report)


if __name__ == "__main__":
    main()
