#!/usr/bin/env python3
"""
warera-bunker-notifications-discord
PROXY_BASE = "https://warera-proxy.toie.workers.dev/trpc"
Polls every 2h via GitHub Actions cron. Detects regional events worldwide
and posts them to Discord. Only fires for regions whose original owner
(per the immutable region.code prefix) is in MONITORED_COUNTRY_CODES.

Two run modes:
  python alert.py              normal run: detect transitions, post alerts
  python alert.py --heartbeat  daily mode: post status summary

Events detected:
  came_online           bunker started running
  went_offline          bunker stopped running
  level_changed         running level changed
  built                 bunker entry appeared in upgradesV2
  destroyed             bunker entry disappeared from upgradesV2
  ownership_changed     region's controlling country changed
  construction_started  bunker construction kicked off
  battle_started        a battle began on this region
  battle_ended          the active battle finished
  bunker_activating     bunker is pending and will activate at willBeActiveAt
  resistance_full       occupied region's resistance bar hit max (liberation
                        battle now available)

We cannot tell *why* a bunker transitioned (oil exhausted, manual disable,
battle damage). The alert states the change and lets humans investigate.

State files (committed back by the workflow):
  state.json  per-region snapshot, compared against on next run
  runs.json   rolling log of per-run stats, used by the heartbeat

NOTE ON OWNERSHIP FIELDS:
  region.countryCode    = the CORE / original owner's code (never changes)
  region.initialCountry = the CORE owner's id (matches countryCode)
  region.country        = the CURRENT controller's id (changes on conquest)
  The current controller's CODE is resolved by looking up `country` in a
  map built from initialCountry -> countryCode (see build_country_id_to_code).

NOTE ON BUNKER ACTIVATION:
  The bulk region object's bunker.status can be STALE (it showed "active"
  while the bunker was really "pending"). The activation timestamp,
  `willBeActiveAt`, lives ONLY on the dedicated upgrade endpoint
  (upgrade.getUpgradeByTypeAndEntity). So for activation we query that
  per-region endpoint and trust it over the bulk status. `came_online`
  still keys off the bulk running level and is unchanged.

NOTE ON RESISTANCE:
  region.resistance     = current resistance bar value (integer)
  region.resistanceMax  = the cap, which equals development * 100
  Resistance only climbs while a region is OCCUPIED; owner-controlled regions
  decay (0.2%/h). When the bar is full a liberation battle can be started, so
  resistance_full only fires for occupied regions. resistanceMax creeps up with
  development, so a region pinned at the cap can briefly read one under it; a
  hysteresis flag (`alerted`) suppresses re-fires until resistance falls back
  below 90% of max. Both fields live on the bulk snapshot, so no extra calls.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────

PROXY_BASE     = "https://warera-proxy.toie.workers.dev/trpc"
GAME_BASE      = "https://app.warera.io"
STATE_FILE     = Path(__file__).parent / "state.json"
RUNS_FILE      = Path(__file__).parent / "runs.json"
WEBHOOK_URL    = os.environ.get("DISCORD_BUNKER_WEBHOOK_URL", "")
HTTP_TIMEOUT   = 30
MAX_RETRIES    = 3
RETRY_BACKOFF  = 5     # seconds, multiplied by attempt number
DISCORD_PAUSE  = 0.6
UPGRADE_PAUSE  = 0.3   # seconds between per-region bunker-upgrade calls
USER_AGENT     = "warera-bunker-bot/1.0"
BOT_USERNAME   = "Bunksby-Bunkerbot"   # overrides whatever the Discord webhook is named
RUNS_KEEP      = 100   # how many recent runs to retain in runs.json

# Fraction of resistanceMax that resistance must fall back below before a
# region that already alerted "full" is allowed to alert again. Stops a region
# pinned at the cap from re-firing every poll when development creep nudges the
# max up by a fraction.
RESISTANCE_REARM_RATIO = 0.9

# If the heartbeat sees the last successful alert run was older than this,
# it warns the channel that the cron may be stalled.
HEARTBEAT_STALE_HOURS = 4

# Only emit alerts for regions whose ORIGINAL country (region.code prefix)
# is in this set. Region codes never change, so conquered regions remain
# watched regardless of current controller.
MONITORED_COUNTRY_CODES = {
    "de",  # Germany
#   "no",  # Norway
#   "se",  # Sweden
#   "fi",  # Finland
#   "ie",  # Ireland
#   "uk",  # United Kingdom
#   "pt",  # Portugal
#   "dk",  # Denmark
#   "be",  # Belgium
#   "nl",  # Netherlands (Holland)
}

# Country names for display. Unknown codes fall back to uppercase code.
COUNTRY_NAMES = {
    "de": "Germany",  "no": "Norway",   "se": "Sweden",      "fi": "Finland",
    "ie": "Ireland",  "uk": "United Kingdom", "pt": "Portugal",
    "dk": "Denmark",  "be": "Belgium",  "nl": "Netherlands",
    "fr": "France",   "es": "Spain",    "it": "Italy",       "pl": "Poland",
    "cz": "Czechia",  "at": "Austria",  "ch": "Switzerland", "lu": "Luxembourg",
    "ru": "Russia",   "ua": "Ukraine",  "by": "Belarus",     "md": "Moldova",
    "ge": "Georgia",  "am": "Armenia",  "az": "Azerbaijan",
    "tr": "Turkey",   "gr": "Greece",   "ro": "Romania",     "hu": "Hungary",
    "sk": "Slovakia", "lt": "Lithuania","lv": "Latvia",      "ee": "Estonia",
    "is": "Iceland",  "mt": "Malta",    "cy": "Cyprus",
    "ba": "Bosnia",   "hr": "Croatia",  "si": "Slovenia",    "rs": "Serbia",
    "mk": "N. Macedonia", "al": "Albania", "bg": "Bulgaria", "me": "Montenegro",
    "ma": "Morocco",  "tn": "Tunisia",  "ly": "Libya",       "dz": "Algeria",
    "eg": "Egypt",
    "il": "Israel",   "lb": "Lebanon",  "sy": "Syria",       "iq": "Iraq",
    "ir": "Iran",     "sa": "Saudi Arabia", "jo": "Jordan",  "ps": "Palestine",
    "us": "USA",      "ca": "Canada",   "mx": "Mexico",      "br": "Brazil",
    "ar": "Argentina","cn": "China",    "jp": "Japan",       "kr": "S. Korea",
    "in": "India",    "au": "Australia","nz": "New Zealand", "za": "S. Africa",
}

# Discord embed colours
COLOR_GREEN    = 0x4ade80
COLOR_RED      = 0xef4444
COLOR_YELLOW   = 0xfbbf24
COLOR_BLUE     = 0x60a5fa
COLOR_GRAY     = 0x9ca3af
COLOR_ORANGE   = 0xfb923c   # ownership flip
COLOR_PURPLE   = 0xa78bfa   # construction started
COLOR_PINK     = 0xf472b6   # battle started
COLOR_TEAL     = 0x2dd4bf   # battle ended
COLOR_INDIGO   = 0x818cf8   # bunker pending activation
COLOR_CRIMSON  = 0xdc2626   # resistance bar full


# ── Generic helpers ───────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def http_get_json(url):
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError,
                json.JSONDecodeError, TimeoutError) as e:
            last_err = e
            log(f"GET failed (attempt {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
    raise RuntimeError(f"GET {url} failed after {MAX_RETRIES} attempts: {last_err}")


def parse_iso(s):
    """Parse ISO timestamp string, handling trailing Z."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


