#!/usr/bin/env python3
"""
Bradbury Challenge - daily reading picker and texter.

Each run (meant to be once a day):
  1. For every category in services.json (Short Story, Poem, Essay, Screenplay),
     randomly picks a service, skipping any service that has already been used
     for that category on each of the last 2 days.
  2. Pulls the list of pieces from that service (RSS/Atom feed or HTML page) and
     randomly picks one you have never been sent before.
  3. Builds a text with "Day N", a random preset from messages.json (not used in
     the last 14 days), and each piece's category, title, service and link, then
     sends it to your phone (Twilio SMS, or email such as a carrier gateway).

Usage:
  python bradbury.py                  # normal daily run
  python bradbury.py --dry-run        # pick and print, but don't send or save
  python bradbury.py --check-sources  # test every service in services.json
  python bradbury.py --force          # run again even if already run today

Standard library only, no pip installs needed.
"""

import argparse
import base64
import datetime as dt
import json
import logging
import os
import random
import re
import smtplib
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SERVICES_FILE = BASE_DIR / "services.json"
MESSAGES_FILE = BASE_DIR / "messages.json"
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "state.json"
LOG_FILE = BASE_DIR / "bradbury.log"

DEFAULT_MAX_CONSECUTIVE_DAYS = 2
DEFAULT_MESSAGE_COOLDOWN_DAYS = 14
SMS_CHUNK_LIMIT = 1500  # Twilio caps a single message body at 1600 chars
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BradburyChallengeBot/1.0"
HTTP_TIMEOUT = 30

log = logging.getLogger("bradbury")


# --------------------------------------------------------------------------- #
# JSON / state helpers
# --------------------------------------------------------------------------- #

