# Copied from the reference implementation by its scripts/paper_r1/sync_kit.py - edit there, not here.
"""Occupancy-gated reservation arbiter: the reservation-scheme family of OccupancyHandover.tla, executable.

The plain ``ReservationManager`` reasons about logical ownership only. A loading bay is not a data object: it cannot
refuse a machine whose grant has ended, and a machine whose lease expired may still stand in it. This arbiter couples
the grant to the machines' presence the way cap-spec/formal/tla/OccupancyHandover.tla does, and it implements every
member of the scheme family there, so the conventional schemes and CAP run on the same code:

  arb         "none"  optimistic grants | "deny" deny-on-busy | "queue" FIFO of the denied requesters
  gate        "none"  no entry check | "local" a grant notice suffices | "permit" a live, single-use entry permit
  release     "onDone" frees the bay at once | "onVacate" the releaser stays in custody until its vacancy is confirmed
  expiry      "none" | "ttl" a holder that stopped renewing loses the bay once its permits are no longer counted |
              "ttlVacate" as "ttl", and it stays in custody until its vacancy is confirmed
  succession  "free" | "designated" the next holder is the plan's successor (the oldest waiter); a release with a
              successor is an atomic hand-over

Named members (``Scheme.named``): mutex_on_done, mutex_group (Open-RMF-style: held until the robot is out),
lease_fenced (renewable lease with fenced, time-limited permits), lease_fenced_vacate, cap.

Entry permits and the lease lemma (A4). A machine times a permit from the moment it SENT its permit request, for
T(1 - rho) on its monotonic clock (``EntryGate``); the arbiter counts it as possibly in use for T(1 + rho) on its own
monotonic clock from the grant. The machine's usable interval therefore lies inside the interval the arbiter counts.

Generations are scoped to the arbiter's boot: grant_epoch = boot << 32 | generation, with a fresh random 32-bit boot
id per start, so a permit or status from an earlier boot is never taken for one of this boot.

The journal is written and synced before a grant, a permit or a hand-over takes effect; when it cannot be written the
arbiter refuses (fail-closed). After a restart it takes up holders, custody, the generation and the counted permits;
when the journal cannot be read it closes every bay ("occupancy unknown") until T(1 + rho) has passed and every
machine has been confirmed out.

Vacancy (A2) is decided by ``VacancyEnvelope``: a machine is out only when its reported position, inflated by the
deterministic position-error bound eps plus top speed x (pose age + link latency) plus braking distance, lies outside
the bay. A missing or stale pose is never out.
"""

from __future__ import annotations

import json
import logging
import math
import os
import secrets
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from cap.v0.core import events_pb2, site_agent_pb2

from .reservation import ReservationManager, ReservationView

logger = logging.getLogger(__name__)

_S = events_pb2.ReservationStatus
_State = _S.ReservationState

PERMIT_REASON = "entry_permit"          # a ReservationRequest with this reason asks for an entry permit
JOURNAL_V2_NAME = "occupancy_journal.jsonl"


