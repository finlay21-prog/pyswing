"""Poll east London tennis and squash court booking APIs and push new availability to ntfy.sh."""

import functools
import json
import os
import datetime as dt
from pathlib import Path

import requests

# ntfy topics. NTFY_TOPIC is the default; NTFY_TOPIC_TENNIS / NTFY_TOPIC_SQUASH
# override it per sport so you can subscribe to them separately.
# If no topic is set at all, the script only prints the summary (handy for local runs).
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
STATE_FILE = Path(os.environ.get("STATE_FILE", "seen_slots.json"))
SUMMARY_FILE = Path(os.environ.get("SUMMARY_FILE", "courts.md"))
SUMMARY_URL = os.environ.get("SUMMARY_URL", "")  # page listing every free court, for the "All courts" button
DAYS_AHEAD = 7  # ClubSpark opens each new day at 6pm, 7 days ahead; Better opens at 10pm, ~6 days ahead

UA = {"User-Agent": "Mozilla/5.0"}
NOT_LISTED = "not listed"  # Better returned no timetable at all for that day


# ---------------------------------------------------------------- Better ----
def fetch_better(venue: str, activity: str, date: str):
    url = f"https://better-admin.org.uk/api/activities/venue/{venue}/activity/{activity}/v2/times"
    headers = {**UA, "Origin": "https://bookings.better.org.uk",
               "Referer": "https://bookings.better.org.uk/"}
    r = requests.get(url, params={"date": date}, headers=headers, timeout=20)
    if r.status_code == 422:  # date not open for booking yet
        return None
    r.raise_for_status()
    data = r.json().get("data") or []
    if not data:
        # Fully booked days still list their slots (with 0 spaces), so an empty
        # list means nothing is on the public timetable for this day.
        return NOT_LISTED
    slots = []
    for s in data:
        if int(s.get("spaces", 0)) > 0:
            start = s["starts_at"]["format_24_hour"]
            end = s["ends_at"]["format_24_hour"]
            slots.append((start, end, s.get("name", "")))
    return slots


def better_link(venue: str, activity: str, date: str) -> str:
    return f"https://bookings.better.org.uk/location/{venue}/{activity}/{date}/by-time"


# ------------------------------------------------------------- ClubSpark ----
def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@functools.cache
def _clubspark_week(venue: str):
    # One request for the whole window
    today = dt.date.today()
    url = f"https://clubspark.lta.org.uk/v0/VenueBooking/{venue}/GetVenueSessions"
    r = requests.get(url, params={"startDate": today.isoformat(),
                                  "endDate": (today + dt.timedelta(days=DAYS_AHEAD)).isoformat()},
                     headers=UA, timeout=20)
    r.raise_for_status()
    return r.json()


def fetch_clubspark(venue: str, date: str):
    data = _clubspark_week(venue)
    slots = []
    # Sessions are what occupies a court (bookings, coaching, "Closed"), so free
    # slots are the gaps between EarliestStartTime and LatestEndTime.
    step = data.get("MinimumInterval", 60)
    for court in data.get("Resources", []):
        days = [d for d in court.get("Days", []) if d["Date"].startswith(date)]
        if not days:
            continue
        busy = [(s["StartTime"], s["EndTime"]) for d in days for s in d.get("Sessions", [])]
        for t in range(data["EarliestStartTime"], data["LatestEndTime"] - step + 1, step):
            if not any(a < t + step and t < b for a, b in busy):
                slots.append((_hhmm(t), _hhmm(t + step), court.get("Name", "")))
    return slots


def clubspark_link(venue: str, date: str) -> str:
    return f"https://clubspark.lta.org.uk/{venue}/Booking/BookByDate#?date={date}&role=guest"


PLATFORMS = {"better": (fetch_better, better_link), "clubspark": (fetch_clubspark, clubspark_link)}

