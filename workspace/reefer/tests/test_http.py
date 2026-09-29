"""HTTP 接口端到端：起真实端口，操作台页面与主要 API 全部走网络。"""
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from reefer.server import make_handler
from reefer.service import Service, ServiceError
from .fixture import topology_body, fleet_body, seeded_service, t


def _request(url, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="reefer-http-")
        cls.svc = seeded_service(cls.dir)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                        make_handler(cls.svc))
        cls.port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.th = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.th.start()
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def test_console_page_and_readonly_views(self):
        with urllib.request.urlopen(self.base + "/reefer", timeout=5) as r:
            html = r.read().decode()
        self.assertIn("冷藏箱岸电", html)
        for path in ("/healthz", "/reefer/overview", "/reefer/topology",
                     "/reefer/loads", "/reefer/unpowered",
                     "/reefer/quarantine", "/reefer/trips",
                     "/reefer/migrations", "/reefer/events",
                     "/reefer/verify", "/reefer/samples"):
            st, j = _request(self.base + path)
            self.assertEqual(st, 200, path)
        st, j = _request(self.base + "/reefer/verify")
        self.assertTrue(j["ok"], j["problems"])

    def test_trip_and_migration_over_http(self):
        st, j = _request(self.base + "/reefer/trips", "POST",
                         {"circuit_id": "C1", "reason": "http e2e"})
        self.assertEqual(st, 201)
        st, j = _request(self.base + "/reefer/migrations", "POST",
                         {"circuit_id": "C1", "operator": "ET-http"})
        self.assertIn(st, (201, 207))
        plan_id = j["result"]["plan_id"]
        step = j["result"]["steps"][0]
        for action in ("unplug", "occupy", "energize"):
            st, j = _request(self.base + "/reefer/migrations/confirm",
                             "POST", {"plan_id": plan_id,
                                      "step_id": step["step_id"],
                                      "action": action})
            self.assertEqual(st, 200, j)
        st, j = _request(self.base + "/reefer/verify")
        self.assertTrue(j["ok"], j["problems"])

    def test_command_id_conflict_over_http(self):
        body = {"command_id": "SAME",
                "samples": [{"box_id": "RFU4", "kind": "temp", "value": 1,
                             "sensor": "X", "measured_at": t(90)}]}
        st, j = _request(self.base + "/reefer/samples", "POST", body)
        self.assertEqual(st, 200)
        # 同体重放（HTTP 层把 replay 标志放在响应顶层）
        st, j = _request(self.base + "/reefer/samples", "POST", body)
        self.assertEqual(st, 200)
        self.assertTrue(j["replay"])
        # 同命令标识不同载荷（测量值变了）
        body2 = {"command_id": "SAME",
                 "samples": [{"box_id": "RFU4", "kind": "temp", "value": 999,
                              "sensor": "X", "measured_at": t(90)}]}
        st, j = _request(self.base + "/reefer/samples", "POST", body2)
        self.assertEqual(st, 409)
        self.assertEqual(j["error"]["code"], "HASH_CONFLICT")

    def test_concurrent_reservations_one_winner(self):
        # 两个并发请求抢同一空闲插座：恰好一个成功
        svc = Service(tempfile.mkdtemp(prefix="reefer-conc-"))
        svc.register_topology(topology_body(), at=t(0))
        svc.import_fleet({"containers": [
            {"id": f"BX{i}", "type": "RF", "phase": "3P", "voltage": 440,
             "rated_a": 10, "demand_a": 10, "priority": 1,
             "zones": ["Z1"], "hold_hours": 4} for i in range(2)]}, at=t(1))
        import concurrent.futures

        def hit(i):
            try:
                return (svc.reserve_socket(
                    {"box_id": f"BX{i}", "socket_id": "S2A",
                     "command_id": f"C{i}"}, at=t(2 + i))[0], "")
            except ServiceError as e:
                return (e.status, e.code)
        with concurrent.futures.ThreadPoolExecutor(2) as ex:
            codes = sorted(code for code, _ in ex.map(hit, range(2)))
        self.assertEqual(codes, [201, 409])
        self.assertEqual(len(svc.state["leases"]), 1)


if __name__ == "__main__":
    unittest.main()