# ---------------------------------------------------------------------------- scheme
@dataclass(frozen=True)
class Scheme:
    arb: str = "deny"
    gate: str = "local"
    release: str = "onDone"
    expiry: str = "none"
    succession: str = "free"
    permit_ttl_s: float = 30.0          # T: an entry permit's validity
    rho: float = 1e-3                   # bound on the relative rate error of a monotonic clock
    lease_ttl_s: float = 6.0            # a holder silent this long has stopped renewing
    name: str = ""

    _NAMED = {
        "mutex_on_done": dict(arb="deny", gate="local", release="onDone", expiry="none", succession="free"),
        "mutex_group": dict(arb="deny", gate="local", release="onVacate", expiry="none", succession="free"),
        "lease_fenced": dict(arb="deny", gate="permit", release="onDone", expiry="ttl", succession="free"),
        "lease_fenced_vacate": dict(arb="deny", gate="permit", release="onVacate", expiry="ttlVacate",
                                    succession="free"),
        "cap": dict(arb="queue", gate="permit", release="onVacate", expiry="ttlVacate", succession="designated"),
    }

    def __post_init__(self) -> None:
        assert self.arb in ("none", "deny", "queue"), self.arb
        assert self.gate in ("none", "local", "permit"), self.gate
        assert self.release in ("onDone", "onVacate"), self.release
        assert self.expiry in ("none", "ttl", "ttlVacate"), self.expiry
        assert self.succession in ("free", "designated"), self.succession
        assert not (self.arb == "none" and self.succession == "designated"), "designation needs an arbiter"
        assert 0.0 <= self.rho < 1.0

    @classmethod
    def named(cls, name: str, **overrides: Any) -> Scheme:
        return cls(**cls._NAMED[name], name=name, **overrides)

    @classmethod
    def names(cls) -> list[str]:
        return list(cls._NAMED)

    @classmethod
    def from_env(cls) -> Scheme | None:
        """CAP_RESERVATION_SCHEME=<named> (unset: None, the plain ReservationManager). CAP_PERMIT_TTL_S, CAP_CLOCK_RHO
        and CAP_LEASE_TTL_S override the timing."""
        name = os.environ.get("CAP_RESERVATION_SCHEME", "").strip()
        if not name:
            return None
        kw: dict[str, Any] = {}
        for env, key in (("CAP_PERMIT_TTL_S", "permit_ttl_s"), ("CAP_CLOCK_RHO", "rho"),
                         ("CAP_LEASE_TTL_S", "lease_ttl_s")):
            if os.environ.get(env):
                kw[key] = float(os.environ[env])
        return cls.named(name, **kw)

    @property
    def occ_aware(self) -> bool:
        return self.release == "onVacate" or self.expiry == "ttlVacate"

    @property
    def leased(self) -> bool:
        return self.gate == "permit" or self.expiry in ("ttl", "ttlVacate")

    @property
    def arbiter_permit_life_s(self) -> float:
        """How long the arbiter counts a permit from its grant: T(1 + rho) on its own clock."""
        return self.permit_ttl_s * (1.0 + self.rho)


# ---------------------------------------------------------------------------- geometry
@dataclass(frozen=True)
class Zone:
    """A conflict zone: the loading bay around a loader. Machines in ``compatible`` (the loader itself) may be in it
    with the holder; every other pair of machines is incompatible in it."""

    resource_id: str
    x: float
    y: float
    radius_m: float
    compatible: frozenset[str] = frozenset()

    def distance(self, x: float, y: float) -> float:
        return math.hypot(float(x) - self.x, float(y) - self.y)


@dataclass(frozen=True)
class VacancyEnvelope:
    """A2: when a machine counts as out of a zone. The inflation is deterministic; eps is a design bound (e.g. the
    positioning system's specified error bound), never the largest error a test happened to see."""

    eps_m: float = 0.5                  # deterministic bound on the position error
    v_max_mps: float = 1.5              # top speed of the machine
    latency_s: float = 0.3              # link latency bound for the pose report
    d_brake_m: float = 1.0              # braking distance at top speed
    max_pose_age_s: float = 1.0         # an older pose is not used: the machine is not out

    def inflation_m(self, pose_age_s: float) -> float:
        return self.eps_m + self.v_max_mps * (max(0.0, pose_age_s) + self.latency_s) + self.d_brake_m

    def out_of(self, zone: Zone, pose: tuple[float, float] | None, pose_age_s: float | None) -> bool:
        if pose is None or pose_age_s is None or pose_age_s > self.max_pose_age_s:
            return False                # missing or stale: never out (fail-closed)
        return zone.distance(*pose) - self.inflation_m(pose_age_s) >= zone.radius_m

    @classmethod
    def from_env(cls) -> VacancyEnvelope:
        kw: dict[str, Any] = {}
        for env, key in (("CAP_VACANCY_EPS_M", "eps_m"), ("CAP_VACANCY_VMAX_MPS", "v_max_mps"),
                         ("CAP_VACANCY_LATENCY_S", "latency_s"), ("CAP_VACANCY_BRAKE_M", "d_brake_m"),
                         ("CAP_VACANCY_MAX_AGE_S", "max_pose_age_s")):
            if os.environ.get(env):
                kw[key] = float(os.environ[env])
        return cls(**kw)