# sport -> venue -> (platform, *platform args)
# To find a Better venue/activity: open the booking page on bookings.better.org.uk;
# the URL is /location/<venue>/<activity>/<date>/by-time.
VENUES = {
    "Tennis": {
        "Haggerston Park": ("better", "hackney-parks", "tennis-court-outdoor"),
        "Britannia (rooftop)": ("better", "britannia-leisure-centre", "tennis-court-outdoor"),
        "Rosemary Gardens": ("better", "islington-tennis-centre", "rosemary-gardens-tennis"),
        "Joe White Gardens": ("clubspark", "AskeGardens"),
        "London Fields": ("clubspark", "LondonFieldsPark"),
        "Hackney Downs": ("clubspark", "HackneyDowns"),
        "Clissold Park": ("clubspark", "ClissoldParkHackney"),
    },
    "Squash": {  # nearest to E2 first
        "Britannia": ("better", "britannia-leisure-centre", "squash-40min"),
        "Ironmonger Row": ("better", "ironmonger-row-baths", "squash-40min"),
        "Finsbury": ("better", "finsbury-leisure-centre", "squash-40min"),
        "Clissold Leisure Centre": ("better", "clissold-leisure-centre", "squash-40min"),
        "Sobell": ("better", "sobell-leisure-centre", "squash-40min"),
    },
}
NTFY_TAGS = {"Tennis": "tennis", "Squash": "squash"}

# Only alert for slots in these windows (the free-courts page still lists everything).
# days: 0=Mon ... 6=Sun; a slot qualifies if it starts between "from" and "to".
ALERT_WINDOWS = {
    "Tennis": {"days": {5, 6}, "from": "09:00", "to": "17:00"},  # weekends, finishing by 6pm
}

# Courts we can't check automatically (Tower Hamlets turns away automated requests), so a
# reminder with links goes out on Friday evening for the weekend.
TH_BOOK = "https://tennistowerhamlets.com/book/courts/{park}/{date}"
CHECK_YOURSELF = {"Bethnal Green Gardens": "bethnal-green-gardens", "Victoria Park": "victoria-park"}


def wanted(sport: str, date: dt.date, start: str) -> bool:
    w = ALERT_WINDOWS.get(sport)
    return not w or (date.weekday() in w["days"] and w["from"] <= start <= w["to"])

# Which booking platforms this run checks, e.g. PLATFORMS=better. Default: all.
# ClubSpark's firewall blocks cloud servers (incl. GitHub Actions), so GitHub runs
# PLATFORMS=better and a phone/home machine runs PLATFORMS=clubspark (see phone/).
ENABLED = {p.strip() for p in os.environ.get("PLATFORMS", "").split(",") if p.strip()} or set(PLATFORMS)
if ENABLED - set(PLATFORMS):
    raise SystemExit(f"Unknown PLATFORMS: {', '.join(sorted(ENABLED - set(PLATFORMS)))}")


def topic_for(sport: str):
    return os.environ.get(f"NTFY_TOPIC_{sport.upper()}") or NTFY_TOPIC


# ----------------------------------------------------------------- core -----
def short_name(venue: str) -> str:
    for suffix in (" Leisure Centre", " Gardens", " Park"):
        venue = venue.removesuffix(suffix)
    return venue


def _button(label: str, url: str) -> str:
    label = label.replace(",", " ").replace(";", " ").replace('"', "")
    return f'view, "{label}", {url}, clear=true'


def notify(topic: str, by_sport: dict[str, list[tuple[str, str, str]]]):
    """by_sport: sport -> [(alert line, short label, booking link), ...]"""
    mixed = len(by_sport) > 1
    items = [(f"{sport} · {line}" if mixed else line, label, link)
             for sport, sport_items in by_sport.items() for line, label, link in sport_items]
    lines = [line for line, _, _ in items]
    # ntfy caps message bodies at 4096 bytes
    body = "\n".join(lines[:30] + ([f"...and {len(lines) - 30} more"] if len(lines) > 30 else []))
    title = "New courts free" if mixed else f"New {next(iter(by_sport)).lower()} courts free"
    # Tapping the alert opens the booking page for the first new slot; up to 3 buttons
    # (ntfy's limit) open the others, with "All courts" taking the last one if there are more.
    buttons = [_button(f"Book {label}", link) for _, label, link in items]
    if len(buttons) > 3 and SUMMARY_URL:
        buttons = buttons[:2] + [_button("All courts", SUMMARY_URL)]
    headers = {"Title": title,
               "Tags": ",".join(NTFY_TAGS.get(s, s.lower()) for s in by_sport),
               "Click": items[0][2],
               "Actions": "; ".join(buttons[:3])}
    r = requests.post(f"https://ntfy.sh/{topic}", data=body.encode(),
                      headers={k: v.encode("latin-1", "replace").decode("latin-1") for k, v in headers.items()},
                      timeout=20)
    if not r.ok:
        print(f"ntfy error {r.status_code}: {r.text[:200]}")


