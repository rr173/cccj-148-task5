"""验收 7：持久化边界强制终止后的归并恢复 + 快照/撕裂流水。

恢复后不得出现：
  - 双重占用（一个箱两个插座 / 一个插座两个箱）；
  - 负载统计失真；
  - 迁移已完成但箱仍标记断电。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

from reefer.service import Service, ServiceError
from reefer import views
from .fixture import topology_body, fleet_body, t


def run_crash_worker(data_dir, point):
    env = dict(os.environ)
    env["REEFER_ALLOW_CRASH"] = "1"
    env["REEFER_CRASH"] = point
    proc = subprocess.run(
        [sys.executable, "-m", "reefer.tests.crash_worker", data_dir, point],
        env=env, capture_output=True, text=True)
    return proc


class CrashRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="reefer-crash-")

    def _healthy(self, svc):
        v = views.verify_invariants(svc)
        self.assertTrue(v["ok"], f"不变量失败: {v['problems']}")
        return v

    def test_crash_after_fsync_command_is_durable_and_replays(self):
        # 预留批次已 fsync 即终止 -> 重启后预留必须存在；凭命令标识重发得到
        # 首次答复而不是再建一条。
        proc = run_crash_worker(self.dir, "reserve_after")
        self.assertEqual(proc.returncode, 77, proc.stderr)
        svc = Service(self.dir)
        self.assertEqual(svc.state["leases"]["S2A"]["box_id"], "RFZ")
        self.assertEqual(svc.state["leases"]["S2A"]["status"], "reserved")
        self._healthy(svc)
        # 命令标识重放（首次答复冻结在 CommandResult）
        _, res, replay = svc.reserve_socket(
            {"box_id": "RFZ", "socket_id": "S2A",
             "command_id": "CMD-CRASH-RESV"}, at=t(40))
        self.assertTrue(replay)
        self.assertEqual(res["reservation_id"],
                         svc.state["leases"]["S2A"]["reservation_id"])

    def test_crash_before_append_command_is_absent(self):
        proc = run_crash_worker(self.dir, "reserve_before")
        self.assertEqual(proc.returncode, 77, proc.stderr)
        svc = Service(self.dir)
        # 崩溃点精确武装在预留批次（SocketReserved）；此前的 RFZ 导入已落盘，
        # 但预留本身不存在：S2A 空闲、RFZ 无租约。
        self.assertNotIn("S2A", svc.state["leases"])
        self.assertIn("RFZ", svc.state["containers"])
        self._healthy(svc)
        # 重发预留命令：正常生效
        st, res, replay = svc.reserve_socket(
            {"box_id": "RFZ", "socket_id": "S2A",
             "command_id": "CMD-CRASH-RESV"}, at=t(41))
        self.assertEqual(st, 201)
        self.assertFalse(replay)

    def test_crash_after_energize_durable(self):
        proc = run_crash_worker(self.dir, "energize_after")
        self.assertEqual(proc.returncode, 77, proc.stderr)
        svc = Service(self.dir)
        self.assertEqual(svc.state["leases"]["S2A"]["status"], "powered")
        loads = {c["circuit_id"]: c["committed_a"]
                 for c in views.circuit_loads(svc)["circuits"]}
        self.assertAlmostEqual(loads["C2"], 10)
        self._healthy(svc)

    def test_crash_after_trip_freezes_durably(self):
        proc = run_crash_worker(self.dir, "trip_after")
        self.assertEqual(proc.returncode, 77, proc.stderr)
        svc = Service(self.dir)
        self.assertIn("C1", svc.state["trips"])
        self._healthy(svc)
        with self.assertRaises(ServiceError) as cm:
            svc.reserve_socket({"box_id": "RFU4", "socket_id": "S1C"},
                               at=t(40))
        # RFU4 是 1P，先撞相制；用直接送电验证冻结由专门视图佐证
        trips = views.trip_impacts(svc, now=t(45))["trips"]
        self.assertEqual({x["circuit_id"] for x in trips}, {"C1"})
        self.assertEqual(
            {b["box_id"] for x in trips for b in x["affected_boxes"]},
            {"RFU1", "RFU2", "RFU3"})

    def test_crash_mid_swap_then_commit_is_atomic(self):
        # 崩溃于"目标已占用、换插座未提交"：原插座仍绑定拔除中的箱，
        # 目标仍被保留；恢复后凭命令标识继续送电，单条事件原子转正。
        proc = run_crash_worker(self.dir, "swap_occupy_after")
        self.assertEqual(proc.returncode, 77, proc.stderr)
        svc = Service(self.dir)
        self._healthy(svc)
        self.assertEqual(svc.state["leases"]["S1A"]["status"], "unplugged")
        self.assertEqual(svc.state["leases"]["S1A"]["box_id"], "RFU1")
        # 找到 RFU1 在方案中保留的目标插座（贪心分配可能是 S2A/S2B）
        target = next(sid for sid, h in svc.state["holds"].items()
                      if h["box_id"] == "RFU1")
        self.assertNotIn(target, svc.state["leases"])  # 绝无双重占用
        self.assertEqual(svc.state["holds"][target]["box_id"], "RFU1")
        plan_id = next(iter(svc.state["plans"]))
        step_id = svc.state["holds"][target]["step_id"]
        # 目标占用命令是幂等的：重放首次答复（target_occupied）
        _, r1, replay = svc.confirm_step(
            {"plan_id": plan_id, "step_id": step_id, "action": "occupy",
             "command_id": "CMD-CRASH-OCC"}, at=t(40))
        self.assertTrue(replay)
        self.assertEqual(r1["status"], "target_occupied")
        # 继续完成换插座（送电）
        _, r2, _ = svc.confirm_step(
            {"plan_id": plan_id, "step_id": step_id, "action": "energize",
             "command_id": "CMD-RECOVER-SWAP"}, at=t(41))
        self.assertEqual(r2["status"], "energized")
        self.assertNotIn("S1A", svc.state["leases"])
        self.assertEqual(svc.state["leases"][target]["box_id"], "RFU1")
        self.assertEqual(svc.state["leases"][target]["status"], "powered")
        self._healthy(svc)
        # C2 负载：RFU1 已供电 32A + 同方案 RFU2 未执行步骤的保留 32A = 64A
        circ = svc.state["sockets"][target]["circuit_id"]
        loads = {c["circuit_id"]: c
                 for c in views.circuit_loads(svc)["circuits"]}
        self.assertAlmostEqual(loads[circ]["committed_a"], 64)
        self.assertAlmostEqual(loads[circ]["parts"]["powered"], 32)
        self.assertAlmostEqual(loads[circ]["parts"]["held"], 32)

    def test_crash_after_swap_commit_replays_as_completed(self):
        proc = run_crash_worker(self.dir, "swap_after")
        self.assertEqual(proc.returncode, 77, proc.stderr)
        svc = Service(self.dir)
        self._healthy(svc)
        plan = next(iter(svc.state["plans"].values()))
        step = next(s for s in plan["steps"].values()
                    if s["box_id"] == "RFU1")
        target = step["to_socket"]
        self.assertEqual(svc.state["leases"][target]["box_id"], "RFU1")
        self.assertEqual(svc.state["leases"][target]["status"], "powered")
        self.assertNotIn("S1A", svc.state["leases"])
        self.assertEqual(step["status"], "energized")
        # 同一送电命令标识重放：首次答复，不产生第二条租约
        before = svc.state["seq"]
        _, r, replay = svc.confirm_step(
            {"plan_id": plan["plan_id"], "step_id": step["step_id"],
             "action": "energize", "command_id": "CMD-CRASH-SWAP"},
            at=t(50))
        self.assertTrue(replay)
        self.assertEqual(r["to_socket"], target)
        self.assertEqual(svc.state["seq"], before)
        self._healthy(svc)


class SnapshotAndTornLogTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="reefer-snap-")

    def test_snapshot_then_replay_converges_to_unique_state(self):
        svc = Service(self.dir)
        svc.register_topology(topology_body(), at=t(0))
        svc.import_fleet(fleet_body(), at=t(5))
        svc.auto_reserve({}, at=t(6))
        seq_snap = svc.force_snapshot()
        svc.energize({"box_id": "RFU1", "socket_id": "S1A",
                      "command_id": "C1"}, at=t(11))
        svc.trip_circuit({"circuit_id": "C1"}, at=t(20))
        # 重启：快照 + 其后事件 -> 与全量重放同一状态
        svc2 = Service(self.dir)
        from reefer.store import Store
        from reefer import domain as D
        full = D.replay(Store(self.dir).read_events())
        self.assertEqual(svc2.state["leases"], full["leases"])
        self.assertEqual(svc2.state["trips"], full["trips"])
        self.assertEqual(svc2.state["seq"], full["seq"])
        self.assertTrue(views.verify_invariants(svc2)["ok"])

    def test_snapshot_preserves_latest_samples_across_restart(self):
        # 回归：samples_latest 的复合键必须能经 JSON 快照往返，否则带快照
        # 重启后最新采样（参与负载/保温计算）会丢失。
        svc = Service(self.dir)
        svc.register_topology(topology_body(), at=t(0))
        svc.import_fleet(fleet_body(), at=t(5))
        svc.ingest_samples({"samples": [
            {"box_id": "RFU1", "kind": "current", "value": 31.4,
             "sensor": "SN-1", "measured_at": t(10)},
            {"box_id": "RFU1", "kind": "temp", "value": -18.0,
             "sensor": "SN-1", "measured_at": t(10)}]}, at=t(11))
        svc.force_snapshot()
        svc2 = Service(self.dir)
        from reefer import domain as D
        self.assertEqual(
            svc2.state["samples_latest"][
                D.sample_latest_key("RFU1", "current")]["value"], 31.4)
        self.assertEqual(
            svc2.state["samples_latest"][
                D.sample_latest_key("RFU1", "temp")]["value"], -18.0)
        latest = {(x["box_id"], x["kind"]): x
                  for x in views.samples(svc2)["latest"]}
        self.assertEqual(latest[("RFU1", "current")]["value"], 31.4)

    def test_torn_tail_line_is_truncated_and_command_rolled_back(self):
        svc = Service(self.dir)
        svc.register_topology(topology_body(), at=t(0))
        svc.import_fleet(fleet_body(), at=t(5))
        seq_before = svc.state["seq"]
        # 手工在流水末尾写半行（模拟写一半掉电）
        with open(os.path.join(self.dir, "events.log"), "ab") as f:
            f.write(b'{"seq": 999, "kind": "SocketReserved", "payloa')
            f.flush()
            os.fsync(f.fileno())
        svc2 = Service(self.dir)  # 构造时自动截断坏尾
        self.assertEqual(svc2.state["seq"], seq_before)
        self.assertEqual(len(svc2.store.read_events()), seq_before)
        self.assertTrue(views.verify_invariants(svc2)["ok"])

    def test_torn_mid_batch_entire_batch_is_absent(self):
        # 一个批次在真实写入时是单次 write；这里模拟批次只落了第一条（撕裂）。
        svc = Service(self.dir)
        svc.register_topology(topology_body(), at=t(0))
        seq_before = svc.state["seq"]
        events = svc.store.read_events()
        good = "\n".join(json.dumps(e, ensure_ascii=False)
                         for e in events) + "\n"
        partial = ('{"seq": %d, "at": "%s", "kind": "SocketReserved",'
                   '"batch_id": "BX", "payload": {"box_id": "RFU1"'
                   % (seq_before + 1, t(6)))
        with open(os.path.join(self.dir, "events.log"), "wb") as f:
            f.write(good.encode("utf-8"))
            f.write(partial.encode("utf-8"))  # 无行尾、JSON 不完整
            f.flush()
            os.fsync(f.fileno())
        svc2 = Service(self.dir)
        self.assertEqual(svc2.state["seq"], seq_before)
        self.assertNotIn("S1A", svc2.state["leases"])
        self.assertTrue(views.verify_invariants(svc2)["ok"])


if __name__ == "__main__":
    unittest.main()
