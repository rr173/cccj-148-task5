'use strict';

import { normPhase, num, round2, nameplateCurrentA, bad, DomainError } from './util.js';

// 状态形态：
// {
//   boards:{id:{id,name}}, circuits:{id:{...}}, sockets:{id:{...}},
//   reefers:{id:{...}},
//   reservations:{socketId: {reeferId, leaseId}},   // RESERVE 租约的映射（活动）
//   leases:[ {id, reeferId, socketId, state, fromLeaseId?} ],
//   plans:{id:{...steps}},
//   samples:{ [key]:{...} },                        // 去重后的样本（含隔离/重复）
//   latestCurrent:{socketId:{...}}, powerLossAt:{reeferId:ts},
//   seq: number
// }
export function initialState() {
  return {
    boards: new Map(),
    circuits: new Map(),
    sockets: new Map(),
    reefers: new Map(),
    reservations: new Map(),
    leases: [],
    plans: new Map(),
    samples: new Map(),
    latestCurrent: new Map(),
    powerLossAt: new Map(),
    seq: 0,
    planSeq: 0,
  };
}

export function assertInvariants(s, tag) {
  // 任一时刻一个插座只能服务一个箱（活动租约唯一）
  const bySocket = new Map();
  const byReefer = new Map();
  for (const l of s.leases) {
    if (leaseClosed(l)) continue;
    const a = bySocket.get(l.socketId) || [];
    a.push(l);
    bySocket.set(l.socketId, a);
    const b = byReefer.get(l.reeferId) || [];
    b.push(l);
    byReefer.set(l.reeferId, b);
  }
  for (const [sid, ls] of bySocket) {
    if (ls.length > 1) {
      throw new Error(`不变量被破坏 (${tag}): 插座 ${sid} 双重占用 ${ls.map((l) => `${l.reeferId}:${l.state}`)}`);
    }
  }
  for (const [rid, ls] of byReefer) {
    // 迁移期间允许同一箱同时持有 FROZEN(原) 与 CLAIM(目标)，其余情况唯一
    const nonMigPair = ls.filter((l) => l.state !== 'FROZEN' && l.state !== 'CLAIM');
    if (nonMigPair.length > 1) {
      throw new Error(`不变量被破坏 (${tag}): 箱 ${rid} 占用多个插座`);
    }
    if (ls.length > 2) {
      throw new Error(`不变量被破坏 (${tag}): 箱 ${rid} 租约数 ${ls.length}`);
    }
    if (ls.length === 2) {
      const st = ls.map((l) => l.state).sort().join('+');
      // 迁移中间态：目标 CLAIM + 原 FROZEN（刚占位）或 UNPLUGGED（已拔除待送电）
      if (st !== 'CLAIM+FROZEN' && st !== 'CLAIM+UNPLUGGED') {
        throw new Error(`不变量被破坏 (${tag}): 箱 ${rid} 非法租约组合 ${st}`);
      }
    }
  }
  // 预留映射与租约一致
  for (const [sid, r] of s.reservations) {
    const l = leaseById(s, r.leaseId);
    if (!l || leaseClosed(l) || l.socketId !== sid) throw new Error(`不变量被破坏 (${tag}): 预留映射漂移 ${sid}`);
  }
  // 计划步骤状态一致性
  for (const plan of s.plans.values()) {
    for (const st of plan.steps) {
      if (st.state === 'PENDING') {
        // 步骤 0 在方案创建时即预占目标插座（CLAIM），属设计内状态
        if (st.claimLeaseId && st.occupyCommitted) continue;
        if (st.claimLeaseId) throw new Error(`不变量被破坏 (${tag}): PENDING 步骤持有租约`);
      }
      if (st.state === 'DONE') {
        const l = leaseById(s, st.newLeaseId);
        if (!l || l.state !== 'POWER' || l.socketId !== st.targetSocketId) {
          throw new Error(`不变量被破坏 (${tag}): 计划 ${plan.id} 步骤 ${st.index} 已完成但目标未送电`);
        }
      }
    }
    if (plan.state === 'COMPLETED') {
      for (const reeferId of plan.reeferIds) {
        const r = s.reefers.get(reeferId);
        if (r && !poweredLease(s, reeferId)) {
          throw new Error(`不变量被破坏 (${tag}): 计划 ${plan.id} 已完成但箱 ${reeferId} 仍断电`);
        }
      }
    }
  }
}