# War Era uses a few country codes that aren't valid ISO 3166-1 alpha-2,
# so the regional-indicator emoji renders blank. Translate here before
# building the flag. Display name still uses the game's code (e.g. "uk").
FLAG_CODE_ALIASES = {
    "uk": "gb",  # United Kingdom is GB in ISO 3166
}


def flag(cc):
    """2-letter country code to flag emoji."""
    if not cc or len(cc) != 2 or not cc.isalpha():
        return "🏳️"
    code = FLAG_CODE_ALIASES.get(cc.lower(), cc.lower())
    return "".join(chr(0x1F1E6 + ord(ch) - ord('a')) for ch in code)


def country_label(cc):
    if not cc:
        return "Unknown"
    return COUNTRY_NAMES.get(cc.lower(), cc.upper())


def country_with_flag(cc):
    if not cc:
        return "🏳️ **Unknown**"
    return f"{flag(cc)} **{country_label(cc)}**"


def origin_country_code(region_code):
    """e.g. 'ie-leinster' -> 'ie'. Empty string when no prefix."""
    if not region_code or "-" not in region_code:
        return ""
    return region_code.split("-", 1)[0].lower()


# ── API ───────────────────────────────────────────────────────────────────

def fetch_all_regions():
    body = http_get_json(f"{PROXY_BASE}/region.getRegionsObject")
    data = body.get("result", {}).get("data", {})
    if not isinstance(data, dict):
        raise RuntimeError(f"region.getRegionsObject returned unexpected shape: {type(data)}")
    log(f"fetched {len(data)} regions")
    return data


