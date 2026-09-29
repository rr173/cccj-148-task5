"""崩溃注入工作进程：在持久化边界强制终止（os._exit(77)）。

用法：
  python3 -m reefer.tests.crash_worker <data_dir> <point>

point：
  reserve_after      预留批次 fsync 之后、返回前
  reserve_before     预留批次打开流水之前
  energize_after     送电批次 fsync 之后
  trip_after         跳闸登记批次 fsync 之后
  swap_occupy_after  迁移"目标占用"批次 fsync 之后（换插座尚未提交）
  swap_after         换插座提交批次 fsync 之后

开关在种子（建航次并全部送电）完成之后才武装，并用 REEFER_CRASH_KIND
限定到目标命令的事件种类，因此只会在指定边界终止一次。
"""
import os
import sys

DATA = sys.argv[1]
POINT = sys.argv[2]
os.environ["REEFER_DATA"] = DATA
os.environ["REEFER_ALLOW_CRASH"] = "1"
# 种子阶段不武装
os.environ["REEFER_CRASH"] = ""

from reefer.service import Service  # noqa: E402
from reefer.tests.fixture import topology_body, fleet_body, t  # noqa: E402

svc = Service(DATA)
if svc.state["voyage"] is not None:  # 复跑同目录时从空库重建
    import shutil
    shutil.rmtree(DATA)
    svc = Service(DATA)
svc.register_topology(topology_body(), at=t(0))
svc.import_fleet(fleet_body(), at=t(5))
svc.auto_reserve({}, at=t(6))
for i, (box, sock) in enumerate(
        [("RFU1", "S1A"), ("RFU2", "S1B"), ("RFU3", "S1C"),
         ("RFU4", "S3A")], start=1):
    svc.energize({"box_id": box, "socket_id": sock,
                  "command_id": f"CMD-EN-{box}"}, at=t(10 + i))


def arm(when, kind=None):
    os.environ["REEFER_CRASH"] = when
    if kind:
        os.environ["REEFER_CRASH_KIND"] = kind
    else:
        os.environ.pop("REEFER_CRASH_KIND", None)


if POINT in ("reserve_after", "reserve_before"):
    svc.import_fleet({"containers": [
        {"id": "RFZ", "type": "RF", "phase": "3P", "voltage": 440,
         "rated_a": 10, "demand_a": 10, "priority": 1, "zones": ["Z1"],
         "hold_hours": 4}]}, at=t(20))
    arm("after_fsync" if POINT == "reserve_after" else "before_append",
        "SocketReserved")
    svc.reserve_socket({"box_id": "RFZ", "socket_id": "S2A",
                        "command_id": "CMD-CRASH-RESV"}, at=t(21))

elif POINT == "energize_after":
    svc.import_fleet({"containers": [
        {"id": "RFZ", "type": "RF", "phase": "3P", "voltage": 440,
         "rated_a": 10, "demand_a": 10, "priority": 1, "zones": ["Z1"],
         "hold_hours": 4}]}, at=t(20))
    svc.reserve_socket({"box_id": "RFZ", "socket_id": "S2A"}, at=t(21))
    arm("after_fsync", "SocketEnergized")
    svc.energize({"box_id": "RFZ", "socket_id": "S2A",
                  "command_id": "CMD-CRASH-EN"}, at=t(22))

elif POINT == "trip_after":
    arm("after_fsync", "CircuitTripped")
    svc.trip_circuit({"circuit_id": "C1", "reason": "注入跳闸"},
                     at=t(30))

elif POINT in ("swap_occupy_after", "swap_after"):
    # 跳闸/方案/拔除都正常落盘，只在目标占用或换插座提交边界终止。
    svc.trip_circuit({"circuit_id": "C1"}, at=t(30))
    _, plan, _ = svc.create_migration({"circuit_id": "C1",
                                       "operator": "crash"}, at=t(31))
    s1 = next(s for s in plan["steps"] if s["box_id"] == "RFU1")
    svc.confirm_step({"plan_id": plan["plan_id"],
                      "step_id": s1["step_id"], "action": "unplug"},
                     at=t(32))
    if POINT == "swap_occupy_after":
        arm("after_fsync", "MigrationTargetOccupied")
        svc.confirm_step({"plan_id": plan["plan_id"],
                          "step_id": s1["step_id"], "action": "occupy",
                          "command_id": "CMD-CRASH-OCC"}, at=t(33))
    else:
        # 先完成 occupy（不崩溃），再在换插座提交后崩溃
        svc.confirm_step({"plan_id": plan["plan_id"],
                          "step_id": s1["step_id"], "action": "occupy"},
                         at=t(33))
        arm("after_fsync", "MigrationSwapCommitted")
        svc.confirm_step({"plan_id": plan["plan_id"],
                          "step_id": s1["step_id"], "action": "energize",
                          "command_id": "CMD-CRASH-SWAP"}, at=t(34))
else:
    print(f"unknown point {POINT}")
    sys.exit(2)

print("worker survived (crash did not fire?)")
sys.exit(0)
