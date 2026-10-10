"""Start the standalone monitor; never starts or stops another application."""

from __future__ import annotations

import argparse
import errno
import sys
import webbrowser
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _port(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 0 <= number <= 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only ETF premium monitor (127.0.0.1 only)")
    parser.add_argument("--port", type=_port, default=8765, help="local port; default 8765, 0 selects a free port")
    parser.add_argument("--open-browser", action="store_true", help="open the local page after binding successfully")
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        print("需要 Python 3.10 或更新版本。", file=sys.stderr)
        return 1
    try:
        ZoneInfo("Asia/Shanghai")
    except ZoneInfoNotFoundError:
        print("缺少时区数据，请运行：python -m pip install -r requirements.txt", file=sys.stderr)
        return 1

    from etf_premium.web import create_server

    try:
        server = create_server(port=args.port)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) == 10048:
            print(f"端口 {args.port} 已被占用；请使用 --port 8766 等其他端口。未停止任何进程。", file=sys.stderr)
        else:
            print(f"无法绑定本地端口 {args.port}（错误代码 {exc.errno}）。", file=sys.stderr)
        return 1

    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"ETF 溢价监测：{url}", flush=True)
    print("仅监听本机；按 Ctrl+C 退出。打开页面后才会请求公开行情。", flush=True)
    if args.open_browser:
        try:
            if not webbrowser.open(url):
                print("未能自动打开浏览器，请手动访问上方地址。", file=sys.stderr)
        except webbrowser.Error:
            print("未能自动打开浏览器，请手动访问上方地址。", file=sys.stderr)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n服务已停止。", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
