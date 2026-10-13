# pyswing 🎾

Polls east London tennis and squash court booking sites and sends ntfy alerts for newly free slots.

Based on [danol0/pyswing](https://github.com/danol0/pyswing), with squash added.

## What it watches

| Sport  | Venue                   | Booking system |
|--------|-------------------------|----------------|
| Tennis | Haggerston Park         | Better         |
| Tennis | Rosemary Gardens        | Better         |
| Tennis | Joe White Gardens       | ClubSpark      |
| Tennis | London Fields           | ClubSpark      |
| Tennis | Clissold Park           | ClubSpark      |
| Squash | Britannia               | Better         |
| Squash | Clissold Leisure Centre | Better         |
| Squash | Sobell                  | Better         |

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
