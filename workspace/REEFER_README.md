# 冷藏箱岸电接入与断电迁移模块

纯 Python 标准库实现（无第三方依赖）。提供浏览器操作台与 HTTP 接口，覆盖：
配电拓扑登记 → 冷藏箱需求导入 → 插座预留 → 终端送电 → 电流/温度/掉电采样
→ 回路跳闸冻结 → 受影响箱保温时限 → 有序迁移（拔除/占用/送电逐步确认）
→ 方案争用与取消 → 崩溃边界恢复。

## 快速开始

```bash
python3 run_reefer.py 8095            # 启动；浏览器打开 http://localhost:8095/reefer
python3 -m unittest reefer.tests.test_acceptance_reserve \
                    reefer.tests.test_acceptance_flow \
                    reefer.tests.test_recovery \
                    reefer.tests.test_http   # 29 个验收用例
```

数据默认落在 `./reefer_data/`，可用第二个参数或环境变量 `REEFER_DATA` 指定。

## 持久化：不可变流水 + 快照（事件溯源）

| 文件 | 说明 |
|---|---|
| `events.log` | 只追加 JSONL；一条命令的全部效果 + 命令结果在**同一批**单次 `write + fsync` |
| `snapshot.json` | 归并结果，临时文件 fsync 后原子 rename；启动时装快照再重放其后事件 |

一条命令要么整批可见、要么整批不存在。启动时自动截断末尾撕裂行；
即使快照丢失，全量重放仍得到**唯一结果**。

### 命令标识（幂等）

写接口可在请求体带 `command_id`：
* 相同标识 + 相同载荷 → 返回**首次答复**（成功或失败都冻结），状态不变；
* 相同标识 + 不同载荷 → `409 HASH_CONFLICT`，**不能改写当前接电状态**。

## 关键不变量

* 任一时刻一个插座至多一条未释放租约、一个箱至多占用一个插座；
* 回路承诺负载 = 供电 + 已预留 + 迁移保留，不得越过额定；
* 跳闸回路立即冻结新接电/送电；
* 迁移步骤未完成前：原插座不释放（拔除中仍绑定）、目标插座被保留不可抢；
* 换插座是**单条事件原子提交**（释放原插座 + 占用目标 + 送电 + 解保留）；
* 迁移已完成的箱必须在目标插座 `powered`，不得仍标记断电。

`GET /reefer/verify` 对以上全部不变量做自检。

## HTTP 接口

写：`topology`、`fleet`、`reservations`、`reservations/auto`、
`reservations/release`、`energize`、`deenergize`、`samples`、`samples/review`、
`trips`、`reset`、`migrations`、`migrations/confirm`、`migrations/cancel`。

读：`overview`、`topology`、`loads`、`unpowered`、`samples`、
`quarantine`、`trips`、`migrations`、`events`、`verify`。

所有写接口接受可选 `"now"`（ISO8601）便于确定性回放/验收。

## 采样与时钟偏差

样本带 `sensor`（启动代号）与 `measured_at`（测量时刻）。倒序且晚于
航次允许时钟偏差（`clock_skew_sec`，默认 30s）的样本进入**待审**，不参与
负载与保温时限计算；完全重复样本（箱+类型+传感器+时刻）被识别为重复。
人工 `samples/review` 采纳后才进入测量状态。

## 迁移方案

`POST /reefer/migrations {"circuit_id": ...}` 仅对跳闸回路生成方案：
按「温控优先级高 → 剩余保温时限短 → 箱号」排序，依据相制、功率余量、
电缆可达性贪心选目标并**立即保留**。无目标的箱进入 `conflicts`（逐插座
给出 OCCUPIED / HELD / CABLE_UNREACHABLE / CIRCUIT_OVERLOADED 等最新原因，
可据此重新计算）。

电气员对每个步骤依次确认 `unplug → occupy → energize`；
`migrations/cancel` 只释放尚未执行（pending）步骤的目标保留，已拔除的箱
仍绑定原插座并断电，须继续完成或复位后重插。

## 崩溃恢复验收

`reefer/tests/crash_worker.py` 用 `os._exit(77)` 在各持久化边界强制终止：
预留前/后、送电后、跳闸后、换插座占用后、换插座提交后。
`test_recovery.py` 验证恢复后无双占用、负载不失真、无"已完成但仍断电"，
并覆盖撕裂尾行截断与跨快照采样存活。