def load_state():
    """State is {"venues": [...checked at least once...], "slots": [...keys...], "reminders": [...]}."""
    if not STATE_FILE.exists():
        return set(), set(), set()
    raw = json.loads(STATE_FILE.read_text() or "{}")
    if isinstance(raw, list):  # original pyswing format; keys differ, so start afresh (silently)
        return set(), set(), set()
    return set(raw.get("slots", [])), set(raw.get("venues", [])), set(raw.get("reminders", []))


def weekend_reminder(reminders: set) -> None:
    """Friday 5-9pm: one ping with links to the courts we can't check, for Saturday."""
    now = dt.datetime.now()
    key = f"Tennis|{now:%G-W%V}"
    topic = topic_for("Tennis")
    if not topic or now.weekday() != 4 or not 17 <= now.hour < 21 or key in reminders:
        return
    sat = (now + dt.timedelta(days=1)).date().isoformat()
    buttons = [_button(f"{name} Sat", TH_BOOK.format(park=slug, date=sat)) for name, slug in CHECK_YOURSELF.items()]
    if SUMMARY_URL:
        buttons.append(_button("All courts", SUMMARY_URL))
    r = requests.post(f"https://ntfy.sh/{topic}", timeout=20,
                      data=("Weekend tennis: these courts can't be checked automatically. "
                            "Have a look for Saturday and Sunday, 9am-6pm.").encode(),
                      headers={"Title": "Check Tower Hamlets tennis courts", "Tags": "tennis",
                               "Click": TH_BOOK.format(park="bethnal-green-gardens", date=sat),
                               "Actions": "; ".join(buttons[:3])})
    if r.ok:
        reminders.add(key)
    else:
        print(f"ntfy error {r.status_code}: {r.text[:200]}")


PAGE_CSS = """
:root{--bg:#f7f7f4;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e4e3dd;--chip:#e8f1ea;--chip-ink:#1f5130;--accent:#2f6b45}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--ink:#ecebe6;--muted:#9a9993;--line:#2e2e2b;--chip:#1f3326;--chip-ink:#bfe3c9;--accent:#7cc293}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:640px;margin:0 auto;padding:20px 16px 48px}h1{font-size:1.5rem;margin:0 0 2px}
.sub{color:var(--muted);font-size:.9rem;margin:0 0 16px}
nav{display:flex;gap:8px;margin:0 0 18px;position:sticky;top:0;background:var(--bg);padding:8px 0;z-index:1}
nav a{flex:1;text-align:center;padding:8px;border:1px solid var(--line);border-radius:10px;color:var(--ink);text-decoration:none;font-weight:600;background:var(--card)}
h2{font-size:1.15rem;margin:26px 0 4px}.win{color:var(--muted);font-size:.85rem;margin:0 0 10px}
.day{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 12px;margin:0 0 10px}
.day h3{font-size:.95rem;margin:0 0 6px}.row{display:flex;gap:10px;align-items:flex-start;padding:6px 0;border-top:1px solid var(--line)}
.row:first-of-type{border-top:0}.venue{flex:0 0 38%;font-size:.92rem}.times{display:flex;flex-wrap:wrap;gap:6px}
.times a{background:var(--chip);color:var(--chip-ink);border-radius:8px;padding:3px 8px;text-decoration:none;font-size:.9rem;font-variant-numeric:tabular-nums}
.none{color:var(--muted);font-size:.92rem}.notes{color:var(--muted);font-size:.85rem;padding-left:18px}
.notes a,.links a{color:var(--accent)}.links{padding-left:18px}
"""
def page_windows() -> dict:
    """What the page shows per sport: the alert window if there is one, otherwise the times
    in bookings.json's goals for that sport (the times you're trying to play)."""
    try:
        goals = json.loads(Path("bookings.json").read_text()).get("goals", [])
    except (OSError, ValueError):
        goals = []
    out = {}
    for sport in VENUES:
        if sport in ALERT_WINDOWS:
            w = ALERT_WINDOWS[sport]
            names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            days = "/".join(names[d] for d in sorted(w["days"]))
            end = (dt.datetime.strptime(w["to"], "%H:%M") + dt.timedelta(hours=1)).strftime("%H:%M")
            out[sport] = ([{"days": [names[d] for d in w["days"]], "earliest_start": w["from"], "latest_start": w["to"]}],
                          f"{days}, {w['from']}–{end}")
        else:
            mine = [g for g in goals if g.get("sport", g.get("name")) == sport
                    and (not g.get("only_date") or g["only_date"] >= dt.date.today().isoformat())]
            text = " · ".join(
                (dt.date.fromisoformat(g["only_date"]).strftime("%a %d %b") if g.get("only_date") else "/".join(g["days"]))
                + f" {g['earliest_start']}–{g['latest_start']} start" for g in mine)
            out[sport] = (mine, text or "All times")
    return out