def fetch_bunker_upgrade(region_id):
    """
    Per-region bunker upgrade detail from the dedicated endpoint. This is the
    ONLY place willBeActiveAt (the activation timestamp) is exposed; the bulk
    region object does not carry it and its bunker.status can be stale.

    Best-effort: returns the upgrade dict, or None on any failure (never raises).
    """
    payload = json.dumps(
        {"upgradeType": "bunker", "regionId": region_id},
        separators=(",", ":"),
    )
    url = f"{PROXY_BASE}/upgrade.getUpgradeByTypeAndEntity?input={urllib.parse.quote(payload)}"
    try:
        body = http_get_json(url)
    except Exception as e:
        log(f"bunker upgrade fetch failed for {region_id} (non-fatal): {e}")
        return None
    data = (body or {}).get("result", {}).get("data")
    return data if isinstance(data, dict) else None


# ── State extraction ─────────────────────────────────────────────────────

def extract_bunker_state(region):
    """
    Snapshot of bunker-related fields. activeUpgradeLevels.bunker tells us
    if it's currently running; upgradesV2.upgrades.bunker holds the built
    record plus construction metadata.
    """
    active = region.get("activeUpgradeLevels") or {}
    running_level = active.get("bunker")
    if not isinstance(running_level, int):
        running_level = None

    upgrades = ((region.get("upgradesV2") or {}).get("upgrades") or {})
    bunker = upgrades.get("bunker")

    if isinstance(bunker, dict):
        built_status = bunker.get("status")
        built_level  = bunker.get("level")
        if not isinstance(built_level, int):
            built_level = None
        # ASSUMPTION: isUnderConstruction is null when idle, truthy mid-build.
        # If the field works differently we may miss construction_started events.
        is_under_construction = bool(bunker.get("isUnderConstruction"))
    else:
        built_status = None
        built_level  = None
        is_under_construction = False

    return {
        "running_level":          running_level,
        "built_status":           built_status,
        "built_level":            built_level,
        "is_under_construction":  is_under_construction,
    }


def extract_resistance_state(region):
    """
    Current resistance value and its cap (resistanceMax == development * 100).
    `alerted` is hysteresis state: it defaults False here and is updated by
    detect_transitions, then persists in state.json so a region pinned at the
    cap doesn't re-alert every poll.
    """
    val = region.get("resistance")
    mx  = region.get("resistanceMax")
    return {
        "value":   val if isinstance(val, (int, float)) else None,
        "max":     mx if isinstance(mx, (int, float)) else None,
        "alerted": False,
    }


def extract_active_battle_id(region):
    """activeBattle is sometimes a string id, sometimes the full object."""
    ab = region.get("activeBattle")
    if isinstance(ab, dict):
        return ab.get("_id")
    if isinstance(ab, str) and ab:
        return ab
    return None


def build_country_id_to_code(regions):
    """
    {country_id: country_code}, derived from CORE ownership.

    A region's `countryCode` is its CORE/original owner's code, and
    `initialCountry` is that same owner's id — so this pair is always
    consistent regardless of who currently occupies the region. Building
    the map off `country` (the *current* controller) would mislabel every
    occupied region's id with the core's code.
    """
    out = {}
    for region in regions.values():
        if not isinstance(region, dict):
            continue
        core_id   = region.get("initialCountry")
        core_code = region.get("countryCode")
        if core_id and core_code:
            out[core_id] = core_code.lower()
    return out


def build_current_state(regions, country_id_to_code):
    now = datetime.now(timezone.utc).isoformat()
    out = {}
    for rid, region in regions.items():
        if not isinstance(region, dict):
            continue
        country_id         = region.get("country")          # CURRENT controller (id)
        initial_country_id = region.get("initialCountry")    # CORE / original owner (id)

        # `countryCode` is the CORE owner's code, NOT the current controller's.
        core_code       = (region.get("countryCode") or "").lower() or None
        # Resolve the current controller's code via the id->code map.
        # Fall back to the core code if the occupier holds no core region of
        # its own in this snapshot (rare); worst case reads as core owner,
        # i.e. the old behaviour, never a crash.
        controller_code = country_id_to_code.get(country_id) or core_code

        out[rid] = {
            "name":                  region.get("name"),
            "code":                  region.get("code"),
            "country_code":          controller_code,     # who controls it NOW
            "country_id":            country_id,
            "initial_country_id":    initial_country_id,
            "initial_country_code":  core_code,           # who it belongs to
            "active_battle_id":      extract_active_battle_id(region),
            "bunker":                extract_bunker_state(region),
            "resistance":            extract_resistance_state(region),
            "observed_at":           now,
            # "bunker_upgrade" is added later by collect_bunker_upgrades() for
            # monitored regions that have a bunker. Absent otherwise.
        }
    return out


