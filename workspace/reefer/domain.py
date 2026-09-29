"""领域状态、归并（reducer）与纯业务规则。

状态全部由事件流水归并得出；归并是确定的：同一条流水在任何机器、任何重启后
都得到**同一个**状态（唯一结果）。规则判断函数不做任何 IO，可被 service 与
验收脚本直接复用。

术语
----
租约(lease)   插座与箱之间的当前占用关系。状态：
                reserved  已预留未送电（插座锁定，其他箱不可抢）
                powered   正在供电
                unplugged 迁移中途：已从原插座拔除，箱断电，租约仍绑定原插座，
                          直到换插座提交或迁移取消/作废
              任一时刻：一个插座至多一条未释放租约；一个箱至多一条未释放租约。
保留(hold)    迁移方案对目标插座的软锁定（plan_id/step_id/box_id）。
              步骤未完成前目标插座不可被其他箱或另一份方案抢占。
"""
from __future__ import annotations

from datetime import datetime

# --------------------------------------------------------------------- 常量
LEASE_RESERVED = "reserved"
LEASE_POWERED = "powered"
LEASE_UNPLUGGED = "unplugged"

STEP_PENDING = "pending"          # 等待电气员确认
STEP_UNPLUGGED = "unplugged"      # 已拔除（箱断电）
STEP_OCCUPIED = "target_occupied" # 目标插座已占用，等待送电
STEP_ENERGIZED = "energized"      # 已重新送电（终态）
STEP_CANCELLED = "cancelled"      # 方案取消时未执行步骤被释放

PLAN_PENDING = "pending"
PLAN_COMPLETED = "completed"
PLAN_CANCELLED = "cancelled"

SAMPLE_CURRENT = "current"        # 电流 A（分相可选）
SAMPLE_TEMP = "temp"              # 箱内温度 ℃
SAMPLE_POWERDOWN = "powerdown"    # 掉电事件（传感器自行检测到失电）

QUAR_REASON_CLOCK = "clock_out_of_order"   # 超过允许时钟偏差的倒序样本
QUAR_REASON_DUP = "duplicate"


def sample_latest_key(box_id: str, kind: str) -> str:
    """最新测量的复合键。必须是字符串：状态会序列化为 JSON 快照，元组键
    会被转成 ``"('box','kind')"`` 而无法按原键查回。"""
    return f"{box_id}\x1f{kind}"


def initial_state():
    return {
        "seq": 0,
        "voyage": None,                 # {voyage_id, clock_skew_sec, imported_at}
        "boards": {},                   # board_id -> {id, name}
        "circuits": {},                 # cid -> {id, board_id, phase, voltage, rating_a}
        "sockets": {},                  # sid -> {id, circuit_id, phase, voltage,
                                        #   rated_a, allowed_types[], reach_zones[]}
        "containers": {},               # box -> {id, type, phase, voltage, rated_a,
                                        #   demand_a, priority, zones[], hold_hours}
        "leases": {},                   # sid -> lease dict（未释放的插座租约）
        "trips": {},                    # cid -> {at, reason}（当前跳闸；复位后删除）
        "samples_latest": {},           # (box, kind) -> {value, at, sensor, seq}
        "sample_keys": {},              # key -> sample_key 身份（去重）
        "quarantine": {},               # qid -> 待审样本
        "plans": {},                    # plan_id -> plan dict
        "holds": {},                    # sid -> {plan_id, step_id, box_id}
        "idempotency": {},              # command_id -> {hash, result, status, at}
        "resv_seq": 0,
        "plan_seq": 0,
        "step_seq": 0,
    }


# --------------------------------------------------------------------- 工具
def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def hours_between(later: str, earlier: str) -> float:
    return (parse_ts(later) - parse_ts(earlier)).total_seconds() / 3600.0


def _lease_of_box(state, box_id):
    for sid, lease in state["leases"].items():
        if lease["box_id"] == box_id:
            return sid, lease
    return None, None


def socket_free(state, socket_id) -> bool:
    """无租约（预留/供电/拔除中都算占用）且无迁移保留。"""
    if socket_id in state["leases"]:
        return False
    if socket_id in state["holds"]:
        return False
    return True


def circuit_is_tripped(state, circuit_id) -> bool:
    return circuit_id in state["trips"]


