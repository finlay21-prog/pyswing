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
            if DAYS[date.weekday()] not in goal["days"] or week_key(goal["name"], date) in state:
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
                if date == now.date() and minutes(start) <= now.hour * 60 + now.minute + 60:
                    continue  # too soon to be useful
                price = (s.get("price") or {}).get("raw", 0) / 100
                if (s.get("action_to_show") or {}).get("status") == "BOOK" and int(s.get("spaces", 0)) > 0 \
                        and price <= goal["max_price"]:
                    found.append(((vi, date, abs(minutes(start) - pref), -minutes(start)), date, s))
    found = [c for c in found if week_key(goal["name"], c[1]) not in booked_weeks]
    found.sort(key=lambda c: c[0])
    return [(date, s) for _, date, s in found], booked_weeks


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


def empty_basket(session, uid, debug: str):
    cart = api(session, "GET", CART, f"{debug}: load basket")
    items = find_key(cart, "items", "cart_items") or []
    ids = [i.get("id") for i in items if isinstance(i, dict) and i.get("id")] if isinstance(items, list) else []
    if ids:
        api(session, "POST", f"{CART}/remove", f"{debug}: remove {len(ids)} item(s)",
            params={k: v for k, v in {"membership_user_id": uid}.items() if v}, json={"cart_item_ids": ids})
    return cart


def book(session, user: dict, date: dt.date, s: dict) -> str:
    """Basket -> credit -> checkout. Returns a status line. In dry-run mode stops before paying."""
    mu = user.get("membership_user")  # None unless you have a Better membership
    uid = mu.get("id") if isinstance(mu, dict) else mu
    # Start from an empty basket so we only pay for this court.
    empty_basket(session, uid, "before")
    completed = False
    try:
        api(session, "POST", f"{CART}/add", "add to basket",
            json={"items": [{"id": s["id"], "type": s.get("cart_type", "purchasableOccurrence"),
                             "pricing_option_id": s.get("pricing_option_id"), "apply_benefit": True,
                             "activity_restriction_ids": s.get("restriction_ids") or []}],
                  "membership_user_id": uid, "selected_user_id": None})
        cart = (api(session, "GET", CART, "load basket").get("data") or {})
        amount = cart.get("total")
        if not isinstance(amount, (int, float)):
            raise BookingError("read basket total", 0, {"message": "basket total missing"})
        general = (cart.get("credits") or {}).get("general") or {}
        available = float(general.get("total_available") or 0)
        if not LIVE:
            return (f"Dry run OK: basket total {cart.get('formattedTotal', amount)}, Better credit available "
                    f"{available:g}{' (enough)' if available >= amount else ' - NOT enough, top up before going live'}. "
                    "Basket emptied again. Nothing was paid.")
        if available < amount:
            raise BookingError("credit check", 402, {"message": "Not enough Better credit - top up to keep auto-booking"})
        if amount:
            api(session, "POST", f"{BASE}/api/credits/apply", "apply credit",
                json={"credits_to_reserve": [{"amount": amount, "type": general.get("type", "general")}],
                      "cart_source": SOURCE, "selected_user_id": None})
        done = api(session, "POST", f"{BASE}/api/checkout/complete", "complete checkout",
                   json={"completed_waivers": [],
                         "payments": [{"tender_type": "credit", "amount": amount}] if amount else [],
                         "selected_user_id": None, "source": SOURCE, "terms": [],
                         "item_hash": cart.get("itemHash")})
        completed = True
        return f"Booked (order {find_key(done, 'complete_order_id', 'completeOrderId', 'order_id') or 'confirmed'})"
    finally:
        if not completed:  # dry run, or something failed: don't leave a court sitting in your basket
            try:
                empty_basket(session, uid, "cleanup")
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
    name = goal["name"]
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
        notify(name, "Court booker: Better login failed",
               "Check the BETTER_EMAIL and BETTER_PASSWORD secrets in GitHub.", priority="high")
        return
    # Clear anything left in the basket by an earlier failed run before looking.
    mu = user.get("membership_user")
    try:
        empty_basket(session, mu.get("id") if isinstance(mu, dict) else mu, "start")
    except Exception as e:
        log(f"couldn't check basket: {type(e).__name__}")
    # Re-check while logged in: shows your own bookings and your own booking window.
    cands, booked = candidates(session, goal, state)
    if not cands:
        log(f"{name}: nothing bookable once logged in ({len(booked)} week(s) already booked)")
    for wk in booked:
        state[wk] = "already booked"
    for date, s in cands[:3]:
        try:
            result = book(session, user, date, s)
        except BookingError as e:
            log(f"{name}: attempt failed: {e.message()}")
            last_error = e.message()
            continue
        except Exception as e:  # unexpected response shape etc.
            log(f"{name}: attempt failed: {type(e).__name__}: {e}")
            last_error = f"{type(e).__name__}: {e}"
            continue
        log(f"{name}: {'booked' if LIVE else 'dry run ok'} (details sent to ntfy)")
        if LIVE:
            state[week_key(name, date)] = "booked"
            notify(name, f"Booked {name.lower()}: {date:%a %d %b} {s['starts_at']['format_24_hour']}",
                   f"{describe(date, s)}\nPaid from Better credit. Cancel in the Better app if you can't make it.",
                   priority="high")
        else:
            notify(name, f"Dry run: would book {name.lower()}", f"{describe(date, s)}\n{result}")
        return
    else:
        if cands:
            notify(name, f"Court booker couldn't book {name.lower()}",
                   f"Tried {describe(*cands[0])}\n{last_error}")


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
    save_state(state)


if __name__ == "__main__":
    main()
