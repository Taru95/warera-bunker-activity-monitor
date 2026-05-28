#!/usr/bin/env python3
"""
patch_state_for_tests.py

Modifies state.json so the next bunker-bot run fires one Discord alert per
scenario. Place this script next to state.json and run:

    python patch_state_for_tests.py

Then commit and push state.json. The cron workflow (or a manual "Run
workflow" trigger) picks up the modified state.json, diffs it against
the live API, and emits the expected alerts.

Scenarios triggered automatically:
  came_online        ie-leinster              running_level cleared
  went_offline       dk-zealand               running_level set
  level_changed      pt-lisbon-alejento       running_level changed
  built              be-flanders              bunker entry cleared
  destroyed          nl-holland               fake bunker entry added
  ownership_changed  de-bavaria               country_code set to "be"
  battle_started     first monitored region currently in a battle (id cleared)
  battle_ended       first monitored region currently NOT in a battle (fake id)

Not triggered automatically:
  construction_started — needs a real in-game bunker upgrade to be in
    progress in a monitored region when the bot next runs. To test:
    start a bunker upgrade on an Irish region in-game (Munster or
    Connacht have no bunker, so they're easy), then trigger the workflow
    manually before construction finishes.
"""

import json
import sys
from pathlib import Path

STATE_PATH = Path(__file__).parent / "state.json"

# Must match alert.py
MONITORED_COUNTRY_CODES = {
    "de", "no", "se", "fi", "ie", "uk", "pt", "dk", "be", "nl",
}


def origin_cc(code):
    if not code or "-" not in code:
        return ""
    return code.split("-", 1)[0].lower()


# Static patches: one specific region per scenario.
STATIC_PATCHES = [
    ("came_online",       "ie-leinster",
        lambda r: r["bunker"].update({"running_level": None})),
    ("went_offline",      "dk-zealand",
        lambda r: r["bunker"].update({"running_level": 2})),
    ("level_changed",     "pt-lisbon-alejento",
        lambda r: r["bunker"].update({"running_level": 2})),
    ("built",             "be-flanders",
        lambda r: r["bunker"].update({"built_status": None, "built_level": None})),
    ("destroyed",         "nl-holland",
        lambda r: r["bunker"].update({"built_status": "active", "built_level": 2})),
    ("ownership_changed", "de-bavaria",
        lambda r: r.update({"country_code": "be"})),
]


def apply_static(state, label, code, mutate):
    match = next((r for r in state.values() if r.get("code") == code), None)
    if match is None:
        print(f"  ✗  {label:18}  region '{code}' not found", file=sys.stderr)
        return False
    mutate(match)
    print(f"  ✓  {label:18}  {match.get('name')} ({code})")
    return True


def apply_dynamic(state, label, predicate, mutate):
    """Pick the first monitored region matching predicate, apply mutation."""
    match = next(
        (r for r in state.values()
         if origin_cc(r.get("code")) in MONITORED_COUNTRY_CODES
         and predicate(r)),
        None,
    )
    if match is None:
        print(f"  ✗  {label:18}  no eligible monitored region found", file=sys.stderr)
        return False
    mutate(match)
    print(f"  ✓  {label:18}  {match.get('name')} ({match.get('code')})")
    return True


def main() -> int:
    if not STATE_PATH.exists():
        print(f"ERROR: {STATE_PATH} not found in this directory.", file=sys.stderr)
        return 1

    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    print(f"Loaded {len(state)} regions from {STATE_PATH.name}.\n")

    failures = 0

    for label, code, mutator in STATIC_PATCHES:
        if not apply_static(state, label, code, mutator):
            failures += 1

    # battle_started: a monitored region currently IN a battle, clear its id
    # so the bot sees prev=null, curr=<real id> on next run.
    if not apply_dynamic(
        state, "battle_started",
        predicate=lambda r: bool(r.get("active_battle_id")),
        mutate=lambda r: r.update({"active_battle_id": None}),
    ):
        failures += 1

    # battle_ended: a monitored region currently NOT in a battle, give it
    # a fake id so the bot sees prev=<fake>, curr=null on next run.
    if not apply_dynamic(
        state, "battle_ended",
        predicate=lambda r: not r.get("active_battle_id"),
        mutate=lambda r: r.update({"active_battle_id": "_test_fake_battle"}),
    ):
        failures += 1

    STATE_PATH.write_text(
        json.dumps(state, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(f"\nWrote {STATE_PATH.name}.")

    if failures:
        print(f"\n⚠  {failures} patch(es) failed; state file written anyway.",
              file=sys.stderr)
        return 1

    print(
        "\nNot triggered automatically:\n"
        "  construction_started — needs a real in-game bunker upgrade in\n"
        "    progress in a monitored region. Start one on an Irish region\n"
        "    (Munster or Connacht both have no bunker yet) and trigger the\n"
        "    workflow manually before construction completes."
    )
    print("\nCommit and push state.json, then trigger the workflow.")
    return 0


if __name__ == "__main__":
    sys.exit(main())