def collect_bunker_upgrades(current_state):
    """
    For monitored regions that already show a bunker in the bulk snapshot,
    fetch the dedicated upgrade record so we can read its real `status` and
    `willBeActiveAt`. Filtering on bulk bunker-presence bounds this to a
    handful of calls per run. Returns {region_id: {status, will_be_active_at, level}}.
    """
    targets = []
    for rid, st in current_state.items():
        if origin_country_code(st.get("code")) not in MONITORED_COUNTRY_CODES:
            continue
        bunker = st.get("bunker") or {}
        if bunker.get("built_status") is None:
            continue  # no bunker built here; nothing to activate
        targets.append(rid)

    if not targets:
        return {}

    log(f"querying bunker upgrade detail for {len(targets)} monitored region(s) with bunkers")
    out = {}
    for i, rid in enumerate(targets):
        up = fetch_bunker_upgrade(rid)
        if isinstance(up, dict):
            out[rid] = {
                "status":            up.get("status"),
                "will_be_active_at": up.get("willBeActiveAt"),
                "level":             up.get("level"),
            }
        if i < len(targets) - 1:
            time.sleep(UPGRADE_PAUSE)
    return out


# ── Transition detection ─────────────────────────────────────────────────

def detect_transitions(prev, curr):
    """
    Emits one or more events per region when state changes. Bunker
    presence/level changes are mutually exclusive within a region (built
    XOR destroyed XOR running-level transitions), but ownership,
    construction, battle, activation, and resistance events fire
    independently and can co-occur.

    Side effect: the resistance arm WRITES the hysteresis `alerted` flag back
    onto curr's resistance dict so it persists into the next saved state.
    """
    events = []
    now = datetime.now(timezone.utc)

    for rid in set(prev.keys()) | set(curr.keys()):
        p = prev.get(rid)
        c = curr.get(rid)
        if p is None or c is None:
            continue  # first observation or vanished

        # Ownership flip (compares CURRENT controllers, not core owners)
        p_cc = p.get("country_code")
        c_cc = c.get("country_code")
        if p_cc and c_cc and p_cc != c_cc:
            events.append(_event("ownership_changed", rid, p, c))

        # Bunker state machine (exclusive arm)
        p_b = p.get("bunker") or {}
        c_b = c.get("bunker") or {}
        p_has = p_b.get("built_status") is not None
        c_has = c_b.get("built_status") is not None

        if not p_has and c_has:
            events.append(_event("built", rid, p, c))
        elif p_has and not c_has:
            events.append(_event("destroyed", rid, p, c))
        else:
            p_run = p_b.get("running_level")
            c_run = c_b.get("running_level")
            if p_run is None and c_run is not None:
                events.append(_event("came_online", rid, p, c))
            elif p_run is not None and c_run is None:
                events.append(_event("went_offline", rid, p, c))
            elif p_run is not None and c_run is not None and p_run != c_run:
                events.append(_event("level_changed", rid, p, c))

        # Construction kicked off
        if not p_b.get("is_under_construction") and c_b.get("is_under_construction"):
            events.append(_event("construction_started", rid, p, c))

        # Battle presence
        p_bid = p.get("active_battle_id")
        c_bid = c.get("active_battle_id")
        if not p_bid and c_bid:
            events.append(_event("battle_started", rid, p, c))
        elif p_bid and not c_bid:
            events.append(_event("battle_ended", rid, p, c))

        # Bunker pending activation (from the dedicated upgrade endpoint).
        # Fire ONCE per pending episode: when the bunker first becomes pending,
        # or when its target activation time changes (a new cycle). Keying the
        # dedupe on willBeActiveAt means the same pending state across multiple
        # 2h polls won't re-alert, but each new cycle (new timestamp) will.
        p_up = p.get("bunker_upgrade") or {}
        c_up = c.get("bunker_upgrade") or {}
        c_status   = c_up.get("status")
        c_activeat = c_up.get("will_be_active_at")
        if c_status == "pending" and c_activeat:
            dt = parse_iso(c_activeat)
            if dt and dt > now:  # ignore stale/past timestamps
                if (p_up.get("status") != "pending"
                        or p_up.get("will_be_active_at") != c_activeat):
                    events.append(_event("bunker_activating", rid, p, c))

        # Resistance bar full (occupied regions only), with hysteresis.
        # Resistance only climbs while a region is occupied; owner-controlled
        # regions decay, so we only signal for occupied ones. A full bar lets a
        # liberation battle be started. resistanceMax creeps up with
        # development, so a region pinned at the cap can briefly read one under
        # it; the `alerted` flag suppresses re-fires until resistance falls back
        # below RESISTANCE_REARM_RATIO of max, giving one alert per cycle. The
        # flag is written onto c_res so it survives into the next saved state.
        p_res = p.get("resistance") or {}
        c_res = c.get("resistance")
        if isinstance(c_res, dict):
            c_val = c_res.get("value")
            c_max = c_res.get("max")
            occupied = bool(
                c.get("country_id") and c.get("initial_country_id")
                and c.get("country_id") != c.get("initial_country_id")
            )
            prev_alerted = bool(p_res.get("alerted"))

            if (occupied and isinstance(c_val, (int, float))
                    and isinstance(c_max, (int, float)) and c_max > 0):
                if c_val >= c_max and not prev_alerted:
                    events.append(_event("resistance_full", rid, p, c))
                    c_res["alerted"] = True
                elif prev_alerted and c_val < c_max * RESISTANCE_REARM_RATIO:
                    c_res["alerted"] = False   # re-armed; dropped back below band
                else:
                    c_res["alerted"] = prev_alerted   # carry forward
            else:
                # Not occupied (liberated, or no data): clear so the next
                # occupation cycle can fire fresh.
                c_res["alerted"] = False

    return events


