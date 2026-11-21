# -*- coding: utf-8 -*-
"""看板本地服务：用 HTTP 提供 board.html，并接住「立即抓取」按钮。

为什么需要它：浏览器不允许普通页面直接启动本地程序（安全限制），
按钮唯一能做的动作就是打一个本地接口。没有这个服务时，
board.html 双击（file://）照样能看，只是按钮会提示「服务没在跑」。

安全边界：只绑 127.0.0.1，接口**没有任何鉴权**，所以绝不能改成 0.0.0.0 --
那等于把「跑一次抓取」的能力开放给整个局域网。

用法：
    python scripts/board_server.py          # 前台跑，看得到日志
    pythonw scripts/board_server.py        # 后台跑，无窗口（计划任务用这个）
"""

import json
import os
import pathlib
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("BOARD_PORT", "").strip() or "8765")
REPO = pathlib.Path(__file__).resolve().parent.parent
LOG_DIR = REPO / "logs"
SERVER_LOG = LOG_DIR / "server.log"
BOARD = REPO / "board.html"
HOT = REPO / "scripts" / "hot.py"

# 不新建可见控制台。这个进程本身由 pythonw 拉起，子进程沿用同一个隐藏控制台。
CREATE_NO_WINDOW = 0x08000000

# 一次只允许一轮抓取。第二个请求立刻拿到 409，而不是排队等 15 秒。
_RUN_LOCK = threading.Lock()


def log(msg):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(SERVER_LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n"
                    % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def child_env():
    """和 hot-board.pyw 保持同一套参数。

    FORCE_GITHUB=1 是按钮和定时任务的关键区别：定时轮次用 GitHub 的 1 小时缓存，
    而人主动点按钮时，期望的是「现在就去拿最新的」，所以绕过缓存。
    """
    env = os.environ.copy()
    env.update({
        "MODE": "frequent",
        "BOARD_PATH": str(BOARD),
        "BOARD_EVERY_MIN": "30",
        "BOARD_RELOAD_SEC": "300",
        "SCRAPE_PROXY": "http://127.0.0.1:7890",
        "FETCH_TIMEOUT": "20",
        "FETCH_RETRIES": "2",
        "SKIP_REDDIT": "1",
        "REDDIT_GAP": "5",
        "CN_HOT": "true",
        "SERVERCHAN_KEY": "",
        "FORCE_GITHUB": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    return env


def run_refresh():
    """跑一轮抓取。返回 (ok, took_sec, error)。已被占用时 ok=None。"""
    if not _RUN_LOCK.acquire(blocking=False):
        return None, 0.0, "上一轮还在抓取中，请稍等"
    t0 = time.time()
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(SERVER_LOG, "ab") as out:
            proc = subprocess.run(
                [sys.executable, "-u", str(HOT)],
                cwd=str(REPO),
                env=child_env(),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=out,
                timeout=300,
                creationflags=CREATE_NO_WINDOW,
            )
        took = time.time() - t0
        log("手动刷新%s，耗时 %.1fs" % ("成功" if proc.returncode == 0 else "失败", took))
        if proc.returncode != 0:
            return False, took, "hot.py 退出码 %d，详见 logs/server.log" % proc.returncode
        return True, took, ""
    except subprocess.TimeoutExpired:
        log("手动刷新超时（>300s）")
        return False, time.time() - t0, "抓取超时（超过 5 分钟）"
    except Exception as exc:
        log("手动刷新异常: %r" % (exc,))
        return False, time.time() - t0, "启动失败: %s" % exc
    finally:
        _RUN_LOCK.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "HotBoard/1.0"

    # pythonw 下 sys.stderr 是 None，父类默认实现往 stderr 写会抛异常。
    def log_message(self, fmt, *args):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        # 页面用 file:// 双击打开时，浏览器把它当成「公网来源访问本机地址」，
        # 走 Private Network Access 检查。不带这个头，预检就失败，
        # 表现为按钮一点就报 Failed to fetch，但服务其实是好的。
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _board(self):
        try:
            data = BOARD.read_bytes()
        except FileNotFoundError:
            self._json(404, {"ok": False, "error": "board.html 还不存在，先跑一轮抓取"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        # 绝不能缓存：计划任务每 30 分钟覆盖这个文件，缓存住就永远看不到新内容
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html", "/board.html"):
            self._board()
        elif path == "/api/status":
            self._json(200, {
                "ok": True,
                "running": _RUN_LOCK.locked(),
                "board_mtime": BOARD.stat().st_mtime if BOARD.exists() else 0,
            })
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path != "/api/refresh":
            self._json(404, {"ok": False, "error": "not found"})
            return
        ok, took, err = run_refresh()
        if ok is None:
            self._json(409, {"ok": False, "error": err})
        elif ok:
            self._json(200, {"ok": True, "took": round(took, 1)})
        else:
            self._json(500, {"ok": False, "error": err})


def main():
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as exc:
        # 端口被占用基本等于「服务已经在跑了」，不是错误，安静退出。
        log("端口 %d 起不来（可能已有实例在跑）: %s" % (PORT, exc))
        return 0
    log("服务启动 http://127.0.0.1:%d  pid=%d" % (PORT, os.getpid()))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        log("服务停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