# ---------------------------------------------------------------------------- journal
class JournalUnavailable(RuntimeError):
    """The journal could not be written: the arbiter refuses the grant (fail-closed)."""


class OccupancyJournal:
    """Append-only snapshots of the arbiter's durable state, each synced before the change it records takes effect."""

    def __init__(self, state_dir: str | os.PathLike[str], *, sync: bool = True) -> None:
        self.path = Path(state_dir) / JOURNAL_V2_NAME
        self.sync = sync

    def write(self, snapshot: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(snapshot, separators=(",", ":")) + "\n")
                fh.flush()
                if self.sync:
                    os.fsync(fh.fileno())
        except OSError as exc:  # disk full, read-only, gone
            raise JournalUnavailable(str(exc)) from exc

    def starts(self) -> int:
        """How many arbiter starts this state directory has seen before this one, then count this one (synced).
        A missing journal after an earlier start was lost, not never written."""
        counter = self.path.parent / "occupancy_starts"
        try:
            before = int(counter.read_text().strip() or 0)
        except (FileNotFoundError, ValueError):
            before = 0
        try:
            counter.parent.mkdir(parents=True, exist_ok=True)
            with counter.open("w", encoding="utf-8") as fh:
                fh.write(str(before + 1))
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            raise JournalUnavailable(str(exc)) from exc
        return before

    def load(self) -> tuple[str, dict[str, Any] | None]:
        """("missing", None) | ("ok", last snapshot) | ("unreadable", None)."""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return "missing", None
        except OSError:
            return "unreadable", None
        for line in reversed(lines):
            try:
                snap = json.loads(line)
            except json.JSONDecodeError:
                continue            # a torn last write: take the one before
            if isinstance(snap, dict) and snap.get("v") == 2:
                return "ok", snap
        return "unreadable", None


# ---------------------------------------------------------------------------- arbiter
@dataclass
class _Res:
    holder: str | None = None
    rid: str | None = None
    gen: int = 0
    custody: set[str] = field(default_factory=set)
    # permits counted as possibly in use: (holder, epoch) -> arbiter monotonic time of the grant
    live: dict[tuple[str, int], float] = field(default_factory=dict)
    queue: list[tuple[str, str]] = field(default_factory=list)   # FIFO of (holder, reservation_id) denied while held
    t_grant: float = 0.0          # arbiter monotonic time the current holder got the bay (silence counts from here)