def on_page(windows: dict, sport: str, date: dt.date, start: str) -> bool:
    goals, _ = windows.get(sport, ([], ""))
    if not goals:
        return True
    day = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][date.weekday()]
    return any(day in g["days"] and g["earliest_start"] <= start <= g["latest_start"]
               and (not g.get("only_date") or g["only_date"] == date.isoformat()) for g in goals)


def write_page(path: Path, by_day: dict, notes: dict, elsewhere: list, today: dt.date) -> None:
    """Phone-friendly page of free courts within the alert windows (what you're searching for)."""
    import html, re
    md_link = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    def linkify(text):
        out, last = [], 0
        for m in md_link.finditer(text):
            out.append(html.escape(text[last:m.start()]))
            out.append(f'<a href="{html.escape(m.group(2))}">{html.escape(m.group(1))}</a>')
            last = m.end()
        return "".join(out) + html.escape(text[last:])
    sports = sorted(by_day, key=lambda s: s != "Squash")
    windows = page_windows()
    parts = ["<!doctype html><html lang=en><head><meta charset=utf-8>",
             '<meta name=viewport content="width=device-width,initial-scale=1">',
             "<title>Free courts</title><style>" + PAGE_CSS + "</style></head><body><main>",
             "<h1>Free courts near E2</h1>",
             f'<p class=sub>Updated {dt.datetime.now():%a %d %b, %H:%M}. Tap a time to book.</p>',
             "<nav>" + "".join(f'<a href="#{s.lower()}">{s}</a>' for s in sports) + "</nav>"]
    for sport in sports:
        parts.append(f'<h2 id="{sport.lower()}">{sport}</h2><p class=win>{html.escape(windows.get(sport, ([], ""))[1])}</p>')
        shown = 0
        for date in sorted(by_day[sport]):
            rows = []
            for venue, times, link in by_day[sport][date]:
                ok = [t for t in times if on_page(windows, sport, date, t)]
                if ok:
                    chips = "".join(f'<a href="{html.escape(link)}">{t}</a>' for t in ok)
                    rows.append(f'<div class=row><div class=venue>{html.escape(venue)}</div><div class=times>{chips}</div></div>')
            if rows:
                shown += 1
                parts.append(f'<section class=day><h3>{date:%a %d %b}</h3>{"".join(rows)}</section>')
        if not shown:
            parts.append("<p class=none>Nothing free in this window right now.</p>")
        if notes.get(sport):
            parts.append("<ul class=notes>" + "".join(f"<li>{linkify(n)}</li>" for n in notes[sport]) + "</ul>")
    links = [f"[{name}]({TH_BOOK.format(park=slug, date=today)}) (tennis, Tower Hamlets)" for name, slug in CHECK_YOURSELF.items()]
    links += [e.lstrip("- ") for e in elsewhere]
    parts.append("<h2>Check these yourself</h2><p class=win>Can't be checked automatically from here; your phone still sends alerts for the ClubSpark ones.</p>")
    parts.append("<ul class=links>" + "".join(f"<li>{linkify(l)}</li>" for l in links) + "</ul>")
    full = os.environ.get("SUMMARY_URL")
    if full:
        parts.append(f'<p class=sub style="margin-top:24px"><a href="{html.escape(full)}" style="color:var(--accent)">Every free time, outside these windows too</a></p>')
    parts.append("</main></body></html>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts) + "\n")


def main():
    seen, seeded, reminders = load_state()
    now_seen, now_seeded = set(), set(seeded)
    new: dict[str, list[tuple[str, str, str]]] = {}  # sport -> [(alert line, label, booking link)]
    today = dt.date.today()
    now = dt.datetime.now().strftime("%H:%M")
    # sport -> date -> [(venue, times, link)], and sport -> [notes about venues]
    by_day: dict[str, dict[dt.date, list]] = {}
    notes: dict[str, list[str]] = {}

    elsewhere = []  # venues on platforms this run doesn't check
    for sport, venues in VENUES.items():
        if not any(cfg[0] in ENABLED for cfg in venues.values()):
            continue
        days, sport_notes = by_day.setdefault(sport, {}), notes.setdefault(sport, [])
        for venue, (platform, *args) in venues.items():
            fetch, make_link = PLATFORMS[platform]
            vkey = f"{sport}|{venue}"
            if platform not in ENABLED:
                elsewhere.append(f"- [{venue}]({make_link(*args, today.isoformat())}) ({sport.lower()})")
                now_seen |= {k for k in seen if k.startswith(f"{vkey}|")}  # keep its state intact
                continue
            # A venue we've never checked before is recorded silently, so a new repo
            # (or newly added venue) doesn't fire an alert for every free slot.
            first_check = vkey not in seeded
            unlisted = []
            for i in range(DAYS_AHEAD + 1):
                date = today + dt.timedelta(days=i)
                ds = date.isoformat()
                link = make_link(*args, ds)
                try:
                    slots = fetch(*args, ds)
                except Exception as e:
                    print(f"[{sport} {venue} {ds}] error: {e}")
                    sport_notes.append(f"{venue}: couldn't check from {date:%a %d %b}, [check manually]({link})")
                    # Carry over this venue's known slots so recovery doesn't re-alert them.
                    now_seen |= {k for k in seen if k.startswith(f"{vkey}|")}
                    break
                now_seeded.add(vkey)
                if slots == NOT_LISTED:
                    unlisted.append((date, link))
                    continue
                if slots is None:
                    break  # later days not released yet
                slots = sorted(s for s in slots if not (date == today and s[0] <= now))
                times = sorted({start for start, _, _ in slots})
                if times:
                    days.setdefault(date, []).append((venue, times, link))
                fresh = set()
                for start, _, court in slots:
                    key = f"{vkey}|{ds}|{start}|{court}"
                    now_seen.add(key)
                    if key not in seen and wanted(sport, date, start):
                        fresh.add(start)
                if fresh and not first_check:
                    new.setdefault(sport, []).append(
                        (f"{venue} {date:%a %d %b}: {', '.join(sorted(fresh))}",
                         f"{short_name(venue)} {date:%a %-d}", link))
            if unlisted:
                span = f"{unlisted[0][0]:%a %d}" + (f"–{unlisted[-1][0]:%a %d %b}" if len(unlisted) > 1 else f" {unlisted[0][0]:%b}")
                sport_notes.append(f"{venue}: nothing listed online {span}, [check manually]({unlisted[0][1]})")

    summary = [f"# Free courts", f"_Updated {dt.datetime.now():%a %d %b %H:%M}. Tap a time to book._"]
    for sport, days in sorted(by_day.items(), key=lambda kv: kv[0] != "Squash"):
        summary.append(f"\n## {sport}")
        if not days:
            summary.append("Nothing free in the next week.")
        for date in sorted(days):
            summary.append(f"\n**{date:%a %d %b}**")
            for venue, times, link in days[date]:
                summary.append(f"- {venue}: " + " · ".join(f"[{t}]({link})" for t in times))
        if notes.get(sport):
            summary.append("")
            summary.extend(f"- _{n}_" for n in notes[sport])
    summary.append("\n## Check these yourself")
    # TH forbids scraping
    for name, slug in CHECK_YOURSELF.items():
        summary.append(f"- [{name}]({TH_BOOK.format(park=slug, date=today)}) (tennis, Tower Hamlets)")
    summary.extend(elsewhere)

    text = "\n".join(summary) + "\n"
    print(text)
    SUMMARY_FILE.write_text(text)
    if os.environ.get("PAGE_FILE"):
        write_page(Path(os.environ["PAGE_FILE"]), by_day, notes, elsewhere, today)

    topics = {s: topic_for(s) for s in VENUES}
    if any(topics.values()):
        by_topic: dict[str, dict[str, list[str]]] = {}
        for sport, lines in new.items():
            if topics[sport]:
                by_topic.setdefault(topics[sport], {})[sport] = lines
        for topic, by_sport in by_topic.items():
            notify(topic, by_sport)
        # Shared channel for friends: the same court alerts, none of the booking messages.
        shared = os.environ.get("NTFY_TOPIC_SHARED")
        if shared and new and shared not in by_topic:
            notify(shared, new)
        if "better" in ENABLED:  # the GitHub run, so it's sent once rather than by the phone too
            weekend_reminder(reminders)
        cutoff = f"Tennis|{dt.date.today() - dt.timedelta(days=60):%G-W%V}"
        STATE_FILE.write_text(json.dumps({"venues": sorted(now_seeded), "slots": sorted(now_seen),
                                          "reminders": sorted(r for r in reminders if r >= cutoff)}, indent=0))


if __name__ == "__main__":
    main()
