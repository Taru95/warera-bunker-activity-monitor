# warera-bunker-activity-monitor

A Discord notification bot for War Era. It polls the world map every 2 hours, detects bunker and region events, and posts an alert to a Discord channel whenever something changes in a region that originally belonged to Germany.

Region ownership in War Era changes through conquest, but a region's original owner never changes. The bot keys its filter on the original owner, so a German region that has been occupied by another country is still watched.

## What it tracks

Each run compares the current world state against the last saved snapshot and emits one or more events per region:

| Event | Meaning |
|---|---|
| `came_online` | Bunker started running |
| `went_offline` | Bunker stopped running |
| `level_changed` | Running level changed |
| `built` | A bunker entry appeared |
| `destroyed` | A bunker entry disappeared |
| `ownership_changed` | The region's controlling country changed |
| `construction_started` | Bunker construction began |
| `battle_started` | A battle began on the region |
| `battle_ended` | The active battle finished |
| `bunker_activating` | Bunker is pending and will activate at a scheduled time |
| `resistance_full` | An occupied region's resistance bar hit max, so a liberation battle can be started |

The game does not expose why a bunker changed state (oil exhaustion, manual disable, battle damage), so the alert states the change and leaves humans to investigate.

The bot watches Germany only. To watch more countries, add their two-letter codes to `MONITORED_COUNTRY_CODES` in `alert.py`.

## Rolling it out

The bot runs on a GitHub Actions cron. No server to maintain. Follow these steps in order, because each one depends on the one before it. The final step is a live test, so the data proxy and the webhook both need to exist first.

### 1. Create the repo and push the code

```bash
git init
git add alert.py README.md
git commit -m "initial commit"
git branch -M main
git remote add origin https://github.com/<you>/warera-bunker-activity-monitor.git
git push -u origin main
```

Do not commit any secrets. They go in Cloudflare and GitHub, not the code.

### 2. Set up the data proxy (Cloudflare Worker)

The bot does not talk to War Era directly. It reads its data through a small Cloudflare Worker that forwards requests to the warerastats gateway. The other tools in this project already use one at `warera-proxy.toie.workers.dev`.

If that proxy is already deployed, set `PROXY_BASE` at the top of `alert.py` to `https://warera-proxy.toie.workers.dev/trpc` and skip to step 3.

To deploy your own:

1. Get a gateway API key. It is issued by War Era (Your profile > Settings > API).
2. Sign in at dash.cloudflare.com, open Workers & Pages, click Create, then Create Worker. Give it a name (for example `warera-proxy`) and click Deploy.
3. Click Edit code, replace everything in the editor with the script below, and click Deploy.

```js
export default {
  async fetch(request, env) {
    const cors = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
      "Access-Control-Max-Age": "86400",
    };

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: cors });
    }
    if (request.method !== "GET") {
      return new Response("Method not allowed", { status: 405, headers: cors });
    }

    const url = new URL(request.url);
    if (!url.pathname.startsWith("/trpc/")) {
      return new Response("Not found", { status: 404, headers: cors });
    }

    // Forward to the warerastats gateway. The gateway requires an API key,
    // read from the WARERA_API_KEY secret so it never appears in this code.
    const upstream = "https://gateway.warerastats.io" + url.pathname + url.search;
    const upstreamRes = await fetch(upstream, {
      headers: {
        "X-API-Key": env.WARERA_API_KEY,
        Accept: "application/json",
      },
    });

    const body = await upstreamRes.text();
    return new Response(body, {
      status: upstreamRes.status,
      headers: {
        ...cors,
        "Content-Type": upstreamRes.headers.get("Content-Type") || "application/json",
      },
    });
  },
};
```

4. Add the key as a secret so it stays out of the code. On the worker's page go to Settings, then Variables and Secrets, Add variable. Name it `WARERA_API_KEY`, paste the key as the value, mark it as a Secret (encrypted), and Save. Redeploy if prompted.
5. Set `PROXY_BASE` at the top of `alert.py` to `https://<worker-name>.<your-subdomain>.workers.dev/trpc`.
6. Confirm it works. Open this URL in a browser; it should return JSON starting with `{"result":`

   `https://<worker-name>.<your-subdomain>.workers.dev/trpc/region.getRegionsObject`

### 3. Get the Discord webhook URL

This is the bot's only credential, and it acts as the key that lets the bot post to one channel. Treat it like a password and never put it in the code.

1. In Discord, hover the target channel and click the gear icon (Edit Channel).
2. Go to Integrations, then Webhooks, then New Webhook.
3. Give it a name (for example "Bunker Bot") and confirm the channel.
4. Click Copy Webhook URL.

