"""A small SPL-like search language executed over the event store.

Supported pipeline::

    search <terms> | where <expr> | stats <aggs> [by f,...] | timechart span=1m <agg> [by f]
                   | top [limit=N] f | sort [N] -f | head N | tail N | table f... | fields [-] f...
                   | dedup f... | rename a as b

Search terms: ``field=value`` (``!=  >  >=  <  <=``, ``*`` wildcards), bare words or "quoted phrases"
(matched against the message), ``AND`` / ``OR`` / ``NOT`` and parentheses, plus inline
``earliest=`` / ``latest=`` time modifiers.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .models import iso
from .store import PUSHDOWN_COLUMNS, EventStore

Row = dict[str, Any]


class SPLError(ValueError):
    pass


# --------------------------------------------------------------------------- time

_REL = re.compile(r"^([+-])?(\d+)\s*(s|m|h|d|w)$")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_time(value: Any, now: float | None = None) -> float | None:
    """Parse epoch seconds, ISO-8601, ``now`` or relative offsets like ``-15m``."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if s.lower() == "now":
        return now if now is not None else time.time()
    m = _REL.match(s)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        base = now if now is not None else time.time()
        return base + sign * int(m.group(2)) * _UNIT[m.group(3)]
    try:
        return float(s)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise SPLError(f"cannot parse time {value!r}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def parse_span(span: str) -> float:
    m = re.fullmatch(r"(\d+)\s*(s|m|h|d)", span.strip())
    if not m:
        raise SPLError(f"invalid span {span!r} (use e.g. 30s, 1m, 1h)")
    n = int(m.group(1)) * _UNIT[m.group(2)]
    if n <= 0:
        raise SPLError("span must be positive")
    return float(n)


# ------------------------------------------------------------------ expressions

_TOKEN_RE = re.compile(
    r"""\s*(?:
      (?P<lp>\()|(?P<rp>\))
      |(?P<field>[A-Za-z_][\w.\-]*)(?P<op>!=|>=|<=|=|>|<)(?P<val>"(?:[^"\\]|\\.)*"|[^\s()]+)
      |(?P<q>"(?:[^"\\]|\\.)*")
      |(?P<word>[^\s()]+)
    )""",
    re.X,
)


@dataclass
class Tok:
    kind: str  # lp rp cmp text kw
    field: str = ""
    op: str = ""
    val: str = ""


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return re.sub(r"\\(.)", r"\1", s[1:-1])
    return s


def tokenize(text: str) -> list[Tok]:
    toks, pos = [], 0
    text = text.rstrip()
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m or m.end() == pos:
            raise SPLError(f"cannot parse near {text[pos:pos + 20]!r}")
        pos = m.end()
        if m.group("lp"):
            toks.append(Tok("lp"))
        elif m.group("rp"):
            toks.append(Tok("rp"))
        elif m.group("field"):
            toks.append(Tok("cmp", m.group("field"), m.group("op"), _unquote(m.group("val"))))
        elif m.group("q"):
            toks.append(Tok("text", val=_unquote(m.group("q"))))
        else:
            w = m.group("word")
            toks.append(Tok("kw", val=w) if w in ("AND", "OR", "NOT") else Tok("text", val=w))
    return toks


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _wild(pattern: str) -> re.Pattern[str]:
    return re.compile(".*".join(re.escape(p) for p in pattern.split("*")), re.I | re.S)


def _cmp_pred(name: str, op: str, val: str) -> Callable[[Row], bool]:
    wild = _wild(val) if "*" in val else None
    nval, lval = _num(val), val.lower()

    def eq(x: Any) -> bool:
        if wild is not None:
            return wild.fullmatch(str(x)) is not None
        nx = _num(x)
        if nx is not None and nval is not None:
            return nx == nval
        return str(x).lower() == lval

    def pred(row: Row) -> bool:
        x = row.get(name)
        if x is None:
            return op == "!="
        if op == "=":
            return eq(x)
        if op == "!=":
            return not eq(x)
        nx = _num(x)
        a, b = (nx, nval) if nx is not None and nval is not None else (str(x).lower(), lval)
        return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op]  # type: ignore[operator]

    return pred