class OccupancyArbiter(ReservationManager):
    """The reservation authority for one scheme of the family. Same API as ``ReservationManager`` (request, release,
    transfer_grant, holder_of, pending_for, txlog, audit_double_grants), plus entry permits, custody, vacancy
    confirmation, lease expiry and the restart guard."""

    def __init__(self, scheme: Scheme, *, on_transition: Callable[[dict[str, Any]], None] | None = None,
                 clock: Callable[[], float] = time.monotonic, journal: OccupancyJournal | None = None,
                 envelope: VacancyEnvelope | None = None, boot_id: int | None = None) -> None:
        super().__init__(on_transition=on_transition)
        self.scheme = scheme
        self._clock = clock
        self._journal = journal
        self.envelope = envelope or VacancyEnvelope()
        self.boot = int(boot_id if boot_id is not None else secrets.randbits(32)) & 0xFFFFFFFF
        self._r: dict[str, _Res] = {}
        self.zones: dict[str, Zone] = {}
        # what the vacancy check sees of the reported poses (None: the poses as reported). A trial's fault hook: the
        # position error A2 bounds is injected here, on the arbiter's view alone.
        self.observe_filter: Callable[[dict[str, Any]], dict[str, Any]] | None = None
        self.guard = "open"                       # "open" | "waitTime" | "waitVacancy"
        self._guard_since = self._clock()
        self.denied_by_journal = 0
        self._restore()
        if self._journal is not None:
            try:
                self._persist()          # a start always leaves a record, so a later missing one means it was lost
            except JournalUnavailable:
                logger.error("[occupancy] the journal cannot be written: every grant will be refused")

    # ------------------------------------------------------------------ helpers
    def _res(self, resource_id: str) -> _Res:
        return self._r.setdefault(resource_id, _Res())

    def epoch(self, gen: int) -> int:
        return (self.boot << 32) | (gen & 0xFFFFFFFF)

    @staticmethod
    def epoch_boot(epoch: int) -> int:
        return (int(epoch) >> 32) & 0xFFFFFFFF

    def _incompatible(self, resource_id: str, a: str, b: str) -> bool:
        if a == b:
            return False
        z = self.zones.get(resource_id)
        return not (z is not None and (a in z.compatible or b in z.compatible))

    def _tracks(self, resource_id: str) -> bool:
        """Custody needs a zone to confirm vacancy against: a resource without one (a dump pad here) is released at
        once whatever the scheme."""
        return self.scheme.occ_aware and resource_id in self.zones

    def _blocked_for(self, resource_id: str, m: str) -> str | None:
        """Why m may not be given the bay now (custody or a counted permit of an incompatible machine), or None."""
        r = self._res(resource_id)
        if self.guard != "open":
            return f"bay closed after a restart ({self.guard})"
        for c in r.custody:
            if self._incompatible(resource_id, m, c):
                return f"'{c}' not yet confirmed out"
        for (h, _e) in r.live:
            if self._incompatible(resource_id, m, h):
                return f"a permit of '{h}' may still be in use"
        return None

    def _snapshot(self) -> dict[str, Any]:
        return {"v": 2, "boot": self.boot, "t_wall": time.time(), "scheme": self.scheme.name or asdict(self.scheme),
                "res": {k: {"holder": r.holder, "rid": r.rid, "gen": r.gen, "custody": sorted(r.custody),
                            "live": [[h, e] for (h, e) in r.live]} for k, r in self._r.items()}}

    def _persist(self) -> None:
        if self._journal is not None:
            self._journal.write(self._snapshot())

    def _log(self, event: str, resource_id: str, reservation_id: str, holder: str | None, prior: str | None, *,
             epoch: int | None = None, reason: str = "", now: float | None = None,
             planned_before: str | None = None) -> None:
        r = self._res(resource_id) if resource_id else None
        self._seq += 1
        rec = {"seq": self._seq, "t_wall": now if now is not None else time.time(), "t_mono": self._clock(),
               "event": event, "resource_id": resource_id, "reservation_id": reservation_id,
               "requested_holder": holder, "prior_holder": prior,
               "resolved_holder": r.holder if r is not None else None,
               "epoch": epoch, "custody": sorted(r.custody) if r is not None else [],
               "planned": (r.queue[0][0] if (r is not None and r.queue) else None),
               "planned_before": planned_before,
               "guard": self.guard, "reason": reason, "scheme": self.scheme.name}
        self._txlog.append(rec)
        if self._on_transition is not None:
            try:
                self._on_transition(rec)
            except Exception:  # noqa: BLE001 - logging must never break arbitration
                pass

    def _status(self, rid: str, resource_id: str, holder: str, state: int, *, reason: str = "",
                epoch: int = 0, queue_position: int = 0) -> Any:
        st = _S(reservation_id=rid, resource_id=resource_id, holder_id=holder, state=state, reason=reason,
                grant_epoch=int(epoch), queue_position=int(queue_position))
        return st

    def _enqueue(self, r: _Res, holder: str, rid: str) -> int:
        for i, (h, _x) in enumerate(r.queue):
            if h == holder:
                r.queue[i] = (holder, rid)
                return i + 1
        r.queue.append((holder, rid))
        return len(r.queue)

    def _dequeue(self, r: _Res, holder: str) -> None:
        r.queue = [(h, x) for (h, x) in r.queue if h != holder]

    # ------------------------------------------------------------------ API: zones, plan
    def set_zone(self, zone: Zone) -> None:
        self.zones[zone.resource_id] = zone

    def planned(self, resource_id: str) -> str | None:
        r = self._res(resource_id)
        return r.queue[0][0] if r.queue else None

    def pending_for(self, resource_id: str, *, exclude: str | None = None) -> list[tuple[str, str]]:
        r = self._res(resource_id)
        return [(h, x) for (h, x) in r.queue if h != exclude and h != r.holder]

    def holder_of(self, resource_id: str, *, now: float | None = None) -> str | None:
        return self._res(resource_id).holder

    def reservation_id_of(self, resource_id: str, *, now: float | None = None) -> str | None:
        return self._res(resource_id).rid

    def custody_of(self, resource_id: str) -> set[str]:
        return set(self._res(resource_id).custody)

    def live_permits(self, resource_id: str) -> dict[tuple[str, int], float]:
        return dict(self._res(resource_id).live)

    def active(self, *, now: float | None = None) -> list[ReservationView]:
        return [ReservationView(reservation_id=r.rid or "", resource_id=k, holder_id=r.holder, expires_at=None)
                for k, r in self._r.items() if r.holder is not None]

    # ------------------------------------------------------------------ API: requests
    def request(self, req: Any, *, now: float | None = None) -> Any:
        self.tick()
        if (getattr(req, "reason", "") or "") == PERMIT_REASON:
            return self._request_permit(req, now=now)
        rid, res_id, m = req.reservation_id, req.resource_id, req.holder_id
        r = self._res(res_id)
        sch = self.scheme
        if sch.arb == "none":                       # optimistic: any request is granted, the previous holder forgotten
            return self._grant(r, res_id, m, rid, prior=r.holder, now=now)
        if r.holder == m:                           # the holder asks again: its grant now goes by this id
            r.rid = rid
            return self._status(rid, res_id, m, _State.RESERVATION_STATE_GRANTED, epoch=self.epoch(r.gen))
        reason = None
        if r.holder is not None:
            reason = f"held by '{r.holder}'"
        else:
            reason = self._blocked_for(res_id, m)
            if reason is None and sch.succession == "designated" and r.queue and r.queue[0][0] != m:
                reason = f"the plan's successor is '{r.queue[0][0]}'"
            if reason is None and sch.arb == "queue" and r.queue and r.queue[0][0] != m:
                reason = f"queued behind '{r.queue[0][0]}'"
        if reason is not None:
            pos = self._enqueue(r, m, rid)
            self._log("DENIED", res_id, rid, m, r.holder, reason=reason, now=now)
            state = _State.RESERVATION_STATE_QUEUED if sch.arb == "queue" else _State.RESERVATION_STATE_DENIED
            return self._status(rid, res_id, m, state, reason=reason,
                                queue_position=pos if sch.arb == "queue" else 0)
        return self._grant(r, res_id, m, rid, prior=None, now=now)

    def _grant(self, r: _Res, res_id: str, m: str, rid: str, *, prior: str | None, now: float | None) -> Any:
        planned_before = r.queue[0][0] if r.queue else None
        before = (r.holder, r.rid, r.gen, set(r.custody))
        r.holder, r.rid, r.gen = m, rid, r.gen + 1
        r.t_grant = self._clock()
        r.custody.discard(m)
        try:
            self._persist()
        except JournalUnavailable as exc:
            r.holder, r.rid, r.gen, r.custody = before[0], before[1], before[2], before[3]
            self.denied_by_journal += 1
            self._log("DENIED", res_id, rid, m, before[0], reason=f"journal unavailable: {exc}", now=now)
            return self._status(rid, res_id, m, _State.RESERVATION_STATE_DENIED, reason="journal unavailable")
        self._dequeue(r, m)
        ep = self.epoch(r.gen)
        self._log("GRANTED", res_id, rid, m, prior, epoch=ep, now=now, planned_before=planned_before)
        return self._status(rid, res_id, m, _State.RESERVATION_STATE_GRANTED, epoch=ep)

    def _request_permit(self, req: Any, *, now: float | None) -> Any:
        rid, res_id, m = req.reservation_id, req.resource_id, req.holder_id
        r = self._res(res_id)
        if self.scheme.gate != "permit":
            return self._status(rid, res_id, m, _State.RESERVATION_STATE_DENIED, reason="this scheme has no permits")
        if r.holder != m:
            self._log("PERMIT_DENIED", res_id, rid, m, r.holder, reason="not the holder", now=now)
            return self._status(rid, res_id, m, _State.RESERVATION_STATE_DENIED, reason="not the holder")
        why = self._blocked_for(res_id, m)
        if why is not None:
            self._log("PERMIT_DENIED", res_id, rid, m, r.holder, reason=why, now=now)
            return self._status(rid, res_id, m, _State.RESERVATION_STATE_DENIED, reason=why)
        ep = self.epoch(r.gen)
        r.live[(m, ep)] = self._clock()
        try:
            self._persist()
        except JournalUnavailable as exc:
            r.live.pop((m, ep), None)
            self.denied_by_journal += 1
            self._log("PERMIT_DENIED", res_id, rid, m, r.holder, reason=f"journal unavailable: {exc}", now=now)
            return self._status(rid, res_id, m, _State.RESERVATION_STATE_DENIED, reason="journal unavailable")
        self._log("PERMIT", res_id, rid, m, r.holder, epoch=ep, now=now)
        return self._status(rid, res_id, m, _State.RESERVATION_STATE_GRANTED, reason=PERMIT_REASON, epoch=ep)

    def release(self, rel: Any, *, now: float | None = None) -> Any:
        self.tick()
        rid = rel.reservation_id
        res_id = next((k for k, r in self._r.items() if r.rid == rid), None)
        if res_id is None:
            self._log("RELEASED", "", rid, None, None, reason=rel.reason or "unknown reservation (idempotent)",
                      now=now)
            return self._status(rid, "", "", _State.RESERVATION_STATE_RELEASED, reason="unknown reservation")
        r = self._res(res_id)
        m = r.holder
        waiting = self.pending_for(res_id, exclude=m)
        if self.scheme.succession == "designated" and waiting:
            if self.transfer_grant(res_id, waiting[0][0], waiting[0][1], now=now) is not None:
                return self._status(rid, res_id, m or "", _State.RESERVATION_STATE_RELEASED,
                                    reason=f"handed over: {rel.reason}")
        r.holder, r.rid = None, None
        if self.scheme.release == "onVacate" and m is not None and resource_id_has_zone(self, res_id):
            r.custody.add(m)
        try:
            self._persist()
        except JournalUnavailable:
            # The release stands for the releaser (it has given the bay up). Keep it in custody so nobody else is
            # admitted on a record that did not reach the disk.
            if m is not None:
                r.custody.add(m)
        self._log("RELEASED", res_id, rid, m, m, reason=rel.reason, now=now)
        return self._status(rid, res_id, m or "", _State.RESERVATION_STATE_RELEASED, reason=rel.reason)

    def holder_of_reservation(self, reservation_id: str) -> str | None:
        """Who holds the grant ``reservation_id`` (None: no such grant now)."""
        return next((r.holder for r in self._r.values() if r.rid == reservation_id), None)

    def transfer_grant(self, resource_id: str, receiver_id: str, receiver_reservation_id: str, *,
                       now: float | None = None, expires_at: float | None = None) -> Any:
        """Atomic hand-over from the holder to ``receiver_id``; with custody the sender stays in it until confirmed
        out, so the receiver's permit waits for the sender's vacancy."""
        r = self._res(resource_id)
        sender = r.holder
        if sender is None or receiver_id == sender:
            return None
        planned_before = r.queue[0][0] if r.queue else None
        before = (r.holder, r.rid, r.gen, set(r.custody))
        r.holder, r.rid, r.gen = receiver_id, receiver_reservation_id, r.gen + 1
        r.t_grant = self._clock()
        if self._tracks(resource_id):
            r.custody.add(sender)
        r.custody.discard(receiver_id)
        try:
            self._persist()
        except JournalUnavailable as exc:
            r.holder, r.rid, r.gen, r.custody = before
            self.denied_by_journal += 1
            self._log("HANDOVER_REFUSED", resource_id, receiver_reservation_id, receiver_id, sender,
                      reason=f"journal unavailable: {exc}", now=now)
            return None
        self._dequeue(r, receiver_id)
        ep = self.epoch(r.gen)
        self._log("HANDOVER", resource_id, receiver_reservation_id, receiver_id, sender, epoch=ep, now=now,
                  planned_before=planned_before)
        return self._status(receiver_reservation_id, resource_id, receiver_id, _State.RESERVATION_STATE_GRANTED,
                            epoch=ep)

    # ------------------------------------------------------------------ API: vacancy, time, restarts
    def confirm_vacant(self, resource_id: str, machine_id: str, *, now: float | None = None) -> bool:
        r = self._res(resource_id)
        if machine_id not in r.custody:
            return False
        r.custody.discard(machine_id)
        try:
            self._persist()
        except JournalUnavailable:
            r.custody.add(machine_id)       # keep it until the record is durable
            return False
        self._log("VACATED", resource_id, "", machine_id, r.holder, now=now)
        return True

    def observe(self, poses: dict[str, tuple[tuple[float, float] | None, float | None]], *,
                now: float | None = None) -> None:
        """Confirm the vacancy of machines in custody from their reported poses ({machine: ((x, y), age_s)}), and,
        after a restart that lost the journal, open the bays once every known machine is out of every zone."""
        if self.observe_filter is not None:
            poses = self.observe_filter(poses)
        for res_id, r in self._r.items():
            z = self.zones.get(res_id)
            if z is None:
                continue
            for m in sorted(r.custody):
                pose, age = poses.get(m, (None, None))
                if self.envelope.out_of(z, pose, age):
                    self.confirm_vacant(res_id, m, now=now)
        if self.guard == "waitVacancy" and poses and self.zones:
            if all(self.envelope.out_of(z, *poses.get(m, (None, None)))
                   for z in self.zones.values() for m in poses if m not in z.compatible):
                self.guard = "open"
                self._guard_since = self._clock()
                self._log("GUARD_OPEN", "", "", None, None, reason="every machine confirmed out", now=now)

    def operator_confirm_all_vacant(self) -> None:
        """An operator's confirmation that every bay is empty (the only way out when a machine cannot report)."""
        for r in self._r.values():
            r.custody.clear()
        self.guard = "open"
        self._log("GUARD_OPEN", "", "", None, None, reason="operator confirmed every bay empty")

    def tick(self, *, last_heartbeat: dict[str, float] | None = None, now: float | None = None) -> None:
        """Arbiter-side time: stop counting permits older than T(1 + rho); end the restart grace period; expire
        holders that stopped renewing (``last_heartbeat``: machine -> monotonic time the arbiter last heard from it;
        silence counts from the grant at the earliest)."""
        t = self._clock()
        life = self.scheme.arbiter_permit_life_s
        changed = False
        for r in self._r.values():
            for key, t0 in list(r.live.items()):
                if t - t0 >= life:
                    del r.live[key]
                    changed = True
        if self.guard == "waitTime" and t - self._guard_since >= life:
            self.guard = "waitVacancy" if self.scheme.occ_aware else "open"
            self._guard_since = t
            self._log("GUARD_" + self.guard.upper(), "", "", None, None, reason="restart grace period over", now=now)
        if last_heartbeat is not None and self.scheme.expiry in ("ttl", "ttlVacate"):
            for res_id, r in self._r.items():
                m = r.holder
                if m is None:
                    continue
                silent = t - max(float(last_heartbeat.get(m, -math.inf)), r.t_grant)
                if silent < self.scheme.lease_ttl_s:
                    continue
                if any(h == m for (h, _e) in r.live):
                    continue                     # its permit may still be in use
                self._expire_holder(res_id, r, m, now=now)
                changed = True
        if changed:
            try:
                self._persist()
            except JournalUnavailable:
                pass

    def _expire_holder(self, res_id: str, r: _Res, m: str, *, now: float | None) -> None:
        waiting = self.pending_for(res_id, exclude=m)
        if self.scheme.succession == "designated" and waiting:
            if self.transfer_grant(res_id, waiting[0][0], waiting[0][1], now=now) is not None:
                if self.scheme.expiry != "ttlVacate":
                    r.custody.discard(m)
                return
        r.holder, rid, r.rid = None, r.rid, None
        if self.scheme.expiry == "ttlVacate" and res_id in self.zones:
            r.custody.add(m)
        self._log("EXPIRED", res_id, rid or "", m, m, reason="stopped renewing", now=now)

    def _restore(self) -> None:
        if self._journal is None:
            return
        try:
            earlier_starts = self._journal.starts()
        except JournalUnavailable:
            earlier_starts = 1                   # cannot even count: treat the record as lost
        status, snap = self._journal.load()
        if status == "missing" and earlier_starts == 0:
            return                               # the first start in this state directory
        if status == "missing":
            status = "unreadable"                # an earlier start wrote one and it is gone
        if status == "unreadable" or snap is None:
            self.guard = "waitTime" if self.scheme.leased else ("waitVacancy" if self.scheme.occ_aware else "open")
            self._guard_since = self._clock()
            self._log("GUARD_" + self.guard.upper(), "", "", None, None,
                      reason="journal unreadable after a restart: occupancy unknown")
            return
        t = self._clock()
        for res_id, d in (snap.get("res") or {}).items():
            r = self._res(res_id)
            r.holder, r.rid, r.gen = d.get("holder"), d.get("rid"), int(d.get("gen") or 0)
            r.custody = set(d.get("custody") or [])
            # counted permits: their age is unknown across the restart, so each is counted afresh for T(1 + rho)
            r.live = {(h, int(e)): t for h, e in (d.get("live") or [])}
        self._log("RESTORED", "", "", None, None, reason=f"journal of boot {snap.get('boot')} taken up")

    # ------------------------------------------------------------------ audits
    def audit_double_grants(self) -> list[dict[str, Any]]:
        return [rec for rec in self._txlog
                if rec["event"] == "GRANTED" and rec.get("prior_holder") not in (None, rec.get("requested_holder"))]


