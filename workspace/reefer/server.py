"""冷藏箱岸电模块 HTTP 服务（Python 标准库）。

写接口（均可在 JSON 体内携带 command_id 作为命令标识）：
  POST /reefer/topology            登记配电板/回路/插座
  POST /reefer/fleet               导入本航次冷藏箱需求与温控优先级
  POST /reefer/reservations        指定箱+插座预留
  POST /reefer/reservations/auto   按优先级批量预留
  POST /reefer/reservations/release 释放未送电预留
  POST /reefer/energize            接电终端提交箱号+插座号+命令标识 -> 供电
  POST /reefer/deenergize          断电（回到预留）
  POST /reefer/samples             电流/温度/掉电采样批量上报
  POST /reefer/samples/review      待审样本处置 accept|reject
  POST /reefer/trips               回路跳闸登记
  POST /reefer/reset               回路复位
  POST /reefer/migrations          跳闸后生成有序迁移方案（保留目标插座）
  POST /reefer/migrations/confirm  逐步确认 unplug|occupy|energize
  POST /reefer/migrations/cancel   取消方案（释放未执行步骤）

读接口：
  GET  /reefer/overview / topology / loads / unpowered / samples
  GET  /reefer/quarantine / trips / migrations / events / verify
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import views
from .service import Service, ServiceError, utc_now


def make_handler(svc: Service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ReeferShorePower/1.0"

        def log_message(self, fmt, *args):
            pass

        # ---------------------------------------------------------- helpers
        def _send(self, status, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type",
                             "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(n).decode("utf-8") if n else "{}"
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                raise ServiceError(400, "BAD_JSON", "非法 JSON")
            if not isinstance(body, dict):
                raise ServiceError(400, "BAD_BODY", "请求体必须是 JSON 对象")
            return body

        def _at(self, body):
            return body.get("now") or utc_now()

        def _call(self, fn, **kw):
            try:
                status, result, replay = fn(**kw)
                return self._send(status,
                                  {"result": result, "replay": replay})
            except ServiceError as e:
                return self._send(e.status,
                                  {"error": {"code": e.code,
                                             "message": e.message,
                                             **e.extra}})

        def _console(self):
            path = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "static", "reefer.html")
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except OSError:
                return self._send(404, {"error": {"code": "NO_CONSOLE"}})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # -------------------------------------------------------------- GET
        def do_GET(self):
            path = self.path.split("?")[0]
            try:
                if path in ("/", "/console", "/reefer"):
                    return self._console()
                if path == "/healthz":
                    return self._send(200, {"ok": True})
                routes = {
                    "/reefer/overview": lambda: views.overview(svc),
                    "/reefer/topology": lambda: views.topology(svc),
                    "/reefer/loads": lambda: views.circuit_loads(svc),
                    "/reefer/unpowered": lambda: views.unpowered_boxes(svc),
                    "/reefer/quarantine": lambda: views.quarantine(svc),
                    "/reefer/trips": lambda: views.trip_impacts(svc),
                    "/reefer/migrations": lambda: views.migrations(svc),
                    "/reefer/samples": lambda: views.samples(svc),
                    "/reefer/events": lambda: views.event_stream(svc),
                    "/reefer/verify": lambda: views.verify_invariants(svc),
                }
                if path in routes:
                    return self._send(200, routes[path]())
                self._send(404, {"error": {"code": "NOT_FOUND",
                                           "path": path}})
            except Exception as e:  # noqa: BLE001
                self._send(500, {"error": {"code": "INTERNAL",
                                           "message": str(e)}})

        # ------------------------------------------------------------- POST
        def do_POST(self):
            path = self.path.split("?")[0]
            try:
                body = self._body()
            except ServiceError as e:
                return self._send(e.status,
                                  {"error": {"code": e.code,
                                             "message": e.message}})
            at = self._at(body)
            try:
                if path == "/reefer/topology":
                    return self._call(svc.register_topology, body=body, at=at)
                if path == "/reefer/fleet":
                    return self._call(svc.import_fleet, body=body, at=at)
                if path == "/reefer/reservations":
                    return self._call(svc.reserve_socket, body=body, at=at)
                if path == "/reefer/reservations/auto":
                    return self._call(svc.auto_reserve, body=body, at=at)
                if path == "/reefer/reservations/release":
                    return self._call(svc.release_reservation, body=body, at=at)
                if path == "/reefer/energize":
                    return self._call(svc.energize, body=body, at=at)
                if path == "/reefer/deenergize":
                    return self._call(svc.deenergize, body=body, at=at)
                if path == "/reefer/samples":
                    return self._call(svc.ingest_samples, body=body, at=at)
                if path == "/reefer/samples/review":
                    return self._call(svc.review_sample, body=body, at=at)
                if path == "/reefer/trips":
                    return self._call(svc.trip_circuit, body=body, at=at)
                if path == "/reefer/reset":
                    return self._call(svc.reset_circuit, body=body, at=at)
                if path == "/reefer/migrations":
                    return self._call(svc.create_migration, body=body, at=at)
                if path == "/reefer/migrations/confirm":
                    return self._call(svc.confirm_step, body=body, at=at)
                if path == "/reefer/migrations/cancel":
                    return self._call(svc.cancel_migration, body=body, at=at)
                if path == "/reefer/snapshot":
                    seq = svc.force_snapshot()
                    return self._send(200, {"snapshot_seq": seq})
                self._send(404, {"error": {"code": "NOT_FOUND",
                                           "path": path}})
            except Exception as e:  # noqa: BLE001
                self._send(500, {"error": {"code": "INTERNAL",
                                           "message": str(e)}})

    return Handler


def serve(data_dir: str, port: int = 8095, host="127.0.0.1"):
    svc = Service(data_dir)
    httpd = ThreadingHTTPServer((host, port), make_handler(svc))
    print(f"[reefer] 冷藏箱岸电服务 http://{host}:{port}  操作台 /reefer  "
          f"(data={data_dir}, seq={svc.state['seq']})")
    httpd.serve_forever()


def main(argv=None):
    import sys
    argv = argv or sys.argv
    port = int(argv[1]) if len(argv) > 1 else 8095
    data_dir = os.environ.get(
        "REEFER_DATA",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "reefer_data"))
    if len(argv) > 2:
        data_dir = argv[2]
    serve(data_dir, port)


if __name__ == "__main__":
    main()
