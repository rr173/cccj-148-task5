"""验收 1/2：正常预留接电；容量/相制/箱型/可达性拒绝；命令标识语义。"""
import os
import tempfile
import unittest

from reefer.service import Service, ServiceError
from reefer import views
from .fixture import topology_body, fleet_body, t


class TestReserveAndEnergize(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="reefer-t1-")
        self.svc = Service(self.dir)
        self.svc.register_topology(topology_body(), at=t(0))
        self.svc.import_fleet(fleet_body(), at=t(5))

    # ------------------------------------------------------------ 正常路径
    def test_reserve_then_energize(self):
        st, res, replay = self.svc.reserve_socket(
            {"box_id": "RFU1", "socket_id": "S1A",
             "command_id": "CMD-R1"}, at=t(6))
        self.assertEqual(st, 201)
        self.assertFalse(replay)
        self.assertEqual(res["status"], "reserved")

        # 预留已计入回路负载（32A）
        loads = {c["circuit_id"]: c
                 for c in views.circuit_loads(self.svc, now=t(7))["circuits"]}
        self.assertEqual(loads["C1"]["committed_a"], 32)
        self.assertEqual(loads["C1"]["parts"]["reserved"], 32)

        # 未提交送电前仍是预留，不算已供电
        unpowered = {b["box_id"] for b in
                     views.unpowered_boxes(self.svc, now=t(7))["boxes"]}
        self.assertIn("RFU1", unpowered)

        st, res, replay = self.svc.energize(
            {"box_id": "RFU1", "socket_id": "S1A",
             "command_id": "CMD-E1"}, at=t(8))
        self.assertEqual(res["status"], "powered")
        self.assertFalse(replay)
        powered = {b["box_id"] for b in
                   views.unpowered_boxes(self.svc, now=t(9))["boxes"]}
        self.assertNotIn("RFU1", powered)

    def test_auto_reserve_respects_priority_and_is_deterministic(self):
        st, res, _ = self.svc.auto_reserve({}, at=t(6))
        box_to_socket = {r["box_id"]: r["socket_id"] for r in res["reserved"]}
        # RFU1 只能去 Z1 的 3P 插座 S1A（S2A 也行，但贪心选余量最大回路，
        # C1/C2 初始余量相同按回路号 -> C1）
        self.assertEqual(box_to_socket["RFU1"], "S1A")
        self.assertEqual(box_to_socket["RFU4"], "S3A")
        # 再来一次：所有箱都已有租约，返回空结果（幂等、无事件）
        seq_before = self.svc.state["seq"]
        st, res, _ = self.svc.auto_reserve({}, at=t(7))
        self.assertEqual(res["reserved"], [])
        self.assertEqual(res["rejected"], [])
        self.assertEqual(self.svc.state["seq"], seq_before)

    # ------------------------------------------------------------ 拒绝类
    def test_phase_mismatch_rejected(self):
        # RFU4 是 1P/220V，插 3P/440V 插座
        with self.assertRaises(ServiceError) as cm:
            self.svc.reserve_socket(
                {"box_id": "RFU4", "socket_id": "S1A"}, at=t(6))
        self.assertEqual(cm.exception.code, "PHASE_MISMATCH")

    def test_box_type_and_reach_rejected(self):
        # 追加一个箱型不允许的箱 RFX(RFT)，和一个电缆够不到目标的箱 RFY(RF, Z9)
        self.svc.import_fleet({"containers": [
            {"id": "RFX", "type": "RFT", "phase": "3P", "voltage": 440,
             "rated_a": 20, "demand_a": 20, "priority": 1,
             "zones": ["Z1"], "hold_hours": 4},
            {"id": "RFY", "type": "RF", "phase": "3P", "voltage": 440,
             "rated_a": 20, "demand_a": 20, "priority": 1,
             "zones": ["Z9"], "hold_hours": 4}]}, at=t(6))
        with self.assertRaises(ServiceError) as cm:
            self.svc.reserve_socket(
                {"box_id": "RFX", "socket_id": "S1A"}, at=t(7))
        self.assertEqual(cm.exception.code, "BOX_TYPE_FORBIDDEN")
        # RFY 相制/容量/箱型都满足，但没有任何插座可达 Z9
        with self.assertRaises(ServiceError) as cm:
            self.svc.auto_reserve({"box_ids": ["RFY"]}, at=t(8))
        self.assertEqual(cm.exception.code, "ALL_REJECTED")
        self.assertEqual(cm.exception.extra["rejected"][0]["reason"],
                         "NO_ELIGIBLE_SOCKET")
        # 手工指定同样被可达性拒绝
        with self.assertRaises(ServiceError) as cm:
            self.svc.reserve_socket(
                {"box_id": "RFY", "socket_id": "S2A"}, at=t(9))
        self.assertEqual(cm.exception.code, "CABLE_UNREACHABLE")

    def test_circuit_capacity_rejected(self):
        # 独立小容量拓扑：回路 50A、两个插座；32+20=52 越限
        d = tempfile.mkdtemp(prefix="reefer-cap-")
        svc = Service(d)
        svc.register_topology({
            "voyage_id": "VCAP", "clock_skew_sec": 30,
            "boards": [{"id": "B"}],
            "circuits": [{"id": "K1", "board_id": "B", "phase": "3P",
                          "voltage": 440, "rating_a": 50}],
            "sockets": [
                {"id": "KA", "circuit_id": "K1", "phase": "3P",
                 "voltage": 440, "rated_a": 40, "reach_zones": ["Z1"]},
                {"id": "KB", "circuit_id": "K1", "phase": "3P",
                 "voltage": 440, "rated_a": 40, "reach_zones": ["Z1"]}]},
            at=t(0))
        svc.import_fleet({"containers": [
            {"id": "CA", "type": "RF", "phase": "3P", "voltage": 440,
             "rated_a": 32, "demand_a": 32, "priority": 9, "zones": ["Z1"],
             "hold_hours": 4},
            {"id": "CB", "type": "RF", "phase": "3P", "voltage": 440,
             "rated_a": 20, "demand_a": 20, "priority": 1, "zones": ["Z1"],
             "hold_hours": 4}]}, at=t(1))
        svc.reserve_socket({"box_id": "CA", "socket_id": "KA"}, at=t(2))
        with self.assertRaises(ServiceError) as cm:
            svc.reserve_socket({"box_id": "CB", "socket_id": "KB"}, at=t(3))
        self.assertEqual(cm.exception.code, "CIRCUIT_OVERLOADED")
        self.assertAlmostEqual(cm.exception.extra["current_total"], 32)
        self.assertAlmostEqual(cm.exception.extra["extra"], 20)
        # 自动预留对容量不足同样给出拒绝明细
        with self.assertRaises(ServiceError) as cm:
            svc.auto_reserve({}, at=t(4))
        self.assertEqual(cm.exception.code, "ALL_REJECTED")

    def test_one_box_one_socket_and_socket_exclusivity(self):
        self.svc.reserve_socket({"box_id": "RFU1", "socket_id": "S1A"},
                                at=t(6))
        with self.assertRaises(ServiceError) as cm:
            self.svc.reserve_socket({"box_id": "RFU1", "socket_id": "S2A"},
                                    at=t(7))
        self.assertEqual(cm.exception.code, "BOX_ALREADY_LEASED")
        with self.assertRaises(ServiceError) as cm:
            self.svc.reserve_socket({"box_id": "RFU2", "socket_id": "S1A"},
                                    at=t(8))
        # RFU2(Z2) 与 S1A(仅Z1) 物理不可达先报；用同箱型可达箱 RFU1 再撞占用
        self.assertEqual(cm.exception.code, "CABLE_UNREACHABLE")
        with self.assertRaises(ServiceError) as cm:
            self.svc.reserve_socket({"box_id": "RFU4", "socket_id": "S1A"},
                                    at=t(9))
        self.assertEqual(cm.exception.code, "PHASE_MISMATCH")
        # 真正的"插座已被占"：另一个与 S1A 完全兼容的箱
        self.svc.import_fleet({"containers": [
            {"id": "RFW", "type": "RF", "phase": "3P", "voltage": 440,
             "rated_a": 20, "demand_a": 20, "priority": 1,
             "zones": ["Z1"], "hold_hours": 4}]}, at=t(10))
        with self.assertRaises(ServiceError) as cm:
            self.svc.reserve_socket({"box_id": "RFW", "socket_id": "S1A"},
                                    at=t(11))
        self.assertEqual(cm.exception.code, "SOCKET_IN_USE")

    # -------------------------------------------------------- 命令标识语义
    def test_command_id_replay_and_hash_conflict(self):
        body = {"box_id": "RFU1", "socket_id": "S1A",
                "command_id": "CMD-X"}
        st1, r1, rep1 = self.svc.reserve_socket(body, at=t(6))
        self.assertEqual(st1, 201)
        # 相同命令标识再次提交 -> 首次答复（重放），不产生新事件
        seq_before = self.svc.state["seq"]
        st2, r2, rep2 = self.svc.reserve_socket(body, at=t(7))
        self.assertTrue(rep2)
        self.assertEqual(r1, r2)
        self.assertEqual(self.svc.state["seq"], seq_before)

        # 相同命令标识但载荷（插座）不同 -> 拒绝，且接电状态不被改写
        with self.assertRaises(ServiceError) as cm:
            self.svc.reserve_socket(
                {"box_id": "RFU1", "socket_id": "S2A",
                 "command_id": "CMD-X"}, at=t(8))
        self.assertEqual(cm.exception.code, "HASH_CONFLICT")
        self.assertEqual(self.svc.state["leases"]["S1A"]["box_id"], "RFU1")
        self.assertNotIn("S2A", self.svc.state["leases"])

        # 首次的失败答复同样可重放：用新命令标识做一次必然失败的预留
        bad = {"box_id": "RFU4", "socket_id": "S1A",
               "command_id": "CMD-BAD"}
        for _ in range(2):
            with self.assertRaises(ServiceError) as cm:
                self.svc.reserve_socket(bad, at=t(9))
            self.assertEqual(cm.exception.code, "PHASE_MISMATCH")

    def test_energize_requires_reservation_and_box_match(self):
        with self.assertRaises(ServiceError) as cm:
            self.svc.energize(
                {"box_id": "RFU1", "socket_id": "S1A"}, at=t(6))
        self.assertEqual(cm.exception.code, "NO_RESERVATION")
        self.svc.reserve_socket({"box_id": "RFU1", "socket_id": "S1A"},
                                at=t(7))
        with self.assertRaises(ServiceError) as cm:
            self.svc.energize(
                {"box_id": "RFU2", "socket_id": "S1A"}, at=t(8))
        self.assertEqual(cm.exception.code, "LEASE_BOX_MISMATCH")


if __name__ == "__main__":
    unittest.main()
