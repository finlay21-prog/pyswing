"""Auto-book Better courts (squash, tennis...) for the goals in bookings.json, paying from Better account credit.

Each goal books at most one session per week (Mon-Sun), trying venues nearest-first, then the
earliest matching day, then the start time closest to the preferred one.

Safety:
- Nothing is paid for unless bookings.json has "live": true. Until then each run is a dry run:
  it logs in, puts the best slot in the basket, reads the price, then empties the basket.
- Payment is only ever from Better account credit, never a card.
- This repo's Actions logs are public, so the log never shows your details or what was booked;
  results go to your ntfy topic instead.

Env: BETTER_EMAIL, BETTER_PASSWORD, NTFY_TOPIC (or NTFY_TOPIC_<GOAL>), BOOKING_STATE (state file).
"""

import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import requests

BASE = "https://better-admin.org.uk"
CART = f"{BASE}/api/activities/cart"
SOURCE = "activity-booking"
CLIENT_DOMAIN = "better-admin.org.uk"  # what the booking site sends: its API host
MY_BOOKINGS_URL = "https://myaccount.better.org.uk/"
RELEASE_TIME = dt.time(22, 0)  # Better releases new days at 10pm
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

CONFIG = json.loads(Path(os.environ.get("BOOKING_CONFIG", "bookings.json")).read_text())
LIVE = CONFIG.get("live") is True
FREE_CANCEL_HOURS = float(CONFIG.get("free_cancel_hours", 4))
MIN_NOTICE_HOURS = max(float(CONFIG.get("min_hours_notice", 24)), FREE_CANCEL_HOURS + 1)
STATE_FILE = Path(os.environ.get("BOOKING_STATE", "booking-state.json"))
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json",
           "Origin": "https://bookings.better.org.uk", "Referer": "https://bookings.better.org.uk/"}


def log(msg: str):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def shape(obj, depth=0):
    """Structure of an API response with personal values hidden, for debugging in public logs."""
    if depth > 5:
        return "..."
    if isinstance(obj, dict):
        return {k: shape(v, depth + 1) for k, v in list(obj.items())[:40]}
    if isinstance(obj, list):
        return [shape(obj[0], depth + 1), f"({len(obj)} items)"] if obj else []
    if isinstance(obj, bool) or obj is None:
        return obj
    if isinstance(obj, (int, float)):
        return "<num>"
    return f"<str:{len(str(obj))}>"


def notify(goal: str, title: str, body: str, click: str = MY_BOOKINGS_URL, priority: str = "default"):
    topic = os.environ.get(f"NTFY_TOPIC_{goal.upper()}") or os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    headers = {"Title": title, "Tags": goal.lower(), "Click": click, "Priority": priority}
    requests.post(f"https://ntfy.sh/{topic}", data=body.encode(),
                  headers={k: v.encode("latin-1", "replace").decode("latin-1") for k, v in headers.items()},
                  timeout=20)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict):
    cutoff = (dt.date.today() - dt.timedelta(days=21)).isocalendar()
    keep = {k: v for k, v in state.items()
            if not k.startswith("week:") or tuple(map(int, k.split(":")[2].split("-W"))) >= cutoff[:2]}
    STATE_FILE.write_text(json.dumps(keep, indent=1))


def week_taken(state: dict, key: str) -> bool:
    v = state.get(key)
    if not v:
        return False
    if v.startswith("held:"):  # court left in your basket recently; try again later if you didn't pay
        return dt.datetime.now() - dt.datetime.fromisoformat(v[5:]) < dt.timedelta(hours=HOLD_HOURS)
    return True


def week_key(goal: str, date: dt.date) -> str:
    y, w, _ = date.isocalendar()
    return f"week:{goal}:{y}-W{w:02d}"


# ------------------------------------------------------------------ slots ----
def get_slots(session, venue: str, activity: str, date: dt.date) -> list[dict]:
    r = session.get(f"{BASE}/api/activities/venue/{venue}/activity/{activity}/v2/slots",
                    params={"date": date.isoformat()}, timeout=20)
    if r.status_code == 422:  # not released yet
        return []
    r.raise_for_status()
    return r.json().get("data") or []


def is_confirmed(booking) -> bool:
    """A slot's 'booking' field can also reflect a court sitting in your basket; only count real bookings."""
    if not isinstance(booking, dict):
        return bool(booking)
    status = str(booking.get("status") or booking.get("state") or "").lower()
    return status not in ("basket", "cart", "in_cart", "reserved", "pending", "provisional")


def minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def matching(goal: dict, slots: list[dict]) -> list[dict]:
    lo, hi = minutes(goal["earliest_start"]), minutes(goal["latest_start"])
    return [s for s in slots if lo <= minutes(s["starts_at"]["format_24_hour"]) <= hi]


def candidates(session, goal: dict, state: dict) -> tuple[list[tuple], set[str]]:
    """Bookable slots for weeks this goal hasn't booked yet, best first; also weeks seen as booked."""
    now = dt.datetime.now()
    pref = minutes(goal["preferred_start"])
    found, booked_weeks = [], set()
    for vi, (venue, activity) in enumerate(goal["venues_nearest_first"]):
        for i in range(8):
            date = now.date() + dt.timedelta(days=i)
            if DAYS[date.weekday()] not in goal["days"] or week_taken(state, week_key(goal["name"], date)):
                continue
            try:
                day_slots = get_slots(session, venue, activity, date)
            except requests.RequestException as e:
                log(f"{goal['name']}: venue {vi + 1} day +{i}: couldn't load slots ({type(e).__name__})")
                continue
            mine = [s["booking"] for s in day_slots if s.get("booking")]
            if mine and not LIVE:
                log(f"{goal['name']}: venue {vi + 1} day +{i}: 'booking' field on {len(mine)} slot(s): {json.dumps(shape(mine[0]))[:600]}")
            if any(is_confirmed(b) for b in mine):  # you already have a court that day (visible when logged in)
                booked_weeks.add(week_key(goal["name"], date))
            for s in matching(goal, day_slots):
                start = s["starts_at"]["format_24_hour"]
                starts = dt.datetime.combine(date, dt.time(*map(int, start.split(":"))))
                if starts - now < dt.timedelta(hours=MIN_NOTICE_HOURS):
                    continue  # not enough time to cancel free if you can't play
                price = (s.get("price") or {}).get("raw", 0) / 100
                if (s.get("action_to_show") or {}).get("status") == "BOOK" and int(s.get("spaces", 0)) > 0 \
                        and price <= goal["max_price"]:
                    found.append(((vi, date, abs(minutes(start) - pref), -minutes(start)), date, s))
    found = [c for c in found if week_key(goal["name"], c[1]) not in booked_weeks]
    found.sort(key=lambda c: c[0])
    return [(date, s) for _, date, s in found], booked_weeks


def cancel_by(date: dt.date, s: dict) -> dt.datetime:
    start = dt.time(*map(int, s["starts_at"]["format_24_hour"].split(":")))
    return dt.datetime.combine(date, start) - dt.timedelta(hours=FREE_CANCEL_HOURS)


def describe(date: dt.date, s: dict) -> str:
    return (f"{s.get('venue_name', '')} {date:%a %d %b} {s['starts_at']['format_24_hour']}"
            f"-{s['ends_at']['format_24_hour']}, {(s.get('location') or {}).get('name', '').split(',')[0]}, "
            f"{(s.get('price') or {}).get('formatted', '')}")


# ---------------------------------------------------------------- account ----
def find_key(obj, *names):
    if isinstance(obj, dict):
        for n in names:
            if obj.get(n):
                return obj[n]
        for v in obj.values():
            hit = find_key(v, *names)
            if hit:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = find_key(v, *names)
            if hit:
                return hit
    return None


def login(session) -> dict:
    email, password = os.environ.get("BETTER_EMAIL"), os.environ.get("BETTER_PASSWORD")
    if not email or not password:
        raise SystemExit("BETTER_EMAIL / BETTER_PASSWORD secrets aren't set")
    r = session.post(f"{BASE}/api/v1/auth/login", timeout=20,
                     headers={"Client-Domain-Identifier": CLIENT_DOMAIN},  # only the login call sends this
                     json={"data": {"user_type": "customer", "username_or_email": email, "password": password}})
    if r.status_code in (404, 405):  # older login endpoint
        r = session.post(f"{BASE}/api/auth/customer/login", timeout=20,
                         json={"username": email, "password": password})
    if not r.ok:
        log(f"login failed: HTTP {r.status_code} {shape(r.json()) if r.headers.get('content-type', '').startswith('application/json') else ''}")
        raise LoginError(r.status_code)
    body = r.json()
    token = find_key(body, "access_token", "accessToken", "token")
    if not token:
        log(f"login response had no token: {shape(body)}")
        raise LoginError("no token")
    print(f"::add-mask::{token}")
    session.headers["Authorization"] = f"Bearer {token}"
    u = session.get(f"{BASE}/api/auth/user", timeout=20)
    if not u.ok:
        log(f"loading account failed: HTTP {u.status_code}")
        raise LoginError(u.status_code)
    user = u.json().get("data", u.json())
    if not LIVE:
        log(f"account: {json.dumps(shape(user))[:1500]}")
    print(f"::add-mask::{user.get('id', '')}")
    log("logged in")
    return user


