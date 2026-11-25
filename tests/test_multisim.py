"""M6.5: A·B·C 통합 시뮬레이션. 노드마다 다른 시계, 네트워크 고장, 노드 재시작.

안전 불변식(check_invariant): 사람이 있는(또는 노드가 꺼져 모르는) 동안 C가 EMPTY를 내지 않는다.
"""

import random

import pytest

from psafe.core.config import parse_config, parse_hub_config
from psafe.core.verdict import Verdict, VerdictKind as K
from psafe.sim import LinkFault, MultiSim, SimResult, check_invariant, reaction_bound
from psafe.sources.scenario import parse_scenario

A, B, C = ("A", 5006), ("B", 5007), ("C", 5005)
SKEW = {"A": 37.0, "B": -500.0, "hub": 12345.0}       # 파이마다 다른 시계 (E-17)


def node(name, timeline, fps=15):
    cfg = parse_config({
        "source": {"name": "dummy", "options": {"scenario": "unused", "width": 64, "height": 48}},
        "model": {"name": "dummy", "options": {}},
        "outputs": [],
        "runner": {"mode": "threaded"},
        "link": {"node": name, "hub": "C:5005", "bind": f"{name}:{5006 if name == 'A' else 5007}"},
    })
    cfg.source.options["scenario"] = parse_scenario({"fps": fps, "timeline": timeline})
    return cfg


def hub():
    return parse_hub_config({"hub": {
        "bind": "C:5005",
        "nodes": [{"name": "A", "address": "A:5006"}, {"name": "B", "address": "B:5007"}],
    }, "outputs": []})


def empty(sec):
    return [{"from": 0, "to": sec, "objects": []}]


def person_between(t0, t1, sec):
    return [{"from": 0, "to": t0, "objects": []}, {"from": t0, "to": t1, "objects": ["person"]},
            {"from": t1, "to": sec, "objects": []}]


def run(nodes, offsets=None, seed=0, seconds=30.0, setup=None, max_delay=0.0):
    h = hub()
    sim = MultiSim(nodes, h, offsets=offsets, seed=seed)
    if setup:
        setup(sim)
    res = sim.run(seconds)
    bad = check_invariant(res, nodes, reaction_bound(nodes, h, max_delay))
    return res, bad


def changes(res: SimResult):
    """판정이 바뀐 지점만 (시각, kind, reason)."""
    out, last = [], None
    for t, v in res.verdicts:
        if (v.kind, v.reason) != last:
            out.append((round(t, 2), v.kind, v.reason))
            last = (v.kind, v.reason)
    return out


# ---- 기본 흐름 ---------------------------------------------------------------

def test_basic_flow_with_skewed_clocks():
    nodes = {"A": node("A", person_between(10, 14, 30)), "B": node("B", empty(30))}
    res, bad = run(nodes, offsets=SKEW)
    assert bad == []
    first_empty = res.first(K.EMPTY)
    assert 3.0 < first_empty < 5.0                                   # FSM 3초 + 확인
    occ = res.first(K.OCCUPIED)
    assert 10.0 <= occ <= 10.0 + reaction_bound(nodes, hub())        # 사람 등장 -> 빠르게
    again = res.first(K.EMPTY, after=occ)
    assert 14.0 + 3.0 < again < 14.0 + 5.0                           # 퇴장 + 3초 + 확인
    # 사람이 있는 동안은 계속 OCCUPIED
    assert all(v.kind == K.OCCUPIED for t, v in res.verdicts if occ <= t < 14.0)


def test_clock_offsets_do_not_change_results():
    """시각을 보드끼리 비교하는 코드가 있다면 오프셋에 따라 결과가 달라진다 (E-17).
    네트워크 고장을 섞은 상태에서도 판정 순서가 완전히 같아야 한다."""
    def setup(sim):
        for a, b in ((A, C), (B, C), (C, A), (C, B)):
            sim.net.set_fault(a, b, LinkFault(loss=0.15, duplicate=0.1, corrupt=0.05, delay_s=0.03, jitter_s=0.05))
    nodes = {"A": node("A", person_between(8, 11, 25)), "B": node("B", person_between(15, 16, 25))}
    base, bad0 = run(nodes, offsets=None, seed=5, seconds=25, setup=setup, max_delay=0.08)
    for offsets in (SKEW, {"A": -9999.5, "B": 0.001, "hub": -3.3}, {"hub": 86400.0}):
        other, bad = run(nodes, offsets=offsets, seed=5, seconds=25, setup=setup, max_delay=0.08)
        assert bad == bad0 == []
        assert other.kinds() == base.kinds(), f"offsets {offsets} changed the verdicts"