def compatible(state, box_id, socket_id):
    """箱能否物理接入某插座：相制/电压/额定电流/允许箱型/电缆可达。

    返回 (ok, reason)。reason 为机器可读的拒绝码。
    """
    box = state["containers"].get(box_id)
    sock = state["sockets"].get(socket_id)
    if box is None:
        return False, "BOX_UNKNOWN"
    if sock is None:
        return False, "SOCKET_UNKNOWN"
    if sock["phase"] != box["phase"]:
        return False, "PHASE_MISMATCH"
    if sock["voltage"] != box["voltage"]:
        return False, "VOLTAGE_MISMATCH"
    if sock["rated_a"] < box["demand_a"]:
        return False, "SOCKET_UNDERRATED"
    allowed = sock["allowed_types"]
    if allowed and box["type"] not in allowed:
        return False, "BOX_TYPE_FORBIDDEN"
    reach = sock["reach_zones"]
    if reach and not any(z in reach for z in box["zones"]):
        return False, "CABLE_UNREACHABLE"
    return True, None


def circuit_demand(state, circuit_id):
    """回路当前承诺负载（A）与构成明细。

    powered  = 已送电租约（实测负载另见 measured_loads，容量闸门按额定需求）
    reserved = 已预留未送电的承诺
    held     = 迁移方案为目标插座保留的需求
    跳闸回路照常统计结构负载，但新接电被冻结。
    """
    parts = {"powered": 0.0, "reserved": 0.0, "held": 0.0}
    for sid, lease in state["leases"].items():
        sock = state["sockets"].get(sid)
        if not sock or sock["circuit_id"] != circuit_id:
            continue
        demand = lease["demand_a"]
        if lease["status"] == LEASE_POWERED:
            parts["powered"] += demand
        elif lease["status"] == LEASE_RESERVED:
            parts["reserved"] += demand
        # unplugged：原回路已无负载，不计入
    for sid, hold in state["holds"].items():
        sock = state["sockets"].get(sid)
        if not sock or sock["circuit_id"] != circuit_id:
            continue
        box = state["containers"].get(hold["box_id"])
        if box:
            parts["held"] += box["demand_a"]
    total = parts["powered"] + parts["reserved"] + parts["held"]
    return total, parts


def measured_circuit_current(state, circuit_id):
    """按最近一次通过审核的电流采样汇总的实测电流（无采样则回退额定）。"""
    total = 0.0
    used_samples = 0
    for sid, lease in state["leases"].items():
        sock = state["sockets"].get(sid)
        if not sock or sock["circuit_id"] != circuit_id:
            continue
        if lease["status"] != LEASE_POWERED:
            continue
        latest = state["samples_latest"].get(
            sample_latest_key(lease["box_id"], SAMPLE_CURRENT))
        if latest is not None:
            total += float(latest["value"])
            used_samples += 1
        else:
            total += lease["demand_a"]
    return total, used_samples


def can_fit(state, circuit_id, extra_a) -> bool:
    c = state["circuits"][circuit_id]
    total, _ = circuit_demand(state, circuit_id)
    return total + float(extra_a) <= c["rating_a"] + 1e-9


def box_powered(state, box_id) -> bool:
    _, lease = _lease_of_box(state, box_id)
    if not lease or lease["status"] != LEASE_POWERED:
        return False
    sock = state["sockets"].get(_socket_of_box(state, box_id))
    return bool(sock and not circuit_is_tripped(state, sock["circuit_id"]))


def _socket_of_box(state, box_id):
    for sid, lease in state["leases"].items():
        if lease["box_id"] == box_id:
            return sid
    return None


def unpowered_since(state, box_id, now):
    """箱当前断电的起始时刻；正在供电返回 None。

    跳闸导致的断电以跳闸时刻为准（租约仍在但回路断）；拔除/无租约以租约记录
    的 unpowered_at 为准；从未接电以箱导入时刻（保守口径）为准。
    """
    sid, lease = _lease_of_box(state, box_id)
    if lease is not None:
        sock = state["sockets"][sid]
        if lease["status"] == LEASE_POWERED and \
                not circuit_is_tripped(state, sock["circuit_id"]):
            return None
        if lease["status"] == LEASE_POWERED:  # powered 但回路跳闸
            return state["trips"][sock["circuit_id"]]["at"]
        return lease.get("unpowered_at")
    return state["containers"][box_id].get("imported_at")


def remaining_insulation_hours(state, box_id, now) -> float:
    since = unpowered_since(state, box_id, now)
    if since is None:
        return float("inf")
    hold = state["containers"][box_id]["hold_hours"]
    return round(hold - hours_between(now, since), 3)


