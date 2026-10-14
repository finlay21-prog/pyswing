# pyswing 🎾

Polls east London tennis and squash court booking sites and sends ntfy alerts for newly free slots.

Based on [danol0/pyswing](https://github.com/danol0/pyswing), with squash added.

## What it watches

| Sport  | Venue                   | Booking system | Checked from   |
|--------|-------------------------|----------------|----------------|
| Tennis | Haggerston Park         | Better         | GitHub Actions |
| Tennis | Britannia (rooftop)     | Better         | GitHub Actions |
| Tennis | Rosemary Gardens        | Better         | GitHub Actions |
| Tennis | Joe White Gardens       | ClubSpark      | Phone          |
| Tennis | London Fields           | ClubSpark      | Phone          |
| Tennis | Hackney Downs           | ClubSpark      | Phone          |
| Tennis | Clissold Park           | ClubSpark      | Phone          |
| Squash | Britannia               | Better         | GitHub Actions |
| Squash | Ironmonger Row Baths    | Better         | GitHub Actions |
| Squash | Finsbury Leisure Centre | Better         | GitHub Actions |
| Squash | Clissold Leisure Centre | Better         | GitHub Actions |
| Squash | Sobell                  | Better         | GitHub Actions |

ClubSpark sits behind a Cloudflare firewall that blocks cloud servers, GitHub's included
(as of October 2026). So the ClubSpark venues are checked hourly from an Android
phone instead, using its own connection. See [Phone checks](#phone-checks-clubspark).
Which platforms a run checks is set by `PLATFORMS` (e.g. `PLATFORMS=better`); by default it checks all of them.

Bethnal Green Gardens (Tower Hamlets) is link-only, since that site forbids scraping.

To add a Better venue, open its booking page on bookings.better.org.uk. The URL is
`/location/<venue>/<activity>/<date>/by-time`; add `("better", "<venue>", "<activity>")`
to `VENUES` in `court_watch.py`.

## How it behaves

- Runs hourly on GitHub Actions and checks the next 7 days (or as far as each site has opened).
- Alerts only on slots that weren't free last time, so you hear about cancellations and newly opened days.
- The first check of any venue (a fresh repo, or a venue you've just added) is recorded silently, so you don't get a flood of alerts.
- If a Better venue has nothing at all on its online timetable, the summary says so with a link to check manually, rather than showing it as fully booked.

## Setup

1. **Create a repo** on your GitHub account and push these files to it. Public repos get Actions for free. A private repo uses your Actions minutes, at about 1 minute per run.
2. **Get notifications.** Install the ntfy app on your phone and subscribe to a topic name nobody would guess, such as `fin-courts-8k2x`. Anyone who knows the name can read it.
3. **Add the topic as a secret.** In the repo, go to Settings → Secrets and variables → Actions → New repository secret:
   - `NTFY_TOPIC`: alerts for everything, **or**
   - `NTFY_TOPIC_SQUASH` and/or `NTFY_TOPIC_TENNIS`: separate topics per sport. Set only the squash one if you just want squash alerts.
4. **Run it once by hand.** Go to Actions → court-watch → Run workflow. The first run records what's free without alerting. After that it runs hourly.

### Phone checks (ClubSpark)

Runs the ClubSpark venues every hour on an Android phone with Termux.

1. Install **Termux** and **Termux:API** from F-Droid (f-droid.org). Get both from the same place; the Play Store and F-Droid versions don't work together.
2. Settings → Apps → Termux → App battery usage → **Unrestricted**, so Android doesn't stop the hourly job. Do the same for Termux:API.
3. Open Termux and paste, with your ntfy topic at the end:
   ```
   pkg update -y && pkg install -y git && git clone https://github.com/finlay21-prog/pyswing && bash pyswing/phone/setup.sh YOUR-NTFY-TOPIC
   ```
   It installs Python, runs a first check (silently recording what's free) and schedules hourly checks.
   If ClubSpark can't be reached from the phone, it says so and stops.

Check it's running with `tail ~/pyswing-runs.log` (one line per run). The phone pulls code
changes from GitHub before each run, so it stays up to date. To stop it: `termux-job-scheduler --cancel --job-id 1`.

### Optional: summary page

Each run writes a `courts.md` summary of every free slot. To publish it:

1. Create a gist containing a file called `courts.md`. Any content will do; it gets overwritten.
2. Add a repository **variable** `GIST_ID` (the id from the gist URL).
3. Add a repository **secret** `GIST_TOKEN`: a personal access token with the `gist` scope.

Tapping a notification then opens the summary.

### Optional: more reliable timing

GitHub's built-in scheduler can run late at busy times. For dependable hourly checks, set up a free
cron-job.org job that calls the
[workflow_dispatch API](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)
for `court-watch.yml`, using a fine-grained token with Actions read/write on this repo.

### Run locally

```
pip install requests
python court_watch.py
```

With no ntfy topic set, it just prints the summary and changes nothing.
