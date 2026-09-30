<img src="brands/icon.png" width="72" align="right" alt="">

# 1st Energy for Home Assistant

Pulls your electricity usage and cost from 1st Energy into Home Assistant's
**Energy dashboard**, with hourly detail and full history back to the day
your account was opened.

> **Unofficial and independent.** This project is not affiliated with,
> endorsed by, or sponsored by 1st Energy Pty Ltd, who provide no support
> for it. "1st Energy" and the 1st Energy logo are their trademarks, used
> here only to identify the service this integration connects to. See
> [TRADEMARKS.md](TRADEMARKS.md).
>
> **It uses a private API.** 1st Energy publish no public API. Everything
> here was worked out from their customer portal, and they may change or
> block it at any time, without notice.

![Australia only](https://img.shields.io/badge/region-Australia-blue)
![Electricity](https://img.shields.io/badge/fuel-electricity-yellow)
![hacs](https://img.shields.io/badge/HACS-custom%20repository-orange)

## What you get

- **Hourly** consumption and cost as long-term statistics, ready to select
  in the Energy dashboard. Costs are 1st Energy's own figures, time-of-use
  bands included, not an estimate from a rate you type in.
- A **historical backfill** on first run, going back to the day your
  account was opened (or five years, whichever is shorter).
- **Revisions applied.** Each poll re-reads the last six days and corrects
  anything that changed, which also fills any gap left by a missed poll or
  a Home Assistant outage.
- **Account sensors:** balance, next invoice amount, next invoice due date,
  and how far the meter data currently reaches.

1st Energy's meter data runs about a day behind. This polls **every 6
hours**, so yesterday's usage turns up soon after it is published. It is
not, and cannot be, a live power meter: for that you want a pulse or
optical reader on the meter itself.

## Requirements

- A 1st Energy **electricity** account you can sign in to at
  [myaccount.1stenergy.com.au](https://myaccount.1stenergy.com.au), with
  usage data showing there.
- Home Assistant **2026.3 or newer**.
- The recorder integration enabled, which it is by default.

## Install

### HACS

1. Install [HACS](https://hacs.xyz/docs/use/download/download/) if you haven't already.
2. [![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=JKook-Plus&repository=ha-1st-Energy&category=integration)
3. Press **Download**.
4. Restart Home Assistant.

That button adds this repository to HACS for you. If it doesn't work (the
My Home Assistant links need to be [set up
once](https://my.home-assistant.io/) per instance), do it by hand instead:
**HACS → ⋮ → Custom repositories**, add
`https://github.com/JKook-Plus/ha-1st-Energy` with category
**Integration**, then install **1st Energy** and restart.

### Manual

Copy `custom_components/first_energy` into your `config/custom_components/`
directory and restart Home Assistant.

## Set up

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=first_energy)

Or by hand: **Settings → Devices & services → Add integration → 1st
Energy.**

Sign in with the email address and password you use at
`myaccount.1stenergy.com.au`. If the login covers more than one electricity
account you'll be asked which to add; repeat the process for the others.

The history import then starts in the background. The last few days appear
straight away; older history fills in a month at a time, oldest first, and
each month is saved as it arrives. If a restart or an error interrupts it,
it picks up where it stopped.

If 1st Energy ever rejects the saved password, Home Assistant raises a
re-authentication prompt, and your history is kept.

## Add it to the Energy dashboard

Nothing appears automatically; you have to point the dashboard at the
statistics once. The recent days are written within a minute of setup, so
if the dropdowns are empty, give it a moment and reload.

Go to **Settings → Dashboards → Energy → Grid consumption → Add
consumption**, and in the *Configure grid connection* dialog:

| Field | Set it to |
|---|---|
| **Energy imported from grid** | `1st Energy <NMI> energy` |
| **Energy exported to grid** | *leave empty*: solar export isn't imported |
| **Cost tracking** | **Use an entity tracking the total costs** |
| **Entity with the total costs** | `1st Energy <NMI> cost` |
| **Type of power measurement** | **No power sensor** |

Then **Save**.

Two of those are easy to get wrong. *Use an entity with current price*
looks plausible but is for a live tariff; 1st Energy gives costs already
calculated, so the total-cost option is the right one. And **No power
sensor** is correct rather than a limitation: power sensors measure
instantaneous watts, and this data is hourly totals arriving a day late.

Once saved, the dashboard shows your full history as the import fills it
in, rather than starting from today.

The statistic is the meter's total: a controlled-load register (such as
off-peak hot water) is added in, and registers the meter no longer uses are
left out. Statistics are named `first_energy:energy_<nmi>` and
`first_energy:cost_<nmi>`.

### If it isn't in the dropdown

The list only offers statistics that already exist, so an empty dropdown
means nothing has been written yet. Check **Developer tools → Statistics**
and filter for `first_energy`; if nothing is there, see
[Troubleshooting](#troubleshooting).

## Sensors

Each account gets a device, **1st Energy \<account number\>**, with:

| Sensor | What it shows |
|---|---|
| **Account balance** | Current balance, in AUD |
| **Next invoice amount** | The unpaid invoice due soonest, in AUD |
| **Next invoice due** | Its due date |
| **Meter data up to** | The latest day with meter data, which shows the one-day lag at a glance |

Consumption and cost are deliberately **not** sensors; see
[How it works](#how-it-works).

## Limitations

- **Electricity only.** Gas accounts have no connection this can read.
- **A day behind.** Today's usage won't appear; yesterday's generally will.
- **One connection per account.** An account with more than one
  electricity connection imports only the first, and logs a warning.
- **Solar export is not imported,** and meters with solar haven't been
  tested.
- **Private API.** It can break without warning if 1st Energy change their
  portal.

## Troubleshooting

**"That email address and password were rejected."** Check them by
signing in at [myaccount.1stenergy.com.au](https://myaccount.1stenergy.com.au).

**"Could not reach 1st Energy."** Usually temporary; wait a few minutes and
retry. If it persists, 1st Energy may have changed their site.

**Energy dashboard shows nothing.** The import runs in the background after
setup; give it a minute, then check **Developer tools → Statistics** for
`first_energy:`. The dashboard only lists statistics that already exist.

**"1st Energy history import keeps failing."** This repair appears after
five failed attempts in a row. Recent days are still updated, and each
retry continues from where the last one stopped; the repair clears itself
once an attempt succeeds.

**History re-imports after an update.** Some updates fix how past data was
stored and re-run the history import once to correct it. That's expected;
the dashboard keeps working while it runs.

**Numbers stop updating.** Check the **Meter data up to** sensor: if it's
yesterday, everything is current. Enable debug logging to see what the
poll is doing:

```yaml
logger:
  logs:
    custom_components.first_energy: debug
```

## Security

Your 1st Energy password is stored in the Home Assistant config entry,
which lives as plain JSON under `config/.storage/`. That is standard for
integrations that must log in with a password, but it is worth knowing:
anyone who can read that directory can read the password. The integration
only reads from your account; it never changes anything.

The password is sent to 1st Energy once each time Home Assistant starts.
After that, the session is kept alive with the refresh token the login
returns, which is held in memory only and never written to disk. The
password is sent again only if 1st Energy refuses a refresh.

If you capture HAR files while investigating the API yourself, they
contain your password, live tokens, NMI and address in plain text. Keep
them out of version control; `.gitignore` here already excludes `*.har`.

## Development

```bash
uv sync --group dev
uv run pytest
uv run ruff check custom_components tests
uv run mypy custom_components
```

Tests run against synthetic payloads in the shape the API returns
(`dev/make_synthetic_fixtures.py` regenerates them). Sanitised captures from
a live account (`dev/capture_fixtures.py`) can replace them for a stronger
check. The Home Assistant layer is tested against a real recorder database
via `pytest-homeassistant-custom-component`.

The client under `custom_components/first_energy/api/` and the models under
`domain/` import nothing from Home Assistant, so they can be tested, and
reused, on their own.

`brands/make_assets.py` rebuilds the icon and logo; see
[brands/README.md](brands/README.md).

## How it works

Home Assistant's long-term statistics take hourly buckets carrying an
absolute running sum. 1st Energy's data is dated 5-minute reads arriving a
day late, not a ticking meter, so the integration adds them up into UTC
hours and writes **external statistics** directly, the same approach
core's `opower` integration uses for utility billing data. A sensor would
file yesterday's kilowatt-hours under today and skew every Energy dashboard
total.

**The energy data is not an entity.** `first_energy:energy_…` and
`first_energy:cost_…` are statistics, not sensors: they live in the
recorder's `statistics` table, appear under **Developer tools →
Statistics**, and won't show up in **Developer tools → States** or in
templates. The Energy dashboard reads statistics directly, which is why
this works without an entity in between.

Consequences worth knowing:

- Each write continues the running total from the last stored hour before
  it, however far back, and carries any later hours along. The total never
  resets after a gap.
- Re-importing is safe. Statistics are keyed by hour, so a re-run
  overwrites rather than duplicating.
- The backfill runs oldest first and saves its place after each month, so
  an interrupted one resumes instead of starting over.

## Licence

The **code** is MIT; see [Licence](Licence).

That licence does not extend to 1st Energy's trademarks or logo, which
remain their property. If you fork or redistribute this, the marks are
yours to consider separately; [TRADEMARKS.md](TRADEMARKS.md) sets out the
position, including how to get them removed.
