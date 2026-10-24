#!/usr/bin/env python
"""本机真实发信 e2e。

使用前提：
- email 服务在本机 127.0.0.1:8010 运行，且 MAIL_MODE=smtp；
- 已通过 SSH 隧道把本机 127.0.0.1:2525 转到服务器的 Postfix 25 端口；
- 你提供一封可查收的测试邮箱。

脚本会：
1. 在本机 127.0.0.1:9001 起 callback 接收器；
2. 调 POST /v1/verify，真实发邮件；
3. 等你在邮箱里点确认链接（本机浏览器打开确认页并点“确认”）；
4. 收到 callback 后校验 HMAC、状态和 delivery，再查一次状态接口。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.security import verify_callback  # noqa: E402


class CallbackState:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.body: bytes = b""
        self.headers: dict[str, str] = {}
        self.error: str | None = None


def make_handler(state: CallbackState, secret: str):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("content-length", "0") or 0)
            body = self.rfile.read(length)
            state.body = body
            state.headers = {key.lower(): value for key, value in self.headers.items()}
            try:
                timestamp = int(state.headers.get("x-elicloud-timestamp", "0"))
                signature = state.headers.get("x-elicloud-signature", "")
                if not verify_callback(secret, timestamp, body, signature):
                    state.error = "invalid callback signature"
                elif state.headers.get("x-elicloud-event") != "email.verification":
                    state.error = "unexpected event header"
            except Exception as exc:  # noqa: BLE001
                state.error = f"{type(exc).__name__}: {exc}"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            state.event.set()

        def log_message(self, _format: str, *_args) -> None:
            return

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--to", required=True, help="真实收件邮箱")
    parser.add_argument("--purpose", default="register")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--callback-port", type=int, default=9001)
    args = parser.parse_args()

    token = os.environ.get("EMAIL_INTERNAL_TOKEN", "")
    secret = os.environ.get("CALLBACK_SIGNING_SECRET", "")
    if not token or not secret:
        print("缺少 EMAIL_INTERNAL_TOKEN / CALLBACK_SIGNING_SECRET 环境变量", file=sys.stderr)
        return 1

    state = CallbackState()
    server = ThreadingHTTPServer(("127.0.0.1", args.callback_port), make_handler(state, secret))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    callback_url = f"http://127.0.0.1:{args.callback_port}/cb"
    payload = {
        "email": args.to,
        "purpose": args.purpose,
        "source": "local-e2e",
        "callback_url": callback_url,
        "metadata": {"test": "local-e2e"},
        "expires_in": 900,
        "content": {"mode": "template", "template": "register"},
    }
    print(f"POST {args.base_url}/v1/verify -> {args.to}")
    response = httpx.post(
        f"{args.base_url}/v1/verify",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    print(f"HTTP {response.status_code}: {response.text}")
    if response.status_code != 202:
        server.shutdown()
        return 1
    request_id = response.json()["id"]
    print(f"\n请求已创建：{request_id}")
    print(f"确认邮件已发往 {args.to}。")
    print("请在这台电脑的浏览器打开邮件里唯一的确认按钮；点击即确认（无需网页二次确认）。")
    print(f"等待 callback（最长 {args.timeout}s）...")

    deadline = time.time() + args.timeout
    while time.time() < deadline and not state.event.is_set():
        time.sleep(0.5)
    server.shutdown()

    if not state.event.is_set():
        print("超时：没有收到 callback", file=sys.stderr)
        return 1
    if state.error:
        print(f"callback 校验失败：{state.error}", file=sys.stderr)
        return 1

    body = json.loads(state.body)
    print("\n收到 callback：")
    print(json.dumps(body, ensure_ascii=False, indent=2))
    if body.get("status") != "approved" or body.get("request_id") != request_id:
        print("callback 状态/ID 不符合预期", file=sys.stderr)
        return 1

    status = httpx.get(
        f"{args.base_url}/v1/verify/{request_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    print("\n状态接口：")
    print(status.text)
    data = status.json()
    if data.get("status") != "approved":
        print("状态不是 approved", file=sys.stderr)
        return 1
    print("\nOK：真实发信 + 确认 + 签名 callback 全链路通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