def load_json(path, default=None):
    if not path.exists():
        if default is not None:
            return default
        raise FileNotFoundError(f"Missing required file: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json_atomic(path, data):
    """Write to a temp file then swap it in, so a crash can't corrupt state."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def default_state():
    return {
        "day": 0,                 # last day number that was sent
        "last_run_date": None,    # ISO date of last successful send
        "used_pieces": [],        # every piece ever sent (also your reading log)
        "service_history": {},    # category -> [service used each day, oldest first]
        "message_history": [],    # [{"message": text, "date": iso}]
    }


def load_state():
    state = default_state()
    state.update(load_json(STATE_FILE, default={}))
    return state


# --------------------------------------------------------------------------- #
# Fetching & parsing sources
# --------------------------------------------------------------------------- #

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        data = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
    return data, charset


def normalize_url(url):
    """Canonical form used to detect repeats (ignores www, trailing slash, #fragment)."""
    p = urllib.parse.urlsplit(url.strip())
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = urllib.parse.unquote(p.path).rstrip("/")
    return f"{host}{path}" + (f"?{p.query}" if p.query else "")


def clean_title(title, svc):
    title = re.sub(r"\s+", " ", title or "").strip()
    strip_re = svc.get("title_strip")
    if strip_re:
        title = re.sub(strip_re, "", title).strip()
    if svc.get("move_trailing_article"):
        # "Thing, The" -> "The Thing"
        title = re.sub(r"^(.*), (The|A|An)$", r"\2 \1", title)
    title = title.strip(" \"'“”")
    if len(title) > 90:  # e.g. Arts & Letters Daily uses whole blurbs as titles
        title = title[:90].rsplit(" ", 1)[0].rstrip(",;:-") + "..."
    return title


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def parse_feed(data):
    """Parse RSS 2.0 or Atom. Returns [(title, link, [categories])]."""
    root = ET.fromstring(data.strip())
    entries = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        title, link, cats = "", "", []
        for child in el:
            name = _local(child.tag)
            if name == "title":
                title = "".join(child.itertext())
            elif name == "link" and not link:
                href = child.get("href")
                if href and child.get("rel", "alternate") == "alternate":
                    link = href
                elif child.text and child.text.strip():
                    link = child.text.strip()
            elif name == "category":
                cat = child.get("term") or child.text or ""
                if cat.strip():
                    cats.append(cat.strip())
        if link:
            entries.append((title, link, cats))
    return entries


class _LinkCollector(HTMLParser):
    """Collects (href, link text). Tolerates sloppy HTML where </a> is missing
    (IMSDb does `<a ...>Title<td>...`), by ending a link at table/list boundaries."""
    BOUNDARY = {"td", "th", "tr", "table", "li", "ul", "ol"}

    def __init__(self):
        super().__init__()
        self.links = []
        self._href = None
        self._text = []

    def _close(self):
        if self._href is not None:
            self.links.append((self._href, "".join(self._text)))
            self._href = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._close()
            self._href = dict(attrs).get("href")
            self._text = []
        elif tag in self.BOUNDARY:
            self._close()

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" or tag in self.BOUNDARY:
            self._close()


def parse_html_links(data, charset, page_url):
    parser = _LinkCollector()
    parser.feed(data.decode(charset, errors="replace"))
    # Spaces in hrefs (e.g. IMSDb "/Movie Scripts/...") would break the link in a text.
    return [(text, urllib.parse.urljoin(page_url, href.strip()).replace(" ", "%20"), [])
            for href, text in parser.links if href]


def passes_filters(svc, title, url, cats):
    lower_cats = {c.lower() for c in cats}
    if svc.get("link_pattern") and not re.search(svc["link_pattern"], url, re.I):
        return False
    if svc.get("exclude_link_pattern") and re.search(svc["exclude_link_pattern"], url, re.I):
        return False
    if svc.get("exclude_title_pattern") and re.search(svc["exclude_title_pattern"], title, re.I):
        return False
    if any(c.lower() not in lower_cats for c in svc.get("require_categories", [])):
        return False
    if any(c.lower() in lower_cats for c in svc.get("exclude_categories", [])):
        return False
    return True


def parse_sitemap(data):
    """Parse a sitemap.xml: every <loc> is a piece; the title comes from the URL."""
    root = ET.fromstring(data.strip())
    return [("", el.text.strip(), []) for el in root.iter()
            if _local(el.tag) == "loc" and el.text]


def title_from_url(url):
    """'.../script/kpop-demon-hunters-2025' -> 'Kpop Demon Hunters (2025)'"""
    slug = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
    slug = re.sub(r"\.\w{2,4}$", "", slug)
    year = re.search(r"-((?:19|20)\d\d)$", slug)
    if year:
        slug = slug[:year.start()]
    small = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or",
             "the", "to", "with"}
    words = [w for w in re.split(r"[-_]+", slug) if w]
    words = [w if w.isupper() else (w.lower() if i and w.lower() in small else w.capitalize())
             for i, w in enumerate(words)]
    return " ".join(words) + (f" ({year.group(1)})" if year else "")


def strip_tracking(url):
    """Drop utm_* tracking parameters so texted links stay short and clean."""
    p = urllib.parse.urlsplit(url)
    if "utm_" not in p.query:
        return url
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if not k.startswith("utm_")]
    return urllib.parse.urlunsplit(p._replace(query=urllib.parse.urlencode(q)))


def service_urls(svc):
    return svc["urls"] if "urls" in svc else [svc["url"]]


def is_paged(svc):
    return any("{page}" in u for u in service_urls(svc))


def page_url(svc, rng):
    """Pick one of the service's urls; for archives ("{page}") also pick a random page."""
    url = rng.choice(service_urls(svc))
    if "{page}" not in url:
        return url
    first = int(svc.get("first_page", 1))
    last = max(first, int(svc.get("max_page", first)))
    return url.replace("{page}", str(rng.randint(first, last)))


def get_candidates(svc, rng=None):
    """Return [{"title", "url"}] of pieces offered by a service (one random archive page)."""
    rng = rng or random.SystemRandom()
    url = page_url(svc, rng)
    data, charset = fetch(url)
    if svc.get("type", "rss") == "rss":
        raw = parse_feed(data)
    elif svc["type"] == "html":
        raw = parse_html_links(data, charset, url)
    elif svc["type"] == "sitemap":
        raw = parse_sitemap(data)
    else:
        raise ValueError(f"Unknown service type {svc['type']!r} for {svc['name']}")

    seen, out = set(), []
    for title, url, cats in raw:
        url = strip_tracking(url)
        if svc.get("title_from_url") or svc.get("type") == "sitemap":
            title = title_from_url(url)
        title = clean_title(title, svc)
        if not title or not passes_filters(svc, title, url, cats):
            continue
        key = normalize_url(url)
        if key in seen:
            continue
        seen.add(key)
        out.append({"title": title, "url": url})
    return out


