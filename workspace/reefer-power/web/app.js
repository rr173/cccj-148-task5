'use strict';

let state = null;

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (x) => String(x ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmtTime = (t) => (t == null ? '—' : new Date(t).toLocaleTimeString('zh-CN', { hour12: false }));
const fmtDateTime = (t) => (t == null ? '—' : new Date(t).toLocaleString('zh-CN', { hour12: false }));

async function api(path, body, opts = {}) {
  const headers = { 'content-type': 'application/json' };
  if (opts.commandId) headers['x-command-id'] = opts.commandId;
  const res = await fetch(path, {
    method: 'POST',
    headers,
    body: JSON.stringify(body ?? {}),
  });
  const json = await res.json();
  if (!res.ok || json.ok === false) {
    const e = new Error(json.error?.message || `HTTP ${res.status}`);
    e.payload = json;
    e.status = res.status;
    throw e;
  }
  return json;
}

function toast(msg, kind = 'ok') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $('#toast').appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

// ---------------- 渲染 ----------------
function render() {
  if (!state) return;
  $('#clock').textContent = new Date(state.at).toLocaleString('zh-CN', { hour12: false });
  renderSummary();
  renderTopology();
  renderReefers();
  renderTrips();
  renderSamples();
}

function renderSummary() {
  const reefers = state.reefers;
  const powered = reefers.filter((r) => r.status === 'POWERED').length;
  const unpowered = reefers.filter((r) => r.status === 'UNPOWERED').length;
  const reserved = reefers.filter((r) => r.status === 'RESERVED').length;
  const loss = reefers.filter((r) => r.status === 'POWER_LOSS').length;
  const tripped = state.circuits.filter((c) => c.state === 'TRIPPED').length;
  const q = state.samples.quarantined.length;
  const activePlans = state.plans.filter((p) => p.state === 'ACTIVE').length;
  const overloaded = state.circuits.filter((c) => c.overloaded).length;

  $('#summary').innerHTML = [
    ['总箱数', reefers.length, ''],
    ['已供电', powered, 'ok'],
    ['预留中', reserved, 'warn'],
    ['未接电', unpowered, 'warn'],
    ['掉电', loss, loss ? 'alert' : ''],
    ['跳闸回路', tripped, tripped ? 'alert' : ''],
    ['过载回路', overloaded, overloaded ? 'alert' : ''],
    ['迁移中方案', activePlans, activePlans ? 'warn' : ''],
    ['待审样本', q, q ? 'warn' : ''],
  ].map(([l, n, cls]) => `<div class="card ${cls}"><div class="n">${n}</div><div class="l">${l}</div></div>`).join('');
}

function meter(c) {
  const pct = Math.min(100, Math.round((c.loadA / c.ratedCurrentA) * 100));
  return `<div class="meter">
    <div class="track"><div class="fill ${c.overloaded ? 'over' : ''}" style="width:${pct}%"></div></div>
    <div class="nums">${c.loadA} / ${c.ratedCurrentA} A · 余量 ${c.headroomA} A${c.overloaded ? ' · <span class="danger">过载</span>' : ''}</div>
  </div>`;
}

function socketHtml(s) {
  const who = s.reeferId
    ? `<div class="who">${esc(s.reeferId)}${s.measuredCurrentA != null ? ` · ${s.measuredCurrentA}A` : ''}</div>`
    : '<div class="who muted">空闲</div>';
  return `<div class="socket">
    <div class="sid"><span>${esc(s.id)}</span><span class="tag ${s.status}">${s.status}</span></div>
    <div class="meta">${esc(s.phase)} · ${s.ratedCurrentA}A · ${esc(s.allowedBoxTypes.join('/'))} · ${esc(s.reachAreas.join('/'))}</div>
    ${who}
  </div>`;
}

function renderTopology() {
  const boards = state.boards.map((b) => {
    const circuits = state.circuits.filter((c) => c.boardId === b.id);
    return `<div class="board">
      <h3>配电板 ${esc(b.id)} · ${esc(b.name)}</h3>
      ${circuits.map((c) => `<div class="circuit ${c.state === 'TRIPPED' ? 'tripped' : ''}">
        <div class="circuit-head">
          <span class="name">${esc(c.id)} ${esc(c.name)}</span>
          <span class="tag p${c.phase === '3P' ? 3 : 1}">${c.phase}</span>
          <span class="tag ${c.state}">${c.state === 'TRIPPED' ? `跳闸 ${fmtTime(c.trippedAt)}` : '正常'}</span>
          ${meter(c)}
          ${c.state === 'TRIPPED'
            ? `<button class="warn" onclick="planMigration('${c.id}')">迁移方案</button> <button class="ghost" onclick="resetCircuit('${c.id}')">复位</button>`
            : ''}
        </div>
        <div class="sockets">${c.sockets.map(socketHtml).join('')}</div>
      </div>`).join('')}
    </div>`;
  }).join('');
  $('#tab-topology').innerHTML = boards || '<p class="muted">尚未录入配电拓扑。请到「录入/导入」页提交。</p>';
}

function renderReefers() {
  const rows = state.reefers.map((r) => {
    const ins = state.insulation[r.id];
    const statusTxt = {
      POWERED: '<span class="good">供电</span>',
      RESERVED: '<span class="warn">已预留</span>',
      UNPOWERED: '<span class="muted">未接电</span>',
      POWER_LOSS: '<span class="danger">掉电</span>',
    }[r.status];
    return `<tr>
      <td><b>${esc(r.id)}</b></td>
      <td>${esc(r.boxType)}</td>
      <td>${r.phase}</td>
      <td>${r.ratedCurrentA}A${r.powerKw != null ? `<div class="muted">${r.powerKw}kW</div>` : ''}</td>
      <td>${esc(r.area)}</td>
      <td>${r.tempPriority}</td>
      <td>${r.insulationHours}h${ins ? `<div class="muted ${ins.expired ? 'danger' : ''}">剩余 ${ins.remainingMinutes} 分钟</div>` : ''}</td>
      <td>${esc(r.socketId) ?? '—'}</td>
      <td>${statusTxt}</td>
    </tr>`;
  }).join('');
  $('#tab-reefers').innerHTML = `<div class="panel">
    <h3>冷藏箱（${state.reefers.length}）</h3>
    <table><thead><tr><th>箱号</th><th>箱型</th><th>相制</th><th>额定</th><th>区域</th><th>温控优先级</th><th>保温时限</th><th>插座</th><th>状态</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

function renderTrips() {
  const trippedCircuits = state.circuits.filter((c) => c.state === 'TRIPPED');
  let html = `<div class="panel"><h3>跳闸影响</h3>`;
  if (!trippedCircuits.length) {
    html += '<p class="muted">当前没有跳闸回路。</p>';
  } else {
    for (const c of trippedCircuits) {
      const affected = [];
      for (const sock of c.sockets) {
        if (sock.status === 'FROZEN' && sock.reeferId) affected.push(sock.reeferId);
      }
      const insRows = affected.map((rid) => {
        const r = state.reefers.find((x) => x.id === rid);
        const i = state.insulation[rid];
        return `<tr><td><b>${esc(rid)}</b></td><td>${r?.tempPriority}</td>
          <td>${i ? `${i.remainingMinutes} 分钟` : '—'}</td>
          <td>${i ? fmtDateTime(i.powerLossAt) : '—'}</td></tr>`;
      }).join('');
      html += `<p>回路 <b>${esc(c.id)}</b> 于 ${fmtDateTime(c.trippedAt)} 跳闸，新接电已冻结。</p>
        <table><thead><tr><th>箱号</th><th>温控优先级</th><th>剩余保温</th><th>掉电时刻</th></tr></thead>
        <tbody>${insRows}</tbody></table>
        <p style="margin-top:10px">
          <button onclick="planMigration('${c.id}')">生成迁移方案</button>
          <button class="ghost" onclick="resetCircuit('${c.id}')">回路复位</button>
        </p>`;
    }
  }
  html += '</div>';

  html += `<div class="panel"><h3>迁移执行状态</h3>`;
  if (!state.plans.length) {
    html += '<p class="muted">尚无迁移方案。</p>';
  } else {
    for (const p of [...state.plans].reverse()) {
      const stateTag = { ACTIVE: '<span class="warn">进行中</span>', COMPLETED: '<span class="good">已完成</span>', CANCELLED: '<span class="muted">已取消</span>' }[p.state];
      html += `<div class="circuit"><div class="circuit-head">
        <span class="name">方案 ${esc(p.id)} · 回路 ${esc(p.circuitId)}</span>${stateTag}
        <span class="muted">创建 ${fmtTime(p.createdAt)}${p.completedAt ? ` · 完成 ${fmtTime(p.completedAt)}` : ''}${p.cancelledAt ? ` · 取消 ${fmtTime(p.cancelledAt)}` : ''}</span>
        ${p.state === 'ACTIVE' ? `<button class="bad" onclick="cancelPlan('${p.id}')">取消方案</button>` : ''}
      </div><div class="steps">`;
      for (const st of p.steps) {
        const stTag = { PENDING: '待执行', UNPLUGGED: '已拔除', OCCUPIED: '已占用', DONE: '已送电', CANCELLED: '已取消' }[st.state];
        const arrow = st.sameSite
          ? `${esc(st.fromSocketId)} 原位复位`
          : `${esc(st.fromSocketId)} → ${esc(st.targetSocketId)}`;
        html += `<div class="step ${st.state.toLowerCase()}">
          <span>#${st.index}</span><b>${esc(st.reeferId)}</b><span>${arrow}</span>
          <span class="muted">${stTag}</span>
          <span style="flex:1"></span>${stepButtons(p, st)}
        </div>`;
      }
      html += '</div></div>';
    }
  }
  html += '</div>';
  $('#tab-trips').innerHTML = html;
}

function stepButtons(p, st) {
  if (p.state !== 'ACTIVE') return '';
  const btns = [];
  if (st.sameSocket) {
    if (st.state !== 'DONE') btns.push(`<button onclick="step('${p.id}',${st.index},'repower')">重新送电</button>`);
  } else {
    if (st.state === 'PENDING') {
      btns.push(`<button class="warn" onclick="step('${p.id}',${st.index},'unplug')">确认拔除</button>`);
      if (!st.occupyCommitted) btns.push(`<button disabled>占用目标</button>`);
    }
    if (st.state === 'UNPLUGGED') {
      btns.push(`<button class="ghost" onclick="step('${p.id}',${st.index},'occupy')">确认占用目标</button>`);
      btns.push(`<button disabled>送电</button>`);
    }
    if (st.state === 'OCCUPIED') btns.push(`<button onclick="step('${p.id}',${st.index},'repower')">重新送电</button>`);
  }
  return btns.join(' ');
}

function renderSamples() {
  const q = state.samples.quarantined;
  let html = `<div class="panel"><h3>待审（倒序/超时钟偏差）样本 ${q.length}</h3>`;
  html += q.length
    ? `<table><thead><tr><th>种类</th><th>传感器启动代号</th><th>对象</th><th>测量时刻</th><th>接收时刻</th><th>数值</th><th>原因</th></tr></thead><tbody>
      ${q.map((s) => `<tr><td>${s.kind}</td><td>${esc(s.sensorBootId)}</td>
        <td>${esc(s.reeferId || s.socketId || s.circuitId)}</td>
        <td>${fmtDateTime(s.measuredAt)}</td><td>${fmtDateTime(s.receivedAt)}</td>
        <td>${s.value}</td><td class="danger">${esc(s.reason)}</td></tr>`).join('')}
      </tbody></table>`
    : '<p class="muted">没有隔离样本。</p>';
  html += '</div>';

  const a = state.samples.accepted;
  html += `<div class="panel"><h3>最新接受样本（${a.length}，最多 200 条）</h3>
    <table><thead><tr><th>种类</th><th>传感器</th><th>对象</th><th>测量时刻</th><th>数值</th></tr></thead><tbody>
    ${a.slice(0, 50).map((s) => `<tr><td>${s.kind}</td><td>${esc(s.sensorBootId)}</td>
      <td>${esc(s.reeferId || s.socketId || s.circuitId)}</td>
      <td>${fmtDateTime(s.measuredAt)}</td><td>${s.value}</td></tr>`).join('')}
    </tbody></table></div>`;
  $('#tab-samples').innerHTML = html;
}

// ---------------- 操作 ----------------
window.planMigration = async (circuitId) => {
  const recompute = false;
  try {
    const dry = await api('/api/migrations', { circuitId, commit: false });
    const msg = dry.steps.map((s) =>
      `#${s.index} ${s.reeferId}: ${s.fromSocketId} → ${s.sameSocket ? '原位复位' : s.targetSocketId}`
    ).join('\n');
    if (!confirm(`迁移步骤（按温控优先级排序）：\n\n${msg || '（无）'}\n\n确认提交并锁定首个目标？`)) return;
    const created = await api('/api/migrations', { circuitId, commit: true });
    toast(`方案 ${created.planId} 已创建`);
    await refresh();
  } catch (e) {
    if (e.payload?.error?.code === 'TARGET_CONTENDED') {
      const c = e.payload.error;
      const alt = c.alternative.steps.map((s) =>
        `#${s.index} ${s.reeferId}: ${s.fromSocketId} → ${s.sameSocket ? '原位复位' : s.targetSocketId}`
      ).join('\n');
      if (confirm(`目标被争用：\n${c.conflicts.map((x) => `${x.targetSocketId} 已被 ${x.heldByReeferId} (${x.leaseState}) 锁定`).join('\n')}\n\n按最新占用重算的备选：\n${alt || '（无可行目标）'}\n\n是否采用备选提交？`)) {
        try {
          const created = await api('/api/migrations', { circuitId, recompute: true });
          toast(`备选方案 ${created.planId} 已创建`);
          await refresh();
        } catch (e2) { toast(e2.message, 'err'); }
      }
    } else {
      toast(e.message, 'err');
    }
  }
};

