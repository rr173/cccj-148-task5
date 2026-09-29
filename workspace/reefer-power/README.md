# 冷藏箱岸电接入与断电迁移模块

零依赖纯 Node.js（>=20）实现。提供浏览器操作台与 HTTP 接口，管理冷藏箱岸电插座预留、接电、
传感器采样（电流/温度/掉电）、回路跳闸冻结、按温控优先级的有序迁移与争用裁决；
全部供电事实、插座租约、迁移动作以**不可变流水 + 快照**持久化，任何持久化边界崩溃后重启都归并唯一结果。

## 启动

```bash
node src/server.js                 # 默认 http://localhost:8080
PORT=9000 node src/server.js       # 自定义端口
REEFER_DATA_DIR=/data node src/server.js
node src/server.js --test-faults   # 启用崩溃注入接口（仅测试环境）
```

浏览器打开根路径即为操作台：配电拓扑/实时负载、未接电箱、异常采样、跳闸影响、迁移执行状态。

播种演示数据（先启动服务）：

```bash
node scripts/seed-demo.mjs
```

## 验收测试

```bash
npm test          # node --test test/
```

覆盖需求全部七个验收场景：
1. 正常预留并接电（一箱/一座位唯一、送电容量复核）
2. 容量 / 相制 / 箱型 / 可达性 / 无预留的拒绝
3. 采样重复回放、同键异值与命令标识复用冲突拒绝、超时钟偏差倒序样本隔离
4. 跳闸冻结新接电、剩余保温时限、按温控优先级的有序迁移与逐步确认
5. 两名电气员真实并行争抢同一备用插座（先提交者成功，后者收到冲突+可重算备选）
6. 迁移中途取消：释放未执行步骤目标、保留已完成步骤、可重新规划
7. 送电 / 迁移创建 / 换插座 repower 三个持久化边界 `process.exit` 崩溃后的恢复

## HTTP 接口

所有写接口为 `POST` + JSON；建议带请求头 `X-Command-Id`（或 body.commandId）。

| 接口 | 说明 |
|---|---|
| `POST /api/topology` | 录入配电板、回路（相制/额定电流）、插座（额定/允许箱型/电缆可达区域） |
| `POST /api/reefers` | 导入本航次冷藏箱：箱型、相制、powerKw 或 ratedCurrentA、区域、tempPriority、insulationHours |
| `POST /api/reservations` | 为所有未接电箱自动生成插座预留（温控优先级降序，同回路装箱保留备用回路） |
| `POST /api/connect` | 接电终端：`{reeferId, socketId}` + 命令标识，预留转供电；送电前容量/跳闸复核 |
| `POST /api/samples` | `{kind: current/temperature/powerLoss, sensorBootId, socketId|reeferId, measuredAt, value}` |
| `POST /api/circuits/trip` | `{circuitId}` 跳闸登记：冻结该回路新接电，返回受影响箱与剩余保温分钟 |
| `POST /api/circuits/reset` | 回路复位，FROZEN 箱就地恢复供电 |
| `POST /api/migrations` | 生成/提交迁移方案；`{commit:false}` 只出草案；`{recompute:true}` 按最新占用重算 |
| `POST /api/migrations/steps` | 逐步确认：`{planId, index, action: unplug|occupy|repower}`（必须按序号执行） |
| `POST /api/migrations/cancel` | 取消进行中方案：释放未执行步骤的目标租约，已完成步骤保留 |
| `GET /api/state` | 操作台读模型：拓扑、实时负载、箱、保温、计划、样本 |

### 幂等语义

* 相同 `X-Command-Id` + 相同载荷摘要：返回首次答复（含首次失败，`replayed:true`），不产生任何副作用。
* 相同 `X-Command-Id` + **不同**载荷摘要：`409 IDEMPOTENCY_CONFLICT`，不改写当前接电状态。
* 样本接口按内容去重（同传感器启动代号/种类/对象/测量时刻）：完全重复回放首次答复；
  同键不同数值返回 `409 SAMPLE_CONFLICT`。

### 错误码

`BAD_REQUEST`（400）、`NOT_FOUND`（404）、`CONFLICT`（409），业务码在 `error.code`：
`NO_RESERVATION / SOCKET_MISMATCH / SOCKET_OCCUPIED / PHASE_MISMATCH / SOCKET_RATING_EXCEEDED /
BOX_TYPE_NOT_ALLOWED / AREA_UNREACHABLE / CIRCUIT_CAPACITY / CIRCUIT_TRIPPED /
TARGET_CONTENDED / TARGET_NOT_CLAIMED / STEP_ORDER / PLAN_CANCELLED /
IDEMPOTENCY_CONFLICT / SAMPLE_CONFLICT / OUT_OF_ORDER_CLOCK_SKEW` 等。

## 核心规则

* **预留**：按温控优先级（数字大优先，再按剩余保温时间）排序，逐箱贪心；候选必须相制、
  额定电流、允许箱型、电缆可达区域全部匹配，且接入后回路承诺负载不越限。
  同回路装箱策略保留完全空闲的备用回路；并列按插座编号确定。
* **接电**：只有 RESERVE 租约可转 POWER；任一时刻一个插座最多一个活动租约、一个箱最多占用一个插座
  （迁移中间态允许 FROZEN/UNPLUGGED + CLAIM 成对存在）。跳闸回路拒绝新接电。
* **采样**：同一 `sensorBootId` 游标内测量时刻倒退超过 `REEFER_MAX_CLOCK_SKEW_MS`（默认 5000ms）
  的样本进入待审隔离区，不更新实时电流游标、不参与负载与保温时限计算。实时负载优先取
  5 分钟新鲜窗口内的电流采样，否则回退箱铭牌电流（`P/(√3·400·0.85)` 三相 / `P/(230·0.85)` 单相）。
* **跳闸**：该回路所有 POWER 租约立即 FROZEN，记录掉电时刻；保温剩余 = 保温时限 − (now − 掉电时刻)。
* **迁移**：按温控优先级、剩余保温时间生成有序步骤：
  `拔除(unplug) → 占用目标(occupy) → 送电(repower)`。方案创建即锁定首个目标（CLAIM），
  步骤必须按序号执行；未 repower 前原插座不释放、目标插座他人不可抢占。
  两人争用同一目标时，提交瞬间由服务端串行裁决：先提交者成功；后者 409 返回
  `conflicts`（当前占用人）+ `alternative`（计入所有 CLAIM 后重新计算的备选），用 `recompute:true` 提交。
* **取消**：未完成步骤的 CLAIM 释放为 AVAILABLE；已拔除的箱回到 FROZEN（仍占原插座、仍计掉电），可重新规划。

## 持久化与崩溃恢复

```
data/events.log      不可变流水，每行一个命令批次（JSON）：{seq,commandId,hash,facts[],reply,status}
data/snapshot.json   周期快照（Map/Set 结构化），tmp+rename 原子替换
```

* 批次先 `write+fsync` 整批落盘，再内存归并；不存在“半条命令”。末行撕裂在重启时截断丢弃。
* 故障注入（`--test-faults`）在 fsync 之后、归并之前 `exit(17)`，重启后从快照重放流水归并，
  并由 `assertInvariants` 校验：无双重占用、PENDING 步骤无漂移租约、COMPLETED 计划的箱必须 POWERED。
* 快照中的首次答复随批次持久化，崩溃后用同一命令标识重试仍得到首次答复。