# --------------------------------------------------------------------------- #
# Selection logic
# --------------------------------------------------------------------------- #

def streak_blocked(history, max_days):
    """A service used for the last `max_days` days straight can't be used today."""
    recent = history[-max_days:]
    if len(recent) == max_days and recent[0] is not None and len(set(recent)) == 1:
        return {recent[0]}
    return set()


def pick_pieces(services, state, rng, max_days):
    used = {normalize_url(p["url"]) for p in state["used_pieces"]}
    picks = []

    for category, svc_list in services.items():
        if category.startswith("_"):
            continue  # allow "_comment" keys in the JSON
        history = state["service_history"].get(category, [])
        blocked = streak_blocked(history, max_days)
        eligible = [s for s in svc_list if s.get("enabled", True) and s["name"] not in blocked]
        if blocked:
            log.info("%s: %s used %d days in a row, skipping it today",
                     category, next(iter(blocked)), max_days)
        rng.shuffle(eligible)

        chosen = None
        for svc in eligible:
            # Archive services get a few random pages; plain feeds get one fetch.
            attempts = int(svc.get("page_attempts", 3)) if is_paged(svc) else 1
            for _ in range(attempts):
                try:
                    candidates = get_candidates(svc, rng)
                except (urllib.error.URLError, ET.ParseError, OSError, ValueError) as e:
                    log.warning("%s / %s: fetch failed (%s)", category, svc["name"], e)
                    continue
                fresh = [c for c in candidates if normalize_url(c["url"]) not in used]
                log.info("%s / %s: %d pieces found, %d not sent before",
                         category, svc["name"], len(candidates), len(fresh))
                if fresh:
                    piece = rng.choice(fresh)
                    used.add(normalize_url(piece["url"]))
                    chosen = {"category": category, "service": svc["name"], **piece}
                    break
            if chosen:
                break
            log.info("%s / %s: nothing new, trying another service", category, svc["name"])

        if chosen is None:
            log.error("%s: no eligible service had a new piece today", category)
            chosen = {"category": category, "service": None, "title": None, "url": None}
        picks.append(chosen)
    return picks


def choose_message(messages, history, today, rng, cooldown_days):
    last_sent = {}
    for h in history:
        d = dt.date.fromisoformat(h["date"])
        if h["message"] not in last_sent or d > last_sent[h["message"]]:
            last_sent[h["message"]] = d

    eligible = [m for m in messages
                if m not in last_sent or (today - last_sent[m]).days >= cooldown_days]
    if eligible:
        return rng.choice(eligible)

    log.warning("Every preset message was used in the last %d days; add more to %s. "
                "Using the least recently sent one.", cooldown_days, MESSAGES_FILE.name)
    return min(messages, key=lambda m: last_sent.get(m, dt.date.min))


def build_text(day, preset, picks):
    lines = [f"Day {day}", preset.replace("{day}", str(day)), ""]
    for p in picks:
        if p["url"]:
            lines.append(f"{p['category']}: {p['title']} ({p['service']})")
            lines.append(p["url"])
        else:
            lines.append(f"{p['category']}: no new piece found today")
        lines.append("")
    return "\n".join(lines).rstrip()


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #

def setting(cfg, key, env_name):
    """Environment variable wins over config.json, so secrets can stay out of files.
    Stray spaces/newlines from pasting a secret are trimmed."""
    value = os.environ.get(env_name) or cfg.get(key)
    return value.strip() if isinstance(value, str) else value


def split_for_sms(text, limit=SMS_CHUNK_LIMIT):
    chunks, current = [], ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = block[:limit]
    if current:
        chunks.append(current)
    return chunks


