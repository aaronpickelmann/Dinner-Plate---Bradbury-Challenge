# Bradbury Challenge daily texter

Ray Bradbury told young writers to read one short story, one poem and one essay every night for 1,000 nights.
This project picks your reading for you (plus a screenplay) and sends it to your phone every evening as a free push notification (or a text, or an email).
It runs free on GitHub Actions, so your computer doesn't need to be on.

```
Day 12
Find one line tonight you wish you'd written.

Short Story: Possession (Beneath Ceaseless Skies)
https://beneath-ceaseless-skies.com/stories/possession/

Poem: To My Brother Miguel (Poets.org)
https://poets.org/poem/my-brother-miguel

Essay: How Patterns Change (The Marginalian)
https://www.themarginalian.org/...

Screenplay: Hacksaw Ridge (2016) (Script Slug)
https://www.scriptslug.com/script/hacksaw-ridge-2016
```

## What it does

- Picks a random **service** for each category from [`services.json`](services.json). The same service is never used more than 2 days in a row for a category.
- Grabs a random piece from that service. **A piece is never sent twice.**
- Adds a random encouragement from [`messages.json`](messages.json). **The same message won't repeat within 14 days.**
- Keeps a **Day N** counter, and texts it all to you.

Sources include Clarkesworld, Strange Horizons, Lightspeed, Beneath Ceaseless Skies, The Dark, Nightmare, Uncanny,
Poets.org, Poetry Daily, The Marginalian, Paul Graham, Aeon, Psyche, Arts & Letters Daily, several Substacks,
IMSDb, Daily Script and Script Slug.

## Get your own copy (no coding needed)

1. **Copy the repo.** Click **Fork** (top right of this page) to get your own copy.
2. **Clear the previous owner's progress.** In your copy, delete `state.json` if it's there: open it, click the
   trash-can icon, then **Commit changes**. That file is the day counter and reading log, and it's recreated on your first send.
3. **Get the free ntfy app** ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) /
   [iPhone](https://apps.apple.com/us/app/ntfy/id1625396347)), with no account needed.
   - Make up a long, random **topic name**, like `bradbury-k7q2m9xw4t`.
   - In the app, tap **+** and subscribe to that topic.
   - Anyone who knows the name can read your notifications, so make it hard to guess.
4. **Add your topic as a secret.** In your copy go to **Settings → Secrets and variables → Actions → New repository secret**.
   Set the name to `NTFY_TOPIC` and the value to your topic name. Secrets are encrypted, and nobody (including people
   viewing your public repo) can see them.

5. **Set your timezone.** Open [`.github/workflows/daily.yml`](.github/workflows/daily.yml) and click the pencil icon.
   Change `BRADBURY_TIMEZONE` ([list of names](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)),
   and the two `cron` times, which are in UTC. The defaults send at 5:30 PM US Eastern.
6. **Turn on Actions.** Forks start with Actions off: open the **Actions** tab and click the green button to enable them.
7. **Test it.** Go to **Actions → Daily Bradbury text → Run workflow**:
   - Choose `dry-run` to see the text in the log without sending it.
   - Choose `send` to really text yourself.

That's it. It runs every evening on its own.

> **Want a real text message instead?** Add a repository *variable* (same page, **Variables** tab) named `DELIVERY_METHOD`
> with value `twilio`. Then add the secrets `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` and
> `BRADBURY_TO_NUMBER` (numbers look like `+15552223333`). Twilio costs a few dollars a month, and in the US it requires
> toll-free verification or A2P 10DLC registration first.
>
> **Prefer email?** Set `DELIVERY_METHOD` to `email` instead.
> Then add the secrets `SMTP_USERNAME` (your Gmail address), `SMTP_PASSWORD` (a Gmail
> [app password](https://myaccount.google.com/apppasswords)) and `BRADBURY_TO_ADDRESS`.
> That can be any inbox, or your carrier's email-to-text address, though those are unreliable.

## Things to know

- **The run time isn't exact.** GitHub's scheduled runs can start late, or be skipped, when their servers are busy.
  So the workflow tries every 20 minutes from 5:30 PM until about 7 PM, and the first try that runs sends.
  The rest finish in seconds without sending. Runs before 5:30 show as quick successes too.
  If nothing arrives, use **Run workflow → send**.
- **Your reading log is public.** After each send, the workflow saves `state.json` back to the repo: day counter,
  everything you've been sent, and which messages were used. It contains no phone numbers or passwords.
- **Already partway through the challenge?** After your first send, edit `"day"` in `state.json`.
- **Scheduled runs keep going.** GitHub pauses schedules on repos with no activity for 60 days, but the daily save
  of `state.json` counts as activity.

## Customizing

- **Messages:** edit [`messages.json`](messages.json). Keep at least 15 so the 14-day rule always has options.
  You can put `{day}` in a message to insert the day number.
- **Sources:** edit [`services.json`](services.json). Each service has a `name`, a `type` (`rss`, `html` or `sitemap`)
  and a `url`:
  - Put `{page}` in the URL with a `max_page` to pull from a random page of the site's archive. This gives much more variety than recent posts.
  - Use `urls` (a list) instead of `url` to pick one at random. The Substack entry works this way, so swap in your favorite Substacks.
  - `link_pattern`, `exclude_link_pattern` and `exclude_title_pattern` are regexes for filtering.
  - `require_categories` and `exclude_categories` filter feed posts by their tags.
  - Set `title_from_url` when a page's link text is useless (like "Read").
  - Set `"enabled": false` to turn a service off.
  - Keep at least 2 services per category.

## Running it on your own computer instead

Needs Python 3.9+ and nothing else.

```
python bradbury.py --check-sources   # test every source
python bradbury.py --dry-run         # show tonight's text without sending
python bradbury.py                   # send it
```

Settings go in `config.json`. Copy [`config.example.json`](config.example.json); it's git-ignored, so it never gets
uploaded. To run it daily, use Task Scheduler (Windows) or cron (Mac/Linux).