# --------------------------------------------------------------------- 归并
def apply_event(state, ev):
    """把单条事件归并进状态。纯函数：校验已在 service 完成，此处不拒绝事件。"""
    state = dict(state)
    for key in ("boards", "circuits", "sockets", "containers", "leases",
                "trips", "samples_latest", "sample_keys", "quarantine",
                "plans", "holds", "idempotency"):
        state[key] = dict(state[key])
    kind = ev["kind"]
    p = ev.get("payload", {})
    seq = ev["seq"]
    state["seq"] = seq

    if kind == "TopologyRegistered":
        state["voyage"] = {
            "voyage_id": p["voyage_id"],
            "clock_skew_sec": p.get("clock_skew_sec", 30),
            "imported_at": ev["at"],
        }
        boards, circuits, sockets = state["boards"], state["circuits"], \
            state["sockets"]
        for b in p["boards"]:
            boards[b["id"]] = {"id": b["id"], "name": b.get("name", b["id"])}
        for c in p["circuits"]:
            circuits[c["id"]] = {
                "id": c["id"], "board_id": c["board_id"],
                "phase": c["phase"], "voltage": c["voltage"],
                "rating_a": float(c["rating_a"])}
        for s in p["sockets"]:
            sockets[s["id"]] = {
                "id": s["id"], "circuit_id": s["circuit_id"],
                "phase": s["phase"], "voltage": s["voltage"],
                "rated_a": float(s["rated_a"]),
                "allowed_types": list(s.get("allowed_types", [])),
                "reach_zones": list(s.get("reach_zones", []))}

    elif kind == "FleetImported":
        conts = state["containers"]
        for c in p["containers"]:
            conts[c["id"]] = {
                "id": c["id"], "type": c["type"], "phase": c["phase"],
                "voltage": c["voltage"], "rated_a": float(c["rated_a"]),
                "demand_a": float(c.get("demand_a", c["rated_a"])),
                "priority": int(c.get("priority", 0)),
                "zones": list(c.get("zones", [])),
                "hold_hours": float(c.get("hold_hours", 4.0)),
                "imported_at": ev["at"]}

    elif kind == "SocketReserved":
        state["resv_seq"] = max(state["resv_seq"],
                                int(p["reservation_seq"]))
        state["leases"][p["socket_id"]] = {
            "reservation_id": p["reservation_id"],
            "box_id": p["box_id"], "status": LEASE_RESERVED,
            "demand_a": float(p["demand_a"]),
            "reserved_at": p["at"], "energized_at": None,
            "unpowered_at": None}

    elif kind == "ReservationReleased":
        state["leases"].pop(p["socket_id"], None)

    elif kind == "SocketEnergized":
        lease = dict(state["leases"][p["socket_id"]])
        lease["status"] = LEASE_POWERED
        lease["energized_at"] = p["at"]
        lease["unpowered_at"] = None
        state["leases"][p["socket_id"]] = lease

    elif kind == "SocketDeenergized":
        lease = dict(state["leases"][p["socket_id"]])
        lease["status"] = LEASE_RESERVED
        lease["unpowered_at"] = p["at"]
        state["leases"][p["socket_id"]] = lease

    elif kind == "CircuitTripped":
        state["trips"][p["circuit_id"]] = {"at": p["at"],
                                           "reason": p.get("reason", "")}
        for sid, lease in list(state["leases"].items()):
            sock = state["sockets"].get(sid)
            if sock and sock["circuit_id"] == p["circuit_id"] \
                    and lease["status"] == LEASE_POWERED:
                nl = dict(lease)
                nl["unpowered_at"] = p["at"]
                state["leases"][sid] = nl

    elif kind == "CircuitReset":
        state["trips"].pop(p["circuit_id"], None)

    elif kind == "SampleAccepted":
        key = sample_latest_key(p["box_id"], p["kind"])
        state["samples_latest"][key] = {
            "value": p["value"], "at": p["at"], "sensor": p["sensor"],
            "sample_seq": p.get("sample_seq")}
        state["sample_keys"][p["key"]] = True

    elif kind == "SampleQuarantined":
        state["quarantine"][p["qid"]] = {
            "qid": p["qid"], "box_id": p["box_id"], "kind": p["kind"],
            "value": p["value"], "at": p["at"], "sensor": p["sensor"],
            "key": p["key"], "reason": p["reason"],
            "sample_seq": p.get("sample_seq"), "reviewed": False,
            "quarantined_at": ev["at"]}
        state["sample_keys"][p["key"]] = True

    elif kind == "SampleDuplicate":
        # 重复样本不改测量状态，仅留痕（key 已存在）。
        state["sample_keys"].setdefault(p["key"], True)

    elif kind == "SampleReviewed":
        qid = p["qid"]
        if qid in state["quarantine"]:
            q = dict(state["quarantine"][qid])
            q["reviewed"] = True
            q["decision"] = p["decision"]
            q["reviewed_at"] = p["at"]
            state["quarantine"][qid] = q
            if p["decision"] == "accept":
                key = sample_latest_key(q["box_id"], q["kind"])
                state["samples_latest"][key] = {
                    "value": q["value"], "at": q["at"], "sensor": q["sensor"],
                    "sample_seq": q.get("sample_seq"), "reviewed": True}

    elif kind == "MigrationPlanCreated":
        state["plan_seq"] = max(state["plan_seq"], int(p["plan_seq"]))
        steps = {}
        for st in p["steps"]:
            state["step_seq"] = max(state["step_seq"], int(st["step_seq"]))
            steps[st["step_id"]] = dict(st)
            state["holds"][st["to_socket"]] = {
                "plan_id": p["plan_id"], "step_id": st["step_id"],
                "box_id": st["box_id"]}
        state["plans"][p["plan_id"]] = {
            "plan_id": p["plan_id"], "circuit_id": p["circuit_id"],
            "created_at": p["at"], "created_by": p.get("created_by"),
            "steps": steps, "status": PLAN_PENDING}

    elif kind == "MigrationUnplugged":
        plan = state["plans"][p["plan_id"]]
        step = dict(plan["steps"][p["step_id"]])
        step["status"] = STEP_UNPLUGGED
        step["unplugged_at"] = p["at"]
        plan["steps"][p["step_id"]] = step
        lease = dict(state["leases"][step["from_socket"]])
        lease["status"] = LEASE_UNPLUGGED
        lease["unpowered_at"] = p["at"]
        state["leases"][step["from_socket"]] = lease

    elif kind == "MigrationTargetOccupied":
        plan = state["plans"][p["plan_id"]]
        step = dict(plan["steps"][p["step_id"]])
        step["status"] = STEP_OCCUPIED
        step["occupied_at"] = p["at"]
        plan["steps"][p["step_id"]] = step

    elif kind == "MigrationSwapCommitted":
        plan = state["plans"][p["plan_id"]]
        step = dict(plan["steps"][p["step_id"]])
        step["status"] = STEP_ENERGIZED
        step["energized_at"] = p["at"]
        plan["steps"][p["step_id"]] = step
        box_id = step["box_id"]
        old = state["leases"].pop(step["from_socket"], None)
        state["holds"].pop(step["to_socket"], None)
        state["leases"][step["to_socket"]] = {
            "reservation_id": (old or {}).get("reservation_id"),
            "box_id": box_id, "status": LEASE_POWERED,
            "demand_a": step["demand_a"],
            "reserved_at": (old or {}).get("reserved_at"),
            "energized_at": p["at"], "unpowered_at": None,
            "migrated_from": step["from_socket"],
            "plan_id": p["plan_id"]}

    elif kind == "MigrationCancelled":
        plan = state["plans"][p["plan_id"]]
        for sid, step in list(plan["steps"].items()):
            if step["status"] == STEP_PENDING:
                ns = dict(step)
                ns["status"] = STEP_CANCELLED
                plan["steps"][sid] = ns
                state["holds"].pop(step["to_socket"], None)
        plan["status"] = PLAN_CANCELLED
        plan["cancelled_at"] = p["at"]

    elif kind == "CommandResult":
        state["idempotency"][p["command_id"]] = {
            "hash": p["hash"], "result": p["result"],
            "status": p.get("http_status", 200), "at": ev["at"],
            "scope": p.get("scope")}

    elif kind == "CommandRejected":
        state["idempotency"][p["command_id"]] = {
            "hash": p["hash"], "result": p["result"],
            "status": p.get("http_status", 409), "at": ev["at"],
            "scope": p.get("scope"), "rejected": True}

    else:
        raise ValueError(f"unknown event kind: {kind}")

    return _recompute_plan_status(state, p_latest_plan(ev))


def p_latest_plan(ev):
    p = ev.get("payload", {})
    return p.get("plan_id")


def _recompute_plan_status(state, plan_id):
    if not plan_id or plan_id not in state["plans"]:
        return state
    plan = state["plans"][plan_id]
    if plan["status"] in (PLAN_COMPLETED, PLAN_CANCELLED):
        return state
    if all(s["status"] == STEP_ENERGIZED for s in plan["steps"].values()):
        plan["status"] = PLAN_COMPLETED
    return state


def replay(events, base=None):
    state = base or initial_state()
    for ev in events:
        state = apply_event(state, ev)
    return state
