"""本地捕获服务器：冒充 Langfuse 云端，断言**实际收到的字节**。

⚠️ 为什么不用 mock：mock 掉的正是最容易出问题的接缝
   （上一版 `builder.py:93-97` 就栽在「注释声称安全 ≠ 实际安全」）。

用法：
    with CapturingLangfuse() as cap:
        ...做点什么...
    assert "2023123456" not in cap.body()
"""
from __future__ import annotations

import http.server
import threading


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        self.server.captured.append((self.path, self.rfile.read(n)))  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):  # 静音
        pass


def _reset_sdk_singletons() -> None:
    """清掉 Langfuse SDK 的**进程级单例**，否则第二个捕获服务器收不到任何东西。

    ⚠️⚠️ 不写这一步，用例 2、3 断言的就是**空字节**（实测，2026-10-11）——
       测了个寂寞，且 flush() 会挂死。两层单例都会跨用例串味：

    1. `LangfuseResourceManager.__new__` 是**按 public_key 索引的单例**。
       用例之间 public_key 相同（都是 `pk-lf-*`），于是第二个客户端拿到的
       是**第一个用例那个实例** —— base_url 和 `mask_otel_spans` 全是最早那份。
       `client.shutdown()` **不会**把它从注册表里摘掉（源码 `shutdown()` 只置
       `_shutdown`、停线程），所以必须显式 `reset()`。
    2. OpenTelemetry 全局 TracerProvider **只能 set 一次**（`Once` 护栏），
       第二份会被静默忽略：新客户端依然用第一个用例的 exporter。

    实测后果：用例 2、3 的字节发去**用例 1 已经关掉的端口**（连接被拒 →
    重试 → `flush()` 里 `queue.join()` 永不返回）。现象极具误导性 ——
    看着像「用例 3 卡死」，实际是「根本就没有数据发给这次起的服务器」。

    放在 `__enter__` 里跑：调用方按「先起服务器、再 setup 客户端」的顺序写，
    就自动是对的，不必记得手动清（漏掉就会静默变成空断言）。
    """
    from langfuse._client.resource_manager import LangfuseResourceManager
    from opentelemetry import trace as _otel_trace
    from opentelemetry.util._once import Once

    LangfuseResourceManager.reset()
    _otel_trace._TRACER_PROVIDER = None            # type: ignore[attr-defined]
    _otel_trace._TRACER_PROVIDER_SET_ONCE = Once()  # type: ignore[attr-defined]


class CapturingLangfuse:
    """起一个本地端口假装是 Langfuse，把收到的请求体攒起来。"""

    def __init__(self, port: int = 0):
        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self._srv.captured = []          # type: ignore[attr-defined]
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "CapturingLangfuse":
        _reset_sdk_singletons()
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._srv.shutdown()

    def body(self) -> str:
        """所有捕获到的请求体拼成一个字符串。"""
        return "".join(b.decode("utf-8", errors="replace")
                       for _path, b in self._srv.captured)  # type: ignore[attr-defined]

    def bodies(self) -> list[bytes]:
        """逐次请求的**原始字节**。

        ⚠️ 解 OTLP protobuf 必须用这个，不能用 `body()` ——
           `body()` 是 `errors="replace"` 解码出来的字符串，非 UTF-8 的字节
           已经被换成 U+FFFD，再编回去就是另一份数据，protobuf 会解错或解不动。
        """
        return [b for _path, b in self._srv.captured]  # type: ignore[attr-defined]

    def clear(self) -> None:
        self._srv.captured.clear()  # type: ignore[attr-defined]
