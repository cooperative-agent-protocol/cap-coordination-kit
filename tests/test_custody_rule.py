"""The custody rule and the vacancy confirmed after k looks: the arbiter's behaviour, and the scheme comparison's
scenario that the rule was added for (a bay given up for a carrier that holds an unused permit and cannot be told)."""

from __future__ import annotations

from cap.v0.core import events_pb2, site_agent_pb2

from cap_coordination_kit.occupancy import PERMIT_REASON, OccupancyArbiter, Scheme, VacancyEnvelope, Zone
from cap_coordination_kit.scheme_trial import run_trial

GRANTED = events_pb2.ReservationStatus.RESERVATION_STATE_GRANTED
BAY = "loading:loader"
ZONE = Zone(BAY, 0.0, 0.0, 9.0, frozenset({"loader"}))


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def req(m: str, rid: str, *, permit: bool = False) -> site_agent_pb2.ReservationRequest:
    return site_agent_pb2.ReservationRequest(reservation_id=rid, resource_id=BAY, holder_id=m,
                                             reason=PERMIT_REASON if permit else "")


def rel(rid: str) -> site_agent_pb2.ReservationRelease:
    return site_agent_pb2.ReservationRelease(reservation_id=rid, reason="done")


def arbiter(clock: Clock, **scheme_kw) -> OccupancyArbiter:
    a = OccupancyArbiter(Scheme.named("cap", permit_ttl_s=10.0, rho=0.01, lease_ttl_s=5.0, **scheme_kw), clock=clock,
                         boot_id=7, envelope=VacancyEnvelope(eps_m=0.5, v_max_mps=1.0, latency_s=0.2, d_brake_m=1.0,
                                                            max_pose_age_s=1.0))
    a.set_zone(ZONE)
    for m in ("c1", "c2"):
        a.note_machine(m)
        a.acknowledge_table(m, 7, a.table_version)
    return a


def test_a_machine_given_up_with_an_unused_permit_stays_in_custody_until_the_permit_is_no_longer_counted():
    c = Clock()
    a = arbiter(c)
    assert a.request(req("c1", "r1")).state == GRANTED
    assert a.request(req("c1", "p1", permit=True)).state == GRANTED
    a.release(rel("r1"))                                       # given up for it; its gate is not told
    a.observe({"c1": ((40.0, 0.0), 0.0)})                      # seen outside - and still able to come in
    assert "c1" in a.custody_of(BAY)
    c.advance(5.0)
    a.tick()
    a.observe({"c1": ((3.0, 0.0), 0.0)})                       # it drives in on its permit: still accounted for
    c.advance(6.0)
    a.tick()
    a.observe({"c1": ((3.0, 0.0), 0.0)})
    assert "c1" in a.custody_of(BAY)
    assert a.request(req("c2", "r2")).state != GRANTED          # nobody is let in onto it
    a.observe({"c1": ((40.0, 0.0), 0.0)})                      # out, and no permit of it left: the bay passes on
    assert "c1" not in a.custody_of(BAY)


def test_without_the_rule_the_machine_leaves_custody_while_its_permit_is_counted():
    c = Clock()
    a = arbiter(c, custody_until_lapse=False)
    assert a.request(req("c1", "r1")).state == GRANTED
    assert a.request(req("c1", "p1", permit=True)).state == GRANTED
    a.release(rel("r1"))
    a.observe({"c1": ((40.0, 0.0), 0.0)})
    assert "c1" not in a.custody_of(BAY)


def test_a_vacancy_takes_k_looks_on_newer_reports():
    c = Clock()
    a = OccupancyArbiter(Scheme.named("cap", permit_ttl_s=10.0, rho=0.01, lease_ttl_s=5.0), clock=c, boot_id=7,
                         envelope=VacancyEnvelope(eps_m=0.5, v_max_mps=1.0, latency_s=0.2, d_brake_m=1.0,
                                                  max_pose_age_s=1.0, looks=3))
    a.set_zone(ZONE)
    assert a.request(req("c1", "r1")).state == GRANTED
    a.release(rel("r1"))
    out, inside = {"c1": ((40.0, 0.0), 0.0)}, {"c1": ((3.0, 0.0), 0.0)}
    a.observe(out)
    c.advance(0.2)
    a.observe(out)
    a.observe(out)                                             # the same report again: not a look
    assert a.custody_of(BAY) == {"c1"}
    c.advance(0.2)
    a.observe(inside)                                          # a look that shows it inside starts the count again
    for _ in range(2):
        c.advance(0.2)
        a.observe(out)
    assert a.custody_of(BAY) == {"c1"}
    c.advance(0.2)
    a.observe(out)
    assert a.custody_of(BAY) == set()


def test_the_scenario_the_rule_was_added_for():
    for scheme in ("lease_fenced_vacate", "cap"):
        safe = run_trial(scheme, "released_unused")
        assert not safe.S1 and not safe.S2 and safe.conforming, (scheme, safe.notes)
        unsafe = run_trial(scheme, "released_unused", custody_until_lapse=False)
        assert unsafe.S2, (scheme, unsafe.notes)
        assert not unsafe.conforming and "VACATED" in unsafe.notes[-1]     # the records show the rule broken
