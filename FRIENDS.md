# Court alerts for friends

## Option 1: get the same alerts (2 minutes)

You'll get a phone notification whenever a court frees up near Hackney Road / Haggerston (E2):
squash any time, and tennis at weekends 9am–6pm.

1. Install **ntfy**: [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) · [iPhone](https://apps.apple.com/app/ntfy/id1625396347)
2. Open it, tap **+**, enter the topic **`e2-courts-df3oa5`** and tap **Subscribe**.

Tap an alert to go straight to the booking page. Every free court for the next week is on
**[this page](https://github.com/finlay21-prog/pyswing/blob/main/free-courts.md)**, which updates hourly.

Courts covered: squash at Britannia, Ironmonger Row, Finsbury, Clissold and Sobell; tennis at
Haggerston, Britannia rooftop, Rosemary Gardens, London Fields, Joe White Gardens, Hackney Downs
and Clissold Park.

## Option 2: run your own copy

Do this if you want your own courts, times, or auto-booking. It's free, but takes about 15 minutes
and needs a GitHub account.

1. **Copy the project:** sign in to GitHub, open [finlay21-prog/pyswing](https://github.com/finlay21-prog/pyswing)
   and tap **Fork**.
2. **Choose a private alert topic:** in ntfy, subscribe to a name nobody would guess, e.g. `yourname-courts-7q2k`.
3. **Add it as a secret:** in your copy go to Settings → Secrets and variables → Actions → New repository
   secret. Name `NTFY_TOPIC`, value your topic.
4. **Switch the checks on:** open the **Actions** tab, tap "I understand my workflows, go ahead and enable them",
   then open **court-watch** → **Run workflow**. The first run records what's free without alerting.
5. **Optional, for the ClubSpark tennis courts** (London Fields, Joe White, Hackney Downs, Clissold Park): ClubSpark
   blocks GitHub, so these are checked from an Android phone. Install **Termux** and **Termux:API** from
   [F-Droid](https://f-droid.org), set their battery use to Unrestricted, open Termux and paste:
   ```
   pkg update -y && pkg install -y git && git clone https://github.com/YOUR-USERNAME/pyswing && bash pyswing/phone/setup.sh YOUR-TOPIC https://github.com/YOUR-USERNAME/pyswing
   ```

**Different courts or times:** the venues are in `VENUES` in `court_watch.py`, and tennis alert times are in
`ALERT_WINDOWS`. To add a Better venue, open its booking page on bookings.better.org.uk. The address
ends `/location/<venue>/<activity>/<date>/by-time`, and those two names go in `VENUES`.

**Auto-booking (optional, Better courts only):** first edit `bookings.json` with your days, times and venues.
Then add secrets `BETTER_EMAIL` and `BETTER_PASSWORD`. When a matching court comes up, it puts the court in
your Better basket and sends you a "Pay now" alert. It only books courts at least 24 hours away, so you can
always cancel free. Until those secrets are added it does nothing. The Actions logs of a public repo are public,
so the code never prints your details.
