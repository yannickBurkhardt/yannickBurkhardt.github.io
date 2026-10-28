#!/usr/bin/env python3
"""Fills the News section of index.html with my latest public LinkedIn posts.

The posts come from LinkedIn's Member Data Portability API, which is only available to members in
the EEA and Switzerland, using the access token in the LINKEDIN_TOKEN environment variable.
.github/workflows/news.yml runs this daily and commits the result.

LinkedIn has two sources and neither is enough on its own: the post history (snapshot) can lag
behind by weeks, and the activity feed (changelog) only covers the last 28 days. Posts are merged
from both, and the ones on display are kept in assets/news.json so that none get lost in between.
Only public posts are ever written to the repository, and no post content is logged. The history
may not tell public from connections-only posts, so posts written since the activity feed exists
are only shown once the feed has reported them as public.

With --dump FILE, the raw post data (including non-public posts) is also saved to FILE, to check
LinkedIn's formats locally.
"""

import html
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "index.html"
STATE = ROOT / "assets" / "news.json"
IMAGES = ROOT / "assets" / "images" / "news"

API = "https://api.linkedin.com"
NUM_POSTS = 9
MAX_TEXT = 600
IMAGE_WIDTH = 800
# The history marks public posts as MEMBER_NETWORK, and maybe connections-only ones as well
PUBLIC = {"PUBLIC", "ANYONE", "MEMBER_NETWORK"}
# Since then (when the first token was created), the activity feed reports each new post's visibility
FEED_SINCE = "2026-10-27"
UGC_CONTENT = "com.linkedin.ugc.ShareContent"
UGC_VISIBILITY = "com.linkedin.ugc.MemberNetworkVisibility"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

START, END = "<!-- NEWS:START -->", "<!-- NEWS:END -->"
CARD = """\
          <article class="news-card">
            <div class="news-card-meta">
              <img src="./assets/icons/linkedin.svg" class="news-card-icon" alt="">
              <span class="news-card-meta-text">{kind}</span>
              <time class="news-card-meta-text" datetime="{day}">{month}</time>
            </div>
{image}            <div class="body-text news-card-text">{text}</div>
            <a class="button" target="_blank" href="{link}">
              <span class="button-text">Read on LinkedIn</span>
              <img src="./assets/icons/arrow-right.svg" class="right-arrow-icon" alt="">
            </a>
          </article>
"""
IMAGE = '            <img src="./{path}" class="news-image" alt="" loading="lazy">\n'
# Takes the place of the image on reposts, whose content is only on LinkedIn
ORIGINAL = """\
            <div class="news-placeholder">
              <a class="button" target="_blank" href="{link}">
                <span class="button-text">See original post</span>
                <img src="./assets/icons/arrow-right.svg" class="right-arrow-icon" alt="">
              </a>
            </div>
"""

HINTS = {
    401: "LinkedIn rejected the token, so it has probably expired. Create a new one and update the LINKEDIN_TOKEN secret.",
    403: "LinkedIn denied access. Check that the app still has the Member Data Portability API (Member) product.",
}


class LinkedInError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def get(url, token):
    """One API response. The token only goes to the LinkedIn API, and never along a redirect."""
    if not url.startswith(API + "/rest/"):
        raise LinkedInError(0, f"Refusing to send the token to {url}")
    request = urllib.request.Request(url, headers={"Linkedin-Version": "202312"})
    request.add_unredirected_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            message = str(json.load(error).get("message", ""))
        except (ValueError, AttributeError):
            message = ""
        raise LinkedInError(error.code, HINTS.get(error.code) or f"LinkedIn API error {error.code}: {message[:200]}") from None


def snapshot_rows(token, domain):
    """All rows of a snapshot domain. A 404 means the domain is empty or not prepared yet."""
    url, rows = f"{API}/rest/memberSnapshotData?q=criteria&domain={domain}", []
    for _ in range(50):
        try:
            page = get(url, token)
        except LinkedInError as error:
            if error.status == 404:
                break
            raise
        rows += [row for element in page.get("elements", []) for row in element.get("snapshotData", [])]
        href = next((link.get("href") for link in dig(page, "paging", "links") or [] if link.get("rel") == "next"), None)
        if not href:
            break
        url = urllib.parse.urljoin(API, href)
    return rows


