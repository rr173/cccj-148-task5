"""只读视图：把归并状态投影成操作台 / HTTP GET 需要的 DTO。"""
from __future__ import annotations

from . import domain as D
from .service import utc_now


def _now(now):
    return now or utc_now()


def overview(svc, now=None):
    st = svc.state
    now = _now(now)
    return {
        "voyage": st["voyage"],
        "generated_at": now,
        "counts": {
            "boards": len(st["boards"]),
            "circuits": len(st["circuits"]),
            "sockets": len(st["sockets"]),
            "containers": len(st["containers"]),
            "powered": sum(1 for sid, l in st["leases"].items()
                           if l["status"] == D.LEASE_POWERED
                           and not D.circuit_is_tripped(
                               st, st["sockets"][sid]["circuit_id"])),
            "reserved": sum(1 for l in st["leases"].values()
                            if l["status"] == D.LEASE_RESERVED),
            "unplugged": sum(1 for l in st["leases"].values()
                             if l["status"] == D.LEASE_UNPLUGGED),
            "tripped_circuits": len(st["trips"]),
            "quarantine_open": sum(1 for q in st["quarantine"].values()
                                   if not q["reviewed"]),
        },
        "last_event_seq": st["seq"],
    }


def topology(svc):
    st = svc.state
    boards = {}
    for b in st["boards"].values():
        boards[b["id"]] = {"id": b["id"], "name": b["name"], "circuits": []}
    for c in st["circuits"].values():
        boards.setdefault(c["board_id"],
                          {"id": c["board_id"], "name": c["board_id"],
                           "circuits": []})["circuits"].append(c["id"])
    circuits = {}
    for sid, sock in st["sockets"].items():
        circuits.setdefault(sock["circuit_id"], []).append(sid)
    return {"voyage": st["voyage"], "boards": list(boards.values()),
            "circuits": {cid: circuits.get(cid, [])
                         for cid in st["circuits"]}}


def circuit_loads(svc, now=None):
    st = svc.state
    now = _now(now)
    out = []
    for cid, c in sorted(st["circuits"].items()):
        committed, parts = D.circuit_demand(st, cid)
        measured, n = D.measured_circuit_current(st, cid)
        sockets = []
        for sid, sock in st["sockets"].items():
            if sock["circuit_id"] != cid:
                continue
            lease = st["leases"].get(sid)
            hold = st["holds"].get(sid)
            sockets.append({
                "socket_id": sid, "phase": sock["phase"],
                "voltage": sock["voltage"], "rated_a": sock["rated_a"],
                "allowed_types": sock["allowed_types"],
                "reach_zones": sock["reach_zones"],
                "lease": None if not lease else {
                    "box_id": lease["box_id"], "status": lease["status"],
                    "demand_a": lease["demand_a"],
                    "reserved_at": lease.get("reserved_at"),
                    "energized_at": lease.get("energized_at"),
                    "unpowered_at": lease.get("unpowered_at")},
                "held_by": hold})
        out.append({
            "circuit_id": cid, "board_id": c["board_id"],
            "phase": c["phase"], "voltage": c["voltage"],
            "rating_a": c["rating_a"],
            "tripped": D.circuit_is_tripped(st, cid),
            "committed_a": round(committed, 3),
            "measured_a": round(measured, 3),
            "measured_from_samples": n,
            "spare_a": round(c["rating_a"] - committed, 3),
            "parts": {k: round(v, 3) for k, v in parts.items()},
            "sockets": sockets})
    return {"circuits": out, "at": _now(now)}


def unpowered_boxes(svc, now=None):
    st = svc.state
    now = _now(now)
    rows = []
    for bid, box in sorted(st["containers"].items()):
        sid = None
        for s, lease in st["leases"].items():
            if lease["box_id"] == bid:
                sid, lease_ref = s, lease
                break
        else:
            lease_ref = None
        powered = sid is not None and lease_ref["status"] == D.LEASE_POWERED \
            and not D.circuit_is_tripped(st, st["sockets"][sid]["circuit_id"])
        if powered:
            continue
        rows.append({
            "box_id": bid, "type": box["type"], "phase": box["phase"],
            "voltage": box["voltage"], "demand_a": box["demand_a"],
            "priority": box["priority"], "zones": box["zones"],
            "socket_id": sid,
            "lease_status": lease_ref["status"] if lease_ref else None,
            "unpowered_since": D.unpowered_since(st, bid, now),
            "remaining_insulation_hours":
                D.remaining_insulation_hours(st, bid, now)})
    rows.sort(key=lambda r: (r["remaining_insulation_hours"],
                             -r["priority"], r["box_id"]))
    return {"boxes": rows, "at": now}


def quarantine(svc):
    return {"samples": [q for q in svc.state["quarantine"].values()
                        if not q["reviewed"]]}


def trip_impacts(svc, now=None):
    st = svc.state
    now = _now(now)
    out = []
    for cid, trip in sorted(st["trips"].items()):
        boxes = []
        for sid, sock in st["sockets"].items():
            if sock["circuit_id"] != cid:
                continue
            lease = st["leases"].get(sid)
            if not lease:
                continue
            bid = lease["box_id"]
            boxes.append({
                "box_id": bid, "socket_id": sid,
                "lease_status": lease["status"],
                "priority": st["containers"][bid]["priority"],
                "unpowered_since": D.unpowered_since(st, bid, now),
                "remaining_insulation_hours":
                    D.remaining_insulation_hours(st, bid, now)})
        boxes.sort(key=lambda b: (-b["priority"],
                                  b["remaining_insulation_hours"],
                                  b["box_id"]))
        out.append({"circuit_id": cid, "tripped_at": trip["at"],
                    "reason": trip["reason"], "affected_boxes": boxes})
    return {"trips": out, "at": now}


