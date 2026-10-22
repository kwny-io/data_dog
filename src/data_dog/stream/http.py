"""HTTP 推送/流式接收源（标准库 `http.server`）。

场景
----
设备侧没法跑我们的 HTTP 客户端、只会往一个 URL **POST** 遥测（webhook 风格），
或我们用 `curl`/`requests` 主动喂数据做联调。本源起一个极简 HTTP 服务把这些
推送收进队列，再走同一套数据面。

身份从哪来
----------
- 只信**请求头** `X-Tenant-Id` / `X-Device-Sn`（与主服务的 `/v1/telemetry`
  契约一致）；正文里的同名字段丢弃。
- 可选 `require_device_header=False` + `device_from_field`：从正文取设备号
  （某些设备固件不带头），此时 `tenant_id` 仍只认头。

安全边界（诚实说明）
--------------------
这是**接入用**的极简服务，不含 TLS、不含鉴权。生产应置于反向代理/网关之后
（由网关注入并校验身份），或直接用主服务的 `/v1/telemetry`（已有 API key +
CORS + 租户头校验）。本源的价值在于"设备只会 POST"这一现实场景的落点。

用标准库而非 aiohttp/FastAPI：避免在服务进程里再起一个 ASGI 应用（端口、生命周期
都要额外管），`http.server` 的 `ThreadingHTTPServer` 足够接住轻量推送。
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .base import QueueSource, StreamMessage

_log = logging.getLogger("data_dog.stream.http")


class HttpPushSource(QueueSource):
    """起一个线程化的 HTTP 服务，把 POST 的遥测塞进队列。"""

    name = "http"

    def __init__(self, *, tenant_id: str, host: str = "127.0.0.1",
                 port: int = 8099, device_from_field: str = "",
                 path: str = "/ingest", max_body: int = 256 * 1024,
                 maxsize: int = 10_000, read_timeout: float = 10.0) -> None:
        super().__init__(maxsize=maxsize)
        self._tenant = tenant_id
        self._device_field = device_from_field
        self._path = path
        self._max_body = max_body
        self._read_timeout = read_timeout
        src = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):            # 静音默认的 stderr 访问日志
                pass

            def do_POST(self):                    # noqa: N802 - stdlib 约定
                # 客户端半途断开（RST / 写响应时对端已关）是**正常现象**，不是服务端
                # 错误。不在这里兜住的话，handler 线程会抛未捕获异常：生产日志刷噪声，
                # 测试里被 pytest 的 thread-exception 警告（filterwarnings=error）抓成随机红。
                try:
                    self._handle_post()
                except (ConnectionError, TimeoutError) as e:
                    _log.debug("HTTP 推送客户端断开：%s", e)

            # 读请求体必须有上限等待：客户端可以声明 1MB 却只发 10 字节然后挂在半路，
            # `rfile.read(n)` 会一直阻塞——`ThreadingHTTPServer` 下每个这样的连接
            # 吃掉一个线程（慢速攻击）。`StreamRequestHandler.setup()` 会把这个类属性
            # 应用到 socket 上，超时后 `handle_one_request` 收尾关连接。
            # 可配（`read_timeout`）而不是写死：测试要能在 0.5s 内验证超时真的生效。
            timeout = src._read_timeout

            def _drain(self) -> None:
                """回错误响应前把请求体读掉。

                为什么不直接 `send_error`：没读完的请求体留在内核接收缓冲区里，
                连接关闭时（Linux/Windows 都一样）回的是 **RST 而不是 FIN**，
                客户端拿到 `RemoteDisconnected` / `ConnectionResetError`，
                而不是我们刚发出的 404/400——错误码在客户端侧凭空消失。
                `test_http_push_rejects_bad_json_and_wrong_path` 曾因此在全量套件
                （变慢、写入晚到）下随机红，单跑却永远绿。
                超大声明长度（413 路径）不排空：排空它等于帮攻击者把带宽吃满，
                那种情况就该直接断开。
                """
                try:
                    n = int(self.headers.get("Content-Length", "0") or 0)
                except ValueError:
                    return
                if 0 < n <= src._max_body:
                    try:
                        self.rfile.read(n)
                    except (ConnectionError, TimeoutError) as e:
                        _log.debug("排空请求体时客户端已断开：%s", e)

            def _handle_post(self):
                if self.path.split("?", 1)[0] != src._path:
                    self._drain()
                    self.send_error(404)
                    return
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self.send_error(400)
                    return
                if n <= 0 or n > src._max_body:
                    self.send_error(413)
                    return
                raw = self.rfile.read(n)
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self.send_error(400, "invalid JSON")
                    return
                tenant = self.headers.get("X-Tenant-Id") or src._tenant
                device = self.headers.get("X-Device-Sn", "")
                if not device and src._device_field and isinstance(body, dict):
                    device = str(body.get(src._device_field) or "")
                src.push(StreamMessage(
                    payload=body if isinstance(body, dict) else {"raw": body},
                    auth={"tenant_id": tenant, "device_sn": device,
                          "principal": "http-push"},
                    source="http"))
                self.send_response(202)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

        self._server = ThreadingHTTPServer((host, port), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="dd-http-push", daemon=True)
        self._thread.start()

    @property
    def address(self) -> tuple[str, int]:
        h, p = self._server.server_address[:2]
        return str(h), int(p)

    def close(self) -> None:
        super().close()
        self._server.shutdown()
        self._server.server_close()