def post_events(token):
    """Post events of the last 28 days, oldest first. Other activity (likes, messages, ...) is dropped right away."""
    events, seen, start_time = [], set(), None
    for _ in range(40):
        url = f"{API}/rest/memberChangeLogs?q=memberAndApplication&count=50"
        if start_time:
            url += f"&startTime={start_time}"
        page = [event for event in get(url, token).get("elements", []) if event.get("id") not in seen]
        if not page:
            break
        seen.update(event.get("id") for event in page)
        events += [event for event in page if event.get("resourceName") == "ugcPosts" and event.get("activityStatus") != "FAILURE"]
        start_time = max(event.get("processedAt", 0) for event in page)
    return events


def uploads(rows):
    """(kind, time in ms, link) of my uploads, from RICH_MEDIA rows like {"Date/Time": "You uploaded
    a feed photo on October 20, 2025 at 6:06 AM (GMT)", "Media Link": "https://..."}. Profile photos are skipped."""
    found = []
    for row in rows:
        pattern = r"uploaded an? (.+?) on (\w{3})\w* (\d+), (\d{4}) at (\d+):(\d+) ([AP]M)"
        match = re.search(pattern, str(row.get("Date/Time", "")))
        if match and match.group(1) != "profile photo" and match.group(2) in MONTHS:
            kind, month, day, year, hour, minute, half = match.groups()
            hour = int(hour) % 12 + (12 if half == "PM" else 0)
            when = datetime(int(year), MONTHS.index(month) + 1, int(day), hour, int(minute), tzinfo=timezone.utc)
            found.append((kind, when.timestamp() * 1000, str(row.get("Media Link", ""))))
    return found


def uploads_for(post, uploaded):
    """(time gap in ms, kind, link) of the uploads made within 15 minutes of a post."""
    ms = created(post["urn"])
    return [(abs(ms - when), kind, link) for kind, when, link in uploaded if ms and abs(ms - when) < 15 * 60 * 1000]


def dig(value, *keys):
    """value[key1][key2]..., or None where something is missing."""
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def as_urn(value):
    """A post URN ("urn:li:share:123"), also from a feed link or in percent-encoded form, else None."""
    value = urllib.parse.unquote(value) if isinstance(value, str) else ""
    match = re.search(r"urn:li:(?:share|ugcPost|activity):\d+", value)
    return match.group() if match else None


def created(urn):
    """When a post was created. LinkedIn ids start with the creation time in milliseconds (the top 41 bits)."""
    ms = int(urn.rsplit(":", 1)[1]) >> 22
    return ms if 1e12 < ms < (time.time() + 86400) * 1000 else None


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_row(row):
    """A post from a MEMBER_SHARE_INFO row, which has export-style keys like "ShareLink" and "ShareCommentary"."""
    values = {re.sub(r"[^a-z]", "", key.lower()): value.strip() for key, value in row.items() if isinstance(value, str)}
    urn = as_urn(values.get("sharelink"))
    ms = urn and created(urn)
    date = iso(ms) if ms else values.get("date", "").replace(" ", "T") + "Z"  # "YYYY-MM-DD HH:MM:SS" in UTC
    if not urn or not re.match(r"\d{4}-\d\d-\d\dT", date):
        return None
    post = {"urn": urn, "date": date, "link": values["sharelink"], "public": values.get("visibility", "").upper() in PUBLIC}
    # Fields LinkedIn left empty are omitted, so that they don't overwrite what is already known about the post
    if values.get("sharecommentary"):
        post["text"] = values["sharecommentary"].replace('"\n"', "\n")  # line breaks come quoted, CSV-style
    if values.get("mediaurl", "").startswith("https://"):
        post["media"] = values["mediaurl"]
    # Resharing a LinkedIn post (with a comment) should link to the original post, but doesn't always
    if re.search(r"linkedin\.com/(?:feed/update|posts)/", values.get("sharedurl", "")):
        post["repost"] = True
        post["original"] = values["sharedurl"]
    return post