def _text_pred(val: str) -> Callable[[Row], bool]:
    if "*" in val:
        rx = _wild(val)
        return lambda row: rx.search(str(row.get("message", ""))) is not None
    low = val.lower()
    return lambda row: low in str(row.get("message", "")).lower()


class _Parser:
    def __init__(self, toks: list[Tok]) -> None:
        self.toks, self.i = toks, 0
        self.cmps: list[Tok] = []  # top-level AND-ed comparisons (for pushdown)

    def peek(self) -> Tok | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def parse(self) -> Callable[[Row], bool]:
        if not self.toks:
            return lambda row: True
        pred = self.or_expr(top=True)
        if self.i != len(self.toks):
            raise SPLError("unbalanced parentheses")
        return pred

    def or_expr(self, top: bool = False) -> Callable[[Row], bool]:
        parts = [self.and_expr(top)]
        while (t := self.peek()) and t.kind == "kw" and t.val == "OR":
            self.i += 1
            parts.append(self.and_expr(False))
        if len(parts) > 1:
            self.cmps.clear()  # OR at top level voids pushdown
            return lambda row: any(p(row) for p in parts)
        return parts[0]

    def and_expr(self, top: bool) -> Callable[[Row], bool]:
        parts = [self.unary(top)]
        while (t := self.peek()) is not None and t.kind != "rp" and not (t.kind == "kw" and t.val == "OR"):
            if t.kind == "kw" and t.val == "AND":
                self.i += 1
            parts.append(self.unary(top))
        return parts[0] if len(parts) == 1 else (lambda row: all(p(row) for p in parts))

    def unary(self, top: bool) -> Callable[[Row], bool]:
        t = self.peek()
        if t is None:
            raise SPLError("unexpected end of expression")
        if t.kind == "kw" and t.val == "NOT":
            self.i += 1
            inner = self.unary(False)
            return lambda row: not inner(row)
        self.i += 1
        if t.kind == "lp":
            inner = self.or_expr()
            if (c := self.peek()) is None or c.kind != "rp":
                raise SPLError("unbalanced parentheses")
            self.i += 1
            return inner
        if t.kind == "cmp":
            if top and t.op == "=" and "*" not in t.val and t.field in PUSHDOWN_COLUMNS:
                self.cmps.append(t)
            return _cmp_pred(t.field, t.op, t.val)
        if t.kind == "text":
            return _text_pred(t.val)
        raise SPLError(f"unexpected {t.val or t.kind!r}")


def compile_expr(text: str) -> tuple[Callable[[Row], bool], list[Tok]]:
    p = _Parser(tokenize(text))
    return p.parse(), p.cmps


# ---------------------------------------------------------------------- commands

def _sortkey(v: Any) -> tuple[int, Any]:
    n = _num(v)
    return (0, n) if n is not None else (1, str(v).lower())


def _fmt(v: Any) -> Any:
    return round(v, 3) if isinstance(v, float) else v


def _split_list(s: str) -> list[str]:
    return [x for x in re.split(r"[,\s]+", s.strip()) if x]


_AGG_RE = re.compile(r"(\w+)(?:\(([^)]*)\))?(?:\s+as\s+([\w.]+))?", re.I)


@dataclass
class Agg:
    fn: str
    arg: str | None
    name: str


def parse_aggs(text: str) -> list[Agg]:
    aggs = []
    for m in _AGG_RE.finditer(text):
        fn, arg, alias = m.group(1).lower(), (m.group(2) or "").strip() or None, m.group(3)
        if fn not in _AGG_FUNCS:
            raise SPLError(f"unknown aggregation function {fn!r}")
        if fn != "count" and arg is None:
            raise SPLError(f"{fn}() needs a field")
        aggs.append(Agg(fn, arg, alias or (f"{fn}({arg})" if arg else fn)))
    if not aggs:
        raise SPLError("no aggregation given")
    return aggs


