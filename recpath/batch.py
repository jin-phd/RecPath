"""Run a planned set of recommendation chains from the command line (a study design).

    python -m recpath.batch design.csv [--rounds 5] [--pause 10] [--batch-id NAME] [--dry-run]

Each row of the design CSV is one condition: ``query`` and ``location`` (IE, GB or US), plus
optional ``repeats`` (default 1), ``rounds``, ``strategy`` (top_1/random), ``consent``
(accept_all/reject_all), ``exclude_policy`` (displayed/watched), ``location_cookie``
(true/false), ``category`` (a free label for analysis) and ``label``. Runs are interleaved:
repeat 1 of every condition in row order, then repeat 2, and so on, so conditions listed next to
each other (the same query in IE, GB and US) run minutes apart instead of hours apart.

Every run is saved to data/sessions like a run from the app, tagged with ``batch`` =
{batch_id, condition, repeat, label, category}, so it appears in the Session library and Compare.
data/batches/<batch_id>.json lists the runs and how each ended. The batch drives a real browser
like the app does: do not start a run in the app while a batch is running.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from . import browser_observation as bo
from .paths import DATA_DIR, PROJECT_ROOT
from .storage import save_session, write_text_atomic
from .time_utils import DUBLIN_TZ, timestamp_pair


BATCH_DIR = DATA_DIR / "batches"
STRATEGIES = ("top_1", "random")
BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")
TRUE_WORDS = {"1", "true", "yes", "y"}
FALSE_WORDS = {"0", "false", "no", "n"}


class DesignError(ValueError):
    pass


@dataclass(frozen=True)
class Condition:
    index: int
    query: str
    location: str
    repeats: int
    rounds: int | None
    strategy: str | None
    consent: str | None
    exclude_policy: str | None
    location_cookie: bool | None
    category: str
    label: str


@dataclass(frozen=True)
class PlannedRun:
    condition: Condition
    repeat: int


def parse_bool(value: str, row: int, name: str) -> bool | None:
    text = value.strip().lower()
    if not text:
        return None
    if text in TRUE_WORDS:
        return True
    if text in FALSE_WORDS:
        return False
    raise DesignError(f"Row {row}: {name} must be true or false, not {value!r}.")


def parse_int(value: str, row: int, name: str, low: int, high: int) -> int | None:
    text = value.strip()
    if not text:
        return None
    try:
        number = int(text)
    except ValueError:
        raise DesignError(f"Row {row}: {name} must be a whole number, not {value!r}.") from None
    if not low <= number <= high:
        raise DesignError(f"Row {row}: {name} must be between {low} and {high}, not {number}.")
    return number


def parse_choice(value: str, row: int, name: str, choices: Sequence[str]) -> str | None:
    text = value.strip()
    if not text:
        return None
    if text not in choices:
        raise DesignError(f"Row {row}: {name} must be one of {', '.join(choices)}, not {value!r}.")
    return text


def read_design(path: Path) -> list[Condition]:
    """The conditions of a design CSV; rows without a query are skipped."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = {"query", "location"} - set(reader.fieldnames or [])
        if missing:
            raise DesignError(f"The design needs the column(s) {', '.join(sorted(missing))}.")
        conditions: list[Condition] = []
        for row_number, row in enumerate(reader, start=2):
            cells = {key: (value or "") for key, value in row.items() if key}
            query = " ".join(cells["query"].split())
            if not query:
                continue
            location = parse_choice(cells["location"].upper(), row_number, "location", bo.LOCATION_OPTIONS)
            if location is None:
                raise DesignError(f"Row {row_number}: location is empty.")
            cookie = parse_bool(cells.get("location_cookie", ""), row_number, "location_cookie")
            label = cells.get("label", "").strip() or f"{query} · {location}" + (
                " · URL only" if cookie is False else ""
            )
            conditions.append(
                Condition(
                    index=len(conditions) + 1,
                    query=query,
                    location=location,
                    repeats=parse_int(cells.get("repeats", ""), row_number, "repeats", 1, 50) or 1,
                    rounds=parse_int(cells.get("rounds", ""), row_number, "rounds", 1, 10),
                    strategy=parse_choice(cells.get("strategy", ""), row_number, "strategy", STRATEGIES),
                    consent=parse_choice(cells.get("consent", ""), row_number, "consent", bo.CONSENT_OPTIONS),
                    exclude_policy=parse_choice(
                        cells.get("exclude_policy", ""), row_number, "exclude_policy", bo.EXCLUDE_POLICIES
                    ),
                    location_cookie=cookie,
                    category=cells.get("category", "").strip(),
                    label=label,
                )
            )
    if not conditions:
        raise DesignError("The design has no rows with a query.")
    return conditions


