"""ReservationManager — 共有資源の予約権威 (Site 側)。

CAP の `ReservationRequest`/`ReservationRelease` を受けて `ReservationStatus` を返す。
1 つの資源 (resource_id = ゾーン id / 積込点 / 経路区間) は同時に 1 ホルダのみ GRANTED。
他ホルダが保持中なら DENIED (競合)。保持者の release / 窓の expiry で解放される。

conformance Level 2 (Ch06 §6.3.3) の状態遷移に準拠:
  REQUESTED → GRANTED   (資源が空き or 同一ホルダの再要求)
  REQUESTED → DENIED    (他ホルダが保持中 = 競合)
  GRANTED   → RELEASED  (保持者の ReservationRelease)
  GRANTED   → EXPIRED   (granted_window.latest を過ぎた、lazy 判定)

純ロジック (proto 以外に依存しない) なのでユニットテストで conformance 意味論を直接検証できる。
時刻は呼び側が now (epoch 秒) を渡す (テスト容易性のため; 未指定なら expiry を評価しない)。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cap.v0.core import events_pb2, site_agent_pb2

_S = events_pb2.ReservationStatus
_State = _S.ReservationState


@dataclass(frozen=True)
class ReservationView:
    """有効な予約の読み取り用ビュー (canvas / 問い合わせ用)。"""

    reservation_id: str
    resource_id: str
    holder_id: str
    expires_at: float | None


def _ts_to_epoch(ts: Any) -> float | None:
    """protobuf Timestamp → epoch 秒。未設定 (seconds=nanos=0) は None (=無期限)。"""
    if ts is None:
        return None
    sec = int(getattr(ts, "seconds", 0) or 0)
    nanos = int(getattr(ts, "nanos", 0) or 0)
    if sec == 0 and nanos == 0:
        return None
    return sec + nanos * 1e-9


class ReservationManager:
    """共有資源の予約権威。Site servicer が所有し、機体からの要求を裁定する。"""

    def __init__(self, on_transition: Callable[[dict[str, Any]], None] | None = None) -> None:
        self._by_id: dict[str, ReservationView] = {}       # reservation_id -> view
        self._holder: dict[str, str] = {}                  # resource_id -> reservation_id (有効な GRANT)
        # 権威トランザクションログ (P2 witness の正本)。各 request/release/expiry を arbiter 内部から
        # 単調 seq + 解決済みホルダ付きで記録する。skill 層 (in-process coord.reserve) と wire 層
        # (機体の cooperative_cycle が gRPC 越しに予約) のどちらの client からの呼び出しも、この 1 本の
        # RM を通る = このログ 1 本で二重付与の有無を直接検証できる (skill 側の logger.info ではなく
        # これが P2 の正本)。on_transition は site が JSONL ファイルへ流すなどの sink (任意)。
        self._seq = 0
        self._txlog: list[dict[str, Any]] = []
        self._on_transition = on_transition
        # resource_id -> FIFO of (holder_id, reservation_id) currently DENIED-while-held
        # (deduped by holder; cleared when that holder is granted). Lets the coordinator see
        # who is waiting so a release can be routed as a typed Handover to the next waiter.
        self._pending: dict[str, list[tuple[str, str]]] = {}

    # ── 内部 ────────────────────────────────────────────────────────────
    def _expire_due(self, now: float | None) -> list[ReservationView]:
        if now is None:
            return []
        expired: list[ReservationView] = []
        for resource_id, rid in list(self._holder.items()):
            v = self._by_id.get(rid)
            if v is not None and v.expires_at is not None and v.expires_at <= now:
                expired.append(v)
                self._holder.pop(resource_id, None)
                self._by_id.pop(rid, None)
                self._emit("EXPIRED", resource_id, rid, v.holder_id, v.holder_id,
                           now=now, expires_at=v.expires_at)
        return expired

    @staticmethod
    def _status(reservation_id: str, resource_id: str, holder_id: str,
                state: int, *, reason: str = "", granted_window: Any = None) -> events_pb2.ReservationStatus:
        st = _S(reservation_id=reservation_id, resource_id=resource_id,
                holder_id=holder_id, state=state, reason=reason)
        if granted_window is not None:
            st.granted_window.CopyFrom(granted_window)
        return st

    def _emit(self, event: str, resource_id: str, reservation_id: str,
              requested_holder: str, prior_holder: str | None, *,
              now: float | None = None, expires_at: float | None = None,
              reason: str = "") -> None:
        """arbiter 内部から 1 遷移を権威ログに記録する (P2 witness の正本)。

        resolved_holder = 遷移**後**に RM が実際に保持者として記録しているホルダ。RM は資源あたり
        高々 1 ホルダしか持てない (request が別ホルダ保持中を DENIED するため) ので、このフィールドが
        単一保持者性の正本になる。prior_holder = 遷移**前**の保持者 (二重付与監査に使う)。
        """
        self._seq += 1
        cur = self._holder.get(resource_id)
        resolved = self._by_id[cur].holder_id if (cur and cur in self._by_id) else None
        rec = {
            "seq": self._seq, "t_wall": now, "event": event,
            "resource_id": resource_id, "reservation_id": reservation_id,
            "requested_holder": requested_holder, "prior_holder": prior_holder,
            "resolved_holder": resolved, "expires_at": expires_at, "reason": reason,
        }
        self._txlog.append(rec)
        if self._on_transition is not None:
            try:
                self._on_transition(rec)
            except Exception:  # noqa: BLE001 - witness logging must never break arbitration
                pass

    # ── 公開 API ────────────────────────────────────────────────────────
    def request(self, req: site_agent_pb2.ReservationRequest, *,
                now: float | None = None) -> events_pb2.ReservationStatus:
        """予約要求を裁定する。空き/同一ホルダ → GRANTED、他ホルダ保持中 → DENIED。"""
        self._expire_due(now)
        rid, resource_id, holder_id = req.reservation_id, req.resource_id, req.holder_id
        cur = self._holder.get(resource_id)
        prior_holder = self._by_id[cur].holder_id if (cur is not None and cur in self._by_id) else None
        if prior_holder is not None and prior_holder != holder_id:
            self._emit("DENIED", resource_id, rid, holder_id, prior_holder,
                       now=now, reason=f"held by '{prior_holder}'")
            # remember this waiter (FIFO by FIRST denial; a retry updates its reservation_id
            # in place so a repeatedly-denied waiter keeps its queue position, not moved to back)
            q = self._pending.setdefault(resource_id, [])
            for i, (h, _r) in enumerate(q):
                if h == holder_id:
                    q[i] = (holder_id, rid)
                    break
            else:
                q.append((holder_id, rid))
            return self._status(
                rid, resource_id, holder_id, _State.RESERVATION_STATE_DENIED,
                reason=f"resource '{resource_id}' held by '{prior_holder}'",
            )
        # 空き or 同一ホルダの再要求 (idempotent) → GRANTED
        view = ReservationView(reservation_id=rid, resource_id=resource_id, holder_id=holder_id,
                               expires_at=_ts_to_epoch(req.requested_window.latest)
                               if req.HasField("requested_window") else None)
        # 同一ホルダが別 reservation_id で再要求した場合は旧 grant を畳む
        if cur is not None and cur != rid:
            self._by_id.pop(cur, None)
        self._by_id[rid] = view
        self._holder[resource_id] = rid
        self._clear_pending(resource_id, holder_id)
        self._emit("GRANTED", resource_id, rid, holder_id, prior_holder,
                   now=now, expires_at=view.expires_at)
        return self._status(rid, resource_id, holder_id, _State.RESERVATION_STATE_GRANTED,
                            granted_window=req.requested_window if req.HasField("requested_window") else None)

    def release(self, rel: site_agent_pb2.ReservationRelease, *,
                now: float | None = None) -> events_pb2.ReservationStatus:
        """予約を解放する。未知 id でも RELEASED を返す (idempotent)。"""
        v = self._by_id.pop(rel.reservation_id, None)
        if v is None:
            self._emit("RELEASED", "", rel.reservation_id, "", None, now=now,
                       reason=rel.reason or "unknown reservation (idempotent release)")
            return self._status(rel.reservation_id, "", "", _State.RESERVATION_STATE_RELEASED,
                                reason=rel.reason or "unknown reservation (idempotent release)")
        if self._holder.get(v.resource_id) == rel.reservation_id:
            self._holder.pop(v.resource_id, None)
        self._emit("RELEASED", v.resource_id, rel.reservation_id, v.holder_id, v.holder_id,
                   now=now, reason=rel.reason)
        return self._status(v.reservation_id, v.resource_id, v.holder_id,
                            _State.RESERVATION_STATE_RELEASED, reason=rel.reason)

    def transfer_grant(self, resource_id: str, receiver_id: str, receiver_reservation_id: str, *,
                       now: float | None = None, expires_at: float | None = None,
                       ) -> events_pb2.ReservationStatus | None:
        """機体間 Handover の権威 1 ステップ (TLA `BeginHandoverAtomic` の資源効果に対応)。

        resource_id の保持者を現保持者 (sender) から receiver_id へ **単一ステップ**で付け替える:
        ``self._holder[resource_id]`` は old_rid から new_rid へ直接遷移し、途中で未設定 (free window)
        にも二重保持にもならない。これが atomic ownership transfer と naive release-then-reacquire を
        分ける唯一の点。権威ログには ``HANDOVER`` 遷移として記録する (prior_holder=sender,
        resolved_holder=receiver)。``audit_double_grants`` は ``GRANTED`` のみ走査するので、この正当な
        授受は誤検出されない (free-window/二重保持の検証は `audit_handover` が別途行う)。

        資源が誰にも保持されていなければ None を返す (呼び側は begin_atomic の前提を満たすこと)。
        """
        self._expire_due(now)
        cur = self._holder.get(resource_id)
        sender = self._by_id[cur].holder_id if (cur is not None and cur in self._by_id) else None
        if sender is None:
            return None
        view = ReservationView(reservation_id=receiver_reservation_id, resource_id=resource_id,
                               holder_id=receiver_id, expires_at=expires_at)
        # 単一ステップの付け替え: receiver を保持者にし、sender の grant を同じ操作で畳む。
        # この 2 代入の間に _holder[resource_id] が消える瞬間はない (= no free window)。
        self._by_id[receiver_reservation_id] = view
        self._holder[resource_id] = receiver_reservation_id
        if cur is not None and cur != receiver_reservation_id:
            self._by_id.pop(cur, None)
        self._clear_pending(resource_id, receiver_id)
        self._emit("HANDOVER", resource_id, receiver_reservation_id, receiver_id, sender,
                   now=now, expires_at=expires_at)
        return self._status(receiver_reservation_id, resource_id, receiver_id,
                            _State.RESERVATION_STATE_GRANTED)

    def holder_of(self, resource_id: str, *, now: float | None = None) -> str | None:
        """資源の現在の保持者 holder_id (無ければ None)。"""
        self._expire_due(now)
        rid = self._holder.get(resource_id)
        v = self._by_id.get(rid) if rid else None
        return v.holder_id if v else None

    def reservation_id_of(self, resource_id: str, *, now: float | None = None) -> str | None:
        """資源の現在の保持予約 reservation_id (無ければ None)。naive handover が保持者の grant を
        資源単位で解放する (release は reservation_id を要求するため) のに使う。"""
        self._expire_due(now)
        return self._holder.get(resource_id)

    def _clear_pending(self, resource_id: str, holder_id: str) -> None:
        """holder_id を resource_id の待機キューから外す (grant/handover 完了時)。"""
        q = self._pending.get(resource_id)
        if q:
            q[:] = [(h, r) for (h, r) in q if h != holder_id]
            if not q:
                self._pending.pop(resource_id, None)

    def pending_for(self, resource_id: str, *, exclude: str | None = None) -> list[tuple[str, str]]:
        """resource_id を DENIED-while-held で待っている (holder_id, reservation_id) の FIFO。
        現保持者 (= exclude) は除く。coordinator が release を次の待機者への typed Handover に
        ルーティングする (R3) ために使う。"""
        cur = self.holder_of(resource_id)
        return [(h, r) for (h, r) in self._pending.get(resource_id, [])
                if h != cur and h != exclude]

    def active(self, *, now: float | None = None) -> list[ReservationView]:
        """有効な (GRANTED かつ未 expiry) 予約の一覧 (canvas/問い合わせ用)。"""
        self._expire_due(now)
        return [self._by_id[rid] for rid in self._holder.values() if rid in self._by_id]

    # ── P2 witness 監査 (権威トランザクションログからの直接検証) ───────────────
    def txlog(self) -> list[dict[str, Any]]:
        """権威トランザクションログ (P2 witness の正本) のコピー。"""
        return list(self._txlog)

    def audit_double_grants(self) -> list[dict[str, Any]]:
        """二重付与 (P2 違反) の検出 — 別ホルダ保持中に GRANTED された遷移を返す。

        正しい arbiter では常に空 (request が別ホルダ保持中を必ず DENIED するため)。skill 層と
        wire 層のどちらの client からの予約も 1 本の RM を通るので、このログ 1 本で P2 を検証できる
        (= run#7 の見かけの二重付与が「2 ログ系統を混ぜた計装アーティファクト」だった件を、正本側で
        確定的に否定できる)。
        """
        return [r for r in self._txlog
                if r["event"] == "GRANTED" and r["prior_holder"] not in (None, r["requested_holder"])]