window.resetCircuit = async (circuitId) => {
  if (!confirm(`确认复位回路 ${circuitId}？该回路 FROZEN 箱将就地恢复供电。`)) return;
  try {
    await api('/api/circuits/reset', { circuitId });
    toast('回路已复位');
    await refresh();
  } catch (e) { toast(e.message, 'err'); }
};

window.cancelPlan = async (planId) => {
  if (!confirm(`确认取消方案 ${planId}？未执行步骤的目标插座将被释放，已完成步骤保留。`)) return;
  try {
    await api('/api/migrations/cancel', { planId });
    toast('方案已取消');
    await refresh();
  } catch (e) { toast(e.message, 'err'); }
};

window.step = async (planId, index, action) => {
  const labels = { unplug: '拔除', occupy: '占用目标', repower: '送电' };
  if (!confirm(`确认步骤 #${index} 执行「${labels[action]}」？`)) return;
  try {
    await api('/api/migrations/steps', { planId, index, action });
    toast(`步骤 #${index} ${labels[action]}完成`);
    await refresh();
  } catch (e) { toast(e.message, 'err'); }
};

// ---------------- 录入/导入页 ----------------
const DEFAULT_TOPO = {
  boards: [{ id: 'B1', name: '主配电板' }],
  circuits: [
    { id: 'C1', boardId: 'B1', name: '回路1', phase: '3P', ratedCurrentA: 63 },
    { id: 'C2', boardId: 'B1', name: '回路2', phase: '3P', ratedCurrentA: 63 },
  ],
  sockets: [
    { id: 'S1', circuitId: 'C1', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
    { id: 'S2', circuitId: 'C1', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
    { id: 'S3', circuitId: 'C2', allowedBoxTypes: ['A1'], reachAreas: ['BAY-A'] },
  ],
};
const DEFAULT_REEFERS = {
  reefers: [
    { id: 'R1', boxType: 'A1', phase: '3P', powerKw: 5, area: 'BAY-A', tempPriority: 5, insulationHours: 4 },
    { id: 'R2', boxType: 'A1', phase: '3P', powerKw: 5, area: 'BAY-A', tempPriority: 2, insulationHours: 4 },
  ],
};

function adminHtml() {
  return `
  <div class="grid2">
    <div class="panel">
      <h3>1. 配电拓扑（配电板/回路/插座/相制/额定/箱型/可达区域）</h3>
      <textarea id="topoJson">${esc(JSON.stringify(DEFAULT_TOPO, null, 2))}</textarea>
      <p><button onclick="submitTopo()">录入拓扑</button></p>
    </div>
    <div class="panel">
      <h3>2. 本航次冷藏箱功率需求与温控优先级</h3>
      <textarea id="reeferJson">${esc(JSON.stringify(DEFAULT_REEFERS, null, 2))}</textarea>
      <p><button onclick="submitReefers()">导入冷藏箱</button></p>
    </div>
  </div>
  <div class="panel">
    <h3>3. 自动生成插座预留 & 接电终端</h3>
    <p><button onclick="autoReserve()">为所有未接电箱生成预留</button></p>
    <div class="grid2">
      <label class="fld">箱号<input id="connReefer" placeholder="R1" /></label>
      <label class="fld">插座号<input id="connSocket" placeholder="S1" /></label>
    </div>
    <p><button onclick="connectOne()">预留转供电（提交命令标识由服务端幂等处理）</button></p>
  </div>
  <div class="grid2">
    <div class="panel">
      <h3>4. 传感器采样</h3>
      <div class="grid2">
        <label class="fld">种类
          <select id="sKind"><option value="current">current 电流</option><option value="temperature">temperature 温度</option><option value="powerLoss">powerLoss 掉电</option></select>
        </label>
        <label class="fld">传感器启动代号<input id="sBoot" value="BOOT-1" /></label>
        <label class="fld">对象（电流填插座 / 其余填箱号）<input id="sTarget" placeholder="S1" /></label>
        <label class="fld">数值<input id="sValue" placeholder="17.2" /></label>
      </div>
      <p><button onclick="submitSample()">提交样本</button></p>
    </div>
    <div class="panel">
      <h3>5. 回路操作</h3>
      <label class="fld">回路号<input id="tripCircuit" placeholder="C1" /></label>
      <p>
        <button class="bad" onclick="tripCircuit()">登记跳闸</button>
        <button class="ghost" onclick="resetCircuit2()">复位回路</button>
      </p>
    </div>
  </div>`;
}

function readJson(id) {
  try { return JSON.parse($('#' + id).value); }
  catch (e) { toast('JSON 解析失败: ' + e.message, 'err'); return null; }
}
window.submitTopo = async () => {
  const body = readJson('topoJson'); if (!body) return;
  try { await api('/api/topology', body); toast('拓扑已录入'); await refresh(); } catch (e) { toast(e.message, 'err'); }
};
window.submitReefers = async () => {
  const body = readJson('reeferJson'); if (!body) return;
  try { await api('/api/reefers', body); toast('冷藏箱已导入'); await refresh(); } catch (e) { toast(e.message, 'err'); }
};
window.autoReserve = async () => {
  try { const r = await api('/api/reservations', {});
    toast(`预留 ${r.reserved.length} 个，拒绝 ${r.rejected.length} 个`); await refresh();
  } catch (e) { toast(e.message, 'err'); }
};
window.connectOne = async () => {
  const reeferId = $('#connReefer').value.trim();
  const socketId = $('#connSocket').value.trim();
  try { const r = await api('/api/connect', { reeferId, socketId }); toast(`${r.reeferId} 已在 ${r.socketId} 送电`); await refresh(); }
  catch (e) { toast(e.message, 'err'); }
};
window.submitSample = async () => {
  const kind = $('#sKind').value;
  const boot = $('#sBoot').value.trim();
  const target = $('#sTarget').value.trim();
  const value = Number($('#sValue').value);
  const body = { kind, sensorBootId: boot, measuredAt: Date.now(), value };
  if (kind === 'current') body.socketId = target; else body.reeferId = target;
  try { const r = await api('/api/samples', body);
    toast(r.accepted ? '样本已接受' : `样本隔离待审: ${r.reason}`, r.accepted ? 'ok' : 'err');
    await refresh();
  } catch (e) { toast(e.message, 'err'); }
};
window.tripCircuit = async () => {
  const circuitId = $('#tripCircuit').value.trim();
  try { const r = await api('/api/circuits/trip', { circuitId });
    if (r.alreadyTripped) toast('回路此前已跳闸');
    else toast(`已跳闸，受影响 ${r.impact.affected.length} 箱`, 'err');
    switchTab('trips'); await refresh();
  } catch (e) { toast(e.message, 'err'); }
};
window.resetCircuit2 = async () => {
  const circuitId = $('#tripCircuit').value.trim();
  await window.resetCircuit(circuitId);
};

// ---------------- tabs / refresh ----------------
function switchTab(name) {
  $$('.tabs button').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  $$('.tab').forEach((t) => t.classList.toggle('active', t.id === `tab-${name}`));
}
$$('.tabs button').forEach((b) => b.addEventListener('click', () => switchTab(b.dataset.tab)));

async function refresh() {
  const res = await fetch('/api/state');
  state = await res.json();
  render();
}
$('#refresh').addEventListener('click', refresh);
setInterval(() => { if ($('#auto').checked) refresh(); }, 2000);

$('#tab-admin').innerHTML = adminHtml();
refresh().catch((e) => toast(e.message, 'err'));