def from_event(event):
    """A post from a ugcPosts event, shaped as in LinkedIn's UGC API docs (not yet seen live)."""
    activity = event.get("activity") or {}
    activity = dig(activity, "patch", "$set") or activity
    urn = as_urn(event.get("resourceId")) or as_urn(activity.get("id"))
    if not urn or not created(urn):
        return None
    post = {"urn": urn, "date": iso(created(urn))}
    if event.get("method") == "DELETE":
        return {**post, "deleted": True}
    text = dig(activity, "specificContent", UGC_CONTENT, "shareCommentary", "text")
    visibility = dig(activity, "visibility", UGC_VISIBILITY)
    if isinstance(text, str) and text.strip():
        post["text"] = text
    if isinstance(visibility, str):
        post["public"] = visibility.upper() in PUBLIC
        post["confirmed"] = True
    parent = dig(activity, "responseContext", "parent")
    if parent:
        post["repost"] = True
        if as_urn(parent):
            post["original"] = f"https://www.linkedin.com/feed/update/{as_urn(parent)}/"
    return post


def same_post(a, b):
    """The history and the activity feed may name one post by different URNs (share vs. ugcPost),
    but those are created within milliseconds of each other."""
    if a["urn"] == b["urn"]:
        return True
    a_ms, b_ms = created(a["urn"]), created(b["urn"])
    return bool(a_ms and b_ms) and abs(a_ms - b_ms) < 5000


def image_file(post):
    return IMAGES / (post["urn"].rsplit(":", 1)[1] + ".webp")


def latest(recent, history, known, deleted):
    """The newest public posts, without deleted ones. For each post the freshest source wins and older ones fill
    in its gaps, except that a post the history marks as not public is never shown, whatever the activity feed says.
    Posts written since FEED_SINCE also need the feed's confirmation that they are public."""
    posts = []
    for source in (recent, history, known):
        for record in source:
            if record.get("deleted"):
                continue
            match = next((post for post in posts if same_post(post, record)), None)
            if not match:
                posts.append(dict(record))
                continue
            for key, value in record.items():
                match.setdefault(key, value)
            if source is history and not record["public"]:
                match["public"] = False
            if source is known:
                match["urn"] = record["urn"]  # keeps the name of its image file
    posts = [
        post for post in posts
        if post.get("public")
        and (post.get("confirmed") or post["date"] < FEED_SINCE)
        and (post.get("text") or post.get("media") or image_file(post).exists())
        and not any(same_post(post, gone) for gone in deleted)
    ]
    return sorted(posts, key=lambda post: post["date"], reverse=True)[:NUM_POSTS]