class LoginError(Exception):
    pass


def api(session, method: str, url: str, debug: str, **kw):
    r = session.request(method, url, timeout=30, **kw)
    try:
        body = r.json()
    except ValueError:
        body = {"non_json": r.text[:0]}
    if not LIVE or not r.ok:
        log(f"{debug}: HTTP {r.status_code} {json.dumps(shape(body))[:1500]}")
    if not r.ok:
        raise BookingError(debug, r.status_code, body)
    return body


class BookingError(Exception):
    def message(self) -> str:
        _, status, body = self.args
        msg = find_key(body, "message", "error") if isinstance(body, dict) else None
        return f"{self.args[0]} failed (HTTP {status}){': ' + str(msg)[:200] if msg else ''}"


BASKET_URL = "https://bookings.better.org.uk/basket"
HOLD_HOURS = 3  # after putting a court in your basket, leave that week alone this long


def basket_items(cart: dict) -> list[dict]:
    items = find_key(cart, "items", "cart_items") or []
    return [i for i in items if isinstance(i, dict) and i.get("id")] if isinstance(items, list) else []


def remove_items(session, uid, ids: list, debug: str):
    if ids:
        api(session, "POST", f"{CART}/remove", f"{debug}: remove {len(ids)} item(s)",
            params={k: v for k, v in {"membership_user_id": uid}.items() if v}, json={"cart_item_ids": ids})


def book(session, user: dict, date: dt.date, s: dict) -> tuple[str, str]:
    """Put the court in your Better basket. If your Better credit covers the whole basket, pay with it;
    otherwise leave it there for you to pay by card in the app. Returns (outcome, detail) where outcome
    is "paid", "in basket" or "dry run". Never touches anything already in your basket."""
    mu = user.get("membership_user")  # None unless you have a Better membership
    uid = mu.get("id") if isinstance(mu, dict) else mu
    before = {i["id"] for i in basket_items(api(session, "GET", CART, "load basket"))}
    added, keep = [], False
    try:
        api(session, "POST", f"{CART}/add", "add to basket",
            json={"items": [{"id": s["id"], "type": s.get("cart_type", "purchasableOccurrence"),
                             "pricing_option_id": s.get("pricing_option_id"), "apply_benefit": True,
                             "activity_restriction_ids": s.get("restriction_ids") or []}],
                  "membership_user_id": uid, "selected_user_id": None})
        cart = api(session, "GET", CART, "load basket").get("data") or {}
        added = [i["id"] for i in basket_items(cart) if i["id"] not in before]
        if not added:
            raise BookingError("add to basket", 0, {"message": "court didn't appear in the basket"})
        amount = cart.get("total")
        if not isinstance(amount, (int, float)):
            raise BookingError("read basket total", 0, {"message": "basket total missing"})
        general = (cart.get("credits") or {}).get("general") or {}
        available = float(general.get("total_available") or 0)
        can_pay = not before and available >= amount  # only pay if the basket is just this court
        if not LIVE:
            return "dry run", (f"Basket total {cart.get('formattedTotal', amount)}, Better credit {available:g}. "
                               f"When live it would {'pay from credit' if can_pay else 'leave it in your basket for you to pay'}. "
                               "Removed from your basket again; nothing was paid.")
        if not can_pay:
            keep = True
            return "in basket", "Waiting in your Better basket - pay by card in the app to confirm."
        if amount:
            api(session, "POST", f"{BASE}/api/credits/apply", "apply credit",
                json={"credits_to_reserve": [{"amount": amount, "type": general.get("type", "general")}],
                      "cart_source": SOURCE, "selected_user_id": None})
        done = api(session, "POST", f"{BASE}/api/checkout/complete", "complete checkout",
                   json={"completed_waivers": [],
                         "payments": [{"tender_type": "credit", "amount": amount}] if amount else [],
                         "selected_user_id": None, "source": SOURCE, "terms": [],
                         "item_hash": cart.get("itemHash")})
        keep = True
        return "paid", f"Paid from Better credit (order {find_key(done, 'complete_order_id', 'completeOrderId', 'order_id') or 'confirmed'})."
    finally:
        if added and not keep:  # dry run or failure: take out only what we put in
            try:
                remove_items(session, uid, added, "cleanup")
            except Exception as e:
                log(f"cleanup failed: {type(e).__name__}")