def _event(kind, rid, prev_region, curr_region):
    return {
        "type":                  kind,
        "region_id":             rid,
        "region_name":           curr_region.get("name") or curr_region.get("code") or rid,
        "region_code":           curr_region.get("code"),
        "country_code":          curr_region.get("country_code"),
        "prev_country_code":     prev_region.get("country_code"),
        "initial_country_code":  curr_region.get("initial_country_code"),
        "country_id":            curr_region.get("country_id"),
        "initial_country_id":    curr_region.get("initial_country_id"),
        "prev_bunker":           prev_region.get("bunker") or {},
        "curr_bunker":           curr_region.get("bunker") or {},
        "prev_bunker_upgrade":   prev_region.get("bunker_upgrade") or {},
        "curr_bunker_upgrade":   curr_region.get("bunker_upgrade") or {},
        "prev_resistance":       prev_region.get("resistance") or {},
        "curr_resistance":       curr_region.get("resistance") or {},
        "prev_battle_id":        prev_region.get("active_battle_id"),
        "curr_battle_id":        curr_region.get("active_battle_id"),
    }


def is_monitored(event):
    return origin_country_code(event.get("region_code")) in MONITORED_COUNTRY_CODES


# ── Discord formatting ───────────────────────────────────────────────────

# (emoji, title text, color, footer) per event type
_EMBED_META = {
    "came_online":          ("🟢", "Bunker came online",         COLOR_GREEN,
                             "Newly enabled. Likely refuelled or re-activated."),
    "went_offline":         ("🔴", "Bunker went offline",        COLOR_RED,
                             "Cause unknown: could be oil exhaustion, manual disable, or battle damage."),
    "level_changed":        ("🟡", "Bunker level changed",       COLOR_YELLOW,
                             "Level adjusted by the region's controller."),
    "built":                ("🔵", "Bunker built",               COLOR_BLUE,
                             "New construction completed."),
    "destroyed":            ("⚫", "Bunker destroyed",            COLOR_GRAY,
                             "Cause unknown: could be conquest, ownership change, or upgrade removal."),
    "ownership_changed":    ("🟠", "Region changed hands",       COLOR_ORANGE,
                             "Bunkers and upgrades may be affected. Confirm in-game."),
    "construction_started": ("🟣", "Bunker construction started",COLOR_PURPLE,
                             "Heads up: a new bunker is being built."),
    "battle_started":       ("⚔️", "Region under attack",        COLOR_PINK,
                             "Battle in progress in-game."),
    "battle_ended":         ("🏁", "Battle ended",               COLOR_TEAL,
                             "Battle concluded. Check the outcome in-game."),
    "bunker_activating":    ("⏳", "Bunker activating soon",      COLOR_INDIGO,
                             "Scheduled to go active at the listed time."),
    "resistance_full":      ("🚩", "Resistance bar full",        COLOR_CRIMSON,
                             "Bar at maximum. A liberation battle can now be started against the occupier."),
}