def send_twilio(cfg, text):
    sid = setting(cfg, "account_sid", "TWILIO_ACCOUNT_SID")
    token = setting(cfg, "auth_token", "TWILIO_AUTH_TOKEN")
    from_number = setting(cfg, "from_number", "TWILIO_FROM_NUMBER")
    to_number = setting(cfg, "to_number", "BRADBURY_TO_NUMBER")
    if not all([sid, token, from_number, to_number]):
        raise RuntimeError("Twilio settings incomplete: need account_sid, auth_token, "
                           "from_number and to_number (config.json or env vars).")

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    for chunk in split_for_sms(text):
        body = urllib.parse.urlencode({"To": to_number, "From": from_number,
                                       "Body": chunk}).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        })
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                log.info("Twilio accepted message: %s", json.load(resp).get("sid"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Twilio error {e.code}: {e.read().decode(errors='replace')}")


def send_email(cfg, text, day):
    host = cfg.get("smtp_host", "smtp.gmail.com")
    port = int(cfg.get("smtp_port", 587))
    user = setting(cfg, "username", "SMTP_USERNAME")
    password = setting(cfg, "password", "SMTP_PASSWORD")
    to_addr = setting(cfg, "to_address", "BRADBURY_TO_ADDRESS")
    if not all([user, password, to_addr]):
        raise RuntimeError("Email settings incomplete: need username, password and "
                           "to_address (config.json or env vars).")

    msg = EmailMessage()
    msg["From"] = cfg.get("from_address") or user
    msg["To"] = to_addr
    msg["Subject"] = f"Bradbury Challenge - Day {day}"
    msg.set_content(text)
    with smtplib.SMTP(host, port, timeout=HTTP_TIMEOUT) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(msg)
    log.info("Email sent")  # address deliberately not logged (Actions logs can be public)


def send_ntfy(cfg, text, day):
    """Push notification via ntfy (https://ntfy.sh): free, no account. Anyone who knows
    the topic name can read it, so treat the topic like a password."""
    topic = setting(cfg, "topic", "NTFY_TOPIC")
    server = (setting(cfg, "server", "NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    if topic and "://" in topic:
        # Accept a full link copied from the app, e.g. https://ntfy.sh/my-topic
        p = urllib.parse.urlsplit(topic)
        server, topic = f"{p.scheme}://{p.netloc}", p.path.strip("/")
    token = setting(cfg, "token", "NTFY_TOKEN")  # only for protected/self-hosted topics
    if not topic:
        raise RuntimeError("No ntfy topic found. On GitHub, add a repository secret named "
                           "exactly NTFY_TOPIC (Settings > Secrets and variables > Actions).")
    if not re.fullmatch(r"[-_A-Za-z0-9]{1,64}", topic):
        raise RuntimeError(f"The ntfy topic is {len(topic)} characters and contains something "
                           "ntfy doesn't allow. Use only letters, numbers, - and _ (max 64), "
                           "with no spaces or quotes.")

    headers = {"Title": f"Bradbury Challenge - Day {day}", "Tags": "books",
               "Content-Type": "text/plain; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{server}/{urllib.parse.quote(topic)}",
                                 data=text.encode("utf-8"), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            log.info("ntfy accepted message: %s", json.load(resp).get("id"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"ntfy error {e.code}: {e.read().decode(errors='replace')}")


def deliver(config, text, day):
    method = os.environ.get("BRADBURY_DELIVERY_METHOD") or config.get("delivery_method", "ntfy")
    if method == "ntfy":
        send_ntfy(config.get("ntfy", {}), text, day)
    elif method == "twilio":
        send_twilio(config.get("twilio", {}), text)
    elif method == "email":
        send_email(config.get("email", {}), text, day)
    else:
        raise RuntimeError(f"Unknown delivery_method {method!r} "
                           "(use 'ntfy', 'twilio' or 'email')")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def check_sources(services):
    ok = True
    for category, svc_list in services.items():
        if category.startswith("_"):
            continue
        print(f"\n== {category}")
        for svc in svc_list:
            try:
                cands = get_candidates(svc)
                sample = cands[0]["title"] if cands else "-"
                print(f"  {svc['name']:<28} {len(cands):>4} pieces   e.g. {sample}")
                ok &= bool(cands)
            except Exception as e:  # report every failure, keep going
                print(f"  {svc['name']:<28} FAILED: {e}")
                ok = False
    return 0 if ok else 1


def local_now(config):
    """Current time in your timezone. GitHub's servers run on UTC, so the timezone
    (e.g. "America/New_York") comes from BRADBURY_TIMEZONE or config.json."""
    tz_name = os.environ.get("BRADBURY_TIMEZONE") or config.get("timezone")
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            return dt.datetime.now(ZoneInfo(tz_name))
        except Exception as e:  # Windows without the tzdata package, or a typo
            log.warning("Timezone %r unavailable (%s); using this computer's clock.", tz_name, e)
    return dt.datetime.now()


class _GitHubAnnotations(logging.Handler):
    """On GitHub Actions, turn warnings/errors into annotations so they show on the
    run's summary page instead of only 'Process completed with exit code 1'."""
    def emit(self, record):
        kind = "error" if record.levelno >= logging.ERROR else "warning"
        msg = record.getMessage().replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::{kind} title=Bradbury Challenge::{msg}", flush=True)


def setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        gh = _GitHubAnnotations(level=logging.WARNING)
        log.addHandler(gh)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Bradbury Challenge daily reading texter")
    ap.add_argument("--dry-run", action="store_true",
                    help="pick pieces and print the text without sending or saving")
    ap.add_argument("--force", action="store_true",
                    help="run even if today's text was already sent")
    ap.add_argument("--check-sources", action="store_true",
                    help="fetch every service and report how many pieces it returns")
    ap.add_argument("--not-before", metavar="HH:MM",
                    help="exit quietly if it's earlier than this local time "
                         "(used by GitHub Actions to handle daylight saving)")
    args = ap.parse_args(argv)

    setup_logging()
    services = load_json(SERVICES_FILE)
    if args.check_sources:
        return check_sources(services)

    messages = load_json(MESSAGES_FILE)
    config = load_json(CONFIG_FILE, default={})
    state = load_state()
    now = local_now(config)
    today = now.date()

    if args.not_before and not args.dry_run:
        hh, mm = (int(x) for x in args.not_before.split(":"))
        if (now.hour, now.minute) < (hh, mm):
            log.info("It's %s local time, before %s; not sending yet.",
                     now.strftime("%H:%M"), args.not_before)
            return 0

    if state["last_run_date"] == today.isoformat() and not (args.force or args.dry_run):
        log.info("Day %d was already sent today; use --force to send another.", state["day"])
        return 0

    rng = random.SystemRandom()
    max_days = int(config.get("max_consecutive_service_days", DEFAULT_MAX_CONSECUTIVE_DAYS))
    cooldown = int(config.get("message_cooldown_days", DEFAULT_MESSAGE_COOLDOWN_DAYS))

    picks = pick_pieces(services, state, rng, max_days)
    if not any(p["url"] for p in picks):
        log.error("Couldn't find any pieces today; nothing sent, state unchanged.")
        return 1

    day = state["day"] + 1
    preset = choose_message(messages, state["message_history"], today, rng, cooldown)
    text = build_text(day, preset, picks)
    print(text)

    if args.dry_run:
        print("\n(dry run: nothing sent, state not saved)")
        return 0

    try:
        deliver(config, text, day)
    except Exception as e:
        log.error("Sending failed, state unchanged so tomorrow's run retries: %s", e)
        return 1

    # Only record the day once the text actually went out.
    for p in picks:
        hist = state["service_history"].setdefault(p["category"], [])
        hist.append(p["service"])
        del hist[:-30]
        if p["url"]:
            state["used_pieces"].append({**p, "day": day, "date": today.isoformat()})
    state["message_history"].append({"message": preset, "date": today.isoformat()})
    state["message_history"] = state["message_history"][-500:]
    state["day"] = day
    state["last_run_date"] = today.isoformat()
    save_json_atomic(STATE_FILE, state)
    log.info("Day %d sent and saved.", day)
    return 0


if __name__ == "__main__":
    sys.exit(main())