# ---- 네트워크 고장 -------------------------------------------------------------

def test_lossy_network_keeps_invariant_and_still_reaches_empty():
    def setup(sim):
        for a, b in ((A, C), (B, C), (C, A), (C, B)):
            sim.net.set_fault(a, b, LinkFault(loss=0.2, duplicate=0.2, corrupt=0.1, delay_s=0.05, jitter_s=0.1))
    nodes = {"A": node("A", person_between(10, 12, 40)), "B": node("B", empty(40))}
    res, bad = run(nodes, offsets=SKEW, seed=3, seconds=40, setup=setup, max_delay=0.15)
    assert bad == []
    assert res.first(K.EMPTY) is not None                            # 가용성: 유실 20%에서도 확인 가능
    assert res.first(K.EMPTY, after=12.0) is not None
    assert res.first(K.OCCUPIED) is not None


def test_person_enters_while_link_is_cut():
    """A↔C 단절(8~12초) 동안 A 구역에 사람이 왔다 갔다(9~10초). C는 그동안 판단 불가,
    재연결 후에도 A가 다시 비었음을 확인하기 전(10 + 3초)에는 EMPTY가 아니다."""
    def setup(sim):
        sim.at(8.0, lambda s: s.net.cut(A, C), "cut A-C")
        sim.at(12.0, lambda s: s.net.heal(A, C), "heal A-C")
    nodes = {"A": node("A", person_between(9, 10, 25)), "B": node("B", empty(25))}
    res, bad = run(nodes, offsets=SKEW, setup=setup, seconds=25)
    assert bad == []
    during = [v for t, v in res.verdicts if 8.8 <= t < 12.0]
    assert during and all(v.kind == K.UNKNOWN and v.reason == "A_timeout" for v in during)
    # 단절 직후(반응 지연 안쪽)의 EMPTY는 단절 전에 받은 보고로 낸 것이다
    assert res.first(K.EMPTY, after=8.0 + reaction_bound(nodes, hub())) > 13.0


def test_delay_is_counted_in_evidence_age():
    """A→C 지연 0.9초: C가 보는 근거 나이에 지연이 들어간다 (E-21). 실제 근거 나이 이상이어야 한다."""
    def setup(sim):
        sim.net.set_fault(A, C, LinkFault(delay_s=0.9))
    nodes = {"A": node("A", empty(15)), "B": node("B", empty(15))}
    res, bad = run(nodes, offsets=SKEW, setup=setup, seconds=15, max_delay=0.9)
    assert bad == []
    ages = [v.nodes[0].evidence_age_ms for t, v in res.verdicts if t > 5 and v.nodes[0].evidence_age_ms is not None]
    assert ages and min(ages) >= 900.0


def test_long_delay_is_stale_not_empty():
    """A→C 지연 1.2초: 근거 나이가 1000 ms를 넘으므로 EMPTY가 아니라 판단 불가(A_stale)."""
    def setup(sim):
        sim.net.set_fault(A, C, LinkFault(delay_s=1.2))
    nodes = {"A": node("A", empty(15)), "B": node("B", empty(15))}
    res, bad = run(nodes, offsets=SKEW, setup=setup, seconds=15, max_delay=1.2)
    assert bad == []
    assert res.first(K.EMPTY) is None
    assert any(v.reason == "A_stale" for _, v in res.verdicts)


def test_big_delay_with_person_is_not_unsafe():
    """E-21 회귀: v1에서는 A→C 지연 2.5초일 때 사람이 있는 동안(10~14초) C가 EMPTY를 13번 냈다.
    반응 지연 상한은 지연이 없는 LAN 기준으로 엄격하게 잡는다."""
    def setup(sim):
        sim.net.set_fault(A, C, LinkFault(delay_s=2.5))
    nodes = {"A": node("A", person_between(10, 14, 25)), "B": node("B", empty(25))}
    res, bad = run(nodes, offsets=SKEW, setup=setup, seconds=25, max_delay=0.0)
    assert bad == []
    assert not any(v.kind == K.EMPTY for t, v in res.verdicts if 10.0 <= t < 14.0)


# ---- 노드 고장 ---------------------------------------------------------------

