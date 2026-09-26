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

DEFAULTS = {"region": "us", "zone": "55", "metric": "dps", "judge": "Median", "min_pct": "", "min_key": "", "dungeon": "", "keep": False}
ALL_DUNGEONS = "All dungeons"

# Warcraft Logs parse colors: (min percent, color)
PARSE_COLORS = [
    (100, "#e5cc80"),
    (99, "#e268a8"),
    (95, "#ff8000"),
    (75, "#a335ee"),
    (50, "#0070ff"),
    (25, "#1eff00"),
    (0, "#9d9d9d"),
]

BG = "#1e1e1e"
FG = "#dddddd"
HEADER_BG = "#2d2d30"


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

    Returns (results, rate_limit). Per character: None if not found, else
    {"name", "error", "dungeons"}.
    """
    if not zone.strip():
        raise ValueError("Zone ID is required")
    encounters = zone_encounters(cfg, int(zone))
    enc_fields = " ".join(
        f"e{j}: encounterRankings(encounterID: {eid}, metric: {metric}, byBracket: true)"
        for j, (eid, _) in enumerate(encounters)
    )
    parts = [
        f"c{i}: character(name: {gql_str(name)}, serverSlug: {gql_str(realm_slug(realm))}, "
        f"serverRegion: {gql_str(region)}) {{ name {enc_fields} }}"
        for i, (name, realm) in enumerate(chars)
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
    return results, data.get("rateLimitData")


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


def mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


HELP_TEXT = """\
HOW TO USE
  Type characters as "Name Realm" or "Name-Realm", separated by commas, then
  press Enter. All characters are fetched in one request, so a whole group is
  as fast as one person. Double-click a character's name to open their
  Warcraft Logs page.

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
  Median %  Middle of ALL their runs in that dungeon (each run vs its own
            key level). Half their runs are above, half below.
  Runs      Number of logged runs behind the numbers.
  Spec      Spec used on the highest key.
  Bold row  The character: highest timed key anywhere, and averages of
            Key % and Median % across dungeons.