def _percentile(vals: list[float], p: float) -> float:
    vals = sorted(vals)
    k = (len(vals) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


_PCT = {"median": 0.5, "p50": 0.5, "p90": 0.9, "p95": 0.95, "p99": 0.99}
_AGG_FUNCS = {"count", "sum", "avg", "min", "max", "dc", "earliest", "latest", *_PCT}


def compute_agg(agg: Agg, rows: list[Row]) -> Any:
    if agg.fn == "count":
        return len(rows) if agg.arg is None else sum(1 for r in rows if r.get(agg.arg) is not None)
    vals = [r[agg.arg] for r in rows if r.get(agg.arg) is not None]
    if agg.fn == "dc":
        return len({str(v) for v in vals})
    if agg.fn in ("earliest", "latest"):
        if not rows:
            return None
        pick = (min if agg.fn == "earliest" else max)(rows, key=lambda r: r.get("_time", 0))
        return pick.get(agg.arg)
    nums = [n for n in map(_num, vals) if n is not None]
    if agg.fn in ("min", "max") and vals and not nums:
        return (min if agg.fn == "min" else max)(str(v) for v in vals)
    if not nums:
        return None
    if agg.fn == "sum":
        return sum(nums)
    if agg.fn == "avg":
        return sum(nums) / len(nums)
    if agg.fn == "min":
        return min(nums)
    if agg.fn == "max":
        return max(nums)
    return _percentile(nums, _PCT[agg.fn])


def _cmd_stats(arg: str, rows: list[Row]) -> list[Row]:
    parts = re.split(r"\s+by\s+", arg.strip(), maxsplit=1, flags=re.I)
    aggs = parse_aggs(parts[0])
    by = _split_list(parts[1]) if len(parts) > 1 else []
    groups: dict[tuple, list[Row]] = {}
    for r in rows:
        if any(r.get(f) is None for f in by):
            continue
        groups.setdefault(tuple(r[f] for f in by), []).append(r)
    if not by and not groups:
        groups[()] = []
    out = []
    for key in sorted(groups, key=lambda k: tuple(_sortkey(x) for x in k)):
        row: Row = dict(zip(by, key))
        for a in aggs:
            row[a.name] = _fmt(compute_agg(a, groups[key]))
        out.append(row)
    return out


MAX_BUCKETS = 2000


def _cmd_timechart(arg: str, rows: list[Row], bounds: tuple[float | None, float | None]) -> list[Row]:
    m = re.search(r"\bspan\s*=\s*(\S+)", arg)
    span = parse_span(m.group(1)) if m else 60.0
    rest = re.sub(r"\bspan\s*=\s*\S+", "", arg).strip()
    parts = re.split(r"\s+by\s+", rest, maxsplit=1, flags=re.I)
    aggs = parse_aggs(parts[0])
    if len(aggs) != 1:
        raise SPLError("timechart takes exactly one aggregation")
    agg, by = aggs[0], (parts[1].strip() if len(parts) > 1 else None)
    if not rows:
        return []
    ts = [r["_time"] for r in rows]
    lo = bounds[0] if bounds[0] is not None else min(ts)
    hi = bounds[1] if bounds[1] is not None else max(ts)
    first, last = math.floor(lo / span) * span, math.floor(max(hi - 1e-6, lo) / span) * span
    if (last - first) / span > MAX_BUCKETS:
        raise SPLError(f"timechart would produce more than {MAX_BUCKETS} buckets; use a larger span")
    cells: dict[tuple[float, str], list[Row]] = {}
    for r in rows:
        series = str(r.get(by, "NULL")) if by else agg.name
        cells.setdefault((math.floor(r["_time"] / span) * span, series), []).append(r)
    names = sorted({s for _, s in cells})
    zero = 0 if agg.fn in ("count", "sum", "dc") else None
    if by and len(names) > 10:
        totals = {n: sum(len(v) for (_, s), v in cells.items() if s == n) for n in names}
        keep = sorted(names, key=lambda n: (-totals[n], n))[:10]
        if agg.fn in ("count", "sum", "dc"):
            for (b, s) in [k for k in cells if k[1] not in keep]:
                cells.setdefault((b, "OTHER"), []).extend(cells.pop((b, s)))
            keep.append("OTHER")
        names = sorted(keep)
    out, b = [], first
    while b <= last + 1e-9:
        row: Row = {"_time": b}
        for n in names:
            v = compute_agg(agg, cells[(b, n)]) if (b, n) in cells else zero
            row[n] = _fmt(v)
        out.append(row)
        b += span
    return out


def _cmd_top(arg: str, rows: list[Row]) -> list[Row]:
    limit, toks = 10, []
    for t in _split_list(arg):
        m = re.fullmatch(r"limit=(\d+)", t, re.I)
        if m:
            limit = int(m.group(1))
        else:
            toks.append(t)
    if not toks:
        raise SPLError("top needs a field")
    f = toks[0]
    counts: dict[str, int] = {}
    for r in rows:
        if r.get(f) is not None:
            counts[str(r[f])] = counts.get(str(r[f]), 0) + 1
    total = sum(counts.values())
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    if limit:
        ranked = ranked[:limit]
    return [{f: k, "count": v, "percent": round(100 * v / total, 3)} for k, v in ranked]


def _cmd_sort(arg: str, rows: list[Row]) -> list[Row]:
    arg = arg.strip()
    m = re.match(r"(\d+)\s+(.*)", arg)
    limit = None
    if m:
        limit, arg = int(m.group(1)), m.group(2)
    keys = [(s == "-", f) for s, f in re.findall(r"([+-])?\s*([\w.@]+)", arg)]
    if not keys:
        raise SPLError("sort needs at least one field")
    for desc, f in reversed(keys):
        present = [r for r in rows if r.get(f) is not None]
        absent = [r for r in rows if r.get(f) is None]
        present.sort(key=lambda r: _sortkey(r[f]), reverse=desc)
        rows = present + absent
    return rows[:limit] if limit else rows


def _count_arg(arg: str, default: int = 10) -> int:
    arg = arg.strip()
    if not arg:
        return default
    if not arg.isdigit():
        raise SPLError(f"expected a number, got {arg!r}")
    return int(arg)


def _cmd_dedup(arg: str, rows: list[Row]) -> list[Row]:
    fs = _split_list(arg)
    if not fs:
        raise SPLError("dedup needs a field")
    seen, out = set(), []
    for r in rows:
        if any(r.get(f) is None for f in fs):
            continue
        k = tuple(str(r[f]) for f in fs)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def _cmd_fields(arg: str, rows: list[Row]) -> list[Row]:
    drop = arg.strip().startswith("-")
    fs = _split_list(arg.strip().lstrip("-+"))
    if not fs:
        raise SPLError("fields needs at least one field")
    if drop:
        return [{k: v for k, v in r.items() if k not in fs} for r in rows]
    return [{f: r[f] for f in fs if f in r} for r in rows]


def _cmd_rename(arg: str, rows: list[Row]) -> list[Row]:
    pairs = re.findall(r"([\w.@]+)\s+as\s+([\w.@]+)", arg, re.I)
    if not pairs:
        raise SPLError("rename syntax: rename a as b")
    return [{dict(pairs).get(k, k): v for k, v in r.items()} for r in rows]


# -------------------------------------------------------------------- execution

@dataclass
class SearchResult:
    fields: list[str]
    rows: list[Row]
    scanned: int = 0
    truncated: bool = False
    earliest: float | None = None
    latest: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        def clean(r: Row) -> Row:
            out = {k: _fmt(v) for k, v in r.items()}
            if isinstance(out.get("_time"), (int, float)):
                out["_time"] = iso(out["_time"])
            return out

        return {
            "fields": self.fields,
            "results": [clean(r) for r in self.rows],
            "count": len(self.rows),
            "scanned": self.scanned,
            "truncated": self.truncated,
            "earliest": iso(self.earliest) if self.earliest is not None else None,
            "latest": iso(self.latest) if self.latest is not None else None,
        }


def split_pipeline(query: str) -> list[str]:
    stages, cur, quoted, esc = [], [], False, False
    for ch in query:
        if esc:
            esc = False
        elif ch == "\\" and quoted:
            esc = True
        elif ch == '"':
            quoted = not quoted
        elif ch == "|" and not quoted:
            stages.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if quoted:
        raise SPLError("unterminated quote")
    stages.append("".join(cur).strip())
    return stages


_COMMANDS = {"where", "stats", "timechart", "top", "sort", "head", "tail", "table", "fields", "dedup", "rename"}


def execute(
    store: EventStore,
    query: str,
    earliest: Any = None,
    latest: Any = None,
    index: str | None = None,
    limit: int = 1000,
    now: float | None = None,
    max_events: int = 200_000,
) -> SearchResult:
    if not query or not query.strip():
        raise SPLError("empty query")
    stages = split_pipeline(query)
    if not stages[0]:
        raise SPLError("a query must start with a search term, not a pipe")
    first = re.sub(r"^search\b\s*", "", stages[0], flags=re.I)
    word = first.split(None, 1)[0].lower() if first.split() else ""
    if word in _COMMANDS and not re.match(r"\s*\w+\s*[=!<>]", first):
        raise SPLError("a query must start with a search (e.g. 'service=api level=ERROR | stats count')")

    toks = tokenize(first)
    lo, hi = parse_time(earliest, now), parse_time(latest, now)
    kept = []
    for t in toks:  # inline time modifiers
        if t.kind == "cmp" and t.field in ("earliest", "latest") and t.op == "=":
            v = parse_time(t.val, now)
            if t.field == "earliest":
                lo = v
            else:
                hi = v
        else:
            kept.append(t)
    p = _Parser(kept)
    pred = p.parse()
    equals = {t.field: t.val for t in p.cmps}
    if index:
        equals["index"] = index

    rows: list[Row] = []
    scanned = 0
    for row in store.query(lo, hi, equals):
        scanned += 1
        if scanned > max_events:
            raise SPLError(f"search scanned more than {max_events} events; narrow the time range or filters")
        if pred(row):
            rows.append(row)

    for stage in stages[1:]:
        if not stage:
            raise SPLError("empty pipeline stage")
        cmd, _, arg = stage.partition(" ")
        cmd = cmd.lower()
        if cmd == "where":
            rows = _where(arg, rows)
        elif cmd == "stats":
            rows = _cmd_stats(arg, rows)
        elif cmd == "timechart":
            rows = _cmd_timechart(arg, rows, (lo, hi))
        elif cmd == "top":
            rows = _cmd_top(arg, rows)
        elif cmd == "sort":
            rows = _cmd_sort(arg, rows)
        elif cmd == "head":
            rows = rows[: _count_arg(arg)]
        elif cmd == "tail":
            n = _count_arg(arg)
            rows = rows[-n:] if n else []
        elif cmd in ("table", "fields"):
            rows = _cmd_fields(arg, rows)
        elif cmd == "dedup":
            rows = _cmd_dedup(arg, rows)
        elif cmd == "rename":
            rows = _cmd_rename(arg, rows)
        else:
            raise SPLError(f"unknown command {cmd!r}")

    truncated = len(rows) > limit
    rows = rows[:limit]
    fields: list[str] = []
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    return SearchResult(fields, rows, scanned, truncated, lo, hi)


def _where(arg: str, rows: list[Row]) -> list[Row]:
    pred, _ = compile_expr(arg)
    return [r for r in rows if pred(r)]
