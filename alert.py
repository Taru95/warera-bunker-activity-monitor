#!/usr/bin/env python3
"""
warera-bunker-notifications-discord

Polls every 2h via GitHub Actions cron. Detects bunker state transitions
across all regions worldwide and posts them to Discord — but only fires
alerts for regions whose current OR previous controller is in the
MONITORED_COUNTRY_CODES allowlist below.

Transitions detected:
  - came_online   — bunker started running (joined activeUpgradeLevels)
  - went_offline  — bunker stopped running (left activeUpgradeLevels)
  - level_changed — running level changed (e.g. L2 -> L1)
  - built         — bunker entry appeared (didn't exist in upgradesV2)
  - destroyed     — bunker entry disappeared from upgradesV2

We CANNOT tell *why* a bunker transitioned (oil exhausted / manually
disabled / battle damage). The alert states the change and lets humans
investigate cause.

State is persisted in state.json and committed back to the repo by the
workflow, so each cron run has the previous snapshot to compare against.

Exit conventions:
  - Always exit 0 unless config is broken. Transient API/Discord errors
    are logged and the next cron run retries naturally.
  - On a delivery failure, state.json is NOT saved, so the same
    transitions will be detected and re-attempted next run.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# --- Config -----------------------------------------------------------------

PROXY_BASE     = "https://warera-proxy.toie.workers.dev/trpc"
GAME_BASE      = "https://app.warera.io"
STATE_FILE     = Path(__file__).parent / "state.json"
WEBHOOK_URL    = os.environ.get("DISCORD_BUNKER_WEBHOOK_URL", "")
HTTP_TIMEOUT   = 30
MAX_RETRIES    = 3
RETRY_BACKOFF  = 5     # seconds, multiplied by attempt number
DISCORD_PAUSE  = 0.6   # seconds between Discord chunks
USER_AGENT     = "warera-bunker-bot/1.0"

# Only emit alerts for regions whose current OR previous controller is one of
# these 2-letter game country codes. Edit this set to widen/narrow scope.
MONITORED_COUNTRY_CODES = {
    "de",  # Germany
    "no",  # Norway
    "se",  # Sweden
    "fi",  # Finland
    "ie",  # Ireland
    "uk",  # United Kingdom
    "pt",  # Portugal
    "dk",  # Denmark
    "be",  # Belgium
    "nl",  # Netherlands (Holland)
}

# Country names for display. Unknown codes fall back to uppercase code.
COUNTRY_NAMES = {
    # Monitored
    "de": "Germany",  "no": "Norway",   "se": "Sweden",      "fi": "Finland",
    "ie": "Ireland",  "uk": "United Kingdom", "pt": "Portugal",
    "dk": "Denmark",  "be": "Belgium",  "eg": "Egypt",       "nl": "Netherlands",
    # Common neighbours & likely opponents
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


# --- Generic helpers --------------------------------------------------------

def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def http_get_json(url: str) -> dict:
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


def flag(cc: str) -> str:
    """2-letter country code → flag emoji."""
    if not cc or len(cc) != 2 or not cc.isalpha():
        return "🏳️"
    return "".join(chr(0x1F1E6 + ord(ch) - ord('a')) for ch in cc.lower())


def country_label(cc: str) -> str:
    """Human-readable country name; falls back to uppercased code."""
    if not cc:
        return "Unknown"
    return COUNTRY_NAMES.get(cc.lower(), cc.upper())


def country_with_flag(cc: str) -> str:
    """e.g. '🇩🇪 Germany'."""
    if not cc:
        return "🏳️ Unknown"
    return f"{flag(cc)} **{country_label(cc)}**"


# --- API --------------------------------------------------------------------

def fetch_all_regions() -> dict:
    """One call → every region in the game. Returns {id: region_data}."""
    body = http_get_json(f"{PROXY_BASE}/region.getRegionsObject")
    data = body.get("result", {}).get("data", {})
    if not isinstance(data, dict):
        raise RuntimeError(f"region.getRegionsObject returned unexpected shape: {type(data)}")
    log(f"fetched {len(data)} regions")
    return data


# --- State extraction -------------------------------------------------------

def extract_bunker_state(region: dict) -> dict:
    """
    Two API fields capture everything about a bunker:
      - activeUpgradeLevels.bunker  → currently running at this level (or absent)
      - upgradesV2.upgrades.bunker  → physical structure: status + built level
    """
    active = region.get("activeUpgradeLevels") or {}
    running_level = active.get("bunker")
    if not isinstance(running_level, int):
        running_level = None

    upgrades = ((region.get("upgradesV2") or {}).get("upgrades") or {})
    bunker = upgrades.get("bunker")
    if isinstance(bunker, dict):
        built_status = bunker.get("status")   # "active" | "disabled" | None
        built_level  = bunker.get("level")
        if not isinstance(built_level, int):
            built_level = None
    else:
        built_status = None
        built_level  = None

    return {
        "running_level": running_level,
        "built_status":  built_status,
        "built_level":   built_level,
    }


def build_country_id_to_code(regions: dict) -> dict:
    """
    Reverse-engineer a {country_id: country_code} map from the regions
    response so we can resolve the *original* owner of a conquered region
    (initialCountry is stored as an id, not a code).
    """
    out = {}
    for region in regions.values():
        if not isinstance(region, dict):
            continue
        cid = region.get("country")
        ccode = region.get("countryCode")
        if cid and ccode:
            out[cid] = ccode.lower()
    return out


def build_current_state(regions: dict, country_id_to_code: dict) -> dict:
    """Persistable snapshot of all regions."""
    now = datetime.now(timezone.utc).isoformat()
    out = {}
    for rid, region in regions.items():
        if not isinstance(region, dict):
            continue
        country_id         = region.get("country")
        initial_country_id = region.get("initialCountry")
        out[rid] = {
            "name":                  region.get("name"),
            "code":                  region.get("code"),
            "country_code":          (region.get("countryCode") or "").lower() or None,
            "country_id":            country_id,
            "initial_country_id":    initial_country_id,
            "initial_country_code":  country_id_to_code.get(initial_country_id),
            "bunker":                extract_bunker_state(region),
            "observed_at":           now,
        }
    return out


# --- Transition detection ---------------------------------------------------

def detect_transitions(prev: dict, curr: dict) -> list:
    events = []

    for rid in set(prev.keys()) | set(curr.keys()):
        p = prev.get(rid)
        c = curr.get(rid)

        # First observation or vanished from API: skip.
        if p is None or c is None:
            continue

        p_b = p.get("bunker") or {}
        c_b = c.get("bunker") or {}

        p_has = p_b.get("built_status") is not None
        c_has = c_b.get("built_status") is not None

        if not p_has and c_has:
            events.append(_event("built", rid, p, c))
            continue
        if p_has and not c_has:
            events.append(_event("destroyed", rid, p, c))
            continue

        p_run = p_b.get("running_level")
        c_run = c_b.get("running_level")

        if p_run is None and c_run is not None:
            events.append(_event("came_online", rid, p, c))
        elif p_run is not None and c_run is None:
            events.append(_event("went_offline", rid, p, c))
        elif p_run is not None and c_run is not None and p_run != c_run:
            events.append(_event("level_changed", rid, p, c))

    return events


def _event(kind: str, rid: str, prev_region: dict, curr_region: dict) -> dict:
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
    }


def origin_country_code(region_code) -> str:
    """
    Extract the original country code from a region code like 'ie-leinster'.
    Region codes never change, so this is the canonical 'who originally
    owned this region' even after conquest.
    """
    if not region_code or "-" not in region_code:
        return ""
    return region_code.split("-", 1)[0].lower()


def is_monitored(event: dict) -> bool:
    """
    True if the region's *original* country is in the monitored allowlist.
    Uses the region code prefix (e.g. 'ie-leinster' -> 'ie'), which is
    immutable, so conquered regions stay watched regardless of current owner.
    """
    return origin_country_code(event.get("region_code")) in MONITORED_COUNTRY_CODES


# --- Discord formatting -----------------------------------------------------

# (emoji, title text, color, footer) per event type
_EMBED_META = {
    "came_online":   ("🟢", "Bunker came online",   COLOR_GREEN,
                      "Newly enabled. Likely refuelled or re-activated."),
    "went_offline":  ("🔴", "Bunker went offline",  COLOR_RED,
                      "Cause unknown: could be oil exhaustion, manual disable, or battle damage."),
    "level_changed": ("🟡", "Bunker level changed", COLOR_YELLOW,
                      "Level adjusted by the region's controller."),
    "built":         ("🔵", "Bunker built",         COLOR_BLUE,
                      "New construction completed."),
    "destroyed":     ("⚫", "Bunker destroyed",      COLOR_GRAY,
                      "Cause unknown: could be conquest, ownership change, or upgrade removal."),
}


def format_event_embed(event: dict) -> dict:
    kind        = event["type"]
    region_name = event["region_name"]
    region_code = event.get("region_code") or event["region_id"]
    curr_cc     = (event.get("country_code") or "").lower()
    rid         = event["region_id"]
    url         = f"{GAME_BASE}/region/{rid}"

    emoji, title_label, color, footer_text = _EMBED_META.get(
        kind, ("⚪", "Bunker change", COLOR_GRAY, "")
    )

    p_run      = event["prev_bunker"].get("running_level")
    c_run      = event["curr_bunker"].get("running_level")
    p_built_lv = event["prev_bunker"].get("built_level")
    c_built_lv = event["curr_bunker"].get("built_level")

    # First line: region + original country code, then current controller with flag.
    # e.g. "Northern Ireland (UK) · Controlled by 🇧🇪 Belgium"
    origin_cc = origin_country_code(region_code)
    origin_tag = f" ({origin_cc.upper()})" if origin_cc else ""
    controller = country_with_flag(curr_cc) if curr_cc else "🏳️ **Unknown**"
    header_line = f"**{region_name}**{origin_tag}  ·  Controlled by {controller}"

    # Second line: what changed in plain English.
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
    else:
        change_line = "Bunker state changed."

    description = f"{header_line}\n\n{change_line}"

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


# --- Discord delivery -------------------------------------------------------

def _strip_none(d):
    """Discord rejects null values in some fields; drop them."""
    if isinstance(d, dict):
        return {k: _strip_none(v) for k, v in d.items() if v is not None}
    if isinstance(d, list):
        return [_strip_none(x) for x in d]
    return d


def post_to_discord(webhook_url: str, embeds: list) -> bool:
    """POST embeds (max 10 per request — we chunk)."""
    if not webhook_url:
        log("ERROR: no webhook URL configured")
        return False

    chunks = [embeds[i:i + 10] for i in range(0, len(embeds), 10)]
    for idx, chunk in enumerate(chunks, 1):
        payload = json.dumps(
            _strip_none({"embeds": chunk, "allowed_mentions": {"parse": []}})
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


def post_ops_message(webhook_url: str, text: str) -> None:
    """Plain text ops message. Best-effort, never raises."""
    if not webhook_url:
        return
    payload = json.dumps({
        "content": f"🔧 **bunker-bot**: {text}",
        "allowed_mentions": {"parse": []},
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            webhook_url, data=payload, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        urllib.request.urlopen(req, timeout=HTTP_TIMEOUT).read()
    except Exception as e:
        log(f"ops message send failed (non-fatal): {e}")


# --- State persistence ------------------------------------------------------

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        log(f"failed to load state.json ({e}); starting fresh")
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    tmp.replace(STATE_FILE)
    log(f"wrote state.json ({len(state)} regions)")


# --- Main -------------------------------------------------------------------

def main() -> int:
    if not WEBHOOK_URL:
        log("FATAL: DISCORD_BUNKER_WEBHOOK_URL not set")
        return 1

    try:
        regions = fetch_all_regions()
    except Exception as e:
        log(f"failed to fetch regions: {e}")
        post_ops_message(WEBHOOK_URL, f"failed to fetch regions: `{e}`, will retry next run")
        return 0  # cron retries; don't fail the workflow on transient errors

    id_to_code = build_country_id_to_code(regions)
    current    = build_current_state(regions, id_to_code)
    previous   = load_state()

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
        return 0

    all_events = detect_transitions(previous, current)
    events     = [e for e in all_events if is_monitored(e)]
    log(f"detected {len(all_events)} transition(s), {len(events)} in monitored countries")

    if events:
        # Sort: most attention-grabbing first, then by origin country, then by region
        priority = {"destroyed": 0, "went_offline": 1, "built": 2,
                    "came_online": 3, "level_changed": 4}
        events.sort(key=lambda e: (
            priority.get(e["type"], 9),
            origin_country_code(e.get("region_code")),
            e.get("region_name") or "",
        ))

        ok = post_to_discord(WEBHOOK_URL, [format_event_embed(ev) for ev in events])
        if not ok:
            post_ops_message(
                WEBHOOK_URL,
                f"FAILED to deliver {len(events)} alert(s) after retries. Check webhook health."
            )
            # Don't save state; next run re-detects and retries.
            return 0

    save_state(current)
    return 0


if __name__ == "__main__":
    sys.exit(main())