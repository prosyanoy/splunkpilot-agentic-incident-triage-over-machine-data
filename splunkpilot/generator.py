"""Synthetic log/metric generator with five labeled incident scenarios.

Every run is deterministic for a given (scenario, seed). The generator returns HEC-shaped event
dicts plus a ``GroundTruth`` label that is *never* written to the event store, so the agent can
only discover the cause from the machine data itself.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Callable

from .models import iso

BASE_START = 1768471200.0  # 2026-01-15T10:00:00Z
DURATION = 3600.0  # one hour of data per run

SERVICE_HOSTS: dict[str, list[str]] = {
    "web-frontend": ["web-01", "web-02"],
    "checkout-api": ["api-01", "api-02"],
    "auth-service": ["auth-01"],
    "payments": ["pay-01", "pay-02"],
    "inventory": ["inv-01", "inv-02"],
    "db": ["db-01"],
    "dns-resolver": ["dns-01"],
}
RATE_PER_MIN = {"web-frontend": 18, "checkout-api": 18, "auth-service": 10, "payments": 10,
                "inventory": 10, "db": 8, "dns-resolver": 4}
CATEGORIES = ["bad_deploy", "memory_leak", "dns_failure", "auth_storm", "disk_full"]


@dataclass
class GroundTruth:
    scenario: str
    category: str
    service: str
    host: str
    onset: float
    description: str
    evidence_patterns: list[str]
    alert: dict[str, Any]


@dataclass
class Incident:
    truth: GroundTruth
    events: list[dict[str, Any]]


class Ctx:
    """Mutable generation context handed to scenario builders."""

    def __init__(self, rng: random.Random, index: str, onset: float) -> None:
        self.rng, self.index, self.onset = rng, index, onset
        self.start, self.end = BASE_START, BASE_START + DURATION
        self.events: list[dict[str, Any]] = []
        self.metric_fn: dict[tuple[str, str], Callable[[float], float]] = {}

    def emit(self, ts: float, service: str, host: str, level: str, message: str,
             sourcetype: str = "app", **fields: Any) -> dict[str, Any]:
        ev = {"time": round(ts, 3), "host": host, "source": service, "sourcetype": sourcetype,
              "index": self.index,
              "event": {"service": service, "level": level, "message": message, **fields}}
        self.events.append(ev)
        return ev

    def burst(self, t0: float, t1: float, per_min: float, fn: Callable[[float], None]) -> None:
        """Call ``fn(ts)`` at ~per_min events/minute (Poisson-ish) between t0 and t1."""
        t = t0
        while True:
            t += self.rng.expovariate(per_min / 60.0) if per_min > 0 else 1e9
            if t >= t1:
                return
            fn(t)

    def change(self, ts: float, service: str, kind: str, message: str, **fields: Any) -> dict[str, Any]:
        return self.emit(ts, service, "ci-01", "INFO", message, sourcetype="change",
                         change_type=kind, **fields)


# ------------------------------------------------------------------- baseline

def _commit(rng: random.Random) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(7))


def _normal_message(rng: random.Random, service: str) -> tuple[str, dict[str, Any]]:
    ms = max(3, int(rng.gauss(40, 12)))
    if service == "web-frontend":
        path = rng.choice(["/", "/cart", "/search", "/product/42", "/login"])
        return f"GET {path} 200 {ms}ms", {"status": 200, "latency_ms": ms}
    if service == "checkout-api":
        return f"POST /checkout 200 {ms * 3}ms order=ord_{rng.randint(1000, 99999)}", {"status": 200, "latency_ms": ms * 3}
    if service == "auth-service":
        return (f"token issued user=user_{rng.randint(1, 4000)}",
                {"latency_ms": ms, "ip": f"10.1.{rng.randint(0, 20)}.{rng.randint(2, 250)}"})
    if service == "payments":
        return (f"charge authorized order=ord_{rng.randint(1000, 99999)} amount={rng.randint(5, 400)}.00",
                {"latency_ms": ms * 2, "version": "2.3.9"})
    if service == "inventory":
        return f"stock lookup sku=SKU-{rng.randint(100, 999)} qty={rng.randint(0, 80)}", {"latency_ms": ms}
    if service == "db":
        return (f"query ok table={rng.choice(['orders', 'users', 'stock'])} rows={rng.randint(1, 200)} duration={ms}ms",
                {"latency_ms": ms})
    return f"resolved name={rng.choice(['payments', 'db', 'auth', 'inventory'])}.internal ttl=300", {}


_NOISE_ERRORS = {
    "web-frontend": "upstream timeout after 3000ms",
    "checkout-api": "retrying request after transient connection reset",
    "auth-service": "token refresh failed: client disconnected",
    "payments": "idempotency key replayed, ignoring duplicate",
    "inventory": "cache miss storm on sku lookup, falling back to db",
    "db": "slow query detected duration=1200ms",
    "dns-resolver": "cache eviction under memory pressure",
}

_METRIC_BASE = {"cpu_pct": 28.0, "mem_pct": 52.0, "disk_pct": 41.0}


def _baseline(ctx: Ctx) -> None:
    rng = ctx.rng
    for service, hosts in SERVICE_HOSTS.items():
        for host in hosts:
            def normal(ts: float, service=service, host=host) -> None:
                r = rng.random()
                if r < 0.004:
                    ctx.emit(ts, service, host, "ERROR", _NOISE_ERRORS[service])
                elif r < 0.014:
                    ms = rng.randint(400, 900)
                    ctx.emit(ts, service, host, "WARN", f"slow request {ms}ms", latency_ms=ms)
                else:
                    msg, f = _normal_message(rng, service)
                    ctx.emit(ts, service, host, "INFO", msg, **f)

            ctx.burst(ctx.start, ctx.end, RATE_PER_MIN[service], normal)

    # an old, harmless deploy so the change log is never empty
    ctx.change(ctx.start + 300, "web-frontend", "deploy",
               f"deploy web-frontend version=5.8.1 (was 5.8.0) commit={_commit(rng)} by=dana",
               version_to="5.8.1", version_from="5.8.0")


def _metrics(ctx: Ctx) -> None:
    rng = ctx.rng
    for service, hosts in SERVICE_HOSTS.items():
        for host in hosts:
            offsets = {m: rng.uniform(-6, 6) for m in _METRIC_BASE}
            t = ctx.start
            while t < ctx.end:
                for metric, base in _METRIC_BASE.items():
                    fn = ctx.metric_fn.get((host, metric))
                    val = fn(t) if fn else base + offsets[metric] + rng.gauss(0, 1.5)
                    val = round(min(100.0, max(0.0, val)), 1)
                    ctx.emit(t + rng.uniform(0, 5), service, host, "INFO", f"{metric}={val}",
                             sourcetype="metric", metric=metric, value=val)
                t += 60


def _decoy_deploy(ctx: Ctx, avoid: set[str]) -> None:
    """A routine deploy on an unrelated service, 14-20 minutes before the incident."""
    rng = ctx.rng
    service = rng.choice([s for s in ("inventory", "web-frontend", "payments", "checkout-api") if s not in avoid])
    v = rng.randint(1, 9)
    ctx.change(ctx.onset - rng.uniform(14, 20) * 60, service, "deploy",
               f"deploy {service} version=3.{v + 1}.0 (was 3.{v}.0) commit={_commit(rng)} by=erin",
               version_to=f"3.{v + 1}.0", version_from=f"3.{v}.0")


# ------------------------------------------------------------------- scenarios

def _bad_deploy(ctx: Ctx) -> GroundTruth:
    rng, onset = ctx.rng, ctx.onset
    t_deploy = onset - rng.uniform(60, 120)
    sha = _commit(rng)
    ctx.change(t_deploy, "payments", "deploy",
               f"deploy payments version=2.4.1 (was 2.3.9) commit={sha} by=bob",
               version_to="2.4.1", version_from="2.3.9")
    ctx.change(onset - rng.uniform(12, 16) * 60, "inventory", "deploy",
               f"deploy inventory version=1.9.0 (was 1.8.7) commit={_commit(rng)} by=erin",
               version_to="1.9.0", version_from="1.8.7")
    for ev in ctx.events:  # payments log lines carry the running version
        e = ev["event"]
        if e["service"] == "payments" and ev["time"] >= t_deploy and "version" in e:
            e["version"] = "2.4.1"
    for host in SERVICE_HOSTS["payments"]:
        def bad(ts: float, host=host) -> None:
            ms = int(ctx.rng.uniform(900, 2500))
            ctx.emit(ts, "payments", host, "ERROR",
                     "java.lang.NullPointerException at PricingCalculator.applyDiscount(PricingCalculator.java:88)",
                     version="2.4.1", status=500, latency_ms=ms)
        ctx.burst(onset, ctx.end, 9, bad)
    for host in SERVICE_HOSTS["checkout-api"]:
        def down(ts: float, host=host) -> None:
            ctx.emit(ts, "checkout-api", host, "ERROR", "payments call failed: 502 Bad Gateway from payments",
                     status=502, latency_ms=int(ctx.rng.uniform(1500, 3000)))
        ctx.burst(onset, ctx.end, 7, down)
    return GroundTruth(
        "bad_deploy", "bad_deploy", "payments", "", onset,
        "payments v2.4.1 deployed shortly before a NullPointerException wave in PricingCalculator",
        [r"NullPointerException", r"deploy payments version=2\.4\.1"],
        {"title": "checkout-api 5xx rate above 5%", "service": "checkout-api"},
    )


def _memory_leak(ctx: Ctx) -> GroundTruth:
    rng, onset = ctx.rng, ctx.onset
    leak_start = onset - 25 * 60
    ctx.metric_fn[("inv-01", "mem_pct")] = lambda t: (
        52 + rng.gauss(0, 1) if t < leak_start else min(99.8, 52 + (t - leak_start) / (onset + 240 - leak_start) * 47.5))
    _decoy_deploy(ctx, {"inventory", "checkout-api"})
    # growing GC pressure
    def gc(ts: float) -> None:
        frac = min(1.0, max(0.0, (ts - leak_start) / (onset - leak_start)))
        pause = int(200 + 2600 * frac * frac)
        ctx.emit(ts, "inventory", "inv-01", "WARN", f"GC pause {pause}ms heap={int(70 + 28 * frac)}%", gc_pause_ms=pause)
    ctx.burst(leak_start + 600, onset, 2.5, gc)
    def oom(ts: float) -> None:
        ctx.emit(ts, "inventory", "inv-01", "ERROR", "java.lang.OutOfMemoryError: Java heap space")
    ctx.burst(onset, ctx.end, 5, oom)
    t = onset + 20
    while t < ctx.end:
        ctx.emit(t, "inventory", "inv-01", "ERROR", "container inventory killed (OOMKilled), restarting", sourcetype="kubelet")
        t += rng.uniform(150, 260)
    for host in SERVICE_HOSTS["checkout-api"]:
        def slow(ts: float, host=host) -> None:
            ctx.emit(ts, "checkout-api", host, "ERROR", "inventory lookup timeout after 2000ms", latency_ms=2000, status=504)
        ctx.burst(onset, ctx.end, 6, slow)
    return GroundTruth(
        "memory_leak", "memory_leak", "inventory", "inv-01", onset,
        "inventory on inv-01 leaked heap until OutOfMemoryError and OOMKilled restarts",
        [r"OutOfMemoryError", r"OOMKilled", r"GC pause", r"mem_pct"],
        {"title": "checkout-api p95 latency above 2s", "service": "checkout-api"},
    )


def _dns_failure(ctx: Ctx) -> GroundTruth:
    rng, onset = ctx.rng, ctx.onset
    _decoy_deploy(ctx, {"web-frontend"})
    def resolver(ts: float) -> None:
        ctx.emit(ts, "dns-resolver", "dns-01", "ERROR",
                 f"upstream resolver 10.0.0.2 SERVFAIL for {rng.choice(['db', 'payments', 'auth', 'inventory'])}.internal")
    ctx.burst(onset, ctx.end, 10, resolver)
    ctx.burst(onset - 600, onset, 3, lambda ts: ctx.emit(ts, "dns-resolver", "dns-01", "WARN", "upstream resolver 10.0.0.2 slow response 2400ms"))
    for service, rate in (("checkout-api", 8), ("payments", 6), ("inventory", 6), ("auth-service", 5), ("web-frontend", 5)):
        for host in SERVICE_HOSTS[service]:
            def fail(ts: float, service=service, host=host) -> None:
                target = rng.choice(["db", "payments", "auth", "inventory"])
                ctx.emit(ts, service, host, "ERROR",
                         f"java.net.UnknownHostException: {target}.internal: Temporary failure in name resolution")
            ctx.burst(onset + 5, ctx.end, rate, fail)
    return GroundTruth(
        "dns_failure", "dns_failure", "dns-resolver", "dns-01", onset,
        "the internal resolver started returning SERVFAIL; every service failed name resolution",
        [r"SERVFAIL", r"UnknownHostException", r"name resolution"],
        {"title": "web-frontend elevated 5xx", "service": "web-frontend"},
    )


def _auth_storm(ctx: Ctx) -> GroundTruth:
    rng, onset = ctx.rng, ctx.onset
    _decoy_deploy(ctx, {"web-frontend", "checkout-api"})
    attackers = [f"203.0.113.{rng.randint(10, 240)}" for _ in range(3)]
    ctx.metric_fn[("auth-01", "cpu_pct")] = lambda t: (
        28 + rng.gauss(0, 2) if t < onset else min(99.0, 28 + (t - onset) / 120 * 65 + rng.gauss(0, 2)))
    def fail(ts: float) -> None:
        ctx.emit(ts, "auth-service", "auth-01", "WARN",
                 f"login failed user=user_{rng.randint(1, 90000)} ip={rng.choice(attackers)} reason=invalid_credentials",
                 ip=rng.choice(attackers), latency_ms=int(rng.uniform(600, 2500)))
    ctx.burst(onset, ctx.end, 420, fail)
    ctx.burst(onset + 30, ctx.end, 30,
              lambda ts: ctx.emit(ts, "auth-service", "auth-01", "WARN",
                                  f"rate limit exceeded ip={rng.choice(attackers)} limit=100/min"))
    for service in ("web-frontend", "checkout-api"):
        for host in SERVICE_HOSTS[service]:
            def timeout(ts: float, service=service, host=host) -> None:
                ctx.emit(ts, service, host, "ERROR", "auth token validation timeout after 2000ms", status=503)
            ctx.burst(onset + 20, ctx.end, 8, timeout)
    return GroundTruth(
        "auth_storm", "auth_storm", "auth-service", "auth-01", onset,
        "credential-stuffing flood from a few IPs saturated auth-service, starving token validation",
        [r"login failed", r"rate limit exceeded", r"cpu_pct"],
        {"title": "web-frontend login and checkout errors", "service": "web-frontend"},
    )


def _disk_full(ctx: Ctx) -> GroundTruth:
    rng, onset = ctx.rng, ctx.onset
    _decoy_deploy(ctx, {"checkout-api", "payments"})
    fill_start = onset - 30 * 60
    ctx.metric_fn[("db-01", "disk_pct")] = lambda t: (
        41 + rng.gauss(0, 0.5) if t < fill_start else min(100.0, 41 + (t - fill_start) / (onset - fill_start) * 59))
    ctx.emit(fill_start - 600, "db", "db-01", "ERROR", "logrotate job failed exit=1: cannot rotate /var/lib/postgresql/log",
             sourcetype="change", change_type="cron")
    def warn(ts: float) -> None:
        pct = min(99, int(80 + (ts - fill_start) / (onset - fill_start) * 19))
        ctx.emit(ts, "db", "db-01", "WARN", f"disk usage {pct}% on /var/lib/postgresql", disk_pct=pct)
    ctx.burst(fill_start + 900, onset, 1.5, warn)
    def full(ts: float) -> None:
        ctx.emit(ts, "db", "db-01", "ERROR", "PANIC: could not write to file pg_wal/000000010000: No space left on device")
    ctx.burst(onset, ctx.end, 8, full)
    for service, rate in (("checkout-api", 7), ("payments", 6), ("inventory", 4)):
        for host in SERVICE_HOSTS[service]:
            def fail(ts: float, service=service, host=host) -> None:
                ctx.emit(ts, service, host, "ERROR", "database write failed: connection closed by server", status=500)
            ctx.burst(onset + 10, ctx.end, rate, fail)
    return GroundTruth(
        "disk_full", "disk_full", "db", "db-01", onset,
        "db-01 data volume filled after a failed log rotation; Postgres could not write WAL",
        [r"No space left", r"disk usage", r"logrotate", r"disk_pct"],
        {"title": "checkout-api error rate above 10%", "service": "checkout-api"},
    )


SCENARIOS: dict[str, tuple[str, Callable[[Ctx], GroundTruth]]] = {
    "bad_deploy": ("Bad deploy of payments v2.4.1", _bad_deploy),
    "memory_leak": ("Memory leak in inventory", _memory_leak),
    "dns_failure": ("Internal DNS resolver failure", _dns_failure),
    "auth_storm": ("Credential-stuffing auth storm", _auth_storm),
    "disk_full": ("Database disk full", _disk_full),
}


def generate(scenario: str, seed: int = 0, index: str | None = None) -> Incident:
    """Generate one hour of baseline traffic with ``scenario`` injected at ~T+35min."""
    if scenario not in SCENARIOS:
        raise KeyError(f"unknown scenario {scenario!r}; choose from {', '.join(SCENARIOS)}")
    rng = random.Random(f"{scenario}:{seed}")
    onset = BASE_START + 35 * 60 + int(rng.uniform(0, 300))
    ctx = Ctx(rng, index or f"{scenario}_{seed}", onset)
    _baseline(ctx)
    truth = SCENARIOS[scenario][1](ctx)
    _metrics(ctx)
    ctx.events.sort(key=lambda e: e["time"])
    fired = onset + 240
    truth.alert = {
        **truth.alert,
        "fired_at": iso(fired),
        "earliest": iso(fired - 45 * 60),
        "latest": iso(fired + 60),
        "index": ctx.index,
    }
    return Incident(truth, ctx.events)


def scenario_names() -> list[str]:
    return list(SCENARIOS)
