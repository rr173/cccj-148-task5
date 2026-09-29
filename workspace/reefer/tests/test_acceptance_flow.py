"""验收 3/4/5/6：采样重复与倒序、跳闸后优先迁移、两名电气员争抢、
以及迁移中途取消释放未执行步骤。"""
import unittest

from reefer.service import ServiceError
from reefer import views
from .fixture import seeded_service, t
import tempfile


class TestSamples(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="reefer-t2-")
        self.svc = seeded_service(self.dir)  # 本用例独占临时库，无跨用例污染

    def test_duplicate_and_out_of_order(self):
        samples = {"samples": [
            {"box_id": "RFU1", "kind": "temp", "value": -18.0,
             "sensor": "SN-1", "measured_at": t(30)},
            {"box_id": "RFU1", "kind": "current", "value": 30.5,
             "sensor": "SN-1", "measured_at": t(30)},
            # 完全重复（箱+类型+传感器+时刻）
            {"box_id": "RFU1", "kind": "temp", "value": -18.0,
             "sensor": "SN-1", "measured_at": t(30)},
            # 倒序 5 分钟，超出 30 秒偏差 -> 待审
            {"box_id": "RFU1", "kind": "temp", "value": -12.0,
             "sensor": "SN-1", "measured_at": t(25)},
        ]}
        _, res, _ = self.svc.ingest_samples(samples, at=t(31))
        self.assertEqual(len(res["accepted"]), 2)
        self.assertEqual(len(res["duplicate"]), 1)
        self.assertEqual(len(res["quarantined"]), 1)
        qid = res["quarantined"][0]["qid"]

        # 倒序样本不参与负载/温度：实测电流仍采用 -18 那条？电流是 30.5
        latest = {(x["box_id"], x["kind"]): x
                  for x in views.samples(self.svc)["latest"]}
        self.assertEqual(latest[("RFU1", "temp")]["value"], -18.0)
        self.assertEqual(latest[("RFU1", "current")]["value"], 30.5)

        # 倒序样本出现在异常采样视图
        q = views.quarantine(self.svc)["samples"]
        self.assertEqual([x["qid"] for x in q], [qid])

        # 采纳待审样本后才进入测量状态（审核员对倒序负责）
        self.svc.review_sample({"qid": qid, "decision": "accept",
                                "operator": "ET"}, at=t(32))
        latest = {(x["box_id"], x["kind"]): x
                  for x in views.samples(self.svc)["latest"]}
        self.assertEqual(latest[("RFU1", "temp")]["value"], -12.0)
        self.assertTrue(latest[("RFU1", "temp")].get("reviewed"))

    def test_within_skew_reorder_is_accepted(self):
        # 先收 10:30，再收 10:29:50（倒序 10 秒，在 30 秒允许偏差内）-> 接受
        from datetime import datetime, timedelta
        base = datetime.fromisoformat(t(30))
        back10 = (base - timedelta(seconds=10)).isoformat()
        back45 = (base - timedelta(seconds=45)).isoformat()
        self.svc.ingest_samples({"samples": [
            {"box_id": "RFU2", "kind": "temp", "value": -10,
             "sensor": "SN-2", "measured_at": t(30)}]}, at=t(31))
        _, res, _ = self.svc.ingest_samples({"samples": [
            {"box_id": "RFU2", "kind": "temp", "value": -9.9,
             "sensor": "SN-2", "measured_at": back10}]}, at=t(32))
        self.assertEqual(len(res["accepted"]), 1)
        _, res, _ = self.svc.ingest_samples({"samples": [
            {"box_id": "RFU2", "kind": "temp", "value": -9.0,
             "sensor": "SN-2", "measured_at": back45}]}, at=t(33))
        self.assertEqual(len(res["quarantined"]), 1)

    def test_powerdown_sample_marked(self):
        _, res, _ = self.svc.ingest_samples({"samples": [
            {"box_id": "RFU1", "kind": "powerdown", "value": 1,
             "sensor": "PD-1", "measured_at": t(40)}]}, at=t(41))
        self.assertEqual(len(res["accepted"]), 1)


