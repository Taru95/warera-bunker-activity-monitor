#!/usr/bin/env python3
"""
patch_state_for_tests.py

Modifies state.json so that the next bunker-bot run fires one Discord alert
per scenario. Place this script next to state.json and run it:

    python patch_state_for_tests.py

Then commit and push state.json to GitHub. The cron workflow (or a manual
"Run workflow" trigger) will pick up the modified state.json, diff it
against the live API, and emit the expected alerts.

Scenarios triggered:
  came_online        ie-leinster (Leinster)            running_level cleared
  went_offline       dk-zealand (Zealand)              running_level set to 2
  level_changed      pt-lisbon-alejento (Lisbon)       running_level set to 2 (live is 3)
  built              be-flanders (Flanders)            bunker entry cleared
  destroyed          nl-holland (Holland)              fake bunker entry added
  ownership_changed  de-bavaria (Bavaria)              country_code set to "be"

Not triggered (limitations explained in the message that accompanied this
script):
  battle_started     will fire as noise for regions currently in active battles
  construction_started   needs a real in-game construction to be in progress
"""

import json
import sys
from pathlib import Path

STATE_PATH = Path(__file__).parent / "state.json"


PATCHES = [
    # (scenario_label, region_code, mutator_function)
    (
        "came_online",
        "ie-leinster",
        lambda r: r["bunker"].update({"running_level": None}),
    ),
    (
        "went_offline",
        "dk-zealand",
        lambda r: r["bunker"].update({"running_level": 2}),
    ),
    (
        "level_changed",
        "pt-lisbon-alejento",
        lambda r: r["bunker"].update({"running_level": 2}),
    ),
    (
        "built",
        "be-flanders",
        lambda r: r["bunker"].update({"built_status": None, "built_level": None}),
    ),
    (
        "destroyed",
        "nl-holland",
        lambda r: r["bunker"].update({"built_status": "active", "built_level": 2}),
    ),
    (
        "ownership_changed",
        "de-bavaria",
        lambda r: r.update({"country_code": "be"}),
    ),
]


def main() -> int:
    if not STATE_PATH.exists():
        print(f"ERROR: {STATE_PATH} not found in this directory.", file=sys.stderr)
        return 1

    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    print(f"Loaded {len(state)} regions from {STATE_PATH.name}.\n")

    failures = 0
    for label, code, mutate in PATCHES:
        match = next((r for r in state.values() if r.get("code") == code), None)
        if match is None:
            print(f"  ✗  {label:20}  region code '{code}' NOT FOUND", file=sys.stderr)
            failures += 1
            continue
        mutate(match)
        print(f"  ✓  {label:20}  patched {match.get('name')} ({code})")

    STATE_PATH.write_text(
        json.dumps(state, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(f"\nWrote {STATE_PATH.name}.")

    if failures:
        print(f"\n⚠  {failures} patch(es) failed. State file written anyway.", file=sys.stderr)
        return 1

    print("\nCommit and push state.json, then trigger the workflow.")
    return 0


if __name__ == "__main__":
    sys.exit(main())