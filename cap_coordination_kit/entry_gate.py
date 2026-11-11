# Copied from the reference implementation by its scripts/paper_r1/sync_kit.py - edit there, not here.
"""EntryGate - the machine's gate into conflict zones, on the HAL's command path (A1).

The gate sits where every drive command is checked before it is written (the HAL's speed cap, queried at the drive's
control rate), so a skill, a deterministic operator or an LLM client cannot drive the machine into a loading bay past
it. It is the machine-side half of the occupancy-gated reservation (``occupancy``):

  mode "none"   no check (the scheme has no gate)
  mode "local"  entry allowed while the machine holds a grant notice for the zone (no time limit)
  mode "permit" entry allowed only with a live entry permit with at least delta_entry * (1 + rho) left; a permit is
                single use (consumed on crossing), timed from the moment its request was SENT for T * (1 - rho) on
                this machine's monotonic clock (the lease lemma, A4), and requested on demand when the gate holds the
                machine at a bay's edge

It checks continuously, not once. Every control tick it asks: if this command is written now and a stop is written
at the next tick, where does the machine come to rest? The answer is computed with the drive's own model (A6): the
commands already written take effect ``act_delay_s`` after they were written, the track speed follows them at most
``decel_mps2``, the next check comes at most ``period_s`` later, and a stop then takes the same delay and a braking
run along the heading the machine has by then. A command is written as proposed only if that whole path - and the
braking run from every point of it, which covers the same command driven slower by a later cap - stays out of every
zone the machine has no authority to enter; otherwise the gate tries it at lower speeds and, if none fits, stops the
drive. A straight-line look-ahead is not enough: a machine held at a bay's edge that turns while it creeps is carried
across by the turn (found by the first kinematic campaign of the revision).

The check stands on the next check coming: a command let through is good for one period. If none follows within the
bound (the skill was cancelled, its client died) and the last command was not a stop, the gate stops the drive itself
(``stop_drive``, the dead-man's timer) - a drive that holds its last command would otherwise carry a slowed machine
on into the bay (the second kinematic campaign of the revision: a cancelled move left 0.11 m/s standing).

A machine that is inside a zone it was not admitted to (it crossed in spite of the gate) may only move out. Inside a
zone, under "permit", a machine whose link to the Site has been silent longer than the operating lease may also only
move out - it stops otherwise. Leaving a zone is never refused here (the machine's safety monitor still judges it).

Every crossing into a zone is logged with the authority used (the oracle for S3 compares it with the arbiter's log).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from cap_coordination_kit.occupancy import Zone


@dataclass
class _Permit:
    epoch: int
    t_req: float                     # this machine's monotonic time when the request was SENT


@dataclass
class GateLogEntry:
    t_mono: float
    t_wall: float
    event: str             # "admit" | "enter" | "exit" | "block" | "permit" | "void" | "request" | "late" | "deadman" | "table"
    resource_id: str
    epoch: int | None = None
    reason: str = ""


@dataclass
class EntryGate:
    machine_id: str
    mode: str = "none"                                  # "none" | "local" | "permit"
    permit_ttl_s: float = 30.0                          # T
    rho: float = 1e-3
    delta_entry_s: float = 3.0                          # bound on the time from the gate check to the crossing
    horizon_s: float = 1.5                              # bound on the time between two checks when period_s is unset
    # the drive's model (A6), in the drive's own time (``time_scale`` drive seconds per second of ``clock``)
    act_delay_s: float = 0.0                            # a written command takes effect at most this much later
    decel_mps2: float = 0.0                             # the track speed follows a command at this rate (0: at once)
    period_s: float | None = None                       # bound on the time between two checks (None: horizon_s)
    time_scale: float = 1.0
    drive_clock: Callable[[], float] | None = None      # the drive's own clock (None: clock * time_scale)
    margin_m: float = 0.1                               # the predicted path keeps this far out of a zone
    exit_hysteresis_m: float = 0.5                      # a machine that was inside is out once this far beyond the edge
    stop_drive: Callable[[], None] | None = None        # writes a stop to the drive (the dead-man's timer's)
    link_lease_s: float = 6.0                           # operating lease inside a zone (Site silence bound)
    request_permit: Callable[[str], None] | None = None # ask the Site for an entry permit (fire and forget)
    clock: Callable[[], float] = time.monotonic
    resend_s: float = 1.0                               # do not re-ask more often than this
    zones: dict[str, Zone] = field(default_factory=dict)
    # The zone table of the Site: (the arbiter's start, the table's version), None until one has arrived. With
    # ``require_table`` a gate that has no table, or no pose to judge a command by, lets no drive command through:
    # a zone the gate has not heard of is one it cannot keep its machine out of.
    require_table: bool = False
    table: tuple[int, int] | None = None
    _old_starts: set[int] = field(default_factory=set)   # starts of the arbiter whose table a later start's replaced
    log: list[GateLogEntry] = field(default_factory=list)
    _grants: dict[str, int] = field(default_factory=dict)     # mode "local": resource -> epoch of the grant notice
    _permits: dict[str, _Permit] = field(default_factory=dict)
    _asked: dict[str, float] = field(default_factory=dict)    # resource -> when last asked
    _inside: dict[str, bool] = field(default_factory=dict)
    _crossing: dict[str, int | None] = field(default_factory=dict)  # authority used for the crossing under way
    _voided_at: dict[str, float] = field(default_factory=dict)  # resource -> when the machine gave it up
    _last_site: float | None = None
    _link_down_since: float | None = None   # the CAP stream to the Site ended at (None: it is up)
    _blocked_reason: str | None = None
    _admitted: dict[str, bool] = field(default_factory=dict)   # inside the zone on authority (or found inside)
    _cmds: list[tuple[float, float, float]] = field(default_factory=list)   # (drive time, v, w) the gate let through
    _v_model: float = 0.0                   # the track speed now (measured when the drive reports it, else modelled)
    _t_model: float | None = None           # the drive time of the last check
    _now_ref: float = 0.0                   # the drive time of the running check
    late_ticks: int = 0                     # checks that came later than period_s after the one before (moving)
    deadman_stops: int = 0                  # times the gate stopped a drive whose command was not renewed in time
    _deadman: Any = None                    # the timer of the command last let through

    # ------------------------------------------------------------------ inputs
    def set_zones(self, zones: list[Zone]) -> None:
        for z in zones:
            if self.machine_id in z.compatible:
                continue                 # the loader is not gated by its own bay
            self.zones[z.resource_id] = z

    def set_table(self, boot: int, version: int, zones: list[Zone]) -> None:
        """The Site's zone table: it replaces what the gate knew (a zone no longer in it is no longer enforced). An
        earlier table of the same start, and any table of a start whose table was already replaced, is an old message
        and is dropped. (A restarted arbiter that has lost its zones sends no table while a permit of its earlier
        start may still be good: ``OccupancyArbiter.table_withheld``.)"""
        boot, version = int(boot), int(version)
        if boot in self._old_starts:
            return
        if self.table is not None and self.table[0] == boot and version < self.table[1]:
            return
        if self.table is not None and self.table[0] != boot:
            self._old_starts.add(self.table[0])
        fresh = {z.resource_id: z for z in zones if self.machine_id not in z.compatible}
        for rid in list(self.zones):
            if rid not in fresh:
                del self.zones[rid]
        self.zones.update(fresh)
        if self.table != (boot, version):
            self.table = (boot, version)
            self.log.append(GateLogEntry(t_mono=self.clock(), t_wall=time.time(), event="table", resource_id="",
                                         epoch=version, reason=f"start {boot:08x}"))

    def table_tag(self) -> str:
        """What the machine reports in its heartbeat: the table its gate enforces ("" before the first)."""
        return "" if self.table is None else f"{self.table[0]}:{self.table[1]}"

    def note_site_message(self) -> None:
        self._last_site = self.clock()
        self._link_down_since = None

    def note_link_down(self) -> None:
        """The CAP stream to the Site ended (the agent connects again; a Site frame says it is back)."""
        if self._link_down_since is None:
            self._link_down_since = self.clock()

    def note_grant(self, resource_id: str, epoch: int = 0) -> None:
        """mode "local": a grant notice for the zone (the coordinator's dispatch or a GRANTED status)."""
        self._grants[resource_id] = int(epoch)

    def note_permit(self, resource_id: str, epoch: int, t_req: float) -> None:
        """mode "permit": the Site granted an entry permit to the request SENT at ``t_req`` (this machine's clock)."""
        if float(t_req) <= self._voided_at.get(resource_id, -math.inf):
            self._record("void", resource_id, epoch, reason="permit for a request sent before the zone was given up")
            return                       # a late answer to a request made before the release: discarded
        self._permits[resource_id] = _Permit(epoch=int(epoch), t_req=float(t_req))
        self._record("permit", resource_id, epoch)

    def void(self, resource_id: str | None = None, *, reason: str = "") -> None:
        """The machine gave the zone up (release or hand-over): its grant notice and permit for it are void, and a
        permit for it that arrives later is discarded (the caller drops the late answer)."""
        keys = [resource_id] if resource_id is not None else list({*self._grants, *self._permits})
        t = self.clock()
        for k in keys:
            self._grants.pop(k, None)
            self._permits.pop(k, None)
            self._voided_at[k] = t
            self._record("void", k, reason=reason)

    def revoke(self, resource_id: str, epoch: int = 0, *, reason: str = "") -> None:
        """The Site says the grant ``epoch`` of the zone is no longer this machine's (the coordinator released it on
        the machine's behalf, handed it over, or the arbiter expired it). The notice or permit of that grant is
        void; one of a newer grant is kept (the notice of an old grant's end may arrive after the next grant)."""
        epoch = int(epoch or 0)
        if self.mode == "local":
            cur = self._grants.get(resource_id)
            if cur is None or (epoch and cur and int(cur) != epoch):
                return
            self._grants.pop(resource_id, None)
            self._record("void", resource_id, epoch, reason=reason or "revoked by the Site")
        elif self.mode == "permit":
            cur = self._permits.get(resource_id)
            if cur is None or (epoch and cur.epoch != epoch):
                return
            self._permits.pop(resource_id, None)
            self._record("void", resource_id, epoch, reason=reason or "revoked by the Site")

    def note_stop(self) -> None:
        """A stop was written to the drive by someone else (a skill at its end): nothing is left to watch."""
        if self._deadman is not None:
            self._deadman.cancel()
            self._deadman = None
        self._cmds.append((self._drive_time(), 0.0, 0.0))

    # ------------------------------------------------------------------ permit time (A4)
    def permit_remaining_s(self, resource_id: str) -> float:
        p = self._permits.get(resource_id)
        if p is None:
            return -math.inf
        return p.t_req + self.permit_ttl_s * (1.0 - self.rho) - self.clock()

    def permit_live(self, resource_id: str) -> bool:
        return self.permit_remaining_s(resource_id) > 0.0

    def may_start_entry(self, resource_id: str) -> bool:
        if self.mode == "none":
            return True
        if self.mode == "local":
            return resource_id in self._grants
        return self.permit_remaining_s(resource_id) >= self.delta_entry_s * (1.0 + self.rho)

    def _link_silent(self) -> bool:
        """The link has been down longer than the operating lease (the Site sends nothing periodically, so silence is
        judged from the stream's state, not from the time since the last frame)."""
        if self._link_down_since is None:
            return False
        return self.clock() - self._link_down_since > self.link_lease_s * (1.0 - self.rho)

    # ------------------------------------------------------------------ the drive's model (A6)
    _STEP_S = 0.05
    _SCALES = (1.0, 0.5, 0.25, 0.125)

    def _period(self) -> float:
        return float(self.period_s) if self.period_s is not None else float(self.horizon_s)

    def _drive_time(self) -> float:
        return float(self.drive_clock()) if self.drive_clock is not None else self.clock() * self.time_scale

    def _advance_model(self, now: float, measured_speed: float | None = None) -> None:
        """Bring the track speed up to the drive time ``now``: modelled under the commands the gate let through, or
        the drive's own report (unsigned: it takes the sign of the model or of the command in force)."""
        if self._t_model is None:
            self._t_model = now
            return
        elapsed = max(0.0, now - self._t_model)
        moving = (abs(measured_speed) > 1e-3 if measured_speed is not None
                  else abs(self._v_model) > 1e-6 or any(abs(c[1]) > 1e-6 for c in self._cmds))
        if elapsed > self._period() + 1e-9 and moving:      # the drive ran unchecked for longer than the bound (A6)
            self.late_ticks += 1
            if self.late_ticks <= 20:
                self._record("late", "", reason=f"{elapsed:.2f} s since the last check (bound {self._period():.2f} s)")
        t = 0.0
        while t < elapsed - 1e-12:
            h = min(self._STEP_S, elapsed - t)
            v_t, _w = self._in_force(t - elapsed)
            self._v_model = self._follow(self._v_model, v_t, h)
            t += h
        self._t_model = now
        # keep the commands still on their way and the one in force
        eff = [(tc, v, w) for tc, v, w in self._cmds if tc - now + self.act_delay_s > 0.0]
        past = [(tc, v, w) for tc, v, w in self._cmds if tc - now + self.act_delay_s <= 0.0]
        self._cmds = past[-1:] + eff
        if measured_speed is not None:
            sign = (1.0 if self._v_model > 1e-6 else -1.0 if self._v_model < -1e-6
                    else -1.0 if self._in_force(0.0)[0] < 0 else 1.0)
            self._v_model = sign * abs(float(measured_speed))
            if abs(self._v_model) <= 1e-3 and not eff and elapsed > self._period():
                self._cmds = []          # at rest with nothing on its way: whatever stopped it, no command is in force

    def _in_force(self, now_rel: float) -> tuple[float, float]:
        """The command in force ``now_rel`` drive seconds from the running check (``_now_ref``)."""
        cur = (0.0, 0.0)
        for tc, v, w in self._cmds:
            if tc - self._now_ref + self.act_delay_s <= now_rel + 1e-12:
                cur = (v, w)
            else:
                break
        return cur

    def _follow(self, v: float, target: float, h: float) -> float:
        if self.decel_mps2 <= 0.0:
            return target
        return v + max(-self.decel_mps2 * h, min(self.decel_mps2 * h, target - v))

    def _stop_path(self, pose: tuple[float, float, float], v_cmd: float, w_cmd: float) -> list[tuple[float, float]]:
        """Every point the machine can be at if (v_cmd, w_cmd) is written now and a stop at the next check: the
        commands on their way run out, then this one until the stop takes effect, then the braking run; plus the
        braking run from each point on the way (the same path driven slower stops earlier, along its tangent)."""
        x, y, yaw = pose
        v = self._v_model
        t_stop = self._period() + self.act_delay_s          # when a stop written at the next check takes effect
        pts: list[tuple[float, float]] = []
        t = 0.0
        while True:
            if t >= t_stop - 1e-12:
                tv, tw = 0.0, 0.0
            elif t >= self.act_delay_s - 1e-12:
                tv, tw = v_cmd, w_cmd
            else:
                tv, tw = self._in_force(t)
            h = self._STEP_S
            v = self._follow(v, tv, h)
            x += v * math.cos(yaw) * h
            y += v * math.sin(yaw) * h
            yaw += tw * h
            pts.append((x, y))
            if self.decel_mps2 > 0.0 and abs(v) > 1e-6:      # the braking run from here, along the heading
                run = v * v / (2.0 * self.decel_mps2)
                n = max(1, math.ceil(run / 0.1))
                sgn = 1.0 if v > 0 else -1.0
                for k in range(1, n + 1):
                    d = sgn * run * k / n
                    pts.append((x + d * math.cos(yaw), y + d * math.sin(yaw)))
            t += h
            if t >= t_stop - 1e-12 and abs(v) <= 1e-6:
                break
            if t > t_stop + 60.0:                             # never (a guard against a bad parameter)
                break
        return pts

    # ------------------------------------------------------------------ the check
    def _refusal(self, pose: tuple[float, float, float], v: float, w: float, *, log_admit: bool) -> str | None:
        """Why (v, w) must not be written at ``pose`` (None: it may)."""
        x, y, _yaw = pose
        pts = self._stop_path(pose, v, w)
        reason = None
        for rid, z in self.zones.items():
            d_now = z.distance(x, y)
            d_min = min((z.distance(px, py) for px, py in pts), default=d_now)
            if not self._inside.get(rid, d_now < z.radius_m):    # outside
                if d_min >= z.radius_m + self.margin_m:
                    continue                                     # the path keeps clear of the zone
                if d_now < z.radius_m + self.margin_m and d_min >= d_now - 1e-3:
                    continue                                     # in the margin band, not getting closer
                if self.may_start_entry(rid):
                    if d_min < z.radius_m and log_admit and rid not in self._crossing:
                        used = (self._permits[rid].epoch if self.mode == "permit" else self._grants.get(rid))
                        self._crossing[rid] = used               # the gate's decision: logged once per crossing
                        self._record("admit", rid, used)
                    continue
                reason = f"entry gate: no live authority for {rid}"
            elif not self._admitted.get(rid, True):              # inside a zone it was not admitted to
                if d_min < d_now - 1e-3:
                    reason = f"entry gate: inside {rid} without authority - exit only"
            elif self.mode == "permit" and self._link_silent() and d_min < d_now - 1e-3:
                reason = f"entry gate: the Site is silent inside {rid} - exit only"
        return reason

    def _track(self, pose: tuple[float, float, float]) -> None:
        """Crossings since the last check (an entry consumes the permit it used)."""
        x, y, _yaw = pose
        for rid, z in self.zones.items():
            was_inside = self._inside.get(rid)
            d = z.distance(x, y)
            # in at the edge; out only once clear of it (a body leaving along the edge is not out and in again - the
            # arbiter does not count it out before its envelope has left either)
            inside = d < z.radius_m + (self.exit_hysteresis_m if was_inside else 0.0)
            if was_inside is None:
                self._admitted[rid] = True                       # found inside (or outside) at the first look
            elif inside and not was_inside:
                admitted = rid in self._crossing or self.may_start_entry(rid)   # admitted, or authority in hand
                used = self._crossing.pop(rid, None)
                if used is None and admitted:
                    used = (self._permits[rid].epoch if self.mode == "permit" and rid in self._permits
                            else self._grants.get(rid))
                self._admitted[rid] = admitted
                self._record("enter", rid, used, reason="" if admitted else "not admitted")
                if self.mode == "permit":
                    self._permits.pop(rid, None)                 # single use
            elif was_inside and not inside:
                self._record("exit", rid)
                self._crossing.pop(rid, None)
                self._admitted[rid] = True
            self._inside[rid] = inside

    def check(self, pose: tuple[float, ...] | None,
              proposed: tuple[float, float] | None = None) -> tuple[str | None, float | None]:
        """(why the command as proposed must not be written, the speed it may be written at): (None, None) lets it
        through as it is, (reason, 0.0) stops the drive, (reason, cap) lets it through scaled down to ``cap``.
        ``pose`` is (x, y, yaw) or (x, y, yaw, track speed) when the drive reports its speed."""
        if self.mode == "none":
            self._blocked_reason = None
            return None, None
        if self.require_table and (self.table is None or (self.zones and pose is None)):
            # fail closed: nothing is known of the zones, or of where the machine is among them
            if proposed is None or (abs(float(proposed[0])) < 1e-9 and abs(float(proposed[1])) < 1e-9):
                return None, None
            why = ("entry gate: no zone table from the Site yet" if self.table is None
                   else "entry gate: no pose to check the zones against")
            if self._blocked_reason != why:
                self.log.append(GateLogEntry(t_mono=self.clock(), t_wall=time.time(), event="block",
                                             resource_id="", reason=why))
            self._blocked_reason = why
            return why, 0.0
        if not self.zones or pose is None:
            self._blocked_reason = None
            return None, None
        measured = float(pose[3]) if len(pose) > 3 and pose[3] is not None else None
        pose = (float(pose[0]), float(pose[1]), float(pose[2]))
        now = self._drive_time()
        self._now_ref = now
        self._advance_model(now, measured)
        self._track(pose)
        if proposed is None:                                     # no command to judge: bookkeeping only
            return None, None
        v, w = float(proposed[0]), float(proposed[1])
        reason = self._refusal(pose, v, w, log_admit=True)
        cap: float | None = None
        if reason is not None:
            cap = 0.0
            for r in self._SCALES[1:]:
                if abs(v) * r < 0.02:
                    break
                if self._refusal(pose, v * r, w * r, log_admit=False) is None:
                    cap = abs(v) * r
                    break
            if self.mode == "permit":
                for rid in self.zones:
                    if rid in reason and "no live authority" in reason:
                        self._ask(rid)
        held = reason if cap == 0.0 else None
        if held is not None and held != self._blocked_reason:
            self._record("block", held.split(" - ")[0].split()[-1], reason=held)
        self._blocked_reason = held
        scale = 1.0 if cap is None else (cap / abs(v) if abs(v) > 1e-9 else 0.0)
        self._cmds.append((now, v * scale, w * scale))
        self._arm_deadman(v * scale, w * scale)
        return reason, cap

    # ------------------------------------------------------------------ the dead-man's timer
    def _arm_deadman(self, v: float, w: float) -> None:
        """A command let through is good until the next check; one that is not renewed within the bound is stopped."""
        if self._deadman is not None:
            self._deadman.cancel()
            self._deadman = None
        if self.stop_drive is None or (abs(v) < 1e-9 and abs(w) < 1e-9):
            return
        try:
            import asyncio
            loop = asyncio.get_running_loop()
        except RuntimeError:                 # no loop (a synchronous caller): no timer
            return
        self._deadman = loop.call_later(self._period() / max(self.time_scale, 1e-9), self._deadman_fire)

    def _deadman_fire(self) -> None:
        self._deadman = None
        self.deadman_stops += 1
        if self.deadman_stops <= 50:
            self._record("deadman", "", reason=f"no check within {self._period():.2f} s of a command: the drive stopped")
        self._cmds.append((self._drive_time(), 0.0, 0.0))
        try:
            if self.stop_drive is not None:
                self.stop_drive()
        except Exception:  # noqa: BLE001 - the timer must not raise into the loop
            pass

    def blocks(self, pose: tuple[float, ...] | None,
               proposed: tuple[float, float] | None = None) -> str | None:
        """Why the drive command ``proposed`` = (v, w) at ``pose`` = (x, y, yaw) must not be written as it is (None:
        it may). Also tracks crossings."""
        return self.check(pose, proposed)[0]

    def _ask(self, resource_id: str) -> None:
        t = self.clock()
        if self.request_permit is None or t - self._asked.get(resource_id, -math.inf) < self.resend_s:
            return
        self._asked[resource_id] = t
        self._record("request", resource_id)
        self.request_permit(resource_id)

    def speed_cap(self, pose_fn: Callable[[], tuple[float, ...] | None],
                  inner: Callable[..., Any] | None = None) -> Callable[..., Any]:
        """A HAL speed-cap hook (``hal.speed_cap_mps``) that refuses gated commands and otherwise defers to
        ``inner`` (the safety monitor's cap): 0.0 stops the drive, None leaves it uncapped."""
        def cap(proposed=None):
            v_w = None
            if proposed is not None:
                v_w = (float(proposed[0]), float(proposed[1]))
            _reason, own = self.check(pose_fn(), v_w)
            if own is not None and own <= 0.0:
                return 0.0
            if inner is None:
                return own
            try:
                other = inner(proposed=proposed)
            except TypeError:
                other = inner()
            if own is None:
                return other
            return own if other is None else min(own, float(other))
        return cap

    @property
    def hold_reason(self) -> str | None:
        return self._blocked_reason

    def _record(self, event: str, resource_id: str, epoch: int | None = None, *, reason: str = "") -> None:
        self.log.append(GateLogEntry(t_mono=self.clock(), t_wall=time.time(), event=event, resource_id=resource_id,
                                     epoch=epoch, reason=reason))


def gate_from_env(machine_id: str, **kw: Any) -> EntryGate | None:
    """The gate of the scheme in CAP_RESERVATION_SCHEME (None when unset: no gate)."""
    from cap_coordination_kit.occupancy import Scheme

    scheme = Scheme.from_env()
    if scheme is None:
        return None
    import os
    return EntryGate(machine_id=machine_id, mode=scheme.gate, permit_ttl_s=scheme.permit_ttl_s, rho=scheme.rho,
                     link_lease_s=scheme.lease_ttl_s,
                     delta_entry_s=float(os.environ.get("CAP_ENTRY_DELTA_S", "3.0")),
                     horizon_s=float(os.environ.get("CAP_ENTRY_HORIZON_S", "1.5")),
                     act_delay_s=float(os.environ.get("CAP_ENTRY_ACT_DELAY_S", "0.0")),
                     decel_mps2=float(os.environ.get("CAP_ENTRY_DECEL_MPS2", "0.0")),
                     period_s=(float(os.environ["CAP_ENTRY_PERIOD_S"]) if os.environ.get("CAP_ENTRY_PERIOD_S")
                               else None),
                     time_scale=float(os.environ.get("CAP_ENTRY_TIME_SCALE", "1.0")),
                     margin_m=float(os.environ.get("CAP_ENTRY_MARGIN_M", "0.1")),
                     require_table=os.environ.get("CAP_ENTRY_REQUIRE_TABLE", "1") != "0",
                     resend_s=min(1.0, scheme.permit_ttl_s / 8.0), **kw)