def format_event_embed(event):
    kind        = event["type"]
    region_name = event["region_name"]
    region_code = event.get("region_code") or event["region_id"]
    curr_cc     = (event.get("country_code") or "").lower()
    prev_cc     = (event.get("prev_country_code") or "").lower()
    rid         = event["region_id"]
    url         = f"{GAME_BASE}/region/{rid}"

    emoji, title_label, color, footer_text = _EMBED_META.get(
        kind, ("⚪", "Region change", COLOR_GRAY, "")
    )

    origin_cc  = origin_country_code(region_code)   # core owner, from region code prefix
    origin_tag = f" ({origin_cc.upper()})" if origin_cc else ""

    p_run      = event["prev_bunker"].get("running_level")
    c_run      = event["curr_bunker"].get("running_level")
    p_built_lv = event["prev_bunker"].get("built_level")
    c_built_lv = event["curr_bunker"].get("built_level")

    # ── Header line: region + current controller (or flip details) ──
    if kind == "ownership_changed":
        header_line = (
            f"**{region_name}**{origin_tag}  ·  "
            f"flipped from {country_with_flag(prev_cc)} to {country_with_flag(curr_cc)}"
        )
    else:
        if not curr_cc:
            header_line = f"**{region_name}**{origin_tag}  ·  Controlled by 🏳️ **Unknown**"
        else:
            # "Occupied by" when the holder isn't the core owner; else "Controlled by".
            verb = "Occupied by" if (origin_cc and curr_cc != origin_cc) else "Controlled by"
            header_line = f"**{region_name}**{origin_tag}  ·  {verb} {country_with_flag(curr_cc)}"

    # ── Detail line: what specifically changed ──
    if kind == "came_online":
        change_line = f"Now running at **L{c_run}**."
    elif kind == "went_offline":
        lvl = p_run or p_built_lv or "?"
        change_line = f"Was running at **L{lvl}**. No longer active."
    elif kind == "level_changed":
        change_line = f"Running level: **L{p_run} → L{c_run}**."
    elif kind == "built":
        change_line = f"New construction at **L{c_built_lv or '?'}**."
    elif kind == "destroyed":
        change_line = f"Previously **L{p_built_lv or '?'}**. Bunker entry gone."
    elif kind == "ownership_changed":
        change_line = ""  # header line already says it
    elif kind == "construction_started":
        target = c_built_lv
        if target:
            change_line = f"Construction in progress. Target level: **L{target}**."
        else:
            change_line = "Construction in progress."
    elif kind == "battle_started":
        change_line = "A battle has begun on this region."
    elif kind == "battle_ended":
        change_line = "The active battle has concluded."
    elif kind == "bunker_activating":
        up   = event.get("curr_bunker_upgrade") or {}
        wa   = up.get("will_be_active_at")
        lvl  = up.get("level")
        dt   = parse_iso(wa)
        # Discord renders <t:unix:R> as a live relative countdown and <t:unix:f>
        # as a localized datetime — auto-converted to each viewer's timezone.
        lvl_txt = f" Level **L{lvl}**." if isinstance(lvl, int) else ""
        if dt:
            unix = int(dt.timestamp())
            change_line = f"Pending — activates <t:{unix}:R> (<t:{unix}:f>).{lvl_txt}"
        else:
            change_line = f"Pending activation.{lvl_txt}"
    elif kind == "resistance_full":
        rr  = event.get("curr_resistance") or {}
        val = rr.get("value")
        mx  = rr.get("max")
        if isinstance(val, (int, float)) and isinstance(mx, (int, float)):
            change_line = f"Resistance **{int(val)}/{int(mx)}**, bar is full. Liberation battle available."
        else:
            change_line = "Resistance bar is full. Liberation battle available."
    else:
        change_line = "Region state changed."

    description = header_line
    if change_line:
        description += f"\n\n{change_line}"

    # Append current resistance context to every alert except resistance_full,
    # which already leads with the bar. Shown only when we have the numbers.
    if kind != "resistance_full":
        rr  = event.get("curr_resistance") or {}
        val = rr.get("value")
        mx  = rr.get("max")
        if isinstance(val, (int, float)) and isinstance(mx, (int, float)) and mx > 0:
            pct = int(round(val / mx * 100))
            description += f"\n\nResistance: **{int(val)}/{int(mx)}** ({pct}%)"

    title_text = f"{emoji}  {title_label}"
    if len(title_text) > 256:
        title_text = title_text[:253] + "…"

    return {
        "title":       title_text,
        "url":         url,
        "color":       color,
        "description": description,
        "footer":      {"text": footer_text} if footer_text else None,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    }