The URL looks like `https://discord.com/api/webhooks/<id>/<token>`. Keep it private and click Regenerate on the same screen if it ever leaks.

### 4. Add the webhook as a repository secret

In the repo on GitHub: Settings, Secrets and variables, Actions, New repository secret.

- Name: `DISCORD_BUNKER_WEBHOOK_URL`
- Value: the webhook URL you copied.

### 5. Add the monitor workflow

Create `.github/workflows/monitor.yml`:

```yaml
name: bunker-monitor

on:
  schedule:
    - cron: "0 */2 * * *"   # every 2 hours
  workflow_dispatch:          # lets you run it by hand from the Actions tab

permissions:
  contents: write             # needed to commit state back

concurrency:
  group: bunker-state         # never let two runs race on the committed state
  cancel-in-progress: false

jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install requests
      - run: python alert.py
        env:
          DISCORD_BUNKER_WEBHOOK_URL: ${{ secrets.DISCORD_BUNKER_WEBHOOK_URL }}
      - name: Commit updated state
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add state.json runs.json
          git diff --quiet --cached || git commit -m "update state [skip ci]"
          git push
```

### 6. Add the heartbeat workflow

Create `.github/workflows/heartbeat.yml`. This run posts a daily status summary and warns the channel if the last successful run is more than 4 hours old, which usually means the cron has stalled. It does not write state, so it needs no extra permissions:

```yaml
name: bunker-heartbeat

on:
  schedule:
    - cron: "0 9 * * *"       # once a day at 09:00 UTC
  workflow_dispatch:

jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install requests
      - run: python alert.py --heartbeat
        env:
          DISCORD_BUNKER_WEBHOOK_URL: ${{ secrets.DISCORD_BUNKER_WEBHOOK_URL }}
```

### 7. Seed and verify

Push the workflow files, then open the Actions tab and trigger `bunker-monitor` once by hand with "Run workflow". The first run snapshots the world, seeds `state.json`, and sends no alerts. Trigger it again to confirm it detects changes and posts to the channel. After that the cron takes over.

Two practical notes. GitHub Actions cron is best-effort, not exact, and gets delayed under load, so the gap between runs varies (the timestamps in `runs.json` show this). And the bot only alerts on changes between two runs it actually saw, so if a run is skipped, anything that happened and reverted in that window is missed. A 2 hour cadence is a deliberate trade between freshness and noise.

## State files

Both files are committed back by the workflow and should be kept in the repo. On a fresh rollout you do not need to create them, the first run writes them. If you want the paths to exist up front, commit `state.json` as `{}` and `runs.json` as `[]`.

`state.json` holds a per-region snapshot from the last run. It is the baseline the next run compares against. Deleting it forces a fresh seed on the next run (no alerts that run).

`runs.json` is a rolling log of the last 100 runs, recording timestamp, success, event counts, and region count. The heartbeat reads it to build its summary.

## How ownership is resolved

Three fields on each region matter:

- `countryCode` is the original owner's code and never changes.
- `initialCountry` is the original owner's id and matches `countryCode`.
- `country` is the current controller's id and changes on conquest.

The current controller's code is resolved by mapping `country` against an id-to-code table built from every region's `initialCountry` to `countryCode` pairing. Alerts say "Occupied by" when the current holder is not the original owner, and "Controlled by" otherwise.

## How activation is detected

The bulk region object's `bunker.status` can be stale, and it never carries the activation timestamp. So for any monitored region that has a bunker, the bot makes one extra call to `upgrade.getUpgradeByTypeAndEntity` to read the real status and `willBeActiveAt`. A `bunker_activating` alert fires once when the bunker first becomes pending or when its activation time changes, so the same pending state across multiple polls does not re-alert.

## How resistance is handled

Resistance only climbs while a region is occupied (owner-controlled regions decay), so `resistance_full` fires only for occupied regions. Because `resistanceMax` creeps up with development, a region pinned at the cap can briefly read just under it. A hysteresis flag suppresses repeat alerts until resistance falls back below 90% of max, giving one alert per cycle.

## Reliability

The Cloudflare proxy occasionally times out or cold-starts slowly. The bot retries transient failures (HTTP 5xx, timeouts, connection errors) up to 3 times with a backoff. If a run still fails to fetch, it posts an ops message, logs the failure to `runs.json`, and exits cleanly so the cron does not spam errors. If alert delivery fails after retries, state is not saved, so the next run re-detects and retries the same alerts.