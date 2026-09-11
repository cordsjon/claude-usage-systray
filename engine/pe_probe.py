# engine/pe_probe.py
"""CLI: probe configured PosterEngine (PE) instances for health.

Wires the three pieces an operator otherwise hand-rolls a urllib script for:
pe_instances.json (engine/pe_config.py) -> Keychain Bearer token
(engine/providers.keychain_get) -> the same /api/jobs/summary and
/api/admin/router-metrics fetches the poller uses (engine/pe_poller.py).

This is the read-only twin of the poller: no DB writes, no alert state, no
loop — one shot, then exit. Useful when /pe/status looks wrong and the
question is "is it the instance, the token, or the engine?".

    python3 -m engine.pe_probe              # all configured instances
    python3 -m engine.pe_probe --instance dev --json

Exit codes: 0 every probed instance reachable, 1 at least one failed,
2 nothing to probe (missing/empty/bad config, or unknown --instance).
"""

import argparse
import json
import sys

from engine.pe_config import (
    DEFAULT_CONFIG_PATH,
    PEConfigError,
    PEInstance,
    load_pe_instances,
)
from engine.pe_poller import compute_stalled, fetch_jobs_summary, fetch_router_metrics
from engine.providers import keychain_get

PROBE_TIMEOUT_S = 5


def probe_instance(
    instance: PEInstance,
    get_token=None,
    timeout: int = PROBE_TIMEOUT_S,
) -> dict:
    """Probe one instance. Never raises; failures land in the returned dict.

    `get_token(token_ref) -> str | None` is injected for the same reason
    pe_poll_loop injects it: tests must not touch the real Keychain. It
    defaults to None rather than to `keychain_get` directly — a default
    argument binds at def time, which would pin the real Keychain reader
    past any monkeypatch of the module attribute. The token value itself is
    never echoed into the result — only whether the Keychain lookup hit —
    so `--json` output is safe to paste into a log.
    """
    if get_token is None:
        get_token = keychain_get

    result = {
        "instance": instance.name,
        "base_url": instance.base_url,
        "token_ref": instance.token_ref,
        "token_present": False,
        "ok": False,
        "error": None,
        "jobs": None,
        "cost": None,
        "budget_24h_usd": instance.budget_24h_usd,
    }

    token = get_token(instance.token_ref)
    if not token:
        result["error"] = f"no Keychain token for service '{instance.token_ref}'"
        return result
    result["token_present"] = True

    summary, summary_err = fetch_jobs_summary(instance, token, timeout)
    metrics, metrics_err = fetch_router_metrics(instance, token, timeout)

    result["ok"] = summary is not None
    result["error"] = summary_err

    if summary is not None:
        counts = summary.get("counts", {})
        oldest = summary.get("oldest_claimable_queued_s", 0)
        result["jobs"] = {
            "counts": counts,
            "oldest_claimable_queued_s": oldest,
            "stalled": compute_stalled(oldest, counts.get("running", 0)),
        }

    if metrics is not None:
        available = bool(metrics.get("available"))
        cost = metrics.get("cost_24h_usd", 0.0) if available else 0.0
        result["cost"] = {
            "available": available,
            "d24h_usd": cost,
            "calls": metrics.get("calls", 0) if available else 0,
            "over_budget": available and cost >= instance.budget_24h_usd,
            "error": None,
        }
    else:
        result["cost"] = {
            "available": False,
            "d24h_usd": None,
            "calls": None,
            "over_budget": False,
            "error": metrics_err,
        }

    return result


def format_probe(result: dict) -> str:
    """Render one probe result as operator-readable lines."""
    head = "ok" if result["ok"] else "FAIL"
    lines = [f"{result['instance']}: {head}  ({result['base_url']})"]

    if not result["token_present"]:
        lines.append(f"  token: MISSING — {result['error']}")
        return "\n".join(lines)
    lines.append(f"  token: present (keychain '{result['token_ref']}')")

    if result["error"]:
        lines.append(f"  jobs: unreachable — {result['error']}")
    else:
        jobs = result["jobs"] or {}
        c = jobs.get("counts", {})
        lines.append(
            "  jobs: "
            f"queued={c.get('queued', 0)} running={c.get('running', 0)} "
            f"dead={c.get('dead', 0)} failed={c.get('failed', 0)} "
            f"oldest_queued={jobs.get('oldest_claimable_queued_s', 0)}s"
            + ("  STALLED" if jobs.get("stalled") else "")
        )

    cost = result["cost"] or {}
    if cost.get("error"):
        lines.append(f"  cost: unreachable — {cost['error']}")
    elif not cost.get("available"):
        lines.append("  cost: router metrics unavailable")
    else:
        lines.append(
            f"  cost: ${cost['d24h_usd']:.4f}/24h of "
            f"${result['budget_24h_usd']:.2f} budget, calls={cost['calls']}"
            + ("  OVER BUDGET" if cost.get("over_budget") else "")
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Probe configured PosterEngine instances with their Keychain tokens."
    )
    ap.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"pe_instances.json path (default: {DEFAULT_CONFIG_PATH})",
    )
    ap.add_argument(
        "--instance",
        help="Probe only this instance name (default: all configured)",
    )
    ap.add_argument(
        "--timeout", type=int, default=PROBE_TIMEOUT_S,
        help=f"Per-request timeout in seconds (default: {PROBE_TIMEOUT_S})",
    )
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = ap.parse_args(argv)

    try:
        instances = load_pe_instances(args.config)
    except (PEConfigError, json.JSONDecodeError, OSError) as exc:
        print(f"pe_probe: cannot load {args.config}: {exc}", file=sys.stderr)
        return 2

    if not instances:
        print(f"pe_probe: no instances configured in {args.config}", file=sys.stderr)
        return 2

    if args.instance:
        instances = [i for i in instances if i.name == args.instance]
        if not instances:
            print(
                f"pe_probe: no instance named '{args.instance}' in {args.config}",
                file=sys.stderr,
            )
            return 2

    results = [probe_instance(i, timeout=args.timeout) for i in instances]

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print("\n".join(format_probe(r) for r in results))

    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
