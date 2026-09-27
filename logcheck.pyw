"""LogCheck - quick Warcraft Logs best-parse lookup for group forming.

Enter one or more characters (comma or newline separated) as
"Name Realm" or "Name-Realm" and press Enter. All characters are fetched in a
single API request.

Requires a free Warcraft Logs API client: https://www.warcraftlogs.com/api/clients
(any name, any redirect URL). You'll be asked for the ID/secret on first run.
"""

import base64
import json
import re
import statistics
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import simpledialog, ttk

CONFIG_PATH = Path(__file__).with_name("logcheck_config.json")
TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
API_URL = "https://www.warcraftlogs.com/api/v2/client"

DEFAULTS = {"region": "us", "zone": "55", "metric": "dps", "judge": "Median", "min_mode": "Min %", "min_pct": "", "min_dps": "", "min_key": "", "dungeon": "", "keep": False,
            "expand_new": True, "sort_col": "", "sort_desc": True,
            "color_by": "parse", "ref_amount": {},  # ref_amount: "zone:metric" -> reference DPS text
            "dps_tiers": {}}  # dps_tiers: "zone:metric" -> {band %: min DPS text} for custom DPS colors
TEXT_COLS = ("#0", "spec")  # sort A-Z first; numbers sort high-to-low first
ALL_DUNGEONS = "All dungeons"
MIN_MODES = {"Min %": "min_pct", "Min DPS": "min_dps"}  # threshold mode -> config key for its value

# Warcraft Logs parse colors: (min percent, color)
PARSE_COLORS = [  # names: see COLOR_NAMES
    (100, "#e5cc80"),
    (99, "#e268a8"),
    (95, "#ff8000"),
    (75, "#a335ee"),
    (50, "#0070ff"),
    (25, "#1eff00"),
    (0, "#9d9d9d"),
]
COLOR_NAMES = ["Gold", "Pink", "Orange", "Purple", "Blue", "Green", "Gray"]

BG = "#1e1e1e"
FG = "#dddddd"
HEADER_BG = "#2d2d30"
HOVER_BG = "#3e3e42"  # hovered button/heading; dark enough for FG text
PRESS_BG = "#094771"


# ---------------------------------------------------------------- config/api

def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def get_token(cfg):
    if cfg.get("token") and cfg.get("token_expires", 0) > time.time() + 60:
        return cfg["token"]
    creds = f"{cfg['client_id']}:{cfg['client_secret']}".encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=b"grant_type=client_credentials",
        headers={"Authorization": "Basic " + base64.b64encode(creds).decode()},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)
    cfg["token"] = data["access_token"]
    cfg["token_expires"] = time.time() + data.get("expires_in", 3600)
    save_config(cfg)
    return cfg["token"]


def realm_slug(realm):
    """'Area 52' -> 'area-52', "Mal'Ganis" -> 'malganis'."""
    s = realm.strip().lower().replace("'", "")
    return re.sub(r"[\s_]+", "-", s)


def realm_key(realm):
    """Spelling-insensitive realm key: 'Area 52', 'Area52', 'area-52' -> 'area52'."""
    return re.sub(r"[\W_]+", "", realm.lower())


def realm_slugs(cfg, region):
    """{realm_key: WCL slug} for a region, from WCL's server list (all regions cached on first use)."""
    cache = cfg.get("realm_slugs") or {}
    if region not in cache:
        pages = " ".join(f"p{n}: servers(limit: 100, page: {n}) {{ data {{ name slug }} }}" for n in range(1, 6))
        data = post_query(cfg, f"{{ worldData {{ regions {{ slug {pages} }} }} }}")
        cache = {}
        for r in (data.get("worldData") or {}).get("regions") or []:
            slugs = cache.setdefault(r["slug"].lower(), {})
            for n in range(1, 6):
                for server in (r.get(f"p{n}") or {}).get("data") or []:
                    slugs[realm_key(server["name"])] = server["slug"]
                    slugs[realm_key(server["slug"])] = server["slug"]
        cfg["realm_slugs"] = cache
        save_config(cfg)
    return cache.get(region, {})


def resolve_slug(cfg, region, realm):
    """WCL slug for a realm however it's typed ('MoonGuard' -> 'moon-guard'); falls back to realm_slug."""
    try:
        slugs = realm_slugs(cfg, region)
    except Exception:  # server list is a nice-to-have; never block a lookup on it
        slugs = {}
    return slugs.get(realm_key(realm)) or realm_slug(realm)


def parse_characters(text):
    """Returns list of (name, realm) from 'Name Realm' / 'Name-Realm' entries."""
    out = []
    for entry in re.split(r"[,\n;]+", text):
        entry = entry.strip()
        if not entry:
            continue
        m = re.match(r"^([^\s-]+)[\s-]+(.+)$", entry)
        if not m:
            raise ValueError(f"'{entry}' needs a realm (e.g. Vardamere Sargeras)")
        out.append((m.group(1), m.group(2)))
    return out


def gql_str(s):
    return json.dumps(s)  # JSON string escaping is valid GraphQL string syntax


