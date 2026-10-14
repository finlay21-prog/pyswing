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
        "Rosemary Gardens": ("better", "islington-tennis-centre", "rosemary-gardens-tennis"),
        "Joe White Gardens": ("clubspark", "AskeGardens"),
        "London Fields": ("clubspark", "LondonFieldsPark"),
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
    """State is {"venues": [...checked at least once...], "slots": [...keys...]}."""
    if not STATE_FILE.exists():
        return set(), set()
    raw = json.loads(STATE_FILE.read_text() or "{}")
    if isinstance(raw, list):  # original pyswing format; keys differ, so start afresh (silently)
        return set(), set()
    return set(raw.get("slots", [])), set(raw.get("venues", []))


def main():
    seen, seeded = load_state()
    now_seen, now_seeded = set(), set(seeded)
    new: dict[str, list[tuple[str, str, str]]] = {}  # sport -> [(alert line, label, booking link)]
    summary = ["# Free courts"]
    today = dt.date.today()
    now = dt.datetime.now().strftime("%H:%M")

    elsewhere = []  # venues on platforms this run doesn't check
    for sport, venues in VENUES.items():
        if not any(cfg[0] in ENABLED for cfg in venues.values()):
            continue
        summary.append(f"\n# {sport}")
        for venue, (platform, *args) in venues.items():
            fetch, make_link = PLATFORMS[platform]
            vkey = f"{sport}|{venue}"
            if platform not in ENABLED:
                elsewhere.append(f"\n## {venue} ({sport.lower()}, checked separately)\n"
                                 f"- [Book]({make_link(*args, today.isoformat())})")
                now_seen |= {k for k in seen if k.startswith(f"{vkey}|")}  # keep its state intact
                continue
            # A venue we've never checked before is recorded silently, so a new repo
            # (or newly added venue) doesn't fire an alert for every free slot.
            first_check = vkey not in seeded
            summary.append(f"\n## {venue}")
            unlisted = []  # consecutive days with nothing on the timetable, shown as one line

            def flush_unlisted():
                if unlisted:
                    span = f"{unlisted[0][0]:%a %d %b}" + (
                        f" – {unlisted[-1][0]:%a %d %b}" if len(unlisted) > 1 else "")
                    summary.append(f"- {span}: nothing listed online, [check manually]({unlisted[0][1]})")
                    unlisted.clear()

            for i in range(DAYS_AHEAD + 1):
                date = today + dt.timedelta(days=i)
                ds = date.isoformat()
                link = make_link(*args, ds)
                try:
                    slots = fetch(*args, ds)
                except Exception as e:
                    print(f"[{sport} {venue} {ds}] error: {e}")
                    flush_unlisted()
                    summary.append(f"- {date:%a %d %b} onwards: couldn't check, [check manually]({link})")
                    # Carry over this venue's known slots so recovery doesn't re-alert them.
                    now_seen |= {k for k in seen if k.startswith(f"{vkey}|")}
                    break
                now_seeded.add(vkey)
                if slots == NOT_LISTED:
                    unlisted.append((date, link))
                    continue
                flush_unlisted()
                if slots is None:
                    summary.append(f"- {date:%a %d %b} onwards: not open for booking yet")
                    break
                slots = sorted(s for s in slots if not (date == today and s[0] <= now))
                times = sorted({start for start, _, _ in slots})
                summary.append(f"- {date:%a %d %b}: "
                               + (", ".join(f"[{t}]({link})" for t in times) or "-"))
                fresh = set()
                for start, _, court in slots:
                    key = f"{vkey}|{ds}|{start}|{court}"
                    now_seen.add(key)
                    if key not in seen:
                        fresh.add(start)
                if fresh and not first_check:
                    new.setdefault(sport, []).append(
                        (f"{venue} {date:%a %d %b}: {', '.join(sorted(fresh))}",
                         f"{short_name(venue)} {date:%a %-d}", link))
            flush_unlisted()

    # TH forbids scraping
    summary.append("\n# Check manually\n"
                   "\n## Bethnal Green Gardens (tennis + padel)\n"
                   f"- [Book](https://tennistowerhamlets.com/book/courts/bethnal-green-gardens/{today}#book)")
    summary.extend(elsewhere)

    text = "\n".join(summary) + "\n"
    print(text)
    SUMMARY_FILE.write_text(text)

    topics = {s: topic_for(s) for s in VENUES}
    if any(topics.values()):
        by_topic: dict[str, dict[str, list[str]]] = {}
        for sport, lines in new.items():
            if topics[sport]:
                by_topic.setdefault(topics[sport], {})[sport] = lines
        for topic, by_sport in by_topic.items():
            notify(topic, by_sport)
        STATE_FILE.write_text(json.dumps({"venues": sorted(now_seeded),
                                          "slots": sorted(now_seen)}, indent=0))


if __name__ == "__main__":
    main()