# ── Discord delivery ─────────────────────────────────────────────────────

def _strip_none(d):
    if isinstance(d, dict):
        return {k: _strip_none(v) for k, v in d.items() if v is not None}
    if isinstance(d, list):
        return [_strip_none(x) for x in d]
    return d


def post_to_discord(webhook_url, embeds):
    if not webhook_url:
        log("ERROR: no webhook URL configured")
        return False

    chunks = [embeds[i:i + 10] for i in range(0, len(embeds), 10)]
    for idx, chunk in enumerate(chunks, 1):
        payload = json.dumps(
            _strip_none({
                "username": BOT_USERNAME,
                "embeds": chunk,
                "allowed_mentions": {"parse": []},
            })
        ).encode("utf-8")
        sent = False

        for attempt in range(1, MAX_RETRIES + 1):
            req = urllib.request.Request(
                webhook_url, data=payload, method="POST",
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            )
            try:
                with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                    if 200 <= r.status < 300:
                        sent = True
                        break
                    log(f"Discord chunk {idx}: HTTP {r.status}")
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    try:
                        retry_after = float(e.headers.get("Retry-After", "2"))
                    except (TypeError, ValueError):
                        retry_after = 2.0
                    log(f"Discord rate limited; sleeping {retry_after}s")
                    time.sleep(retry_after + 0.5)
                else:
                    log(f"Discord chunk {idx} HTTP {e.code} (attempt {attempt}/{MAX_RETRIES}): {e}")
                    time.sleep(RETRY_BACKOFF * attempt)
            except (urllib.error.URLError, TimeoutError) as e:
                log(f"Discord chunk {idx} transient (attempt {attempt}/{MAX_RETRIES}): {e}")
                time.sleep(RETRY_BACKOFF * attempt)

        if not sent:
            log(f"Discord chunk {idx} failed after {MAX_RETRIES} attempts")
            return False
        if idx < len(chunks):
            time.sleep(DISCORD_PAUSE)

    return True