def resource_id_has_zone(arbiter: OccupancyArbiter, resource_id: str) -> bool:
    return resource_id in arbiter.zones


def boot_of_epoch(epoch: int) -> int:
    return OccupancyArbiter.epoch_boot(epoch)


def zones_from_params(params: dict[str, Any]) -> list[Zone]:
    """Conflict zones carried in WorkOrder parameters: ``conflict_zones`` = [{resource_id, x, y, radius_m,
    compatible: [...]}, ...] (a JSON string or a list)."""
    raw = params.get("conflict_zones")
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    out = []
    for d in raw if isinstance(raw, Iterable) else []:
        try:
            out.append(Zone(resource_id=str(d["resource_id"]), x=float(d["x"]), y=float(d["y"]),
                            radius_m=float(d["radius_m"]), compatible=frozenset(d.get("compatible") or [])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def zones_to_param(zones: Iterable[Zone]) -> str:
    return json.dumps([{"resource_id": z.resource_id, "x": z.x, "y": z.y, "radius_m": z.radius_m,
                        "compatible": sorted(z.compatible)} for z in zones])


__all__ = ["JournalUnavailable", "OccupancyArbiter", "OccupancyJournal", "PERMIT_REASON", "Scheme",
           "VacancyEnvelope", "Zone", "boot_of_epoch", "replace", "zones_from_params", "zones_to_param"]