# ------------------------------------------------------------------- main ----
def wait_for_release():
    """If we're just before 10pm, sleep until the release so we're first in."""
    now = dt.datetime.now()
    release = dt.datetime.combine(now.date(), RELEASE_TIME) + dt.timedelta(seconds=3)
    if dt.timedelta(0) < release - now < dt.timedelta(minutes=80):
        log(f"waiting {int((release - now).total_seconds())}s for the 10pm release")
        time.sleep((release - now).total_seconds())
        return True
    return False


def run_goal(goal: dict, state: dict, attempts: int) -> None:
    anon = requests.Session()
    anon.headers.update(HEADERS)
    name, sport = goal["name"], goal.get("sport", goal["name"])
    for attempt in range(attempts):
        cands, _ = candidates(anon, goal, state)
        if cands:
            break
        if attempt + 1 < attempts:
            time.sleep(20)
    if not cands:
        log(f"{name}: nothing suitable to book")
        return
    log(f"{name}: {len(cands)} suitable slot(s); logging in")
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        user = login(session)
    except LoginError:
        notify(sport, "Court booker: Better login failed",
               "Check the BETTER_EMAIL and BETTER_PASSWORD secrets in GitHub.", priority="high")
        return
    # Re-check while logged in: shows your own bookings and your own booking window.
    cands, booked = candidates(session, goal, state)
    if not cands:
        log(f"{name}: nothing bookable once logged in ({len(booked)} week(s) already booked)")
        return
    last_error = ""
    for date, s in cands[:3]:
        try:
            outcome, detail = book(session, user, date, s)
        except BookingError as e:
            log(f"{name}: attempt failed: {e.message()}")
            last_error = e.message()
            continue
        except Exception as e:  # unexpected response shape etc.
            log(f"{name}: attempt failed: {type(e).__name__}: {e}")
            last_error = f"{type(e).__name__}: {e}"
            continue
        log(f"{name}: {outcome} (details sent to ntfy)")
        when = f"{date:%a %d %b} {s['starts_at']['format_24_hour']}"
        cancel = f"Can't make it? Cancel in the Better app by {cancel_by(date, s):%a %d %b %H:%M} for a refund to credit."
        if outcome == "paid":
            state[week_key(name, date)] = "booked"
            notify(sport, f"Booked {sport.lower()}: {when}", f"{describe(date, s)}\n{detail}\n{cancel}",
                   priority="high")
        elif outcome == "in basket":
            state[week_key(name, date)] = f"held:{dt.datetime.now():%Y-%m-%dT%H:%M}"
            notify(sport, f"Pay now: {sport.lower()} {when} is in your basket",
                   f"{describe(date, s)}\nTap to pay in Better before someone else gets it.\n{cancel}",
                   click=BASKET_URL, priority="high")
        else:
            notify(sport, f"Dry run: would get {sport.lower()} {when}", f"{describe(date, s)}\n{detail}")
        return
    notify(sport, f"Court booker couldn't get {sport.lower()}", f"Tried {describe(*cands[0])}\n{last_error}")


def main():
    only = set(sys.argv[1:])
    if not LIVE and os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        log("dry-run mode: scheduled runs do nothing until bookings.json has \"live\": true")
        return
    at_release = wait_for_release()
    state = load_state()
    log(f"mode: {'LIVE' if LIVE else 'dry run'}")
    for goal in CONFIG["goals"]:
        if only and goal["name"] not in only:
            continue
        # At release, retry for ~5 minutes in case the new day appears a little late.
        run_goal(goal, state, attempts=15 if at_release else 1)
    if LIVE:  # dry runs never record anything
        save_state(state)


if __name__ == "__main__":
    main()