def post_text_message(webhook_url, text):
    """Plain content message. Best-effort, never raises."""
    if not webhook_url:
        return False
    payload = json.dumps({
        "username": BOT_USERNAME,
        "content": text,
        "allowed_mentions": {"parse": []},
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            webhook_url, data=payload, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        urllib.request.urlopen(req, timeout=HTTP_TIMEOUT).read()
        return True
    except Exception as e:
        log(f"text message send failed (non-fatal): {e}")
        return False


def post_ops_message(webhook_url, text):
    post_text_message(webhook_url, f"🔧 **bunker-bot**: {text}")


# ── State + runs persistence ─────────────────────────────────────────────

def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        log(f"failed to load state.json ({e}); starting fresh")
        return {}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    tmp.replace(STATE_FILE)
    log(f"wrote state.json ({len(state)} regions)")


def load_runs():
    if not RUNS_FILE.exists():
        return []
    try:
        with RUNS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as e:
        log(f"failed to load runs.json ({e}); starting fresh history")
        return []


def append_run(entry):
    runs = load_runs()
    runs.append(entry)
    runs = runs[-RUNS_KEEP:]
    tmp = RUNS_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(runs, f, indent=2)
    tmp.replace(RUNS_FILE)


# ── Alert flow ───────────────────────────────────────────────────────────

def run_alert():
    if not WEBHOOK_URL:
        log("FATAL: DISCORD_BUNKER_WEBHOOK_URL not set")
        return 1

    started_at = datetime.now(timezone.utc).isoformat()

    try:
        regions = fetch_all_regions()
    except Exception as e:
        log(f"failed to fetch regions: {e}")
        post_ops_message(WEBHOOK_URL, f"failed to fetch regions: `{e}`, will retry next run")
        append_run({"at": started_at, "success": False, "error": str(e),
                    "total_events": 0, "monitored_events": 0, "regions": 0})
        return 0

    id_to_code = build_country_id_to_code(regions)
    current    = build_current_state(regions, id_to_code)

    # Enrich monitored regions that have a bunker with willBeActiveAt / status
    # from the dedicated upgrade endpoint (the bulk snapshot lacks it).
    for rid, up in collect_bunker_upgrades(current).items():
        if rid in current:
            current[rid]["bunker_upgrade"] = up

    previous = load_state()

    if not previous:
        watched = sorted(MONITORED_COUNTRY_CODES)
        log("first run, snapshotting, no alerts will be sent")
        post_ops_message(
            WEBHOOK_URL,
            f"first run, watching **{len(current)}** regions worldwide. "
            f"Alerting on transitions in regions originally belonging to: "
            f"{', '.join(c.upper() for c in watched)}."
        )
        save_state(current)
        append_run({"at": started_at, "success": True, "first_run": True,
                    "total_events": 0, "monitored_events": 0, "regions": len(current)})
        return 0

    # NOTE: detect_transitions also writes the resistance `alerted` hysteresis
    # flag back onto `current`, so it must run before save_state(current).
    all_events = detect_transitions(previous, current)
    events     = [e for e in all_events if is_monitored(e)]
    log(f"detected {len(all_events)} transition(s), {len(events)} in monitored countries")

    if events:
        priority = {
            "destroyed": 0, "ownership_changed": 1, "went_offline": 2,
            "battle_started": 3, "resistance_full": 3, "built": 4,
            "construction_started": 5, "bunker_activating": 6,
            "came_online": 7, "level_changed": 8, "battle_ended": 9,
        }
        events.sort(key=lambda e: (
            priority.get(e["type"], 99),
            origin_country_code(e.get("region_code")),
            e.get("region_name") or "",
        ))

        ok = post_to_discord(WEBHOOK_URL, [format_event_embed(ev) for ev in events])
        if not ok:
            post_ops_message(
                WEBHOOK_URL,
                f"FAILED to deliver {len(events)} alert(s) after retries. Check webhook health."
            )
            append_run({"at": started_at, "success": False, "delivery_failed": True,
                        "total_events": len(all_events), "monitored_events": len(events),
                        "regions": len(current)})
            # Don't save state; next run re-detects and retries.
            return 0

    save_state(current)
    append_run({"at": started_at, "success": True,
                "total_events": len(all_events), "monitored_events": len(events),
                "regions": len(current)})
    return 0


# ── Heartbeat flow ───────────────────────────────────────────────────────

def run_heartbeat():
    if not WEBHOOK_URL:
        log("FATAL: DISCORD_BUNKER_WEBHOOK_URL not set")
        return 1

    runs = load_runs()
    now = datetime.now(timezone.utc)

    if not runs:
        post_text_message(
            WEBHOOK_URL,
            "💚 **bunker-bot heartbeat**\nNo run history found yet. Waiting for the first alert run."
        )
        return 0

    # Last successful run
    successful = [r for r in runs if r.get("success")]
    last_success = successful[-1] if successful else None
    last_any     = runs[-1]

    if last_success:
        last_at = parse_iso(last_success.get("at"))
        hours_since = (now - last_at).total_seconds() / 3600 if last_at else None
    else:
        hours_since = None

    # Window: last 24h of runs
    cutoff = now - timedelta(hours=24)
    recent = [r for r in runs if (parse_iso(r.get("at")) or now) >= cutoff]
    runs_24h     = len(recent)
    success_24h  = sum(1 for r in recent if r.get("success"))
    events_24h   = sum(r.get("monitored_events", 0) for r in recent if r.get("success"))
    regions_seen = last_any.get("regions") or "?"

    is_stale = hours_since is None or hours_since > HEARTBEAT_STALE_HOURS
    if is_stale:
        emoji = "⚠️"
        if hours_since is None:
            status_line = "No successful run on record."
        else:
            status_line = (f"**Last successful run was {hours_since:.1f}h ago.** "
                           f"Cron may be stalled or the API may be unreachable.")
    else:
        emoji = "💚"
        status_line = f"Last successful run **{hours_since:.1f}h ago**."

    watched = sorted(MONITORED_COUNTRY_CODES)
    message = (
        f"{emoji} **bunker-bot · daily heartbeat**\n"
        f"{status_line}\n"
        f"Last 24h: **{runs_24h}** runs ({success_24h} successful), "
        f"**{events_24h}** alerts sent.\n"
        f"Watching **{regions_seen}** regions in: "
        f"{', '.join(c.upper() for c in watched)}."
    )

    ok = post_text_message(WEBHOOK_URL, message)
    log("heartbeat sent" if ok else "heartbeat send failed")
    return 0 if ok else 1


# ── CLI ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="warera bunker bot")
    parser.add_argument("--heartbeat", action="store_true",
                        help="Run heartbeat mode (post daily status, no API or state changes)")
    args = parser.parse_args()
    return run_heartbeat() if args.heartbeat else run_alert()


if __name__ == "__main__":
    sys.exit(main())