def plan_runs(conditions: list[Condition]) -> list[PlannedRun]:
    """Repeat 1 of every condition in row order, then repeat 2, ... (conditions with fewer
    repeats drop out once they are done)."""
    most = max(condition.repeats for condition in conditions)
    return [
        PlannedRun(condition, repeat)
        for repeat in range(1, most + 1)
        for condition in conditions
        if repeat <= condition.repeats
    ]


def settings_for(condition: Condition, *, rounds: int, pacing_ms: int) -> bo.BrowserObservationSettings:
    values: dict[str, Any] = {"region": condition.location, "max_rounds": condition.rounds or rounds, "slow_mo_ms": pacing_ms}
    if condition.strategy:
        values["strategy"] = condition.strategy
    if condition.consent:
        values["consent_choice"] = condition.consent
    if condition.exclude_policy:
        values["exclude_policy"] = condition.exclude_policy
    if condition.location_cookie is not None:
        values["location_cookie"] = condition.location_cookie
    return bo.BrowserObservationSettings(**values)


def code_version() -> dict[str, Any] | None:
    """The git commit the batch runs on, and whether the working tree had uncommitted changes."""
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, capture_output=True, text=True,
                                timeout=5, check=True).stdout.strip()
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=PROJECT_ROOT,
                                capture_output=True, text=True, timeout=5, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return {"commit": commit, "uncommitted_changes": bool(status.strip())}


