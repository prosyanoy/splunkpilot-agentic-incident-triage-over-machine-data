"""Command-line interface: serve, generate, replay, investigate, eval."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .agent import Agent, Investigation, make_llm
from .agent.llm import LLMError
from .backend import Backend, HttpBackend, LocalBackend
from .config import Settings
from .evaluation import format_table, load_baseline, results_json, run_eval, score_report, summarise
from .generator import SCENARIOS, generate, scenario_names


def render(inv: Investigation) -> str:
    r = inv.report
    rc = r.root_cause
    out = [
        "=" * 78,
        f"RCA: {r.title}",
        "=" * 78,
        r.summary, "",
        f"Root cause : {rc.category}  service={rc.service or '-'}  host={rc.host or '-'}  (confidence {r.confidence:.0%})",
        f"Mechanism  : {rc.description}",
        f"Impact     : {r.impact}", "", "Timeline:",
    ]
    out += [f"  {t.time}  {t.event}" for t in r.timeline] or ["  (none)"]
    out += ["", "Evidence (cited events, verified against the store):"]
    out += [f"  [#{e.event_id}] {e.time} {e.host}/{e.service}: {e.message}\n        -> {e.why}" for e in r.evidence] or ["  (none)"]
    if r.dropped_citations:
        out.append(f"  (dropped {len(r.dropped_citations)} citation(s) that do not exist in the store: {r.dropped_citations})")
    out += ["", "Remediation:"] + [f"  - {x}" for x in r.remediation]
    out += ["", f"Investigation: {inv.tool_calls} tool calls ({', '.join(s.tool for s in inv.steps)}) "
                f"in {inv.seconds:.2f}s using {inv.llm}"]
    return "\n".join(out)


def _backend(args: argparse.Namespace, settings: Settings) -> Backend:
    return HttpBackend(args.url or settings.api_url, settings.hec_token) if (args.url or args.remote) else LocalBackend()


def cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn

    uvicorn.run("splunkpilot.api:app_factory", factory=True, host=args.host, port=args.port)
    return 0


def cmd_scenarios(args: argparse.Namespace, settings: Settings) -> int:
    for name, (title, _) in SCENARIOS.items():
        print(f"{name:12} {title}")
    return 0


def cmd_generate(args: argparse.Namespace, settings: Settings) -> int:
    inc = generate(args.scenario, args.seed)
    out = open(args.out, "w") if args.out else sys.stdout
    for ev in inc.events:
        out.write(json.dumps(ev) + "\n")
    if args.out:
        out.close()
        print(f"wrote {len(inc.events)} events to {args.out}", file=sys.stderr)
    return 0


def _investigate(backend: Backend, alert: dict[str, Any], args: argparse.Namespace, settings: Settings) -> Investigation:
    llm = make_llm(args.llm, settings)
    return Agent(backend, llm).investigate(alert)


def cmd_replay(args: argparse.Namespace, settings: Settings) -> int:
    inc = generate(args.scenario, args.seed)
    backend = _backend(args, settings)
    print(f"Replaying '{args.scenario}' (seed {args.seed}): ingesting {len(inc.events)} events into index "
          f"'{inc.truth.alert['index']}' via {'HEC at ' + (args.url or settings.api_url) if (args.url or args.remote) else 'in-process store'}...")
    backend.ingest(inc.events)
    a = inc.truth.alert
    print(f"ALERT {a['fired_at']}: {a['title']} (service: {a['service']})\nInvestigating with LLM '{args.llm or settings.llm}'...\n")
    inv = _investigate(backend, a, args, settings)
    print(render(inv))
    s = score_report(inv.report, inc.truth)
    base = load_baseline()[args.scenario]
    print(f"\nGround truth: {inc.truth.category} / {inc.truth.service}  ->  "
          f"{'CORRECT' if s.category else 'WRONG'} (score {s.total}); human baseline diagnosed in ~{base['minutes']} min")
    return 0 if s.category else 1


def cmd_investigate(args: argparse.Namespace, settings: Settings) -> int:
    backend = HttpBackend(args.url or settings.api_url, settings.hec_token)
    alert = {"title": args.title, "service": args.service, "fired_at": args.latest,
             "earliest": args.earliest, "latest": args.latest, "index": args.index}
    print(render(_investigate(backend, alert, args, settings)))
    return 0


def cmd_eval(args: argparse.Namespace, settings: Settings) -> int:
    llm_name = args.llm or settings.llm
    results = run_eval(lambda: make_llm(llm_name, settings), args.scenario or None, args.seeds)
    summary = summarise(results)
    print(format_table(summary, llm_name))
    errors = [r for r in results if r.error]
    for r in errors[:5]:
        print(f"! {r.scenario}#{r.seed}: {r.error}", file=sys.stderr)
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"summary": summary, "runs": results_json(results)}, f, indent=2)
        print(f"\nwrote {args.json}")
    return 2 if errors and len(errors) == len(results) else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="splunkpilot", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the HEC + search API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("scenarios", help="list incident scenarios")
    s.set_defaults(fn=cmd_scenarios)

    s = sub.add_parser("generate", help="emit a scenario as HEC-shaped JSON lines")
    s.add_argument("scenario", choices=scenario_names())
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--out")
    s.set_defaults(fn=cmd_generate)

    s = sub.add_parser("replay", help="generate an incident, ingest it, investigate and print the RCA")
    s.add_argument("scenario", choices=scenario_names())
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--url", help="API base URL (omit to run fully in-process)")
    s.add_argument("--remote", action="store_true", help="use SPLUNKPILOT_URL from the environment")
    s.add_argument("--llm", choices=["mock", "ollama"])
    s.set_defaults(fn=cmd_replay)

    s = sub.add_parser("investigate", help="investigate an alert against data already in a running API")
    s.add_argument("--url")
    s.add_argument("--index", required=True)
    s.add_argument("--title", default="Elevated error rate")
    s.add_argument("--service", default="")
    s.add_argument("--earliest", required=True, help="ISO-8601 window start")
    s.add_argument("--latest", required=True, help="ISO-8601 window end (alert time)")
    s.add_argument("--llm", choices=["mock", "ollama"])
    s.set_defaults(fn=cmd_investigate)

    s = sub.add_parser("eval", help="score root-cause accuracy and time-to-diagnosis vs the human baseline")
    s.add_argument("--llm", choices=["mock", "ollama"])
    s.add_argument("--seeds", type=int, default=3)
    s.add_argument("--scenario", action="append", choices=scenario_names())
    s.add_argument("--json", help="write full results to this file")
    s.set_defaults(fn=cmd_eval)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args, Settings.from_env())
    except LLMError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        if type(e).__module__.startswith("httpx"):
            print(f"error: cannot reach the API ({e}). Start it with `splunkpilot serve`.", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    sys.exit(main())