def save_image(url, path):
    """Saves a post image as WebP, at most IMAGE_WIDTH wide. One that fails to load is skipped."""
    # LinkedIn's image server refuses Python's default User-Agent
    request = urllib.request.Request(url, headers={"User-Agent": "yannickburkhardt.github.io news updater"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            image = Image.open(io.BytesIO(response.read()))
            image.thumbnail((IMAGE_WIDTH, 4 * IMAGE_WIDTH))
            path.parent.mkdir(parents=True, exist_ok=True)
            image.convert("RGBA" if image.mode in ("RGBA", "LA", "P") else "RGB").save(path, "WEBP", quality=80)
    except (OSError, ValueError) as error:
        print(f"Skipped an image: {error}")


def excerpt(text):
    """Card text: LinkedIn's mention and hashtag markup resolved, blank lines dropped, long posts cut short."""
    text = re.sub(r"@\[([^\]]*)\]\(urn:li:[^)]*\)", r"\1", text)
    text = re.sub(r"\{hashtag\|\\?#\|([^}]*)\}", r"#\1", text)
    text = "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT].rsplit(None, 1)[0] + " …"
    return html.escape(text).replace("\n", "<br>")


def card(post):
    day = post["date"][:10]
    image = image_file(post)
    link = post.get("link") or f"https://www.linkedin.com/feed/update/{post['urn']}/"
    if post.get("repost"):
        # Without a link to the original, my repost shows it on LinkedIn below my comment
        image = ORIGINAL.format(link=html.escape(post.get("original") or link))
    elif image.exists():
        image = IMAGE.format(path=image.relative_to(ROOT).as_posix())
    else:
        image = ""
    return CARD.format(
        kind="Reposted" if post.get("repost") else "Posted",
        day=day,
        month=f"{MONTHS[int(day[5:7]) - 1]} {day[:4]}",
        image=image,
        text=excerpt(post.get("text", "")),
        link=html.escape(link),
    )


def main():
    token = os.environ.get("LINKEDIN_TOKEN", "").strip()
    if not token:
        sys.exit("Set LINKEDIN_TOKEN to a LinkedIn access token.")
    try:
        events = post_events(token)
        rows = snapshot_rows(token, "MEMBER_SHARE_INFO")
        media = snapshot_rows(token, "RICH_MEDIA")
    except (LinkedInError, OSError, ValueError) as error:
        sys.exit(str(error))
    if sys.argv[1:2] == ["--dump"]:
        dump = {"history": rows, "events": events, "media": media}
        Path(sys.argv[2]).write_text(json.dumps(dump, indent=2, ensure_ascii=False), encoding="utf-8")

    recent = [post for post in map(from_event, reversed(events)) if post]  # newest event first
    history = [post for post in map(from_row, rows) if post]
    # The history has no image links, but my uploads are listed separately with their time
    uploaded = uploads(media)
    for post in recent + history:
        photos = [(gap, link) for gap, kind, link in uploads_for(post, uploaded) if kind == "feed photo" and link.startswith("https://")]
        if photos and not post.get("media"):
            post["media"] = min(photos)[1]
    # Nor does it mark reposts with a comment. It does give them ugcPost ids, though, while normal posts
    # get share ids, unless they carry media like a video or document, which is listed among the uploads.
    for post in history:
        if post["urn"].startswith("urn:li:ugcPost:") and not uploads_for(post, uploaded):
            post["repost"] = True
    if rows and not any(post["public"] for post in history):
        sys.exit("LinkedIn sent posts, but none that this script recognizes as public, so LinkedIn has probably "
                 "changed its data format. Nothing was changed; check the raw data with --dump.")
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    known = [{**post, "public": True, "confirmed": True} for post in state.get("posts", [])]
    # LinkedIn only reports deletions for 28 days, but its post history may still list a deleted post after that
    deleted = [post for post in recent if post.get("deleted")] + [{"urn": urn} for urn in state.get("deleted", [])]
    posts = latest(recent, history, known, deleted)
    print(f"{len(history)} posts in the history, {len(recent)} post events in the last 28 days; showing {len(posts)} public posts.")

    for post in posts:
        if not image_file(post).exists() and post.get("media"):
            save_image(post["media"], image_file(post))
    page = INDEX.read_text(encoding="utf-8")
    start, end = page.index(START) + len(START), page.index(END)
    INDEX.write_text(page[:start] + "\n" + "".join(map(card, posts)) + " " * 10 + page[end:], encoding="utf-8")
    if posts or STATE.exists():
        keep = ("urn", "date", "link", "text", "repost", "original")
        state = {
            "posts": [{key: post[key] for key in keep if post.get(key)} for post in posts],
            "deleted": sorted({post["urn"] for post in deleted}),
        }
        STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    shown = {image_file(post) for post in posts}
    for image in IMAGES.glob("*") if IMAGES.exists() else []:
        if image not in shown:
            image.unlink()


if __name__ == "__main__":
    main()
