# LogCheck

A small desktop tool for quickly checking Mythic+ players' Warcraft Logs parses while forming groups.

Type one or more characters, press Enter, and it shows each dungeon's highest key, percentile on that key,
DPS, median percentile across all runs, and run count. Every character is fetched in a single API request.

## Features

- **Fair percentiles.** Each run is compared only with runs at the same key level, so players who push
  high keys don't look great just because of the key level.
- **Pass/fail checks.** Set a minimum percentile (or minimum DPS) and/or minimum timed key to get a green
  check or red X per dungeon and per character.
- **Dungeon filter.** Show only one dungeon, such as the key you're forming for. Your choice is remembered.
- **Keep adding mode.** Stack several lookups into one list to compare applicants. Remove people one by
  one with ✕, or clear the whole list. Characters you collapse stay collapsed as you add more. Turn off
  **Expand new** to add people collapsed, for a compact list.
- Double-click a name to open their Warcraft Logs page. Right-click a name to open Warcraft Logs or
  Raider.IO, copy their Name-Realm, or remove them. Press **?** in the app for a full explanation.

## Requirements

- Python 3.9+ with Tkinter. The standard Windows installer from [python.org](https://www.python.org/downloads/)
  includes it. On Linux you may need `sudo apt install python3-tk`.
- No third-party packages.

## Setup

1. **Create a free Warcraft Logs API client**
   - Go to <https://www.warcraftlogs.com/api/clients> and sign in.
   - Click **Create Client**. Any name works, and the redirect URL can be anything
     (e.g. `http://localhost`). Leave "Public client" unchecked.
   - Keep the **Client ID** and **Client Secret** handy.

2. **Download LogCheck**
   ```
   git clone https://github.com/mikeyv19/logcheck.git
   cd logcheck
   ```
   Or use **Code → Download ZIP** on GitHub.

3. **Run it**
   - Windows: double-click `logcheck.pyw`, or run `pythonw logcheck.pyw`.
   - Other: `python3 logcheck.pyw`

4. On your first lookup, LogCheck opens the API clients page and asks for your Client ID and Secret.

Your credentials, access token, and settings are saved to `logcheck_config.json` next to the script.
That file is listed in `.gitignore` and stays on your computer. **Don't share it or commit it.**
To change credentials, delete that file (or its `client_id`/`client_secret` entries) and run LogCheck again.

### Optional: desktop shortcut with icon (Windows)

```
python logcheck.pyw --make-icon
```

This writes `logcheck.ico`. Right-click `logcheck.pyw` → **Create shortcut**, then open the shortcut's
**Properties → Change Icon** and select `logcheck.ico`.

## Usage

- Enter characters as `Name Realm` or `Name-Realm`, separated by commas, e.g.
  `Vardamere Sargeras, Someone-Area 52`. Names copied from in-game (`Someone-Area52`, `Someone-MoonGuard`)
  work too, because realms are matched against Warcraft Logs' server list.
- **Zone ID** is the Warcraft Logs zone (the `?zone=` value in a WCL URL). The default, `55`, is Midnight
  Mythic+ Season 2. The Dungeon filter list comes from the zone you look up.
- **Metric**: `dps` for damage dealers, `hps` for healers.
- **Options** lets you color rows by DPS instead of parse %. You set one reference DPS (per zone and metric),
  and rows get the parse colors by their % of it, e.g. 75% of the reference = purple.
- The API allows a set number of points per hour, and the remaining amount is shown bottom-right.
  Each lookup costs roughly 8 points per character.

## WoW addon: paste your whole applicant list

The `addon/LogCheck` folder is a small in-game addon. It never talks to the internet or to LogCheck. It
only puts your applicants' names in a box for you to copy.

1. Copy the `addon/LogCheck` folder into `World of Warcraft/_retail_/Interface/AddOns/`.
2. While your group is listed, click the **LogCheck** button at the top-right of the applicant list (or type `/logcheck`).
3. Press **Ctrl+C** (the box closes), then **Ctrl+V** in LogCheck.

Paste again whenever the applicant list changes. LogCheck looks up only the new applicants, so you don't
spend API points twice, and it removes applicants who have left the queue. People you added by hand stay.

If WoW lists the addon as out of date, check your game version with `/dump select(4, GetBuildInfo())` and
add that number to the `## Interface:` line in `LogCheck.toc`, or tick **Load out of date AddOns**.