class TestTripMigration(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="reefer-t3-")
        self.svc = seeded_service(self.dir)
        # C1: RFU1 32A(p9), RFU2 32A(p5), RFU3 30A(p1) 全部供电
        self.svc.trip_circuit({"circuit_id": "C1", "reason": "接地故障"},
                              at=t(60))

    def test_trip_freezes_new_connections_and_lists_impact(self):
        # 跳闸回路立即冻结新接电：释放 RFU1 后无法再在 C1 预留/送电
        with self.assertRaises(ServiceError) as cm:
            self.svc.reserve_socket(
                {"box_id": "RFU4", "socket_id": "S1C"}, at=t(61))
        # RFU4 是 1P -> 先撞相制；用兼容箱型验证冻结：另建 3P 箱
        self.assertIn(cm.exception.code,
                      ("PHASE_MISMATCH", "CIRCUIT_TRIPPED"))
        # 直接往跳闸回路的插座送电（先造一个预留不可能，故用已跳闸租约验证）
        impacts = {x["circuit_id"]: x
                   for x in views.trip_impacts(self.svc, now=t(65))["trips"]}
        boxes = {b["box_id"]: b for b in impacts["C1"]["affected_boxes"]}
        self.assertEqual(set(boxes), {"RFU1", "RFU2", "RFU3"})
        # 剩余保温时限随时间减少
        self.assertAlmostEqual(boxes["RFU1"]["remaining_insulation_hours"],
                               4 - 5 / 60, places=2)
        # 全部受影响箱出现在未接电视图
        unp = {b["box_id"] for b in
               views.unpowered_boxes(self.svc, now=t(65))["boxes"]}
        self.assertTrue({"RFU1", "RFU2", "RFU3"} <= unp)

    def test_priority_ordered_migration_and_stepwise_execution(self):
        _, res, _ = self.svc.create_migration(
            {"circuit_id": "C1", "operator": "ET-甲"}, at=t(61))
        plan_id = res["plan_id"]
        order = [s["box_id"] for s in sorted(res["steps"],
                                             key=lambda s: s["order"])]
        # 优先级 9 > 5 > 1；RFU3 在 Z3 够不到任何备用插座 -> 进冲突结果。
        # C2 容量 70A：RFU1+RFU2=64A 可行，RFU3 的 30A 无可达回路。
        self.assertEqual(order, ["RFU1", "RFU2"])
        conflicted = {c["box_id"] for c in res["conflicts"]}
        self.assertIn("RFU3", conflicted)
        s1 = next(s for s in res["steps"] if s["box_id"] == "RFU1")
        s2 = next(s for s in res["steps"] if s["box_id"] == "RFU2")
        self.assertEqual(sorted([s1["to_socket"], s2["to_socket"]]),
                         ["S2A", "S2B"])

        # 未完成前原插座不能释放、目标不能被抢（用一个新箱 RFZ 抢占 S2B）
        self.svc.import_fleet({"containers": [
            {"id": "RFZ", "type": "RF", "phase": "3P", "voltage": 440,
             "rated_a": 20, "demand_a": 20, "priority": 1,
             "zones": ["Z1"], "hold_hours": 4}]}, at=t(62))
        with self.assertRaises(ServiceError) as cm:
            self.svc.reserve_socket(
                {"box_id": "RFZ", "socket_id": s2["to_socket"]}, at=t(63))
        self.assertEqual(cm.exception.code, "SOCKET_HELD")

        # 必须按 拔除 -> 占用 -> 送电 的顺序
        with self.assertRaises(ServiceError) as cm:
            self.svc.confirm_step(
                {"plan_id": plan_id, "step_id": s1["step_id"],
                 "action": "occupy"}, at=t(62))
        self.assertEqual(cm.exception.code, "STEP_STATE")

        # 完成 RFU1 的三步换插座
        self.svc.confirm_step({"plan_id": plan_id,
                               "step_id": s1["step_id"],
                               "action": "unplug"}, at=t(63))
        # 拔除后箱断电，但原插座仍绑定（不能被别的箱抢）
        with self.assertRaises(ServiceError) as cm:
            self.svc.reserve_socket(
                {"box_id": "RFZ", "socket_id": s1["from_socket"]},
                at=t(64))
        self.assertEqual(cm.exception.code, "SOCKET_IN_USE")
        self.svc.confirm_step({"plan_id": plan_id,
                               "step_id": s1["step_id"],
                               "action": "occupy"}, at=t(64))
        st, sw, _ = self.svc.confirm_step(
            {"plan_id": plan_id, "step_id": s1["step_id"],
             "action": "energize",
             "command_id": "CMD-SWAP-1"}, at=t(65))
        self.assertEqual(sw["status"], "energized")

        # 换插座完成：原插座已释放、目标插座由 RFU1 供电、无双重占用
        tgt = s1["to_socket"]
        self.assertNotIn(s1["from_socket"], self.svc.state["leases"])
        self.assertEqual(self.svc.state["leases"][tgt]["box_id"], "RFU1")
        self.assertEqual(self.svc.state["leases"][tgt]["status"], "powered")
        self.assertNotIn(tgt, self.svc.state["holds"])
        v = views.verify_invariants(self.svc)
        self.assertTrue(v["ok"], v["problems"])
        # 换插座命令标识重放：返回首次答复，状态不变
        seq = self.svc.state["seq"]
        _, r2, replay = self.svc.confirm_step(
            {"plan_id": plan_id, "step_id": s1["step_id"],
             "action": "energize", "command_id": "CMD-SWAP-1"}, at=t(66))
        self.assertTrue(replay)
        self.assertEqual(r2["to_socket"], tgt)
        self.assertEqual(self.svc.state["seq"], seq)

    def test_two_electricians_contend_for_spare_socket(self):
        """两名电气员争用同一组备用插座：先提交者成功保留，后提交者拿到最新
        占用与可重新计算的冲突结果。"""
        _, plan_a, _ = self.svc.create_migration(
            {"circuit_id": "C1", "operator": "ET-甲"}, at=t(61))
        held = {s["to_socket"]: s["box_id"] for s in plan_a["steps"]}
        self.assertEqual(set(held), {"S2A", "S2B"})
        target_of = {s["box_id"]: s["to_socket"] for s in plan_a["steps"]}

        # ET-乙 立即提交第二份方案：RFU1/RFU2 的目标均已被甲保留，RFU3 物理
        # 上够不到任何备用插座；整体被拒，冲突结果逐插座给出最新原因。
        with self.assertRaises(ServiceError) as cm:
            self.svc.create_migration(
                {"circuit_id": "C1", "operator": "ET-乙"}, at=t(62))
        self.assertEqual(cm.exception.code, "MIGRATION_BLOCKED")
        detail = {c["box_id"]: c for c in cm.exception.extra["conflicts"]}
        for bid in ("RFU1", "RFU2"):
            codes = {r["socket_id"]: r
                     for r in detail[bid]["rejected_targets"]}
            tgt = target_of[bid]
            self.assertEqual(codes[tgt]["code"], "HELD")
            self.assertEqual(codes[tgt]["by_plan"], plan_a["plan_id"])
        rfu3 = {r["code"] for r in detail["RFU3"]["rejected_targets"]}
        self.assertIn("CABLE_UNREACHABLE", rfu3)
        self.assertEqual(
            {h["plan_id"] for h in self.svc.state["holds"].values()},
            {plan_a["plan_id"]})

        # 冲突视图随状态推进保持最新：甲完成 RFU1 的换插座后，其目标转为
        # OCCUPIED，RFU1 不再出现在新方案的受影响集合；乙重算时仍会因 RFU2
        # 的目标被 pending 步骤保留而被拒，得到最新占用。
        s1 = next(s for s in plan_a["steps"] if s["box_id"] == "RFU1")
        for act, mins in (("unplug", 63), ("occupy", 64), ("energize", 65)):
            self.svc.confirm_step({"plan_id": plan_a["plan_id"],
                                   "step_id": s1["step_id"], "action": act},
                                  at=t(mins))
        with self.assertRaises(ServiceError) as cm:
            self.svc.create_migration(
                {"circuit_id": "C1", "operator": "ET-乙"}, at=t(66))
        detail = {c["box_id"]: c for c in cm.exception.extra["conflicts"]}
        self.assertNotIn("RFU1", detail)
        codes = {r["socket_id"]: r
                 for r in detail["RFU2"]["rejected_targets"]}
        self.assertEqual(codes[target_of["RFU2"]]["code"], "HELD")

    def test_cancel_migration_releases_only_pending_steps(self):
        _, plan, _ = self.svc.create_migration(
            {"circuit_id": "C1", "operator": "ET-甲"}, at=t(61))
        steps = {s["box_id"]: s for s in plan["steps"]}
        # RFU1 走到"已拔除"，RFU2 仍 pending
        s1 = steps["RFU1"]
        self.svc.confirm_step({"plan_id": plan["plan_id"],
                               "step_id": s1["step_id"],
                               "action": "unplug"}, at=t(63))
        _, cancelled, _ = self.svc.cancel_migration(
            {"plan_id": plan["plan_id"]}, at=t(64))
        # 只有 pending 的 RFU2 步骤释放目标；RFU1 在途步骤保留目标与原插座
        released = set(cancelled["released_steps"])
        self.assertEqual(released, {steps["RFU2"]["step_id"]})
        in_flight = {x["step_id"] for x in cancelled["in_flight_steps"]}
        self.assertEqual(in_flight, {s1["step_id"]})
        self.assertNotIn(steps["RFU2"]["to_socket"],
                         self.svc.state["holds"])
        self.assertIn(s1["to_socket"], self.svc.state["holds"])
        self.assertIn(s1["from_socket"], self.svc.state["leases"])
        self.assertEqual(
            self.svc.state["leases"][s1["from_socket"]]["status"],
            "unplugged")
        # 被释放的目标插座立即可用（其他箱可以抢）
        self.assertTrue(
            steps["RFU2"]["to_socket"] not in self.svc.state["leases"]
            and steps["RFU2"]["to_socket"] not in self.svc.state["holds"])
        # 取消后不能再确认该方案任何步骤
        with self.assertRaises(ServiceError) as cm:
            self.svc.confirm_step({"plan_id": plan["plan_id"],
                                   "step_id": s1["step_id"],
                                   "action": "occupy"}, at=t(65))
        self.assertEqual(cm.exception.code, "PLAN_CANCELLED")


if __name__ == "__main__":
    unittest.main()