export function leaseClosed(l) {
  return l.state === 'RELEASED' || l.state === 'SUPERSEDED';
}
export function activeLeases(s) {
  return s.leases.filter((l) => !leaseClosed(l));
}
export function leaseById(s, id) {
  return s.leases.find((l) => l.id === id) || null;
}
export function activeLeaseOnSocket(s, socketId) {
  return s.leases.find((l) => l.socketId === socketId && !leaseClosed(l)) || null;
}
export function activeLeaseOfReefer(s, reeferId) {
  return s.leases.find((l) => l.reeferId === reeferId && !leaseClosed(l)) || null;
}
export function activeLeasesOfReefer(s, reeferId) {
  return s.leases.filter((l) => l.reeferId === reeferId && !leaseClosed(l));
}
export function poweredLease(s, reeferId) {
  return s.leases.find((l) => l.reeferId === reeferId && l.state === 'POWER') || null;
}

// ---------- 事实归并（确定性纯函数） ----------
export function applyFact(s, fact) {
  switch (fact.type) {
    case 'topologyImported': return applyTopology(s, fact);
    case 'reefersImported': return applyReefers(s, fact);
    case 'reserved': return applyReserved(s, fact);
    case 'powered': return applyPowered(s, fact);
    case 'sampleRecorded': return applySample(s, fact);
    case 'tripped': return applyTripped(s, fact);
    case 'circuitReset': return applyReset(s, fact);
    case 'migrationCreated': return applyMigrationCreated(s, fact);
    case 'stepAdvanced': return applyStepAdvanced(s, fact);
    case 'migrationCancelled': return applyMigrationCancelled(s, fact);
    default:
      throw new Error(`未知事实类型: ${fact.type}`);
  }
}

function nextLeaseId(s) {
  s.seq += 1;
  return `L${String(s.seq).padStart(5, '0')}`;
}

function applyTopology(s, f) {
  for (const b of f.boards) {
    s.boards.set(b.id, { id: b.id, name: b.name });
  }
  for (const c of f.circuits) {
    const prev = s.circuits.get(c.id);
    s.circuits.set(c.id, {
      id: c.id,
      boardId: c.boardId,
      name: c.name,
      phase: normPhase(c.phase),
      ratedCurrentA: num(c.ratedCurrentA, '回路额定电流', { min: 0.1 }),
      state: prev?.state === 'TRIPPED' ? 'TRIPPED' : 'NORMAL',
      trippedAt: prev?.trippedAt || null,
    });
  }
  for (const sk of f.sockets) {
    const circuit = s.circuits.get(sk.circuitId);
    if (!circuit) throw new Error(`插座 ${sk.id} 引用了未知回路 ${sk.circuitId}`);
    s.sockets.set(sk.id, {
      id: sk.id,
      circuitId: sk.circuitId,
      name: sk.name,
      phase: sk.phase ? normPhase(sk.phase) : circuit.phase,
      ratedCurrentA: sk.ratedCurrentA != null
        ? num(sk.ratedCurrentA, '插座额定电流', { min: 0.1 })
        : circuit.ratedCurrentA,
      allowedBoxTypes: sk.allowedBoxTypes,
      reachAreas: sk.reachAreas,
    });
  }
  return s;
}

function applyReefers(s, f) {
  for (const r of f.reefers) {
    const phase = normPhase(r.phase);
    s.reefers.set(r.id, {
      id: r.id,
      boxType: r.boxType,
      phase,
      ratedCurrentA: r.ratedCurrentA != null
        ? round2(num(r.ratedCurrentA, '箱额定电流', { min: 0.1 }))
        : nameplateCurrentA(r.powerKw, phase),
      powerKw: r.powerKw != null ? Number(r.powerKw) : null,
      area: r.area,
      tempPriority: num(r.tempPriority, '温控优先级', { min: 1, max: 99, integer: true }),
      insulationHours: num(r.insulationHours, '保温时限(小时)', { min: 0.01 }),
      setpointC: r.setpointC != null ? Number(r.setpointC) : null,
    });
  }
  return s;
}