def post_query(cfg, query):
    def post(token):
        req = urllib.request.Request(
            API_URL,
            data=json.dumps({"query": query}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)

    try:
        data = post(get_token(cfg))
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
        cfg.pop("token", None)  # stale token, retry once
        data = post(get_token(cfg))

    if data.get("errors") and not data.get("data"):
        raise RuntimeError(data["errors"][0].get("message", "API error"))
    return data.get("data") or {}


def zone_encounters(cfg, zone):
    """[(encounter_id, name)] for a zone, cached in the config."""
    cache = cfg.setdefault("zone_encounters", {})
    if str(zone) not in cache:
        data = post_query(cfg, f"{{ worldData {{ zone(id: {zone}) {{ encounters {{ id name }} }} }} }}")
        z = (data.get("worldData") or {}).get("zone")
        if not z:
            raise ValueError(f"Unknown zone ID {zone}")
        cache[str(zone)] = [(e["id"], e["name"]) for e in z["encounters"]]
        save_config(cfg)
    return cache[str(zone)]


def is_timed(run):
    return run.get("medal") != "none"  # gold/silver/bronze = +3/+2/+1, none = depleted


def summarize_dungeon(name, rankings):
    """Per-dungeon stats from byBracket encounterRankings (percentiles vs same key level)."""
    ranks = (rankings or {}).get("ranks") or []
    if not ranks:
        return {"name": name, "runs": 0}
    top = max(ranks, key=lambda r: (r.get("bracketData") or 0, is_timed(r), r.get("rankPercent") or 0))
    timed_keys = [r["bracketData"] for r in ranks if is_timed(r) and r.get("bracketData")]
    return {
        "name": name,
        "runs": len(ranks),
        "key": top.get("bracketData"),
        "timed": is_timed(top),
        "max_timed": max(timed_keys, default=None),
        "key_pct": top.get("rankPercent"),
        "dps": top.get("amount"),
        "median": statistics.median(r.get("rankPercent") or 0 for r in ranks),
        "spec": top.get("spec") or "",
    }


def fetch_rankings(cfg, chars, region, zone, metric):
    """One GraphQL request for every character x dungeon, using aliases.

    Returns (results, slugs, rate_limit). Per character: None if not found, else
    {"name", "error", "dungeons"}. slugs are the resolved WCL realm slugs.
    """
    if not zone.strip():
        raise ValueError("Zone ID is required")
    encounters = zone_encounters(cfg, int(zone))
    slugs = [resolve_slug(cfg, region, realm) for _, realm in chars]
    enc_fields = " ".join(
        f"e{j}: encounterRankings(encounterID: {eid}, metric: {metric}, byBracket: true)"
        for j, (eid, _) in enumerate(encounters)
    )
    parts = [
        f"c{i}: character(name: {gql_str(name)}, serverSlug: {gql_str(slug)}, "
        f"serverRegion: {gql_str(region)}) {{ name {enc_fields} }}"
        for i, ((name, _), slug) in enumerate(zip(chars, slugs))
    ]
    data = post_query(
        cfg,
        "{ rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn } "
        "characterData { " + " ".join(parts) + " } }",
    )
    char_data = data.get("characterData") or {}

    results = []
    for i in range(len(chars)):
        char = char_data.get(f"c{i}")
        if char is None:
            results.append(None)
            continue
        dungeons, error = [], None
        for j, (_, ename) in enumerate(encounters):
            er = char.get(f"e{j}") or {}
            error = error or er.get("error")
            dungeons.append(summarize_dungeon(ename, er))
        results.append({"name": char["name"], "error": error, "dungeons": dungeons})
    return results, slugs, data.get("rateLimitData")


def fmt_rate(rate):
    if not rate:
        return ""
    left = rate["limitPerHour"] - rate["pointsSpentThisHour"]
    return (
        f"API: {left:,.0f} / {rate['limitPerHour']:,} left "
        f"(resets in {rate['pointsResetIn'] // 60}m)"
    )


# ---------------------------------------------------------------------- gui

def parse_color(pct):
    if pct is None:
        return FG
    for threshold, color in PARSE_COLORS:
        if pct >= threshold:
            return color
    return FG


def fmt_pct(v):
    return "-" if v is None else f"{v:.1f}"


def fmt_amount(v):
    return f"{v:,.0f}" if v else "-"


def fmt_key(key, timed):
    if not key:
        return "-"
    return f"+{key}" if timed else f"{key} \u2717"


def sort_by(items, value, desc):
    """Sorts items by value(item); items whose value is None always go last."""
    present = sorted((i for i in items if value(i) is not None), key=value, reverse=desc)
    return present + [i for i in items if value(i) is None]


def mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


HELP_TEXT = """\
HOW TO USE
  Type characters as "Name Realm" or "Name-Realm", separated by commas, then
  press Enter. All characters are fetched in one request, so a whole group is
  as fast as one person. Or copy names and press Ctrl+V anywhere in the
  window (or click "Paste & Check") to paste them in and check right away. Names copied from in-game work too, e.g.
  "Name-MoonGuard" or "Name-Area52" (realm spaces are optional).
  Double-click a character's name to open their Warcraft Logs page, or
  right-click it to open Warcraft Logs / Raider.IO, copy Name-Realm, or
  remove them.

  Click a column header to sort by it (click again to flip the order).

  Tick "Keep adding" to stack lookups: each search adds to the list instead
  of replacing it (searching someone already listed refreshes them), so you
  can compare many applicants. Click the ✕ on a character's row (or select
  it and press Delete) to remove them; "Clear" empties the list. Hover over any column header or box for a quick tip.

WHAT THE PERCENTAGES MEAN
  Every percentage is a PERCENTILE versus logged players of the same spec,
  in the same dungeon, at the SAME KEY LEVEL. 90 = did more damage than 90%
  of people who ran that dungeon at that key level. It is NOT a percentage
  of the top player's DPS. These match "Hist. %" on a player's run list.

  (Warcraft Logs' headline "Best %" ranks higher keys above lower keys no
  matter the DPS, so anyone pushing high keys looks 95+ there even with poor
  damage. LogCheck compares each run only against runs at its own key level.)

COLUMNS
  Key       Highest key they've run in that dungeon.
            +17 = timed.  17 \u2717 = depleted (over time).
  Key %     Their percentile ON that highest key - how well they actually
            played at the level they're pushing.
  DPS       Their DPS on that highest key.
  All DPS   Shown when a Dungeon is selected (DPS then reads "Dungeon
            DPS"): the character's average DPS across ALL dungeons, as
            if no dungeon were selected, to compare against that one.
  Median %  Middle of ALL their runs in that dungeon (each run vs its own
            key level). Half their runs are above, half below.
  Runs      Number of logged runs behind the numbers.
  Spec      Spec used on the highest key.
  Bold row  The character: highest timed key anywhere, averages of Key %,
            DPS and Median % across dungeons, total runs, and the spec
            on their highest key.

SORTING
  Click any column header to sort by it; click again to flip the order
  (\u25bc high-to-low, \u25b2 low-to-high). Characters are sorted by their bold
  row, and each character's dungeons by the same column. The sort stays
  as people are added or filters change, and is remembered next time.

COLORS  (by default, rows are colored by whatever "Judge by" is set to.
         In Options you can color by DPS instead: the same bands as a %
         of a reference DPS you set, e.g. 75% of it = purple, or type
         your own minimum DPS for each color with "DPS (custom)")
  Gray    0-24    below average
  Green   25-49   slightly below average
  Blue    50-74   solid, above average
  Purple  75-94   good
  Orange  95-98   excellent
  Pink    99      near the top
  Gold    100     best on record for that spec

MEDIAN OR KEY %?  -> Use Median.
  Median is their typical run, so it predicts what you'll get in YOUR key.
  Key % is one run - useful to see if they can hold up at high keys, but a
  single good or bad run can swing it. High Key % but low Median = can do
  well but isn't consistent. Low Key % on a high key = got carried up.

THRESHOLDS (green check / red X)
  Judge by   Which percentile to check: Median (recommended) or Key %.
  Min %      Lowest percentile you'll accept. Blank = no % requirement.
  Min DPS    Pick "Min DPS" in that dropdown to require raw DPS instead
             (their DPS on the highest key; 450k and 1.2m work). Each
             mode remembers its own value.
  Min key    Lowest TIMED key you'll accept. Blank = no key requirement.
             Depleted keys don't count toward this.
  Dungeon    Show only one dungeon (e.g. the key you're forming for).
             Character rows are then judged on that dungeon alone.
             Remembered between sessions.
  Changing these re-checks instantly, no new lookup needed.
  Dungeon rows are judged on that dungeon's numbers (good when forming for a
  specific dungeon). Character rows are judged on their average % across all
  dungeons (average DPS for Min DPS) and their highest timed key in any
  dungeon.

  Suggested settings (Judge by Median):
    40   not a liability
    50   solid, reliable invite
    75   strong player
  Set Min key to the level you're running, or 1-2 below it, so you know
  they've timed it before and aren't just farming lower keys.

CAVEATS
  - Check Runs: with only 1-2 runs, one run decides everything, so a green
    check means less.
  - Few people run very high keys, so percentiles there compare against a
    smaller, stronger pool.
  - Some specs are simply weaker this season; a percentile only compares a
    player to others of their spec.
  - Tanks and healers look low on DPS. Switch Metric to hps for healers."""

COLUMN_TIPS = {
    "#0": "Dungeon. Bold rows are the character (double-click to open WCL). Click any header to sort.",
    "key": "Highest key run in this dungeon. +17 = timed, 17 \u2717 = depleted.",
    "keypct": "Percentile on their highest key, vs same spec at the same key level.",
    "amount": "DPS on their highest key. With a dungeon selected, that dungeon's DPS.",
    "alldps": "Average DPS across ALL dungeons (ignores the dungeon filter), to compare with Dungeon DPS.",
    "median": "Median percentile of all their runs (each vs its own key level). Best signal for consistency.",
    "runs": "Number of logged runs. More runs = more reliable numbers.",
    "spec": "Spec used on the highest key.",
    "remove": "Click ✕ to remove that character from the list.",
}


def make_icon(color, strokes):
    """16x16 icon drawn from line segments (2px thick)."""
    img = tk.PhotoImage(width=16, height=16)
    for (x0, y0), (x1, y1) in strokes:
        steps = max(abs(x1 - x0), abs(y1 - y0))
        for i in range(steps + 1):
            x = round(x0 + (x1 - x0) * i / steps)
            y = round(y0 + (y1 - y0) * i / steps)
            img.put(color, to=(x, y, x + 2, y + 2))
    return img


def make_app_icon():
    """32x32 bar chart in parse colors."""
    img = tk.PhotoImage(width=32, height=32)
    img.put(HEADER_BG, to=(1, 1, 31, 31))
    for x, height, color in [(5, 10, "#0070ff"), (13, 16, "#a335ee"), (21, 22, "#ff8000")]:
        img.put(color, to=(x, 27 - height, x + 6, 27))
    img.put("#e5cc80", to=(3, 27, 29, 29))
    return img


def write_ico(path):
    """Writes a multi-size .ico (PNG-compressed entries) from the app icon."""
    import os
    import struct
    import tempfile

    root = tk.Tk()
    root.withdraw()
    base = make_app_icon()
    images = [(16, base.subsample(2)), (32, base), (64, base.zoom(2)), (256, base.zoom(8))]
    blobs = []
    for size, img in images:
        fd, tmp = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        img.write(tmp, format="png")
        blobs.append((size, Path(tmp).read_bytes()))
        os.remove(tmp)
    root.destroy()

    header = struct.pack("<HHH", 0, 1, len(blobs))
    offset = len(header) + 16 * len(blobs)
    entries, data = b"", b""
    for size, png in blobs:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    Path(path).write_bytes(header + entries + data)


def to_number(s):
    try:
        return float(s)
    except ValueError:
        return None


def to_amount(s):
    """DPS threshold: '450000', '450,000', '450k' or '1.2m'."""
    s = s.strip().lower().replace(",", "")
    scale = {"k": 1e3, "m": 1e6}.get(s[-1:], 1)
    n = to_number(s[:-1] if scale != 1 else s)
    return None if n is None else n * scale


class Tooltip:
    """Small hover tooltip. Attach to a widget, or drive manually via show/hide."""

    def __init__(self, root, widget=None, text=""):
        self.root, self.text, self.tip = root, text, None
        if widget is not None:
            widget.bind("<Enter>", lambda e: self.show(self.text, e.x_root, e.y_root))
            widget.bind("<Leave>", lambda e: self.hide())

    def show(self, text, x, y):
        self.hide()
        self.tip = tk.Toplevel(self.root)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x + 12}+{y + 16}")
        tk.Label(
            self.tip, text=text, bg="#333337", fg=FG, relief="solid", bd=1,
            padx=6, pady=3, justify="left", wraplength=320,
        ).pack()

    def hide(self):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = {**DEFAULTS, **load_config()}
        self.cfg["ref_amount"] = dict(self.cfg["ref_amount"])  # don't mutate DEFAULTS
        self.cfg["dps_tiers"] = dict(self.cfg["dps_tiers"])
        self.links = {}  # tree item id -> character URL

        root.title("LogCheck")
        self.app_icon = make_app_icon()
        root.iconphoto(True, self.app_icon)
        root.geometry("820x520")
        root.configure(bg=BG)
        self._style()

        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="Characters:").grid(row=0, column=0, sticky="w")
        self.entry = ttk.Entry(top)
        self.entry.grid(row=0, column=1, columnspan=7, sticky="ew", padx=(6, 0))
        self.entry.bind("<Return>", lambda e: self.search())
        self.entry.focus()

        ttk.Label(top, text="Region:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.region = ttk.Combobox(top, values=["us", "eu", "kr", "tw", "cn"], width=5, state="readonly")
        self.region.set(self.cfg["region"])
        self.region.grid(row=1, column=1, sticky="w", padx=(6, 12), pady=(6, 0))

        ttk.Label(top, text="Metric:").grid(row=1, column=2, sticky="w", pady=(6, 0))
        self.metric = ttk.Combobox(top, values=["dps", "hps", "bossdps", "tankhps"], width=8, state="readonly")
        self.metric.set(self.cfg["metric"])
        self.metric.grid(row=1, column=3, sticky="w", padx=(6, 12), pady=(6, 0))

        ttk.Label(top, text="Zone ID:").grid(row=1, column=4, sticky="w", pady=(6, 0))
        self.zone = ttk.Entry(top, width=6)
        self.zone.insert(0, self.cfg["zone"])
        self.zone.grid(row=1, column=5, sticky="w", padx=(6, 12), pady=(6, 0))

        ttk.Label(top, text="Judge by:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.judge = ttk.Combobox(top, values=["Median", "Key %"], width=7, state="readonly")
        self.judge.set(self.cfg["judge"] if self.cfg["judge"] in ("Median", "Key %") else "Median")
        self.judge.grid(row=2, column=1, sticky="w", padx=(6, 12), pady=(6, 0))

        self.min_mode = ttk.Combobox(top, values=list(MIN_MODES), width=8, state="readonly")
        self.min_mode.set(self.cfg["min_mode"] if self.cfg["min_mode"] in MIN_MODES else "Min %")
        self.min_mode.grid(row=2, column=2, sticky="w", pady=(6, 0))
        self.min_value = ttk.Entry(top, width=8)
        self.min_value.insert(0, self.cfg[MIN_MODES[self.min_mode.get()]])
        self.min_value.grid(row=2, column=3, sticky="w", padx=(6, 12), pady=(6, 0))

        ttk.Label(top, text="Min key:").grid(row=2, column=4, sticky="w", pady=(6, 0))
        self.min_key = ttk.Entry(top, width=6)
        self.min_key.insert(0, self.cfg["min_key"])
        self.min_key.grid(row=2, column=5, sticky="w", padx=(6, 12), pady=(6, 0))

        ttk.Label(top, text="Dungeon:").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.dungeon = ttk.Combobox(top, width=24, state="readonly")
        self.dungeon.set(self.cfg["dungeon"] or ALL_DUNGEONS)
        self.dungeon.grid(row=3, column=1, columnspan=3, sticky="w", padx=(6, 12), pady=(6, 0))
        self.refresh_dungeons()

        self.judge.bind("<<ComboboxSelected>>", lambda e: self.render())
        self.dungeon.bind("<<ComboboxSelected>>", lambda e: self.render())
        self.min_mode.bind("<<ComboboxSelected>>", lambda e: self.switch_min_mode())
        self.zone.bind("<KeyRelease>", lambda e: self.refresh_dungeons())
        for w in (self.min_value, self.min_key):
            w.bind("<KeyRelease>", lambda e: self.render())

        self.icons = {
            True: make_icon("#3ccf4e", [((2, 8), (6, 12)), ((6, 12), (13, 3))]),
            False: make_icon("#e04848", [((3, 3), (12, 12)), ((12, 3), (3, 12))]),
            None: tk.PhotoImage(width=16, height=16),
        }
        self.rows = []  # one dict per listed character: name, realm, region, zone, metric, char
        self.done_msg = ""
        self.items = {}  # character tree item id -> row
        self.expanded = {}  # _row_id -> whether that character's dungeons are shown

        self.keep = tk.BooleanVar(value=bool(self.cfg["keep"]))
        keep_box = ttk.Checkbutton(top, text="Keep adding", variable=self.keep, command=self.save_keep)
        keep_box.grid(row=3, column=4, columnspan=2, sticky="w", pady=(6, 0))
        self.expand_new = tk.BooleanVar(value=bool(self.cfg["expand_new"]))
        expand_box = ttk.Checkbutton(top, text="Expand new", variable=self.expand_new, command=self.save_expand)
        expand_box.grid(row=3, column=6, sticky="e", padx=(0, 6), pady=(6, 0))
        clear_btn = ttk.Button(top, text="Clear", command=self.clear)
        clear_btn.grid(row=3, column=7, sticky="e", pady=(6, 0))
        Tooltip(root, keep_box, "On: each search adds characters to the list instead of replacing it, "
                "so you can compare many people. Re-searching someone refreshes their data.")
        Tooltip(root, clear_btn, "Remove everyone from the list.")
        Tooltip(root, expand_box, "On: newly added characters show their dungeons. Off: they're added "
                "collapsed, for a compact list. People already listed keep however you left them.")

        side = ttk.Frame(top)
        side.grid(row=1, column=6, sticky="e", padx=(0, 6), pady=(6, 0))
        help_btn = ttk.Button(side, text="?", width=3, command=self.show_help)
        help_btn.pack(side="right")
        options_btn = ttk.Button(side, text="Options", command=self.show_options)
        options_btn.pack(side="right", padx=(0, 6))
        self.btn = ttk.Button(top, text="Check", command=self.search)
        self.btn.grid(row=1, column=7, sticky="e", pady=(6, 0))
        paste_btn = ttk.Button(top, text="Paste & Check", command=self.paste_search)
        paste_btn.grid(row=2, column=6, columnspan=2, sticky="e", pady=(6, 0))
        # Ctrl+V anywhere in the window pastes into Characters and checks. The small
        # filter boxes keep normal paste (see paste_search).
        for seq in ("<Control-v>", "<Control-V>"):
            self.entry.bind(seq, self.paste_search)
            root.bind(seq, self.paste_search)
        top.columnconfigure(6, weight=1)

        Tooltip(root, self.entry, "Name Realm or Name-Realm. Separate multiple characters with commas.")
        Tooltip(root, self.region, "Region the characters are on.")
        Tooltip(root, self.metric, "dps for damage dealers, hps for healers.")
        Tooltip(root, self.zone, "Warcraft Logs zone ID (from ?zone= in the URL). 55 = Mythic+ Season 2.")
        Tooltip(root, help_btn, "What do these numbers mean?")
        Tooltip(root, options_btn, "Display settings, such as coloring rows by DPS instead of parse %.")
        Tooltip(root, paste_btn, "Replace Characters with your clipboard and check. "
                "Ctrl+V does the same from anywhere in the window.")
        Tooltip(root, self.judge, "Which percentile the threshold checks. Median (typical run) is "
                "the better predictor; Key % is how they did on their highest key.")
        Tooltip(root, self.min_mode, "Threshold by percentile (Min %) or raw DPS on their highest key "
                "(Min DPS). Each keeps its own value.")
        Tooltip(root, self.min_value, "Minimum percentile or DPS to get a green check. Blank = no requirement. "
                "DPS accepts 450k / 1.2m. Character row uses their average across dungeons.")
        Tooltip(root, self.min_key, "Minimum key level. Blank = no key requirement. "
                "Only timed keys count. Character row uses their highest timed key anywhere.")
        Tooltip(root, self.dungeon, "Only show one dungeon. Character rows are then judged on that "
                "dungeon alone. List comes from the zone's dungeons; remembered between sessions.")

        cols = ("key", "keypct", "amount", "alldps", "median", "runs", "spec", "remove")
        self.tree = ttk.Treeview(root, columns=cols, show="tree headings")
        self.titles = {"#0": "Dungeon"}
        self.sort_col, self.sort_desc = self.cfg["sort_col"], self.cfg["sort_desc"]
        self.tree.heading("#0", anchor="w", command=lambda: self.sort_on("#0"))
        self.tree.column("#0", width=240)
        for col, title, width in [
            ("key", "Key", 55),
            ("keypct", "Key %", 70),
            ("amount", "DPS", 90),
            ("alldps", "All DPS", 80),
            ("median", "Median %", 80),
            ("runs", "Runs", 60),
            ("spec", "Spec", 110),
            ("remove", "", 30),
        ]:
            self.titles[col] = title
            if col != "remove":
                self.tree.heading(col, command=lambda c=col: self.sort_on(c))
            self.tree.column(col, width=width, anchor="center")
        self.update_headings()
        self.tree.pack(fill="both", expand=True, padx=8)
        self.tree.bind("<Double-1>", self.open_link)
        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<Delete>", lambda e: self.remove(self.tree.focus()))
        self.tree.bind("<Button-3>", self.show_menu)
        self.menu = tk.Menu(
            root, tearoff=0, bg=HEADER_BG, fg=FG, activebackground="#094771", activeforeground=FG, bd=0
        )
        self.head_tip, self.head_col = Tooltip(root), None
        self.tree.bind("<Motion>", self.on_tree_motion)
        self.tree.bind("<Leave>", lambda e: self._hide_head_tip())

        footer = ttk.Frame(root)
        footer.pack(fill="x", padx=8, pady=6)
        self.status = ttk.Label(footer, text="Enter: Name Realm, Name2 Realm2 ...  (double-click a name to open WCL)")
        self.status.pack(side="left")
        self.rate_label = ttk.Label(footer, text="", foreground="#9d9d9d")
        self.rate_label.pack(side="right")
        Tooltip(root, self.rate_label, "Warcraft Logs API points left this hour. "
                "Each lookup costs roughly 8 points per character.")

    def refresh_dungeons(self):
        """Fills the dungeon filter from the zone's cached encounter list."""
        names = [name for _, name in self.cfg.get("zone_encounters", {}).get(self.zone.get().strip(), [])]
        self.dungeon["values"] = [ALL_DUNGEONS, *sorted(names)]
        if names and self.dungeon.get() not in names:
            self.dungeon.set(ALL_DUNGEONS)  # saved dungeon isn't in this zone

    def _style(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=FG, fieldbackground=HEADER_BG)
        s.configure("TEntry", foreground=FG, insertcolor=FG)
        s.configure("TCombobox", foreground=FG, background=HEADER_BG, arrowcolor=FG)
        s.map(
            "TCombobox",
            fieldbackground=[("readonly", HEADER_BG)],
            foreground=[("readonly", FG)],
            background=[("pressed", PRESS_BG), ("active", HOVER_BG)],
        )
        s.configure("TButton", background=HEADER_BG, lightcolor=HEADER_BG, darkcolor=HEADER_BG)
        # clam's default hover/press colors are near-white, which hides the light text
        s.map(
            "TButton",
            background=[("disabled", BG), ("pressed", PRESS_BG), ("active", HOVER_BG)],
            foreground=[("disabled", "#6d6d6d")],
            lightcolor=[("pressed", PRESS_BG), ("active", HOVER_BG)],
            darkcolor=[("pressed", PRESS_BG), ("active", HOVER_BG)],
        )
        s.map("TCheckbutton", background=[("active", BG)], foreground=[("active", FG)],
              indicatorbackground=[("pressed", HOVER_BG), ("active", HOVER_BG)])
        s.configure("TCheckbutton", indicatorbackground=HEADER_BG, indicatorforeground=FG)
        s.map("TRadiobutton", background=[("active", BG)], foreground=[("active", FG)],
              indicatorbackground=[("pressed", HOVER_BG), ("active", HOVER_BG)])
        s.configure("TRadiobutton", indicatorbackground=HEADER_BG, indicatorforeground=FG)
        s.configure("Treeview", background=BG, fieldbackground=BG, foreground=FG, rowheight=22)
        s.configure("Treeview.Heading", background=HEADER_BG, foreground=FG)
        s.map("Treeview.Heading", background=[("pressed", PRESS_BG), ("active", HOVER_BG)])
        s.map("Treeview", background=[("selected", PRESS_BG)])
        self.root.option_add("*TCombobox*Listbox.background", HEADER_BG)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)

    def on_tree_motion(self, event):
        if self.tree.identify_region(event.x, event.y) != "heading":
            return self._hide_head_tip()
        col = self.tree.identify_column(event.x)
        col = "#0" if col == "#0" else self.tree.column(col, "id")
        if col != self.head_col:
            self.head_col = col
            self.head_tip.show(COLUMN_TIPS.get(col, ""), event.x_root, event.y_root)

    def _hide_head_tip(self):
        self.head_col = None
        self.head_tip.hide()

    def show_help(self):
        win = tk.Toplevel(self.root)
        win.title("LogCheck - Help")
        win.configure(bg=BG)
        win.transient(self.root)
        frame = ttk.Frame(win)
        frame.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        text = tk.Text(
            frame, bg=BG, fg=FG, font=("Consolas", 10), width=80, height=32,
            wrap="none", relief="flat", padx=8, pady=6,
        )
        scroll = ttk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        text.insert("1.0", HELP_TEXT)
        for line_no, line in enumerate(HELP_TEXT.splitlines(), start=1):
            if line and line == line.lstrip() and line.split()[0].isupper():
                text.tag_add("h", f"{line_no}.0", f"{line_no}.end")
        text.tag_configure("h", foreground="#e5cc80", font=("Consolas", 10, "bold"))
        text.configure(state="disabled")
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=10)
        win.bind("<Escape>", lambda e: win.destroy())
        win.focus_set()

    def ref_key(self, zone=None, metric=None):
        """Config key for the reference DPS: one per zone (season) and metric."""
        zone = (self.zone.get() if zone is None else zone).strip()
        return f"{zone}:{self.metric.get() if metric is None else metric}"

    def show_options(self):
        win = tk.Toplevel(self.root)
        win.title("LogCheck - Options")
        win.configure(bg=BG)
        win.transient(self.root)
        win.resizable(False, False)
        frame = ttk.Frame(win, padding=12)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Color rows by:").grid(row=0, column=0, sticky="w")
        color_by = tk.StringVar(value=self.cfg["color_by"])
        modes = [("parse", "Parse %"), ("dps", "DPS (% of reference)"), ("dps_custom", "DPS (custom)")]
        for i, (value, text) in enumerate(modes):
            ttk.Radiobutton(frame, text=text, value=value, variable=color_by).grid(
                row=0, column=1 + i, sticky="w", padx=(8, 0))

        key, metric = self.ref_key(), self.metric.get()
        ttk.Label(frame, text=f"Reference {metric.upper()} (zone {key.split(':')[0]}):").grid(
            row=1, column=0, sticky="w", pady=(10, 0))
        ref = ttk.Entry(frame, width=12)
        ref.insert(0, self.cfg["ref_amount"].get(key, ""))
        ref.grid(row=1, column=1, columnspan=3, sticky="w", padx=(8, 0), pady=(10, 0))
        hint = ttk.Label(frame, foreground="#9d9d9d", wraplength=400, justify="left")
        hint.grid(row=2, column=0, columnspan=4, sticky="w", pady=(4, 8))

        # One row per color: computed range in reference mode, an editable minimum in custom mode
        tiers = ttk.Frame(frame)
        tiers.grid(row=3, column=0, columnspan=4, sticky="w")
        saved = self.cfg["dps_tiers"].get(key, {})
        tier_rows = []
        for i, ((pct, color), name) in enumerate(zip(PARSE_COLORS, COLOR_NAMES)):
            ttk.Label(tiers, text=name, foreground=color, width=8).grid(row=i, column=0, sticky="w")
            computed = ttk.Label(tiers, foreground=color)
            computed.grid(row=i, column=1, sticky="w")
            entry = ttk.Entry(tiers, width=10) if pct else ttk.Label(tiers, foreground=color)
            entry.grid(row=i, column=2, sticky="w", pady=1)
            if pct:
                entry.insert(0, saved.get(str(pct), ""))
                entry.bind("<KeyRelease>", lambda e: update())
            tier_rows.append((pct, computed, entry))

        def update(*_):
            mode = color_by.get()
            ref.state(["!disabled"] if mode == "dps" else ["disabled"])
            hint.config(text={
                "parse": "Rows use the same colors as Warcraft Logs parse %.",
                "dps": "Roughly the top DPS for the keys you run this season (450k / 1.2m work). "
                       "Colors use the parse bands as a % of it. Saved separately for each zone "
                       "and metric, so set it again when the season changes.",
                "dps_custom": "Minimum DPS for each color (450k / 1.2m work). Leave a color blank to "
                              "skip it; anything below every minimum is gray. Saved separately for "
                              "each zone and metric.",
            }[mode])
            amount = to_amount(ref.get())
            custom = {}
            for pct, computed, entry in tier_rows:
                if mode == "dps_custom":
                    computed.grid_remove()
                    entry.grid()
                    if pct and entry.get().strip():
                        custom[str(pct)] = entry.get().strip()
                    elif not pct:
                        entry.config(text="below the rest")
                else:
                    entry.grid_remove()
                    computed.grid()
                    if pct:
                        computed.config(text=f"{pct:>3}%+   " + (f">= {fmt_amount(amount * pct / 100)}" if amount else ""))
                    else:
                        computed.config(text="<25%    " + (f"< {fmt_amount(amount * 0.25)}" if amount else ""))
            if mode == "parse":
                tiers.grid_remove()
            else:
                tiers.grid()
            self.cfg["color_by"] = mode
            text = ref.get().strip()
            if text:
                self.cfg["ref_amount"][key] = text
            else:
                self.cfg["ref_amount"].pop(key, None)
            if custom:
                self.cfg["dps_tiers"][key] = custom
            else:
                self.cfg["dps_tiers"].pop(key, None)
            self.render()  # saves the config and recolors live

        color_by.trace_add("write", update)
        ref.bind("<KeyRelease>", update)
        update()
        ttk.Button(frame, text="Close", command=win.destroy).grid(row=4, column=0, columnspan=4, pady=(12, 0))
        win.bind("<Escape>", lambda e: win.destroy())
        win.focus_set()

    def ensure_credentials(self):
        if self.cfg.get("client_id") and self.cfg.get("client_secret"):
            return True
        webbrowser.open("https://www.warcraftlogs.com/api/clients")
        cid = simpledialog.askstring(
            "Warcraft Logs API",
            "Create a client at warcraftlogs.com/api/clients (opened in browser).\n\nClient ID:",
            parent=self.root,
        )
        if not cid:
            return False
        secret = simpledialog.askstring("Warcraft Logs API", "Client Secret:", parent=self.root, show="*")
        if not secret:
            return False
        self.cfg.update(client_id=cid.strip(), client_secret=secret.strip())
        save_config(self.cfg)
        return True

    def search(self):
        try:
            chars = parse_characters(self.entry.get())
        except ValueError as e:
            self.status.config(text=str(e))
            return
        if not chars or not self.ensure_credentials():
            return
        region, metric, zone = self.region.get(), self.metric.get(), self.zone.get()
        self.cfg.update(region=region, metric=metric, zone=zone)
        save_config(self.cfg)

        self.btn.state(["disabled"])
        self.status.config(text=f"Fetching {len(chars)} character(s)...")
        started = time.perf_counter()

        def work():
            try:
                results, slugs, rate = fetch_rankings(self.cfg, chars, region, zone, metric)
                err = None
            except Exception as e:  # show any network/API failure in the status bar
                results, slugs, rate, err = None, None, None, e
            self.root.after(
                0, lambda: self.show(chars, region, zone, metric, results, slugs, rate, err, started)
            )

        threading.Thread(target=work, daemon=True).start()

    def paste_search(self, event=None):
        if event and event.widget in (self.zone, self.min_value, self.min_key):
            return None  # the Entry class binding already pasted normally
        try:
            text = self.root.clipboard_get().strip()
        except tk.TclError:  # empty or non-text clipboard
            text = ""
        if not text:
            self.status.config(text="Clipboard is empty")
            return "break"
        self.entry.delete(0, "end")
        self.entry.insert(0, text)
        self.search()
        return "break"

    def show(self, chars, region, zone, metric, results, slugs, rate, err, started):
        self.btn.state(["!disabled"])
        if err:
            if isinstance(err, urllib.error.HTTPError) and err.code in (400, 401):
                self.cfg.pop("client_id", None)
                self.cfg.pop("client_secret", None)
                self.cfg.pop("token", None)
                save_config(self.cfg)
                self.status.config(text="Auth failed - check your client ID/secret and try again.")
            else:
                self.status.config(text=f"Error: {err}")
            return
        new = [
            {
                "name": name, "realm": realm, "slug": slug, "region": region, "zone": zone,
                "metric": metric, "char": char,
            }
            for (name, realm), slug, char in zip(chars, slugs, results)
        ]
        if self.keep.get():
            ids = {self._row_id(r) for r in new}
            self.rows = [r for r in self.rows if self._row_id(r) not in ids] + new  # re-search = refresh
            self.entry.delete(0, "end")  # ready for the next name
        else:
            self.rows = new
        self.refresh_dungeons()  # encounter list is cached on first lookup of a zone
        self.done_msg = f"Done in {time.perf_counter() - started:.2f}s"
        self.rate_label.config(text=fmt_rate(rate))
        self.render()

    def sort_on(self, col):
        """Header click: sort by col, or flip the order if already sorting by it."""
        if col == self.sort_col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col, self.sort_desc = col, col not in TEXT_COLS
        self.cfg.update(sort_col=self.sort_col, sort_desc=self.sort_desc)
        save_config(self.cfg)
        self.update_headings()
        self.render()

    def update_headings(self):
        for col, title in self.titles.items():
            arrow = (" \u25bc" if self.sort_desc else " \u25b2") if col == self.sort_col else ""
            self.tree.heading(col, text=title + arrow)

    def judged_pct(self, key_pct, median):
        return median if self.judge.get() == "Median" else key_pct

    def switch_min_mode(self):
        """Swaps the threshold box to the chosen mode's own saved value."""
        self.min_value.delete(0, "end")
        self.min_value.insert(0, self.cfg[MIN_MODES[self.min_mode.get()]])
        self.render()

    def judge_pass(self, key_pct, median, amount, timed_key):
        """True/False against the thresholds, or None when no threshold is set."""
        by_dps = self.min_mode.get() == "Min DPS"
        minimum = (to_amount if by_dps else to_number)(self.min_value.get())
        min_key = to_number(self.min_key.get())
        if minimum is None and min_key is None:
            return None
        value = amount if by_dps else self.judged_pct(key_pct, median)
        if minimum is not None and (value is None or value < minimum):
            return False
        if min_key is not None and (timed_key is None or timed_key < min_key):
            return False
        return True

    def render(self):
        only = self.dungeon.get()
        only = "" if only == ALL_DUNGEONS else only
        self.cfg[MIN_MODES[self.min_mode.get()]] = self.min_value.get()
        self.cfg.update(judge=self.judge.get(), min_mode=self.min_mode.get(), min_key=self.min_key.get(), dungeon=only)
        save_config(self.cfg)
        # With one dungeon selected, DPS is that dungeon's and All DPS shows every dungeon for comparison
        self.titles["amount"] = "Dungeon DPS" if only else "DPS"
        self.tree["displaycolumns"] = [c for c in self.tree["columns"] if only or c != "alldps"]
        self.update_headings()
        for item, row in self.items.items():  # remember what the user expanded/collapsed
            self.expanded[self._row_id(row)] = bool(self.tree.item(item, "open"))
        self.tree.delete(*self.tree.get_children())
        self.links.clear()
        self.items = {}  # character tree item id -> row
        passed = judged = 0
        col, desc = self.sort_col, self.sort_desc
        chars = [self._summarize(row, only) for row in self.rows]
        if col:
            chars = sort_by(chars, lambda c: c[col], desc)
        for c in chars:
            row, dungeons = c["row"], c["dungeons"]
            verdict = self.judge_pass(c["keypct"], c["median"], c["amount"], c["max_timed"])
            if verdict is not None:
                judged += 1
                passed += verdict
            parent = self.tree.insert(
                "", "end", text=c["label"], image=self.icons[verdict],
                open=self.expanded.setdefault(self._row_id(row), self.expand_new.get()),
                values=(
                    fmt_key(c["top_key"], c["timed"]),
                    fmt_pct(c["keypct"]),
                    fmt_amount(c["amount"]),
                    fmt_amount(c["alldps"]),
                    fmt_pct(c["median"]),
                    c["runs"] or "",
                    c["spec"] or "",
                    "\u2715",
                ),
                tags=(self._row_color_tag(row, self.judged_pct(c["keypct"], c["median"]), c["amount"]), "header"),
            )
            self.links[parent] = c["url"]
            self.items[parent] = row

            if col:
                dungeons = sort_by(dungeons, lambda d: self._dungeon_sort_value(d, col), desc)
            else:
                dungeons = sorted(dungeons, key=lambda d: (d["key"] or 0, d["key_pct"] or 0), reverse=True)
            for d in dungeons:
                verdict = self.judge_pass(d["key_pct"], d["median"], d["dps"], d["max_timed"])
                self.tree.insert(
                    parent, "end", image=self.icons[verdict], text=" " + d["name"],
                    values=(
                        fmt_key(d["key"], d["timed"]),
                        fmt_pct(d["key_pct"]),
                        fmt_amount(d["dps"]),
                        "",
                        fmt_pct(d["median"]),
                        d["runs"],
                        d["spec"],
                        "",
                    ),
                    tags=(self._row_color_tag(row, self.judged_pct(d["key_pct"], d["median"]), d["dps"]),),
                )
        self.tree.tag_configure("header", font=("Segoe UI", 10, "bold"), background=HEADER_BG)
        extra = f"{passed}/{judged} meet the threshold" if judged else "double-click a name to open WCL"
        parts = [self.done_msg] if self.done_msg else []
        if self.rows:
            parts += [f"{len(self.rows)} listed", extra]
            if self.cfg["color_by"] == "dps" and not all(
                to_amount(self.cfg["ref_amount"].get(self.ref_key(r["zone"], r["metric"]), "")) for r in self.rows
            ):
                parts.append("set a reference DPS in Options to color by DPS")
            if self.cfg["color_by"] == "dps_custom" and not all(self._custom_tiers(r) for r in self.rows):
                parts.append("set DPS colors in Options to color by DPS")
        if parts:
            self.status.config(text="  |  ".join(parts))

    @staticmethod
    def _summarize(row, only):
        """Character-row numbers (and sort values, keyed by column) for one listed character."""
        name, realm, char = row["name"], row["realm"], row["char"]
        all_dungeons = [d for d in (char or {}).get("dungeons", []) if d["runs"]]
        dungeons = [d for d in all_dungeons if not only or d["name"] == only]
        if char is None:
            label = f"{name}-{realm}  (not found)"
        elif char["error"] and not dungeons:
            label = f"{char['name']}-{realm}  ({char['error']})"
        elif only and not dungeons:
            label = f"{char['name']}-{realm}  (no {only} runs)"
        else:
            label = f"{char['name']}-{realm}"

        timed = [d["max_timed"] for d in dungeons if d["max_timed"]]
        top_key = max(timed) if timed else max((d["key"] for d in dungeons), default=None)
        top = max(dungeons, key=lambda d: (d["key"] or 0, d["timed"]), default=None)
        return {
            "row": row,
            "dungeons": dungeons,
            "label": label,
            "url": (
                f"https://www.warcraftlogs.com/character/{row['region']}/{row['slug']}/{name.lower()}"
                f"?zone={row['zone']}&metric={row['metric']}"
            ),
            "top_key": top_key,
            "timed": bool(timed),
            "max_timed": max(timed, default=None),
            # sort values, by column
            "#0": label.lower(),
            "key": (top_key, bool(timed)) if top_key else None,
            "keypct": mean(d["key_pct"] for d in dungeons),
            "amount": mean(d["dps"] for d in dungeons),
            "alldps": mean(d["dps"] for d in all_dungeons),
            "median": mean(d["median"] for d in dungeons),
            "runs": sum(d["runs"] for d in dungeons) or None,
            "spec": top["spec"] if top and top["spec"] else None,
        }

    @staticmethod
    def _dungeon_sort_value(d, col):
        return {
            "#0": d["name"].lower(),
            "key": (d["key"], d["timed"]) if d["key"] else None,
            "keypct": d["key_pct"],
            "amount": d["dps"],
            "alldps": None,
            "median": d["median"],
            "runs": d["runs"],
            "spec": d["spec"].lower() or None,
        }[col]

    def _color_tag(self, pct):
        color = parse_color(pct)
        tag = "c" + color.lstrip("#")
        self.tree.tag_configure(tag, foreground=color)
        return tag

    def _custom_tiers(self, row):
        """[(min DPS, band %)] high-to-low from the custom DPS colors set for this row's zone/metric."""
        saved = self.cfg["dps_tiers"].get(self.ref_key(row["zone"], row["metric"]), {})
        tiers = [(to_amount(saved.get(str(pct), "")), pct) for pct, _ in PARSE_COLORS if pct]
        return sorted((t for t in tiers if t[0] is not None), reverse=True)

    def _row_color_tag(self, row, pct, amount):
        """Parse-% color; in DPS mode the same bands as a % of the reference DPS,
        or in custom mode the band of the highest minimum DPS the row reaches."""
        mode = self.cfg["color_by"]
        if mode == "dps_custom":
            tiers = self._custom_tiers(row)
            if not tiers or amount is None:
                return self._color_tag(None)
            return self._color_tag(next((p for minimum, p in tiers if amount >= minimum), 0))
        if mode != "dps":
            return self._color_tag(pct)
        ref = to_amount(self.cfg["ref_amount"].get(self.ref_key(row["zone"], row["metric"]), ""))
        return self._color_tag(amount / ref * 100 if ref and amount else None)

    @staticmethod
    def _row_id(row):
        return (row["name"].lower(), row["slug"], row["region"])

    def save_keep(self):
        self.cfg["keep"] = self.keep.get()
        save_config(self.cfg)

    def save_expand(self):
        self.cfg["expand_new"] = self.expand_new.get()
        save_config(self.cfg)

    def on_tree_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        col = self.tree.identify_column(event.x)
        if col != "#0" and self.tree.column(col, "id") == "remove":
            self.remove(self.tree.identify_row(event.y))
            return "break"

    def remove(self, item):
        row = self.items.get(item)
        if row is not None:
            self.rows.remove(row)
            self.items.pop(item)
            self.expanded.pop(self._row_id(row), None)
            self.done_msg = ""
            self.render()

    def show_menu(self, event):
        """Right-click on a character (or one of their dungeons): open/copy/remove."""
        item = self.tree.identify_row(event.y)
        item = self.tree.parent(item) or item
        row = self.items.get(item)
        if row is None:
            return
        self.tree.selection_set(item)
        self.tree.focus(item)
        name = (row["char"] or {}).get("name") or row["name"]
        full = f"{name}-{row['realm']}"
        raiderio = f"https://raider.io/characters/{row['region']}/{row['slug']}/{urllib.parse.quote(name)}"
        self.menu.delete(0, "end")
        self.menu.add_command(label="Open Warcraft Logs", command=lambda: webbrowser.open(self.links[item]))
        self.menu.add_command(label="Open Raider.IO", command=lambda: webbrowser.open(raiderio))
        self.menu.add_command(label=f"Copy {full}", command=lambda: self.copy(full))
        self.menu.add_separator()
        self.menu.add_command(label="Remove", command=lambda: self.remove(item))
        self.menu.tk_popup(event.x_root, event.y_root)

    def copy(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status.config(text=f"Copied {text}")

    def clear(self):
        self.rows = []
        self.items = {}
        self.expanded = {}
        self.done_msg = ""
        self.render()
        self.status.config(text="List cleared")

    def open_link(self, event):
        item = self.tree.identify_row(event.y)
        if item in self.links:
            webbrowser.open(self.links[item])


def main():
    import sys

    if "--make-icon" in sys.argv:
        write_ico(Path(__file__).with_name("logcheck.ico"))
        return
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