def test_node_kill_and_reboot():
    def setup(sim):
        sim.at(8.0, lambda s: s.kill("B"), "kill B")
        sim.at(10.0, lambda s: s.boot("B"), "boot B")
    nodes = {"A": node("A", empty(30)), "B": node("B", empty(30))}
    res, bad = run(nodes, offsets=SKEW, setup=setup, seconds=25)
    assert bad == []
    timeout_at = next(t for t, v in res.verdicts if t >= 8.0 and v.reason == "B_timeout")
    assert 8.0 < timeout_at <= 8.0 + 0.5 + 0.2 + 0.2 + 0.01         # 무응답 기준 + 하트비트 2개
    after_kill = 8.0 + reaction_bound(nodes, hub())                  # 종료 직후 EMPTY는 종료 전 보고로 낸 것
    assert res.first(K.EMPTY, after=after_kill) > 10.0 + 3.0         # 재시작 후 처음부터 다시 확인
    assert res.first(K.EMPTY, after=after_kill) < 10.0 + 5.0


def test_normal_shutdown_is_seen_immediately():
    nodes = {"A": node("A", empty(10)), "B": node("B", empty(20))}
    res, bad = run(nodes, offsets=SKEW, seconds=15)
    assert bad == []
    first_fault = next(t for t, v in res.verdicts if v.reason == "A_fault")
    assert 10.0 <= first_fault <= 10.0 + 0.2 + 0.2 + 0.01           # 마지막 shutdown 보고 -> 무응답보다 빠름


# ---- 무작위 ----------------------------------------------------------------

def random_timeline(rng, sec):
    segs, t = [], 0.0
    while t < sec:
        d = rng.uniform(0.5, 5.0)
        seg = {"from": round(t, 3), "to": round(min(sec, t + d), 3),
               "objects": ["person"] if rng.random() < 0.35 else []}
        r = rng.random()
        if r < 0.06:
            seg["camera"] = "blackout"
        elif r < 0.10:
            seg["camera"] = "freeze"
        elif r < 0.14:
            seg["camera"] = "outage"
        segs.append(seg)
        t += d
    return segs


@pytest.mark.parametrize("seed", range(30))
def test_random_faults_never_unsafe(seed):
    rng = random.Random(seed)
    sec = 40.0
    nodes = {"A": node("A", random_timeline(rng, sec)), "B": node("B", random_timeline(rng, sec))}
    offsets = {"A": rng.uniform(-1e4, 1e4), "B": rng.uniform(-1e4, 1e4), "hub": rng.uniform(-1e4, 1e4)}
    max_delay = 0.0

    def setup(sim):
        nonlocal max_delay
        for a, b in ((A, C), (B, C), (C, A), (C, B)):
            f = LinkFault(loss=rng.uniform(0, 0.3), duplicate=rng.uniform(0, 0.2), corrupt=rng.uniform(0, 0.1),
                          delay_s=rng.uniform(0, 0.1), jitter_s=rng.uniform(0, 0.1))
            max_delay = max(max_delay, f.delay_s + f.jitter_s)
            sim.net.set_fault(a, b, f)
        for _ in range(rng.randint(0, 2)):
            t0 = rng.uniform(2, sec - 5)
            pair = rng.choice([(A, C), (B, C)])
            sim.at(t0, lambda s, p=pair: s.net.cut(*p), f"cut {pair}")
            sim.at(t0 + rng.uniform(0.2, 4), lambda s, p=pair: s.net.heal(*p), f"heal {pair}")
        if rng.random() < 0.5:
            who = rng.choice(["A", "B"])
            t0 = rng.uniform(2, sec - 8)
            sim.at(t0, lambda s: s.kill(who), f"kill {who}")
            sim.at(t0 + rng.uniform(0.5, 5), lambda s: s.boot(who), f"boot {who}")

    res, bad = run(nodes, offsets=offsets, seed=seed, seconds=sec, setup=setup, max_delay=0.2)
    assert bad == [], (bad[:3], res.events)
    assert res.verdicts                                              # 실제로 돌았다


# ---- 검사기 자체 -------------------------------------------------------------

def test_invariant_checker_catches_injected_violation():
    """검사기가 실제로 잡는지: 사람이 있는 구간에 가짜 EMPTY를 끼워 넣는다."""
    nodes = {"A": node("A", person_between(5, 15, 20)), "B": node("B", empty(20))}
    h = hub()
    res = MultiSim(nodes, h, seed=1).run(20)
    L = reaction_bound(nodes, h)
    assert check_invariant(res, nodes, L) == []
    fake = Verdict(K.EMPTY, "confirmed", (), None, 0.0)
    res.verdicts.append((10.0, fake))                                # 사람이 있는 한가운데
    assert check_invariant(res, nodes, L)
    res.verdicts[-1] = (5.0 + L / 2, fake)                           # 반응 지연 안쪽은 허용
    assert check_invariant(res, nodes, L) == []
