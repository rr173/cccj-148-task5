"""验收测试共用航次夹具。

拓扑（配电板 MSB-1）：
  C1 3P 440V 额定 100A（主回路）: S1A/Z1, S1B/Z1+Z2, S1C/Z3
  C2 3P 440V 额定 70A （备用）:   S2A/Z1+Z2, S2B/Z1+Z2
  C3 1P 220V 额定 50A:           S3A/Z1
允许时钟偏差 30 秒。

冷藏箱（本航次）：
  RFU1 3P 440V 32A 优先级9 Z1 保温4h    → C1/S1A
  RFU2 3P 440V 32A 优先级5 Z2 保温6h    → C1/S1B
  RFU3 3P 440V 30A 优先级1 Z3 保温8h    → C1/S1C
  RFU4 1P 220V 20A 优先级7 Z1 保温5h    → C3/S3A
C1 正常时承诺 94A ≤ 100A。C1 跳闸后：RFU1/RFU2 都能触及 C2 的两个备用
插座 S2A/S2B，C2 容量恰好容下两者（32+32=64≤70）；RFU3 所在 Z3 没有
可达的备用插座，于是 S2A/S2B 正好成为两名电气员争抢的目标。
"""
from __future__ import annotations

import shutil

from reefer.service import Service

T0 = "2026-09-29T10:00:00+00:00"


def t(minutes: int, base=T0) -> str:
    from datetime import datetime, timedelta
    dt = datetime.fromisoformat(base) + timedelta(minutes=minutes)
    return dt.isoformat()


def topology_body():
    return {
        "voyage_id": "V-2026-09W", "clock_skew_sec": 30,
        "boards": [{"id": "MSB-1", "name": "1号主配电板"}],
        "circuits": [
            {"id": "C1", "board_id": "MSB-1", "phase": "3P", "voltage": 440,
             "rating_a": 100},
            {"id": "C2", "board_id": "MSB-1", "phase": "3P", "voltage": 440,
             "rating_a": 70},
            {"id": "C3", "board_id": "MSB-1", "phase": "1P", "voltage": 220,
             "rating_a": 50}],
        "sockets": [
            {"id": "S1A", "circuit_id": "C1", "phase": "3P", "voltage": 440,
             "rated_a": 40, "allowed_types": ["RF"],
             "reach_zones": ["Z1"]},
            {"id": "S1B", "circuit_id": "C1", "phase": "3P", "voltage": 440,
             "rated_a": 40, "allowed_types": ["RF"],
             "reach_zones": ["Z1", "Z2"]},
            {"id": "S1C", "circuit_id": "C1", "phase": "3P", "voltage": 440,
             "rated_a": 40, "allowed_types": ["RF"],
             "reach_zones": ["Z3"]},
            {"id": "S2A", "circuit_id": "C2", "phase": "3P", "voltage": 440,
             "rated_a": 40, "allowed_types": ["RF"],
             "reach_zones": ["Z1", "Z2"]},
            {"id": "S2B", "circuit_id": "C2", "phase": "3P", "voltage": 440,
             "rated_a": 40, "allowed_types": ["RF"],
             "reach_zones": ["Z1", "Z2"]},
            {"id": "S3A", "circuit_id": "C3", "phase": "1P", "voltage": 220,
             "rated_a": 30, "allowed_types": ["RF"],
             "reach_zones": ["Z1"]}],
    }


def fleet_body():
    return {"containers": [
        {"id": "RFU1", "type": "RF", "phase": "3P", "voltage": 440,
         "rated_a": 32, "demand_a": 32, "priority": 9, "zones": ["Z1"],
         "hold_hours": 4},
        {"id": "RFU2", "type": "RF", "phase": "3P", "voltage": 440,
         "rated_a": 32, "demand_a": 32, "priority": 5, "zones": ["Z2"],
         "hold_hours": 6},
        {"id": "RFU3", "type": "RF", "phase": "3P", "voltage": 440,
         "rated_a": 30, "demand_a": 30, "priority": 1, "zones": ["Z3"],
         "hold_hours": 8},
        {"id": "RFU4", "type": "RF", "phase": "1P", "voltage": 220,
         "rated_a": 20, "demand_a": 20, "priority": 7, "zones": ["Z1"],
         "hold_hours": 5}]}


def fresh_service(tmpdir):
    """空库。"""
    return Service(tmpdir)


def seeded_service(tmpdir, energized=True):
    """登记拓扑 + 导入箱队；energized=True 时全部自动预留并送电。

    插座分配以自动预留结果为准（贪心是确定的），送电命令标识按箱固定。
    """
    svc = Service(tmpdir)
    svc.register_topology(topology_body(), at=t(0))
    svc.import_fleet(fleet_body(), at=t(5))
    if energized:
        _, auto, _ = svc.auto_reserve({}, at=t(6))
        for i, r in enumerate(sorted(auto["reserved"],
                                     key=lambda x: x["box_id"]), start=1):
            svc.energize(
                {"box_id": r["box_id"], "socket_id": r["socket_id"],
                 "command_id": f"CMD-EN-{r['box_id']}"}, at=t(10 + i))
    return svc


def reset_dir(path):
    shutil.rmtree(path, ignore_errors=True)