def migrations(svc):
    st = svc.state
    plans = []
    for plan in sorted(st["plans"].values(),
                       key=lambda p: p["created_at"] + p["plan_id"]):
        steps = [plan["steps"][sid] for sid in
                 sorted(plan["steps"], key=lambda s: plan["steps"][s]["order"])]
        plans.append({"plan_id": plan["plan_id"],
                      "circuit_id": plan["circuit_id"],
                      "status": plan["status"],
                      "created_at": plan["created_at"],
                      "created_by": plan.get("created_by"),
                      "cancelled_at": plan.get("cancelled_at"),
                      "steps": steps})
    return {"plans": plans}


def samples(svc):
    st = svc.state
    latest = []
    for skey, v in st["samples_latest"].items():
        bid, kind = skey.split("\x1f", 1)
        latest.append({"box_id": bid, "kind": kind, **v})
    latest.sort(key=lambda x: (x["box_id"], x["kind"]))
    return {"latest": latest,
            "duplicate_marks":
                len(st["sample_keys"]) - len(st["samples_latest"])}


def event_stream(svc, limit=200):
    return {"events": svc.store.read_events()[-limit:][::-1],
            "total": svc.state["seq"]}


def verify_invariants(svc):
    """归并后一致性自检（崩溃恢复验收直接调用）。"""
    st = svc.state
    problems = []

    # 1) 一个插座至多一条未释放租约（leases 以 sid 为键天然保证），
    #    一个箱至多占用一个插座。
    seen_boxes = {}
    for sid, lease in st["leases"].items():
        if lease["box_id"] in seen_boxes:
            problems.append(f"箱 {lease['box_id']} 双重占用: "
                            f"{seen_boxes[lease['box_id']]} 与 {sid}")
        seen_boxes[lease["box_id"]] = sid

    # 2) 迁移保留不能落在已被租约占用的插座上（拔除中除外——其租约在原插座）。
    for sid, hold in st["holds"].items():
        lease = st["leases"].get(sid)
        if lease is not None:
            problems.append(f"保留与租约冲突于插座 {sid}")
        if sid not in st["sockets"]:
            problems.append(f"保留指向未知插座 {sid}")

    # 3) 负载统计：手算回路负载必须等于投影值且不越额定（跳闸冻结回路除外，
    #    跳闸回路本身仍要求统计不失真）。
    for cid, c in st["circuits"].items():
        total, parts = D.circuit_demand(st, cid)
        manual = 0.0
        for sid, lease in st["leases"].items():
            sock = st["sockets"][sid]
            if sock["circuit_id"] != cid:
                continue
            if lease["status"] in (D.LEASE_POWERED, D.LEASE_RESERVED):
                manual += lease["demand_a"]
        for sid, hold in st["holds"].items():
            sock = st["sockets"][sid]
            if sock["circuit_id"] == cid:
                manual += st["containers"][hold["box_id"]]["demand_a"]
        if abs(manual - total) > 1e-6:
            problems.append(f"回路 {cid} 负载统计失真: {manual} != {total}")
        if not D.circuit_is_tripped(st, cid) and total > c["rating_a"] + 1e-9:
            problems.append(f"非跳闸回路 {cid} 越额定: {total} > "
                            f"{c['rating_a']}")

    # 4) 已完成迁移步骤：箱必须在目标插座上 powered，原插座必须已释放，
    #    不得出现"迁移已完成但箱仍标记断电"。
    for plan in st["plans"].values():
        for step in plan["steps"].values():
            if step["status"] != D.STEP_ENERGIZED:
                continue
            tgt = st["leases"].get(step["to_socket"])
            if tgt is None or tgt["box_id"] != step["box_id"] \
                    or tgt["status"] != D.LEASE_POWERED:
                problems.append(
                    f"步骤 {step['step_id']} 已完成但目标插座未供电")
            if step["from_socket"] in st["leases"]:
                problems.append(
                    f"步骤 {step['step_id']} 已完成但原插座未释放")
            if step["to_socket"] in st["holds"]:
                problems.append(
                    f"步骤 {step['step_id']} 已完成但目标保留未解除")

    # 5) 拔除中的步骤：箱恰好绑定原插座且状态 unplugged。
    for plan in st["plans"].values():
        for step in plan["steps"].values():
            if step["status"] in (D.STEP_UNPLUGGED, D.STEP_OCCUPIED):
                src = st["leases"].get(step["from_socket"])
                if src is None or src["box_id"] != step["box_id"] \
                        or src["status"] != D.LEASE_UNPLUGGED:
                    problems.append(
                        f"在途步骤 {step['step_id']} 原插座状态不一致")
                if st["holds"].get(step["to_socket"], {}).get("step_id") \
                        != step["step_id"]:
                    problems.append(
                        f"在途步骤 {step['step_id']} 丢失目标保留")

    return {"ok": not problems, "problems": problems,
            "event_seq": st["seq"],
            "leases": len(st["leases"]), "holds": len(st["holds"])}
