"""HandoverManager — 機体間 (machine-to-machine) 資源 Handover サブプロトコル (Ch06 §6.4)。

`CAPCoordination.tla` の Handover サブプロトコルを **実行時に駆動**する。typed Handover primitive
(P4 = HandoverReceiverOwnership) は TLA+ で検証済みだが、従来ランタイムでは積込スロットの引継ぎを
「保持者が release → 次の機体が reserve」(= naive release-then-reacquire) で実現しており、検証済みの
原子的授受 primitive は一度も発火していなかった。本クラスはそれを権威 ReservationManager 上で実際に
動かし、原子的授受 (atomic) と naive を同一の状態機械の下で切替えて対比できるようにする。

状態 (hoState[r]):  NONE → REQUESTED → ACKNOWLEDGED → IN_PROGRESS → COMPLETED →(cleanup)→ NONE
受信側 (hoReceiver[r]): 授受先機体 holder_id。

  atomic=True  : begin() は ReservationManager.transfer_grant で holder を sender→receiver に
                 **単一ステップ**で付け替える (TLA `BeginHandoverAtomic`)。IN_PROGRESS 中つねに
                 holder=receiver = P4 (ReceiverOwnership) 保持・free window なし。
  atomic=False : begin() は sender を release して holder=NONE にし (free window が開く; TLA
                 `BeginHandoverNaive`)、その後 grant_to_receiver で receiver が再取得する
                 (TLA `GrantToReceiver`)。IN_PROGRESS の開始時に holder≠receiver = P4 違反を実演。

純ロジック (proto と ReservationManager 以外に依存しない)。時刻は呼び側が now を渡す。
各遷移を holder スナップショット付きで記録するので、HandoverOracle が txlog 単独でなく
ho 遷移ログから ReceiverOwnership を直接検証できる。
"""

from __future__ import annotations

from typing import Any, Callable

from cap.v0.core import site_agent_pb2

from .reservation import ReservationManager

NONE, REQUESTED, ACKNOWLEDGED, IN_PROGRESS, COMPLETED = (
    "NONE", "REQUESTED", "ACKNOWLEDGED", "IN_PROGRESS", "COMPLETED")
HANDOVER_STATES = {NONE, REQUESTED, ACKNOWLEDGED, IN_PROGRESS, COMPLETED}


