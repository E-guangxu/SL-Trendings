#!/usr/bin/env python3
"""失败告警：往微信（Server酱）发一条纯文本消息。

由 workflow 在 `if: failure()` 时调用 —— 让"静默失败"变成看得见的告警。
这个脚本本身永不返回非 0：告警发不出去不能影响任务结论。

用法：python scripts/alert.py "标题" "正文"
"""

import os
import sys
import urllib.parse
import urllib.request

KEY = os.environ.get("SERVERCHAN_KEY", "").strip()
UA = "Mozilla/5.0 (compatible; SL-Trendings-alert/1.0)"


def main():
    if not KEY:
        print("[alert] 未配置 SERVERCHAN_KEY，跳过告警")
        return
    title = sys.argv[1] if len(sys.argv) > 1 else "每日热榜告警"
    body = sys.argv[2] if len(sys.argv) > 2 else "(无详情)"
    payload = urllib.parse.urlencode({"title": title, "desp": body}).encode("utf-8")
    req = urllib.request.Request(
        "https://sctapi.ftqq.com/%s.send" % KEY,
        data=payload,
        headers={"User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "ignore")
        print("[alert] 告警已发送: %s" % raw[:200])
    except Exception as exc:
        print("[alert] 告警发送失败（不影响主任务结论）: %s" % exc)


if __name__ == "__main__":
    main()
