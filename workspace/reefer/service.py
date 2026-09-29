"""命令服务：校验 -> 生成事件批次 -> 不可变流水落盘 -> 归并。

每个改变状态的命令都可携带 ``command_id``（接电终端的"命令标识"）：
  * 首次提交：校验通过则效果事件与一条 CommandResult 同批落盘，返回首次结果；
  * 相同 command_id 再提交：原样返回首次答复（重放，不重新计算、不改状态）；
  * 相同 command_id 但载荷指纹不同：拒绝（409 HASH_CONFLICT），不能改写当前
    接电状态；拒绝本身也落盘为 CommandRejected，保证答复同样唯一。

并发：所有命令在同一把 RLock 内串行（含 HTTP 多线程），方案争用同一目标插座
时，只有先提交者拿到保留，后提交者得到最新占用情况与可重新计算的冲突结果。
"""
from __future__ import annotations

import copy
import itertools
import threading
from datetime import datetime, timezone

from . import domain as D
from .store import Store, digest

SNAPSHOT_EVERY = 50      # 每 50 个事件归并一次快照


class ServiceError(Exception):
    def __init__(self, status, code, message, extra=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _req(obj, key):
    if key not in obj or obj[key] in (None, ""):
        raise ServiceError(400, "MISSING_FIELD", f"缺少字段: {key}")
    return obj[key]


class Service:
    def __init__(self, data_dir: str):
        self.store = Store(data_dir)
        self.lock = threading.RLock()
        self._snap_seq = 0
        self.state = self._load()

    # ------------------------------------------------------------ 装载/快照
    def _load(self):
        snap = self.store.load_snapshot()
        if snap:
            self._snap_seq = snap["seq"]
            state = copy.deepcopy(snap)
            # 快照只持久化归并状态；其后的事件在其上继续重放。
            after = self.store.read_events()[snap["seq"]:]
            state = D.replay(after, state)
            return state
        return D.replay(self.store.read_events())

    def _persist(self, events, at):
        """为事件编号并整批落盘、归并、（按阈值）存快照。返回带编号的事件。"""
        base = self.state["seq"]
        batch_id = f"B{base + 1:08d}"
        numbered = []
        for i, (kind, payload) in enumerate(events, start=1):
            numbered.append({"seq": base + i, "at": at,
                             "kind": kind, "batch_id": batch_id,
                             "payload": payload})
        self.store.append_batch(numbered)
        self.state = D.replay(numbered, self.state)
        if self.state["seq"] - self._snap_seq >= SNAPSHOT_EVERY:
            self.store.save_snapshot(self.state)
            self._snap_seq = self.state["seq"]
        return numbered

    def force_snapshot(self):
        with self.lock:
            self.store.save_snapshot(self.state)
            self._snap_seq = self.state["seq"]
            return self.state["seq"]

    # ------------------------------------------------------------ 幂等外壳
    def _run(self, scope, body, build, at=None, command_id=None,
             success_status=200):
        """通用命令执行：build(payload, at) -> (http_status, result, effects)。

        effects 为 (kind, payload) 列表；result 为返回给调用方的 JSON。
        """
        at = at or utc_now()
        cid = command_id or body.get("command_id")
        with self.lock:
            if cid:
                prev = self.state["idempotency"].get(cid)
                h = digest(body)
                if prev is not None:
                    if prev["hash"] != h:
                        # 不覆盖任何记录：首次答复/拒绝都冻结。
                        raise ServiceError(
                            409, "HASH_CONFLICT",
                            "相同命令标识的载荷与首次提交不一致，已拒绝",
                            {"first_status": prev["status"],
                             "current_status_locked": True})
                    # 重放首次结果：首次成功 -> 原答复；首次失败 -> 原拒绝。
                    if prev.get("rejected"):
                        err = prev["result"]["error"]
                        raise ServiceError(
                            prev["status"], err["code"],
                            err.get("message", ""),
                            {k: v for k, v in err.items()
                             if k not in ("code", "message")})
                    return prev["status"], copy.deepcopy(prev["result"]), True

            try:
                status, result, effects = build(body, at)
            except ServiceError as e:
                if cid:
                    rejected = [("CommandRejected", {
                        "command_id": cid, "scope": scope,
                        "hash": digest(body),
                        "http_status": e.status,
                        "result": {"error": {"code": e.code,
                                             "message": e.message,
                                             **e.extra}}})]
                    self._persist(rejected, at)
                raise

            if cid:
                effects = effects + [("CommandResult", {
                    "command_id": cid, "scope": scope, "hash": digest(body),
                    "http_status": status, "result": result})]
            if effects:
                self._persist(effects, at)
            return status, result, False

    # ============================================================ 拓扑/箱
    def register_topology(self, body, at=None):
        def build(b, now):
            voyage_id = _req(b, "voyage_id")
            if self.state["voyage"] is not None:
                raise ServiceError(409, "TOPOLOGY_EXISTS",
                                   "拓扑已登记，不可重复登记（不可变流水）")
            boards, circuits, sockets = _req(b, "boards"), \
                _req(b, "circuits"), _req(b, "sockets")
            board_ids = set()
            for x in boards:
                if x["id"] in board_ids:
                    raise ServiceError(400, "DUP_BOARD", f"重复配电板 {x['id']}")
                board_ids.add(x["id"])
            circ_ids = set()
            for x in circuits:
                if x["id"] in circ_ids:
                    raise ServiceError(400, "DUP_CIRCUIT", f"重复回路 {x['id']}")
                if x["board_id"] not in board_ids:
                    raise ServiceError(400, "BOARD_UNKNOWN",
                                       f"回路 {x['id']} 引用未知配电板")
                if float(x["rating_a"]) <= 0:
                    raise ServiceError(400, "BAD_RATING", "回路额定电流必须为正")
                circ_ids.add(x["id"])
            sock_ids = set()
            for x in sockets:
                if x["id"] in sock_ids:
                    raise ServiceError(400, "DUP_SOCKET", f"重复插座 {x['id']}")
                if x["circuit_id"] not in circ_ids:
                    raise ServiceError(400, "CIRCUIT_UNKNOWN",
                                       f"插座 {x['id']} 引用未知回路")
                if float(x["rated_a"]) <= 0:
                    raise ServiceError(400, "BAD_RATING", "插座额定电流必须为正")
                sock_ids.add(x["id"])
            result = {"voyage_id": voyage_id,
                      "boards": len(boards), "circuits": len(circuits),
                      "sockets": len(sockets)}
            effects = [("TopologyRegistered", {
                "voyage_id": voyage_id,
                "clock_skew_sec": b.get("clock_skew_sec", 30),
                "boards": boards, "circuits": circuits,
                "sockets": sockets})]
            return 201, result, effects
        return self._run("topology", body, build, at)

    def import_fleet(self, body, at=None):
        def build(b, now):
            if self.state["voyage"] is None:
                raise ServiceError(409, "NO_TOPOLOGY", "请先登记配电拓扑")
            conts = _req(b, "containers")
            ids = set()
            for c in conts:
                cid = _req(c, "id")
                if cid in ids:
                    raise ServiceError(400, "DUP_CONTAINER", f"重复箱号 {cid}")
                if cid in self.state["containers"]:
                    raise ServiceError(409, "CONTAINER_EXISTS",
                                       f"箱 {cid} 已在本航次")
                if float(c["rated_a"]) <= 0:
                    raise ServiceError(400, "BAD_RATING", "箱额定电流必须为正")
                ids.add(cid)
            effects = [("FleetImported", {"containers": conts})]
            return 201, {"imported": len(conts)}, effects
        return self._run("fleet", body, build, at)

    # ============================================================ 预留/送电
    def reserve_socket(self, body, at=None):
        def build(b, now):
            box_id = _req(b, "box_id")
            socket_id = _req(b, "socket_id")
            effects = self._reserve_effects(box_id, socket_id, now)
            resv_id = effects[0][1]["reservation_id"]
            return 201, {"reservation_id": resv_id, "box_id": box_id,
                         "socket_id": socket_id, "status": "reserved"}, effects
        return self._run("reserve", body, build, at)

    def auto_reserve(self, body, at=None):
        """按温控优先级为尚未占用插座的箱批量预留。

        排序：优先级高者先 -> 需求电流大者先 -> 箱号；插座选择：可接入且剩余
        容量最多的回路优先（贪心，确定性 tie-break 用编号）。整批一次落盘。
        """
        def build(b, now):
            if self.state["voyage"] is None:
                raise ServiceError(409, "NO_TOPOLOGY", "请先登记配电拓扑")
            wanted = b.get("box_ids")  # None 表示全部未接电箱
            boxes = [cid for cid in self.state["containers"]
                     if wanted is None or cid in wanted]
            chosen = set()
            for sid, lease in self.state["leases"].items():
                chosen.add(lease["box_id"])
            boxes = [c for c in boxes if c not in chosen]
            boxes.sort(key=lambda c: (-self.state["containers"][c]["priority"],
                                      -self.state["containers"][c]["demand_a"],
                                      c))
            effects, results, rejected = [], [], []
            sim = self.state  # 模拟状态：直接在真实状态上追加事件预览，
            # 成功时预览事件就是落盘事件；失败箱不产生事件。
            seq = self.state["resv_seq"]
            remaining = list(boxes)

            def eligible(sim_state, cid, sid):
                if not D.socket_free(sim_state, sid):
                    return False
                ok, _ = D.compatible(sim_state, cid, sid)
                if not ok:
                    return False
                circ = sim_state["sockets"][sid]["circuit_id"]
                if D.circuit_is_tripped(sim_state, circ):
                    return False
                return D.can_fit(sim_state, circ,
                                 sim_state["containers"][cid]["demand_a"])

            def candidates_for(sim_state, cid):
                return [sid for sid in sorted(sim_state["sockets"])
                        if eligible(sim_state, cid, sid)]

            def assign(cid, cand):
                nonlocal sim, seq
                # 多候选选座：
                #  1) 与箱区域的贴合度（插座区域集合越小、越专，越优先，
                #     使 RFU2 落到专座 S1B，而不是泛用备用 S2A/S2B）；
                #  2) 回路利用度高者优先（先填主回路，把备用回路留空迁移）；
                #  3) 回路编号、插座编号兜底，保证完全确定。
                box = sim["containers"][cid]
                if len(cand) > 1:
                    def key(s):
                        sock = sim["sockets"][s]
                        total, _ = D.circuit_demand(
                            sim, sock["circuit_id"])
                        utilization = total / \
                            sim["circuits"][sock["circuit_id"]]["rating_a"]
                        return (len(sock["reach_zones"]), -utilization,
                                sock["circuit_id"], s)
                    cand = sorted(cand, key=key)
                sid = cand[0]
                seq += 1
                resv_id = f"RSV-{seq:05d}"
                eff = ("SocketReserved", {
                    "reservation_id": resv_id, "reservation_seq": seq,
                    "box_id": cid, "socket_id": sid,
                    "demand_a": self.state["containers"][cid]["demand_a"],
                    "at": now})
                effects.append(eff)
                sim = D.apply_event(sim, self._stub(eff))
                results.append({"box_id": cid, "socket_id": sid,
                                "reservation_id": resv_id})

            # 第一趟：唯一候选箱逐个锁定（每轮重算候选，优先级高者先）。
            # 这样 RFU4->S3A、RFU3->S1C、RFU1->S1A 先把结构上钉死的匹配
            # 全部占好，避免多候选箱提前抢走主回路专座。
            while True:
                cand_map = {cid: candidates_for(sim, cid)
                            for cid in remaining}
                forced_now = sorted(
                    (c for c in remaining if len(cand_map[c]) == 1),
                    key=lambda c: (-self.state["containers"][c]["priority"],
                                   c))
                if not forced_now:
                    break
                cid = forced_now[0]
                assign(cid, cand_map[cid])
                remaining.remove(cid)
            # 第二趟：剩余箱都有多个候选，按温控优先级贪心。
            remaining.sort(key=lambda c: (
                -self.state["containers"][c]["priority"],
                -self.state["containers"][c]["demand_a"], c))
            for cid in remaining:
                cand = candidates_for(sim, cid)
                if cand:
                    assign(cid, cand)
                else:
                    rejected.append({"box_id": cid,
                                     "reason": "NO_ELIGIBLE_SOCKET"})
            if not results and rejected:
                raise ServiceError(409, "ALL_REJECTED",
                                   "没有箱能完成预留",
                                   {"rejected": rejected})
            if not results:
                return 200, {"reserved": [], "rejected": []}, []
            return 201, {"reserved": results, "rejected": rejected}, effects
        return self._run("auto_reserve", body, build, at)

    @staticmethod
    def _stub(eff):
        kind, payload = eff
        return {"seq": 0, "at": payload.get("at") or utc_now(),
                "kind": kind, "batch_id": "preview", "payload": payload}

    def _reserve_effects(self, box_id, socket_id, now):
        st = self.state
        if st["voyage"] is None:
            raise ServiceError(409, "NO_TOPOLOGY", "请先登记配电拓扑")
        if box_id not in st["containers"]:
            raise ServiceError(404, "BOX_UNKNOWN", f"未知箱号 {box_id}")
        if socket_id not in st["sockets"]:
            raise ServiceError(404, "SOCKET_UNKNOWN", f"未知插座 {socket_id}")
        _, busy = None, None
        for sid, lease in st["leases"].items():
            if lease["box_id"] == box_id:
                busy = sid
        # 物理适配性优先判定（即使插座已被占，相制不符也是更本质的拒绝原因）。
        ok, reason = D.compatible(st, box_id, socket_id)
        if not ok:
            raise ServiceError(409, reason,
                               f"箱 {box_id} 与插座 {socket_id} 不匹配: {reason}")
        if busy:
            raise ServiceError(409, "BOX_ALREADY_LEASED",
                               f"箱 {box_id} 已占用插座 {busy}（一个箱只能占用一个插座）")
        if socket_id in st["leases"]:
            raise ServiceError(409, "SOCKET_IN_USE",
                               f"插座 {socket_id} 已服务另一个箱")
        if socket_id in st["holds"]:
            h = st["holds"][socket_id]
            raise ServiceError(409, "SOCKET_HELD",
                               f"插座 {socket_id} 被迁移方案 {h['plan_id']} 保留",
                               {"held_by": h})
        cid_circuit = st["sockets"][socket_id]["circuit_id"]
        if D.circuit_is_tripped(st, cid_circuit):
            raise ServiceError(409, "CIRCUIT_TRIPPED",
                               f"回路 {cid_circuit} 已跳闸，冻结新接电")
        demand = st["containers"][box_id]["demand_a"]
        if not D.can_fit(st, cid_circuit, demand):
            total, parts = D.circuit_demand(st, cid_circuit)
            raise ServiceError(
                409, "CIRCUIT_OVERLOADED",
                f"回路容量不足: {total:g}+{demand:g} > "
                f"{st['circuits'][cid_circuit]['rating_a']:g}A",
                {"circuit_id": cid_circuit,
                 "current_total": total, "extra": demand,
                 "rating_a": st["circuits"][cid_circuit]["rating_a"],
                 "parts": parts})
        seq = st["resv_seq"] + 1
        resv_id = f"RSV-{seq:05d}"
        return [("SocketReserved", {
            "reservation_id": resv_id, "reservation_seq": seq,
            "box_id": box_id, "socket_id": socket_id,
            "demand_a": demand, "at": now})]

    def _best_socket(self, st, box_id):
        """为箱挑选最佳可接入插座；无候选返回 None。

        评分（全部升序，完全确定）：
          1. 插座对该箱的"专用度"——可达区域交集越小越优先（RFU2 只在 Z2，
             S1B/S2A/S2B 都可接，但 S1B 与 Z2 的绑定最直接时仍由下一项决定）；
          2. 回路当前承诺负载高者优先（先填在用的主回路，把备用回路留空供
             跳闸迁移）；额定/占用相同时落到编号顺序。
          3. 回路编号、插座编号兜底。
        """
        box = st["containers"][box_id]
        candidates = []
        for sid in sorted(st["sockets"]):
            if not D.socket_free(st, sid):
                continue
            sock = st["sockets"][sid]
            ok, _ = D.compatible(st, box_id, sid)
            if not ok:
                continue
            circ = sock["circuit_id"]
            if D.circuit_is_tripped(st, circ):
                continue
            demand = box["demand_a"]
            if not D.can_fit(st, circ, demand):
                continue
            total, _ = D.circuit_demand(st, circ)
            rating = st["circuits"][circ]["rating_a"]
            # 该插座能服务的区域越少，越是"专座"，越优先占；
            # 其次优先填充已利用度高的回路（load/rating 大）。
            specificity = len(sock["reach_zones"])
            utilization = total / rating
            candidates.append((specificity, -utilization, circ, sid))
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][3]

    def energize(self, body, at=None):
        def build(b, now):
            box_id = _req(b, "box_id")
            socket_id = _req(b, "socket_id")
            st = self.state
            lease = st["leases"].get(socket_id)
            if lease is None:
                raise ServiceError(404, "NO_RESERVATION",
                                   f"插座 {socket_id} 没有预留")
            if lease["box_id"] != box_id:
                raise ServiceError(409, "LEASE_BOX_MISMATCH",
                                   "预留属于另一个箱",
                                   {"reserved_box": lease["box_id"]})
            if lease["status"] != D.LEASE_RESERVED:
                raise ServiceError(409, "LEASE_NOT_RESERVED",
                                   f"租约当前状态 {lease['status']}，不能送电")
            circ = st["sockets"][socket_id]["circuit_id"]
            if D.circuit_is_tripped(st, circ):
                raise ServiceError(409, "CIRCUIT_TRIPPED",
                                   f"回路 {circ} 已跳闸，冻结送电")
            # 提交时再做一次容量闸门：本预留的需求早已计入回路承诺值（reserved
            # 部分），送电只是租约状态转移，故按当前承诺总量核对即可；这样可
            # 捕获"预留后余量又被迁移保留吃掉"的情况。
            total, parts = D.circuit_demand(st, circ)
            if total > st["circuits"][circ]["rating_a"] + 1e-9:
                raise ServiceError(409, "CIRCUIT_OVERLOADED",
                                   "送电时回路承诺负载已越额定",
                                   {"current_total": total, "parts": parts})
            effects = [("SocketEnergized", {"socket_id": socket_id,
                                             "box_id": box_id, "at": now})]
            return 200, {"box_id": box_id, "socket_id": socket_id,
                         "status": "powered"}, effects
        return self._run("energize", body, build, at)

    def deenergize(self, body, at=None):
        def build(b, now):
            box_id = _req(b, "box_id")
            socket_id = _req(b, "socket_id")
            st = self.state
            lease = st["leases"].get(socket_id)
            if lease is None or lease["box_id"] != box_id:
                raise ServiceError(404, "NO_LEASE", "该插座上没有此箱的租约")
            if lease["status"] != D.LEASE_POWERED:
                raise ServiceError(409, "NOT_POWERED",
                                   f"租约状态 {lease['status']}，未在供电")
            effects = [("SocketDeenergized", {"socket_id": socket_id,
                                               "box_id": box_id, "at": now})]
            return 200, {"box_id": box_id, "socket_id": socket_id,
                         "status": "reserved"}, effects
        return self._run("deenergize", body, build, at)

    def release_reservation(self, body, at=None):
        def build(b, now):
            box_id = _req(b, "box_id")
            st = self.state
            sid, lease = None, None
            for s, l in st["leases"].items():
                if l["box_id"] == box_id:
                    sid, lease = s, l
            if lease is None:
                raise ServiceError(404, "NO_LEASE", f"箱 {box_id} 无租约")
            if lease["status"] == D.LEASE_POWERED:
                raise ServiceError(409, "LEAVE_POWERED",
                                   "供电中的插座不能直接释放，请先断电")
            if lease["status"] == D.LEASE_UNPLUGGED:
                raise ServiceError(409, "MIGRATION_IN_FLIGHT",
                                   "迁移拔除中途，不能释放原插座")
            effects = [("ReservationReleased", {"socket_id": sid,
                                                 "box_id": box_id,
                                                 "at": now})]
            return 200, {"box_id": box_id, "socket_id": sid,
                         "released": True}, effects
        return self._run("release", body, build, at)

    # ============================================================ 采样
    def ingest_samples(self, body, at=None):
        def build(b, now):
            if self.state["voyage"] is None:
                raise ServiceError(409, "NO_TOPOLOGY", "请先登记配电拓扑")
            samples = _req(b, "samples")
            allowed_skew = self.state["voyage"]["clock_skew_sec"]
            effects, accepted, quarantined, duplicate = [], [], [], []
            # 以归并状态为基础增量推进，使同一批内的重复/倒序立即可见。
            sim = self.state
            qseq = len(sim["quarantine"])
            for s in samples:
                box_id = _req(s, "box_id")
                if box_id not in sim["containers"]:
                    raise ServiceError(404, "BOX_UNKNOWN", f"未知箱号 {box_id}")
                kind = _req(s, "kind")
                if kind not in (D.SAMPLE_CURRENT, D.SAMPLE_TEMP,
                                D.SAMPLE_POWERDOWN):
                    raise ServiceError(400, "BAD_SAMPLE_KIND",
                                       f"未知采样类型 {kind}")
                sensor = _req(s, "sensor")
                measured_at = _req(s, "measured_at")
                try:
                    mts = D.parse_ts(measured_at)
                except ValueError:
                    raise ServiceError(400, "BAD_TIMESTAMP", measured_at)
                key = f"{box_id}|{kind}|{sensor}|{measured_at}"
                common = {"box_id": box_id, "kind": kind,
                          "value": s.get("value"), "at": measured_at,
                          "sensor": sensor, "key": key,
                          "sample_seq": s.get("sample_seq")}
                if key in sim["sample_keys"]:
                    duplicate.append({"key": key})
                    effects.append(("SampleDuplicate",
                                    {"key": key, "box_id": box_id,
                                     "kind": kind}))
                    continue
                latest = sim["samples_latest"].get(
                    D.sample_latest_key(box_id, kind))
                out_of_order = latest is not None and \
                    (D.parse_ts(latest["at"]) - mts).total_seconds() \
                    > allowed_skew
                if out_of_order:
                    qseq += 1
                    qid = f"Q-{qseq:05d}"
                    common.update({"qid": qid,
                                   "reason": D.QUAR_REASON_CLOCK})
                    effects.append(("SampleQuarantined", common))
                    # 待审样本不更新 latest（不参与负载/保温计算），但键要登记
                    # 以拦截完全相同的重报。
                    sim = D.apply_event(sim, self._stub(
                        ("SampleQuarantined", common)))
                    quarantined.append({"qid": qid, "box_id": box_id,
                                        "reason": D.QUAR_REASON_CLOCK})
                else:
                    effects.append(("SampleAccepted", dict(common)))
                    sim = D.apply_event(sim, self._stub(
                        ("SampleAccepted", common)))
                    accepted.append({"box_id": box_id, "kind": kind,
                                     "at": measured_at})
            return 200, {"accepted": accepted, "quarantined": quarantined,
                         "duplicate": duplicate}, effects
        return self._run("samples", body, build, at)

    def review_sample(self, body, at=None):
        def build(b, now):
            qid = _req(b, "qid")
            decision = _req(b, "decision")
            if decision not in ("accept", "reject"):
                raise ServiceError(400, "BAD_DECISION", decision)
            if qid not in self.state["quarantine"]:
                raise ServiceError(404, "Q_UNKNOWN", f"未知待审样本 {qid}")
            q = self.state["quarantine"][qid]
            if q["reviewed"]:
                raise ServiceError(409, "Q_REVIEWED", "该样本已处置")
            effects = [("SampleReviewed", {"qid": qid, "decision": decision,
                                           "by": b.get("operator"),
                                           "at": now})]
            return 200, {"qid": qid, "decision": decision}, effects
        return self._run("review", body, build, at)

    # ============================================================ 跳闸/复位
    def trip_circuit(self, body, at=None):
        def build(b, now):
            cid = _req(b, "circuit_id")
            st = self.state
            if cid not in st["circuits"]:
                raise ServiceError(404, "CIRCUIT_UNKNOWN", f"未知回路 {cid}")
            if cid in st["trips"]:
                raise ServiceError(409, "ALREADY_TRIPPED",
                                   f"回路 {cid} 已处于跳闸状态")
            effects = [("CircuitTripped", {"circuit_id": cid, "at": now,
                                           "reason": b.get("reason", "")})]
            impact = self._trip_impact(st, cid, now)
            return 201, {"circuit_id": cid, "tripped_at": now,
                         "impact": impact}, effects
        return self._run("trip", body, build, at)

    def _trip_impact(self, st, cid, now):
        # 跳闸事件落盘前的受影响箱清单（剩余保温时限在视图层基于归并后状态
        # 统一计算，保证与"当前时刻"口径一致）。
        boxes = []
        for sid, sock in st["sockets"].items():
            if sock["circuit_id"] != cid:
                continue
            lease = st["leases"].get(sid)
            if lease and lease["status"] == D.LEASE_POWERED:
                boxes.append({"box_id": lease["box_id"], "socket_id": sid})
        return {"circuit_id": cid, "boxes_at_event": boxes}

    def reset_circuit(self, body, at=None):
        def build(b, now):
            cid = _req(b, "circuit_id")
            if cid not in self.state["circuits"]:
                raise ServiceError(404, "CIRCUIT_UNKNOWN", f"未知回路 {cid}")
            if cid not in self.state["trips"]:
                raise ServiceError(409, "NOT_TRIPPED", f"回路 {cid} 未跳闸")
            return 200, {"circuit_id": cid, "reset": True}, \
                [("CircuitReset", {"circuit_id": cid, "at": now})]
        return self._run("reset", body, build, at)

    # ============================================================ 迁移
    def create_migration(self, body, at=None):
        """为跳闸回路生成有序迁移方案，并立刻保留全部目标插座。"""
        def build(b, now):
            cid = _req(b, "circuit_id")
            st = self.state
            if cid not in st["circuits"]:
                raise ServiceError(404, "CIRCUIT_UNKNOWN", f"未知回路 {cid}")
            if cid not in st["trips"]:
                raise ServiceError(409, "NOT_TRIPPED",
                                   "仅跳闸回路需要迁移方案")
            affected = []
            # 已完成迁移（租约离开本回路）的箱不再纳入；但在途/pending 的箱
            # 物理上仍在跳闸回路上断电，第二份方案必须能看到它们的目标被谁
            # 保留，以给出最新占用的冲突结果。
            moved_off = set()
            for plan in st["plans"].values():
                if plan["status"] == D.PLAN_CANCELLED:
                    continue
                for step in plan["steps"].values():
                    if step["status"] == D.STEP_ENERGIZED:
                        moved_off.add(step["box_id"])
            for sid in sorted(st["sockets"]):
                sock = st["sockets"][sid]
                if sock["circuit_id"] != cid:
                    continue
                lease = st["leases"].get(sid)
                if lease and lease["status"] in (D.LEASE_POWERED,
                                                 D.LEASE_UNPLUGGED):
                    bid = lease["box_id"]
                    if bid in moved_off:
                        continue
                    affected.append((bid, sid))
            # 排序：温控优先级高 -> 剩余保温时限短 -> 箱号
            affected.sort(key=lambda item: (
                -st["containers"][item[0]]["priority"],
                D.remaining_insulation_hours(st, item[0], now),
                item[0]))
            if not affected:
                raise ServiceError(409, "NO_AFFECTED_BOX",
                                   "该回路上没有受影响的供电箱")
            plan_seq = st["plan_seq"] + 1
            plan_id = f"PLAN-{plan_seq:04d}"
            step_seq = st["step_seq"]
            sim_holds = {}   # 方案内贪心模拟：to_socket -> box_id
            steps, conflicts = [], []
            for bid, from_sid in affected:
                target = self._pick_target_greedy(st, bid, cid, sim_holds)
                if target is None:
                    conflicts.append(
                        self._conflict_detail_greedy(st, bid, cid, sim_holds))
                    continue
                step_seq += 1
                step_id = f"STEP-{step_seq:05d}"
                demand = st["containers"][bid]["demand_a"]
                step = {"step_id": step_id, "step_seq": step_seq,
                        "plan_id": plan_id, "box_id": bid,
                        "from_socket": from_sid, "to_socket": target,
                        "priority": st["containers"][bid]["priority"],
                        "demand_a": demand, "remaining_hours_at_plan":
                            D.remaining_insulation_hours(st, bid, now),
                        "status": D.STEP_PENDING, "order": len(steps) + 1}
                steps.append(step)
                sim_holds[target] = bid
            if not steps:
                # 全部箱都没有可重新计算的目标：返回最新占用与冲突，不保留任何插座
                raise ServiceError(409, "MIGRATION_BLOCKED",
                                   "当前没有可用迁移目标，可依据冲突结果重新计算",
                                   {"circuit_id": cid, "conflicts": conflicts})
            effects = [("MigrationPlanCreated", {
                "plan_seq": plan_seq, "plan_id": plan_id,
                "circuit_id": cid, "at": now,
                "created_by": b.get("operator"), "steps": steps})]
            result = {"plan_id": plan_id, "circuit_id": cid,
                      "steps": steps, "conflicts": conflicts}
            status = 207 if conflicts else 201
            return status, result, effects
        return self._run("migration_create", body, build, at,
                         success_status=201)

    def _pick_target_greedy(self, st, box_id, tripped_cid, extra_holds):
        """在真实状态 + 方案内临时保留(extra_holds)上贪心选目标插座。"""
        best = None
        for sid in sorted(st["sockets"]):
            sock = st["sockets"][sid]
            if sock["circuit_id"] == tripped_cid:
                continue
            if not D.socket_free(st, sid) or sid in extra_holds:
                continue
            if D.circuit_is_tripped(st, sock["circuit_id"]):
                continue
            ok, _ = D.compatible(st, box_id, sid)
            if not ok:
                continue
            demand = st["containers"][box_id]["demand_a"]
            total = self._greedy_circuit_total(st, sock["circuit_id"],
                                               extra_holds)
            if total + demand > st["circuits"][sock["circuit_id"]]["rating_a"] \
                    + 1e-9:
                continue
            spare = st["circuits"][sock["circuit_id"]]["rating_a"] - total
            key = (spare, sock["circuit_id"], sid)
            if best is None or key > best[0]:
                best = (key, sid)
        return best[1] if best else None

    @staticmethod
    def _greedy_circuit_total(st, cid, extra_holds):
        total, _ = D.circuit_demand(st, cid)
        for sid, bid in extra_holds.items():
            sock = st["sockets"].get(sid)
            if sock and sock["circuit_id"] == cid:
                total += st["containers"][bid]["demand_a"]
        return total

    def _conflict_detail_greedy(self, st, box_id, tripped_cid, extra_holds):
        """列出每个备选插座不可用的最新原因，供另一方重新计算。

        判定顺序：物理兼容（相制/电压/箱型/可达/插座额定）优先于占用状态——
        "就算空着也插不上"对重新计算最关键；然后是保留/占用/跳闸/容量。
        """
        reasons = []
        for sid in sorted(st["sockets"]):
            sock = st["sockets"][sid]
            if sock["circuit_id"] == tripped_cid:
                continue
            ok, reason = D.compatible(st, box_id, sid)
            if not ok:
                why = {"code": reason}
            elif sid in st["leases"]:
                why = {"code": "OCCUPIED",
                       "by_box": st["leases"][sid]["box_id"],
                       "status": st["leases"][sid]["status"]}
            elif sid in st["holds"]:
                h = st["holds"][sid]
                why = {"code": "HELD", "by_plan": h["plan_id"],
                       "by_box": h["box_id"]}
            elif sid in extra_holds:
                why = {"code": "HELD_IN_PLAN", "by_box": extra_holds[sid]}
            elif D.circuit_is_tripped(st, sock["circuit_id"]):
                why = {"code": "CIRCUIT_TRIPPED"}
            else:
                demand = st["containers"][box_id]["demand_a"]
                total = self._greedy_circuit_total(
                    st, sock["circuit_id"], extra_holds)
                if total + demand > \
                        st["circuits"][sock["circuit_id"]]["rating_a"] \
                        + 1e-9:
                    why = {"code": "CIRCUIT_OVERLOADED",
                           "current_total": total,
                           "rating_a":
                               st["circuits"][sock["circuit_id"]]["rating_a"]}
                else:
                    continue  # 这个插座实际可用，不出现在冲突清单里
            reasons.append({"socket_id": sid, **why})
        return {"box_id": box_id,
                "priority": st["containers"][box_id]["priority"],
                "rejected_targets": reasons}

    def confirm_step(self, body, at=None, action=None):
        """电气员逐步确认：unplug / occupy / energize。"""
        def build(b, now):
            plan_id = _req(b, "plan_id")
            step_id = _req(b, "step_id")
            act = action or _req(b, "action")
            st = self.state
            plan = st["plans"].get(plan_id)
            if plan is None:
                raise ServiceError(404, "PLAN_UNKNOWN", f"未知方案 {plan_id}")
            step = plan["steps"].get(step_id)
            if step is None:
                raise ServiceError(404, "STEP_UNKNOWN", f"未知步骤 {step_id}")
            if plan["status"] == D.PLAN_CANCELLED:
                raise ServiceError(409, "PLAN_CANCELLED",
                                   "方案已取消，未执行步骤已释放")
            if act == "unplug":
                if step["status"] != D.STEP_PENDING:
                    raise ServiceError(409, "STEP_STATE",
                                       f"步骤状态 {step['status']}，不能拔除")
                lease = st["leases"].get(step["from_socket"])
                if not lease or lease["box_id"] != step["box_id"]:
                    raise ServiceError(409, "LEASE_GONE",
                                       "原插座租约已不存在，无法拔除")
                # 目标插座仍须被本步骤保留（未完成前不得被抢）
                if st["holds"].get(step["to_socket"], {}).get("step_id") \
                        != step_id:
                    raise ServiceError(409, "TARGET_LOST",
                                       "目标插座保留已丢失")
                eff = [("MigrationUnplugged", {"plan_id": plan_id,
                                                "step_id": step_id,
                                                "box_id": step["box_id"],
                                                "at": now})]
                return 200, {"step_id": step_id,
                             "status": D.STEP_UNPLUGGED}, eff
            if act == "occupy":
                if step["status"] != D.STEP_UNPLUGGED:
                    raise ServiceError(409, "STEP_STATE",
                                       "必须先确认拔除才能占用目标插座")
                hold = st["holds"].get(step["to_socket"])
                if not hold or hold["step_id"] != step_id:
                    raise ServiceError(409, "TARGET_LOST",
                                       "目标插座已不再为本步骤保留")
                # 箱仍只能绑定原插座；此处仅登记"目标已占用"，不产生第二条租约。
                eff = [("MigrationTargetOccupied", {"plan_id": plan_id,
                                                     "step_id": step_id,
                                                     "box_id": step["box_id"],
                                                     "at": now})]
                return 200, {"step_id": step_id,
                             "status": D.STEP_OCCUPIED}, eff
            if act == "energize":
                if step["status"] != D.STEP_OCCUPIED:
                    raise ServiceError(409, "STEP_STATE",
                                       "必须先确认目标插座占用才能送电")
                target = step["to_socket"]
                # 送电闸门：相制/容量（余量可能在方案生成后变化）
                ok, reason = D.compatible(st, step["box_id"], target)
                if not ok:
                    raise ServiceError(409, reason,
                                       "送电闸门：箱与目标插座不再兼容")
                circ = st["sockets"][target]["circuit_id"]
                if D.circuit_is_tripped(st, circ):
                    raise ServiceError(409, "CIRCUIT_TRIPPED",
                                       "目标回路已跳闸，拒绝送电")
                # 换插座提交后的回路负载 = 当前承诺 - 该 hold（hold 转正为租约）。
                # hold 的需求本就计入承诺，因此总量不变，直接核对总额定即可。
                total, parts = D.circuit_demand(st, circ)
                if total > st["circuits"][circ]["rating_a"] + 1e-9:
                    raise ServiceError(409, "CIRCUIT_OVERLOADED",
                                       "送电闸门：目标回路承诺负载已越额定",
                                       {"current_total": total, "parts": parts})
                # 关键换插座事务：单条事件同时完成"释放原插座 + 占用目标 +
                # 送电 + 解除保留"。中途崩溃则整批不存在，租约保持拔除中状态，
                # 电气员凭同一命令标识重发即可，绝不出现双重占用。
                eff = [("MigrationSwapCommitted", {
                    "plan_id": plan_id, "step_id": step_id,
                    "box_id": step["box_id"],
                    "from_socket": step["from_socket"],
                    "to_socket": target, "demand_a": step["demand_a"],
                    "at": now})]
                return 200, {"step_id": step_id, "box_id": step["box_id"],
                             "from_socket": step["from_socket"],
                             "to_socket": target,
                             "status": D.STEP_ENERGIZED}, eff
            raise ServiceError(400, "BAD_ACTION", f"未知迁移动作 {act}")
        return self._run(f"migration_{action or 'step'}", body, build, at)

    def cancel_migration(self, body, at=None):
        """取消方案：释放尚未执行(pending)步骤的目标保留；已在途步骤保留。"""
        def build(b, now):
            plan_id = _req(b, "plan_id")
            st = self.state
            plan = st["plans"].get(plan_id)
            if plan is None:
                raise ServiceError(404, "PLAN_UNKNOWN", f"未知方案 {plan_id}")
            if plan["status"] == D.PLAN_COMPLETED:
                raise ServiceError(409, "PLAN_COMPLETED", "方案已全部完成")
            if plan["status"] == D.PLAN_CANCELLED:
                return 200, {"plan_id": plan_id, "already_cancelled": True,
                             "released_steps": []}, []
            released, in_flight = [], []
            for step in plan["steps"].values():
                if step["status"] == D.STEP_PENDING:
                    released.append(step["step_id"])
                else:
                    in_flight.append({"step_id": step["step_id"],
                                      "status": step["status"]})
            eff = [("MigrationCancelled", {"plan_id": plan_id, "at": now})]
            return 200, {"plan_id": plan_id, "released_steps": released,
                         "in_flight_steps": in_flight,
                         "note": "已拔除的箱仍断电且占用原插座，需继续完成或复位回路后重插"}, eff
        return self._run("migration_cancel", body, build, at)
