"""不可变事件流水 + 快照存储（纯标准库）。

持久化模型
----------
``events.log``    每行一条 JSON 事件，只追加（append-only）。一次命令产生的
                  效果事件在**同一批写入**中一次性 fsync：要么整批可见，要么
                  整批不存在，崩溃后不可能出现"半截命令"。
``snapshot.json`` 状态归并结果（原子 rename 替换）。重启时先装快照再重放其后
                  的事件；即使快照丢失，全量重放流水仍得到同一结果。

崩溃边界
--------
任何持久化边界（预留 / 送电 / 跳闸登记 / 换插座）上的强制终止都安全：
  * 写前崩溃 -> 事件不存在 -> 重放命令或由终端凭命令标识重发即可；
  * 写后崩溃 -> 事件已在流水 -> 归并出唯一状态；
  * 写一半（撕裂尾行）-> 启动时按 JSONL 完整性截断，命令整体回滚。

故障注入（仅当环境变量 ``REEFER_ALLOW_CRASH=1`` 时生效，供验收脚本使用）：
  REEFER_CRASH=after_fsync    批次 fsync 之后、返回调用方之前退出
  REEFER_CRASH=before_append  打开流水之前退出
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile


class Store:
    def __init__(self, data_dir: str):
        self.data_dir = os.path.abspath(data_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        self.log_path = os.path.join(self.data_dir, "events.log")
        self.snap_path = os.path.join(self.data_dir, "snapshot.json")
        self._lock_path = os.path.join(self.data_dir, ".lock")
        # 服务内串行点：所有命令处理在同一把进程锁内完成（见 service.Service）。
        import threading
        self.append_lock = threading.Lock()
        self.repair_torn_tail()

    # ------------------------------------------------------------------ 恢复
    def repair_torn_tail(self) -> int:
        """截断流水末尾任何不完整/不可解析的行，返回保留的字节数。

        每行都是独立 JSON；写入按批次追加且整行 flush，因此只有最后一行可能
        撕裂（崩溃于单次 write 系统调用之间）。逐行校验，遇到坏行即截断，
        坏行之后也不可能存在好行。
        """
        if not os.path.exists(self.log_path):
            return 0
        good_bytes = 0
        with open(self.log_path, "rb") as f:
            while True:
                line = f.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    break  # 末行未写完
                try:
                    json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    break
                good_bytes = f.tell()
        size = os.path.getsize(self.log_path)
        if good_bytes < size:
            with open(self.log_path, "r+b") as f:
                f.truncate(good_bytes)
                f.flush()
                os.fsync(f.fileno())
        return good_bytes

    def read_events(self):
        if not os.path.exists(self.log_path):
            return []
        events = []
        with open(self.log_path, "rb") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                events.append(json.loads(line.decode("utf-8")))
        return events

    # ------------------------------------------------------------------ 写入
    def _crash_armed(self, when, events):
        """一次性崩溃注入（仅验收脚本使用，需 REEFER_ALLOW_CRASH=1）。

        REEFER_CRASH=before_append|after_fsync 选择边界；
        REEFER_CRASH_KIND 可选，限定只在批次含指定事件种类时触发，从而能
        越过种子阶段、精确命中"跳闸/换插座"等目标批次。触发后立即清除，
        保证只终止一次。
        """
        if os.environ.get("REEFER_ALLOW_CRASH") != "1":
            return False
        if os.environ.get("REEFER_CRASH") != when:
            return False
        want_kind = os.environ.get("REEFER_CRASH_KIND")
        if want_kind and not any(e["kind"] == want_kind for e in events):
            return False
        os.environ["REEFER_CRASH"] = ""  # 一次性
        return True

    def append_batch(self, events) -> int:
        """原子追加一批事件；返回这批最后一条的流水序号(seq，从 1 起)。

        全部事件先序列化成一段文本再一次 write + fsync。命令的全部效果共享
        同一个 ``batch_id`` 与 ``command_id``，恢复时以此判断命令整体性。
        """
        if self._crash_armed("before_append", events):
            os._exit(77)
        if not events:
            raise ValueError("empty batch")
        payload = "".join(
            json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n"
            for e in events).encode("utf-8")
        with self.append_lock:
            with open(self.log_path, "ab") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            if self._crash_armed("after_fsync", events):
                os._exit(77)
            return events[-1]["seq"]

    # ------------------------------------------------------------------ 快照
    def save_snapshot(self, state) -> None:
        """原子写快照：同目录临时文件 + fsync + rename。"""
        fd, tmp = tempfile.mkstemp(prefix="snap-", suffix=".tmp",
                                   dir=self.data_dir)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(json.dumps(state, ensure_ascii=False,
                                   separators=(",", ":")).encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.snap_path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def load_snapshot(self):
        if not os.path.exists(self.snap_path):
            return None
        try:
            with open(self.snap_path, "rb") as f:
                snap = json.loads(f.read().decode("utf-8"))
            # 快照对应的事件序号必须 <= 流水当前长度，否则快照来自"未来"，
            # 丢弃后全量重放（唯一结果不会变）。
            if snap.get("seq", 0) > len(self.read_events()):
                return None
            return snap
        except (ValueError, OSError):
            return None


def digest(value) -> str:
    """命令载荷指纹：相同命令标识必须携带相同载荷，否则拒绝重放。"""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