class HandoverManager:
    """機体間 Handover サブプロトコルの実行時駆動 (権威 ReservationManager と同居)。"""

    def __init__(self, rm: ReservationManager, *, atomic: bool = True,
                 on_transition: Callable[[dict[str, Any]], None] | None = None) -> None:
        self._rm = rm
        self._atomic = atomic
        self._ho_state: dict[str, str] = {}            # resource_id -> HANDOVER_STATES
        self._ho_receiver: dict[str, str | None] = {}  # resource_id -> receiver holder_id
        self._seq = 0
        self._log: list[dict[str, Any]] = []
        self._on_transition = on_transition

    # ── 内部 ────────────────────────────────────────────────────────────
    def state_of(self, resource_id: str) -> str:
        return self._ho_state.get(resource_id, NONE)

    def receiver_of(self, resource_id: str) -> str | None:
        return self._ho_receiver.get(resource_id)

    def _record(self, action: str, resource_id: str, *, now: float | None,
                sender: str | None = None, receiver: str | None = None) -> None:
        """1 遷移を holder スナップショット付きで記録する。holder_after = 遷移直後に
        ReservationManager が記録している保持者 (= ReceiverOwnership 検証の正本)。"""
        self._seq += 1
        rec = {
            "seq": self._seq, "t_wall": now, "action": action, "resource_id": resource_id,
            "ho_state": self.state_of(resource_id), "receiver": self._ho_receiver.get(resource_id),
            "holder_after": self._rm.holder_of(resource_id, now=now),
            "atomic": self._atomic, "sender": sender,
        }
        self._log.append(rec)
        if self._on_transition is not None:
            try:
                self._on_transition(rec)
            except Exception:  # noqa: BLE001 - witness logging must never break coordination
                pass

    # ── サブプロトコル (TLA actions と 1:1) ──────────────────────────────
    def initiate(self, sender: str, resource_id: str, receiver: str, *,
                 now: float | None = None) -> bool:
        """TLA `InitiateHandover(s, r, rcv)`。保持者 sender が、(待機中の) receiver へ授受を開始。
        前提: hoState[r]=NONE かつ resource を sender が保持中。"""
        if self.state_of(resource_id) != NONE:
            return False
        if self._rm.holder_of(resource_id, now=now) != sender:
            return False
        self._ho_state[resource_id] = REQUESTED
        self._ho_receiver[resource_id] = receiver
        self._record("INITIATE", resource_id, now=now, sender=sender, receiver=receiver)
        return True

    def ack(self, resource_id: str, *, now: float | None = None) -> bool:
        """TLA `AckHandover(r)`: REQUESTED → ACKNOWLEDGED。"""
        if self.state_of(resource_id) != REQUESTED:
            return False
        self._ho_state[resource_id] = ACKNOWLEDGED
        self._record("ACK", resource_id, now=now)
        return True

    def begin(self, resource_id: str, *, receiver_reservation_id: str,
              now: float | None = None) -> bool:
        """ACKNOWLEDGED → IN_PROGRESS。atomic なら TLA `BeginHandoverAtomic` (単一ステップ授受);
        naive なら TLA `BeginHandoverNaive` (sender を release して free window を開く)。"""
        if self.state_of(resource_id) != ACKNOWLEDGED:
            return False
        receiver = self._ho_receiver.get(resource_id)
        if receiver is None:
            return False
        if self._atomic:
            st = self._rm.transfer_grant(resource_id, receiver, receiver_reservation_id, now=now)
            if st is None:
                return False
        else:
            # naive: 保持者の grant を解放 → holder=NONE (free window が開く)。
            sender_rid = self._rm.reservation_id_of(resource_id, now=now)
            if sender_rid is None:
                return False
            self._rm.release(site_agent_pb2.ReservationRelease(
                reservation_id=sender_rid, reason="naive handover release"), now=now)
        self._ho_state[resource_id] = IN_PROGRESS
        self._record("BEGIN", resource_id, now=now, receiver=receiver)
        return True

    def grant_to_receiver(self, resource_id: str, *, receiver_reservation_id: str,
                          now: float | None = None) -> bool:
        """TLA `GrantToReceiver(r)` (naive のみ): free window 中の resource を receiver が再取得。
        第三者が先に GrantResource で奪っていれば (holder≠NONE) ここは発火しない = P4 違反の経路。"""
        if self._atomic or self.state_of(resource_id) != IN_PROGRESS:
            return False
        receiver = self._ho_receiver.get(resource_id)
        if receiver is None or self._rm.holder_of(resource_id, now=now) is not None:
            return False
        self._rm.request(site_agent_pb2.ReservationRequest(
            reservation_id=receiver_reservation_id, resource_id=resource_id, holder_id=receiver),
            now=now)
        self._record("GRANT_TO_RECEIVER", resource_id, now=now, receiver=receiver)
        return True

    def complete(self, resource_id: str, *, now: float | None = None) -> bool:
        """TLA `CompleteHandover(r)`: IN_PROGRESS → COMPLETED。前提: holder=receiver。"""
        if self.state_of(resource_id) != IN_PROGRESS:
            return False
        receiver = self._ho_receiver.get(resource_id)
        if self._rm.holder_of(resource_id, now=now) != receiver:
            return False
        self._ho_state[resource_id] = COMPLETED
        self._record("COMPLETE", resource_id, now=now, receiver=receiver)
        return True

    def cleanup(self, resource_id: str, *, now: float | None = None) -> bool:
        """TLA `CleanupHandover(r)`: COMPLETED → NONE, hoReceiver → NONE。"""
        if self.state_of(resource_id) != COMPLETED:
            return False
        self._ho_state[resource_id] = NONE
        self._ho_receiver[resource_id] = None
        self._record("CLEANUP", resource_id, now=now)
        return True

    # ── witness ─────────────────────────────────────────────────────────
    def log(self) -> list[dict[str, Any]]:
        """ho 遷移ログ (holder スナップショット付き) のコピー。"""
        return list(self._log)