function applyReserved(s, f) {
  const reefer = s.reefers.get(f.reeferId);
  const socket = s.sockets.get(f.socketId);
  if (!reefer || !socket) throw new Error('预留事实引用了未知箱或插座');
  if (activeLeaseOnSocket(s, f.socketId)) throw new Error('预留事实与既有插座租约冲突');
  if (activeLeaseOfReefer(s, f.reeferId)) throw new Error('预留事实与既有箱租约冲突');
  const leaseId = nextLeaseId(s);
  s.leases.push({ id: leaseId, reeferId: f.reeferId, socketId: f.socketId, state: 'RESERVE' });
  s.reservations.set(f.socketId, { reeferId: f.reeferId, leaseId });
  return s;
}

function applyPowered(s, f) {
  const lease = leaseById(s, f.leaseId);
  if (!lease || lease.state !== 'RESERVE') throw new Error('送电事实要求 RESERVE 租约');
  lease.state = 'POWER';
  s.reservations.delete(lease.socketId);
  return s;
}

function applySample(s, f) {
  s.samples.set(f.key, {
    key: f.key,
    kind: f.kind,
    sensorBootId: f.sensorBootId,
    reeferId: f.reeferId ?? null,
    socketId: f.socketId ?? null,
    circuitId: f.circuitId ?? null,
    measuredAt: f.measuredAt,
    receivedAt: f.receivedAt,
    value: f.value,
    accepted: f.accepted,
    reason: f.reason,
  });
  if (f.accepted && f.kind === 'current' && f.socketId) {
    s.latestCurrent.set(f.socketId, { value: f.value, measuredAt: f.measuredAt });
  }
  if (f.accepted && f.kind === 'powerLoss' && f.reeferId) {
    const prev = s.powerLossAt.get(f.reeferId);
    if (prev == null || f.measuredAt > prev) s.powerLossAt.set(f.reeferId, f.measuredAt);
  }
  return s;
}

function applyTripped(s, f) {
  const c = s.circuits.get(f.circuitId);
  if (!c) throw new Error('跳闸事实引用未知回路');
  c.state = 'TRIPPED';
  c.trippedAt = f.at;
  for (const l of activeLeases(s)) {
    if (l.state !== 'POWER') continue;
    const sk = s.sockets.get(l.socketId);
    if (sk && sk.circuitId === f.circuitId) {
      l.state = 'FROZEN';
      if (s.powerLossAt.get(l.reeferId) == null) s.powerLossAt.set(l.reeferId, f.at);
    }
  }
  return s;
}

function applyReset(s, f) {
  const c = s.circuits.get(f.circuitId);
  if (!c) throw new Error('复位事实引用未知回路');
  c.state = 'NORMAL';
  c.trippedAt = null;
  for (const l of activeLeases(s)) {
    if (l.state !== 'FROZEN') continue;
    const sk = s.sockets.get(l.socketId);
    if (sk && sk.circuitId === f.circuitId) {
      // 该箱若正处于迁移中（目标已 CLAIM），不就地恢复，避免 POWER+CLAIM 双租约
      const inMigration = s.leases.some(
        (x) => x.reeferId === l.reeferId && x.state === 'CLAIM'
      );
      if (inMigration) continue;
      l.state = 'POWER';
      s.powerLossAt.delete(l.reeferId);
    }
  }
  return s;
}