def run_batch(
    conditions: list[Condition],
    *,
    batch_id: str,
    design_path: Path,
    rounds: int,
    pause_s: float,
    pacing_ms: int,
    batch_dir: Path = BATCH_DIR,
    out=sys.stdout,
) -> dict[str, Any]:
    """Run the plan one chain at a time, saving every session and the batch manifest as it goes."""
    plan = plan_runs(conditions)
    manifest: dict[str, Any] = {
        "batch_id": batch_id,
        "design": str(design_path.resolve()),
        "code_version": code_version(),
        "started_at": timestamp_pair()[0],
        "finished_at": None,
        "planned_runs": len(plan),
        "conditions": [
            {
                "condition": condition.index,
                "label": condition.label,
                "query": condition.query,
                "location": condition.location,
                "category": condition.category,
                "repeats": condition.repeats,
                "settings": settings_for(condition, rounds=rounds, pacing_ms=pacing_ms).public_dict(),
            }
            for condition in conditions
        ],
        "runs": [],
    }
    manifest_path = batch_dir / f"{batch_id}.json"

    def save_manifest() -> None:
        batch_dir.mkdir(parents=True, exist_ok=True)
        write_text_atomic(manifest_path, json.dumps(manifest, indent=2, ensure_ascii=False))

    save_manifest()
    try:
        for number, planned in enumerate(plan, start=1):
            condition = planned.condition
            if number > 1 and pause_s > 0:
                time.sleep(pause_s)
            print(f"[{number}/{len(plan)}] {condition.label} · repeat {planned.repeat}", file=out, flush=True)
            started = time.monotonic()
            entry: dict[str, Any] = {
                "number": number,
                "condition": condition.index,
                "repeat": planned.repeat,
                "label": condition.label,
                "session_id": None,
            }
            manifest["runs"].append(entry)
            try:
                session = bo.run_browser_observation(
                    start_query=condition.query,
                    settings=settings_for(condition, rounds=rounds, pacing_ms=pacing_ms),
                    checkpoint=save_session,
                    batch={
                        "batch_id": batch_id,
                        "condition": condition.index,
                        "repeat": planned.repeat,
                        "label": condition.label,
                        "category": condition.category,
                    },
                )
            except bo.BrowserObservationError as exc:
                # Only a browser that cannot start raises here; every later run would fail too.
                entry.update({"status": "error", "end_reason": "browser_failed", "error": str(exc)})
                print(f"    browser failed: {exc}", file=out, flush=True)
                break
            entry.update(
                {
                    "session_id": session.get("session_id"),
                    "status": session.get("status"),
                    "end_reason": session.get("end_reason"),
                    "rounds": len(session.get("rounds") or []),
                    "error": (session.get("error") or {}).get("message"),
                    "seconds": round(time.monotonic() - started, 1),
                }
            )
            print(
                f"    {entry['status']} ({entry['end_reason']}), {entry['rounds']} round(s), "
                f"{entry['seconds']} s · {entry['session_id']}",
                file=out,
                flush=True,
            )
            save_manifest()
    finally:
        manifest["finished_at"] = timestamp_pair()[0]
        save_manifest()
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m recpath.batch",
        description="Run the recommendation chains of a design CSV one after another.",
    )
    parser.add_argument("design", type=Path, help="CSV with the columns query, location and optional repeats, rounds, "
                        "strategy, consent, exclude_policy, location_cookie, category, label")
    parser.add_argument("--rounds", type=int, default=5, help="rounds per chain where the design gives none (default 5)")
    parser.add_argument("--pause", type=float, default=10.0, help="seconds to wait between runs (default 10)")
    parser.add_argument("--pacing-ms", type=int, default=250, help="pause before every browser action (default 250)")
    parser.add_argument("--batch-id", help="name of the batch (default batch_<date>_<time>)")
    parser.add_argument("--dry-run", action="store_true", help="print the planned runs and exit")
    args = parser.parse_args(argv)

    try:
        conditions = read_design(args.design)
    except (OSError, DesignError) as exc:
        print(f"Cannot read the design: {exc}", file=sys.stderr)
        return 2
    if not 1 <= args.rounds <= 10:
        print("--rounds must be between 1 and 10.", file=sys.stderr)
        return 2
    plan = plan_runs(conditions)
    if args.dry_run:
        for number, planned in enumerate(plan, start=1):
            print(f"[{number}/{len(plan)}] {planned.condition.label} · repeat {planned.repeat}")
        return 0
    if not bo.playwright_available():
        print("Playwright is not installed; see step 1 of the README.", file=sys.stderr)
        return 2
    batch_id = args.batch_id or datetime.now(DUBLIN_TZ).strftime("batch_%Y%m%d_%H%M%S")
    if not BATCH_ID_RE.match(batch_id):
        print("--batch-id may only use letters, digits, '_', '.' and '-'.", file=sys.stderr)
        return 2
    try:
        manifest = run_batch(
            conditions,
            batch_id=batch_id,
            design_path=args.design,
            rounds=args.rounds,
            pause_s=args.pause,
            pacing_ms=args.pacing_ms,
        )
    except KeyboardInterrupt:
        print(f"Interrupted; the runs so far are saved · {BATCH_DIR / (batch_id + '.json')}", file=sys.stderr)
        return 130
    done = sum(1 for run in manifest["runs"] if run.get("status") == "completed")
    print(f"{done} of {manifest['planned_runs']} run(s) completed · {BATCH_DIR / (batch_id + '.json')}")
    return 0 if done == manifest["planned_runs"] else 1


if __name__ == "__main__":
    sys.exit(main())