COLORS  (rows are colored by whatever "Judge by" is set to)
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
  Min key    Lowest TIMED key you'll accept. Blank = no key requirement.
             Depleted keys don't count toward this.
  Dungeon    Show only one dungeon (e.g. the key you're forming for).
             Character rows are then judged on that dungeon alone.
             Remembered between sessions.
  Changing these re-checks instantly, no new lookup needed.
  Dungeon rows are judged on that dungeon's numbers (good when forming for a
  specific dungeon). Character rows are judged on their average % across all
  dungeons and their highest timed key in any dungeon.

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
    "#0": "Dungeon. Bold rows are the character (double-click to open WCL).",
    "key": "Highest key run in this dungeon. +17 = timed, 17 \u2717 = depleted.",
    "keypct": "Percentile on their highest key, vs same spec at the same key level.",
    "amount": "DPS on their highest key.",
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
        self.links = {}  # tree item id -> character URL

        root.title("LogCheck")
        self.app_icon = make_app_icon()
        root.iconphoto(True, self.app_icon)
        root.geometry("720x520")
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

        ttk.Label(top, text="Min %:").grid(row=2, column=2, sticky="w", pady=(6, 0))
        self.min_pct = ttk.Entry(top, width=6)
        self.min_pct.insert(0, self.cfg["min_pct"])
        self.min_pct.grid(row=2, column=3, sticky="w", padx=(6, 12), pady=(6, 0))

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
        self.zone.bind("<KeyRelease>", lambda e: self.refresh_dungeons())
        for w in (self.min_pct, self.min_key):
            w.bind("<KeyRelease>", lambda e: self.render())

        self.icons = {
            True: make_icon("#3ccf4e", [((2, 8), (6, 12)), ((6, 12), (13, 3))]),
            False: make_icon("#e04848", [((3, 3), (12, 12)), ((12, 3), (3, 12))]),
            None: tk.PhotoImage(width=16, height=16),
        }
        self.rows = []  # one dict per listed character: name, realm, region, zone, metric, char
        self.done_msg = ""
        self.items = {}  # character tree item id -> row

        self.keep = tk.BooleanVar(value=bool(self.cfg["keep"]))
        keep_box = ttk.Checkbutton(top, text="Keep adding", variable=self.keep, command=self.save_keep)
        keep_box.grid(row=3, column=4, columnspan=2, sticky="w", pady=(6, 0))
        clear_btn = ttk.Button(top, text="Clear", command=self.clear)
        clear_btn.grid(row=3, column=7, sticky="e", pady=(6, 0))
        Tooltip(root, keep_box, "On: each search adds characters to the list instead of replacing it, "
                "so you can compare many people. Re-searching someone refreshes their data.")
        Tooltip(root, clear_btn, "Remove everyone from the list.")

        help_btn = ttk.Button(top, text="?", width=3, command=self.show_help)
        help_btn.grid(row=1, column=6, sticky="e", padx=(0, 6), pady=(6, 0))
        self.btn = ttk.Button(top, text="Check", command=self.search)
        self.btn.grid(row=1, column=7, sticky="e", pady=(6, 0))
        top.columnconfigure(6, weight=1)

        Tooltip(root, self.entry, "Name Realm or Name-Realm. Separate multiple characters with commas.")
        Tooltip(root, self.region, "Region the characters are on.")
        Tooltip(root, self.metric, "dps for damage dealers, hps for healers.")
        Tooltip(root, self.zone, "Warcraft Logs zone ID (from ?zone= in the URL). 55 = Mythic+ Season 2.")
        Tooltip(root, help_btn, "What do these numbers mean?")
        Tooltip(root, self.judge, "Which percentile the threshold checks. Median (typical run) is "
                "the better predictor; Key % is how they did on their highest key.")
        Tooltip(root, self.min_pct, "Minimum percentile to get a green check. Blank = no % requirement. "
                "Character row uses their average across dungeons.")
        Tooltip(root, self.min_key, "Minimum key level. Blank = no key requirement. "
                "Only timed keys count. Character row uses their highest timed key anywhere.")
        Tooltip(root, self.dungeon, "Only show one dungeon. Character rows are then judged on that "
                "dungeon alone. List comes from the zone's dungeons; remembered between sessions.")

        cols = ("key", "keypct", "amount", "median", "runs", "spec", "remove")
        self.tree = ttk.Treeview(root, columns=cols, show="tree headings")
        self.tree.heading("#0", text="Dungeon", anchor="w")
        self.tree.column("#0", width=240)
        for col, title, width in [
            ("key", "Key", 55),
            ("keypct", "Key %", 70),
            ("amount", "DPS", 90),
            ("median", "Median %", 80),
            ("runs", "Runs", 60),
            ("spec", "Spec", 110),
            ("remove", "", 30),
        ]:
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=8)
        self.tree.bind("<Double-1>", self.open_link)
        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<Delete>", lambda e: self.remove(self.tree.focus()))
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
        s.configure("TCombobox", foreground=FG)
        s.map("TCombobox", fieldbackground=[("readonly", HEADER_BG)], foreground=[("readonly", FG)])
        s.configure("TButton", background=HEADER_BG)
        s.configure("Treeview", background=BG, fieldbackground=BG, foreground=FG, rowheight=22)
        s.configure("Treeview.Heading", background=HEADER_BG, foreground=FG)
        s.map("Treeview", background=[("selected", "#094771")])
        self.root.option_add("*TCombobox*Listbox.background", HEADER_BG)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)

    def on_tree_motion(self, event):
        if self.tree.identify_region(event.x, event.y) != "heading":
            return self._hide_head_tip()
        col = self.tree.identify_column(event.x)
        col = "#0" if col == "#0" else self.tree["columns"][int(col[1:]) - 1]
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
                results, rate = fetch_rankings(self.cfg, chars, region, zone, metric)
                err = None
            except Exception as e:  # show any network/API failure in the status bar
                results, rate, err = None, None, e
            self.root.after(0, lambda: self.show(chars, region, zone, metric, results, rate, err, started))

        threading.Thread(target=work, daemon=True).start()

    def show(self, chars, region, zone, metric, results, rate, err, started):
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
            {"name": name, "realm": realm, "region": region, "zone": zone, "metric": metric, "char": char}
            for (name, realm), char in zip(chars, results)
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

    def judged_pct(self, key_pct, median):
        return median if self.judge.get() == "Median" else key_pct

    def judge_pass(self, key_pct, median, timed_key):
        """True/False against the thresholds, or None when no threshold is set."""
        min_pct, min_key = to_number(self.min_pct.get()), to_number(self.min_key.get())
        if min_pct is None and min_key is None:
            return None
        pct = self.judged_pct(key_pct, median)
        if min_pct is not None and (pct is None or pct < min_pct):
            return False
        if min_key is not None and (timed_key is None or timed_key < min_key):
            return False
        return True

    def render(self):
        only = self.dungeon.get()
        only = "" if only == ALL_DUNGEONS else only
        self.cfg.update(
            judge=self.judge.get(), min_pct=self.min_pct.get(), min_key=self.min_key.get(), dungeon=only
        )
        save_config(self.cfg)
        self.tree.delete(*self.tree.get_children())
        self.links.clear()
        self.items = {}  # character tree item id -> row
        passed = judged = 0
        for row in self.rows:
            name, realm, region, zone, metric, char = (
                row[k] for k in ("name", "realm", "region", "zone", "metric", "char")
            )
            url = (
                f"https://www.warcraftlogs.com/character/{region}/{realm_slug(realm)}/{name.lower()}"
                f"?zone={zone}&metric={metric}"
            )
            dungeons = [
                d for d in (char or {}).get("dungeons", []) if d["runs"] and (not only or d["name"] == only)
            ]
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
            key_avg = mean(d["key_pct"] for d in dungeons)
            med_avg = mean(d["median"] for d in dungeons)
            verdict = self.judge_pass(key_avg, med_avg, max(timed, default=None))
            if verdict is not None:
                judged += 1
                passed += verdict
            parent = self.tree.insert(
                "", "end", text=label, open=True, image=self.icons[verdict],
                values=(fmt_key(top_key, bool(timed)), fmt_pct(key_avg), "", fmt_pct(med_avg), "", "", "\u2715"),
                tags=(self._color_tag(self.judged_pct(key_avg, med_avg)), "header"),
            )
            self.links[parent] = url
            self.items[parent] = row

            for d in sorted(dungeons, key=lambda d: (d["key"] or 0, d["key_pct"] or 0), reverse=True):
                verdict = self.judge_pass(d["key_pct"], d["median"], d["max_timed"])
                self.tree.insert(
                    parent, "end", image=self.icons[verdict], text=" " + d["name"],
                    values=(
                        fmt_key(d["key"], d["timed"]),
                        fmt_pct(d["key_pct"]),
                        fmt_amount(d["dps"]),
                        fmt_pct(d["median"]),
                        d["runs"],
                        d["spec"],
                        "",
                    ),
                    tags=(self._color_tag(self.judged_pct(d["key_pct"], d["median"])),),
                )
        self.tree.tag_configure("header", font=("Segoe UI", 10, "bold"), background=HEADER_BG)
        extra = f"{passed}/{judged} meet the threshold" if judged else "double-click a name to open WCL"
        parts = [self.done_msg] if self.done_msg else []
        if self.rows:
            parts += [f"{len(self.rows)} listed", extra]
        if parts:
            self.status.config(text="  |  ".join(parts))

    def _color_tag(self, pct):
        color = parse_color(pct)
        tag = "c" + color.lstrip("#")
        self.tree.tag_configure(tag, foreground=color)
        return tag

    @staticmethod
    def _row_id(row):
        return (row["name"].lower(), realm_slug(row["realm"]), row["region"])

    def save_keep(self):
        self.cfg["keep"] = self.keep.get()
        save_config(self.cfg)

    def on_tree_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        col = self.tree.identify_column(event.x)
        if col != "#0" and self.tree["columns"][int(col[1:]) - 1] == "remove":
            self.remove(self.tree.identify_row(event.y))
            return "break"

    def remove(self, item):
        row = self.items.get(item)
        if row is not None:
            self.rows.remove(row)
            self.done_msg = ""
            self.render()

    def clear(self):
        self.rows = []
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