function applyMigrationCreated(s, f) {
  if (s.plans.has(f.planId)) throw new Error(`计划 ${f.planId} 已存在`);
  s.planSeq += 1;
  if (f.planId !== `P${String(s.planSeq).padStart(5, '0')}`) {
    throw new Error(`计划编号不确定: ${f.planId}`);
  }
  const steps = f.steps.map((st, i) => {
    if (st.occupyCommitted) {
      const claimId = nextLeaseId(s);
      s.leases.push({
        id: claimId,
        reeferId: st.reeferId,
        socketId: st.targetSocketId,
        state: 'CLAIM',
      });
      return { ...st, index: i, state: 'PENDING', claimLeaseId: claimId, newLeaseId: null };
    }
    return { ...st, index: i, state: 'PENDING', claimLeaseId: null, newLeaseId: null };
  });
  s.plans.set(f.planId, {
    id: f.planId,
    circuitId: f.circuitId,
    reeferIds: f.reeferIds,
    steps,
    state: 'ACTIVE',
    createdAt: f.at,
    completedAt: null,
    cancelledAt: null,
  });
  return s;
}

function applyStepAdvanced(s, f) {
  const plan = s.plans.get(f.planId);
  if (!plan) throw new Error('步骤事实引用未知计划');
  const st = plan.steps[f.index];
  if (!st) throw new Error('步骤事实索引越界');

  if (f.action === 'unplug') {
    const orig = leaseById(s, f.leaseId);
    if (!orig || orig.state !== 'FROZEN') throw new Error('拔除要求 FROZEN 租约');
    orig.state = 'UNPLUGGED';
    st.state = 'UNPLUGGED';
  } else if (f.action === 'occupy') {
    const claimId = nextLeaseId(s);
    s.leases.push({
      id: claimId,
      reeferId: st.reeferId,
      socketId: st.targetSocketId,
      state: 'CLAIM',
    });
    st.claimLeaseId = claimId;
    st.state = 'OCCUPIED';
  } else if (f.action === 'repower') {
    if (st.claimLeaseId) {
      const claim = leaseById(s, st.claimLeaseId);
      if (!claim || claim.state !== 'CLAIM') throw new Error('送电要求 CLAIM 租约');
      claim.state = 'POWER';
      st.newLeaseId = claim.id;
    } else {
      // 同一插座复位送电（步骤 0 或原=目标）
      const orig = leaseById(s, f.leaseId);
      if (!orig || (orig.state !== 'FROZEN' && orig.state !== 'UNPLUGGED')) {
        throw new Error('送电要求 FROZEN/UNPLUGGED 租约');
      }
      orig.state = 'POWER';
      st.newLeaseId = orig.id;
    }
    const oldLease = leaseById(s, f.oldLeaseId);
    if (oldLease && oldLease.id !== st.newLeaseId) {
      oldLease.state = 'SUPERSEDED';
      s.powerLossAt.delete(st.reeferId);
    }
    if (oldLease && oldLease.id === st.newLeaseId) {
      s.powerLossAt.delete(st.reeferId);
    }
    st.state = 'DONE';
  } else {
    throw new Error(`未知步骤动作: ${f.action}`);
  }

  if (plan.steps.every((x) => x.state === 'DONE')) {
    plan.state = 'COMPLETED';
    plan.completedAt = f.at;
  }
  return s;
}

function applyMigrationCancelled(s, f) {
  const plan = s.plans.get(f.planId);
  if (!plan) throw new Error('取消事实引用未知计划');
  for (const st of plan.steps) {
    if (st.state === 'DONE') continue;
    if (st.claimLeaseId) {
      const claim = leaseById(s, st.claimLeaseId);
      if (claim && claim.state === 'CLAIM') {
        claim.state = 'RELEASED';
        st.claimLeaseId = null;
      }
    }
    // 已拔除但未完成：回到 FROZEN，箱仍处于断电（原插座未释放）
    const orig = s.leases.find(
      (l) => l.reeferId === st.reeferId && l.socketId === st.fromSocketId && (l.state === 'UNPLUGGED' || l.state === 'FROZEN')
    );
    if (orig) {
      orig.state = 'FROZEN';
      st.state = 'CANCELLED';
    } else if (st.state !== 'CANCELLED') {
      st.state = 'CANCELLED';
    }
  }
  plan.state = 'CANCELLED';
  plan.cancelledAt = f.at;
  return s;
}

export { bad, DomainError };
