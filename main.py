#!/usr/bin/env python3
"""
Daily Social Media News Digest
--------------------------------
1. Pulls recent articles from configured RSS feeds
2. Filters for items relevant to social media / platform news
3. Fetches the FULL article text from the source link
4. Writes an original executive summary (Anthropic API) — not the RSS
   snippet, and not quoted text — with a "read more at source" link
5. Keeps a rolling 5-day history so the page shows today + the last 4 days
6. Posts today's highlights to LinkedIn (only if credentials are configured)

Run manually:  python main.py
Scheduled by:  .github/workflows/daily-digest.yml
"""

import os
import re
import json
import math
import time
import random
import datetime
import feedparser
import requests
import trafilatura

# ---------------------------------------------------------------------------
# CONFIG — edit this section to add/remove sources or tune behavior
# ---------------------------------------------------------------------------

RSS_FEEDS = [
    "https://www.marketingdive.com/feeds/news/",
    "https://www.socialmediatoday.com/feeds/news/",
    # Add more feeds here, e.g.:
    # "https://www.adweek.com/feed/",
]

# Only keep articles whose title/summary mentions one of these (case-insensitive).
SOCIAL_KEYWORDS = [
    "social media", "instagram", "tiktok", "linkedin", "facebook", "meta",
    "twitter", " x ", "youtube", "snapchat", "pinterest", "reddit", "threads",
    "influencer", "algorithm", "creator economy", "platform update",
    # Broader campaign/case-study language that MarketingDive's own "Social Media"
    # topic page uses, even when a specific platform isn't named in the title:
    "social-first", "social series", "social content", "social campaign",
    "social strategy", "user-generated content", " ugc ", "viral", "creator",
    "brand partnership", "sponsored content",
]

LOOKBACK_HOURS = 26  # slight buffer past 24h so a daily run never misses items
MAX_ARTICLES = 6     # cap on how many stories go into one day's digest
HISTORY_DAYS = 5     # how many days of digests to keep on the page

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = "claude-sonnet-5"

LINKEDIN_ACCESS_TOKEN = os.environ.get("LINKEDIN_ACCESS_TOKEN")
LINKEDIN_AUTHOR_URN = os.environ.get("LINKEDIN_AUTHOR_URN")

FACEBOOK_PAGE_ID = os.environ.get("FACEBOOK_PAGE_ID")
FACEBOOK_PAGE_TOKEN = os.environ.get("FACEBOOK_PAGE_TOKEN")

# Morning nudge: emails you the finished WhatsApp Channel post (text + banner)
# so you can paste it into the Channel. WhatsApp has no official posting API.
NUDGE_EMAIL_TO = os.environ.get("NUDGE_EMAIL_TO")
SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = os.environ.get("SMTP_PORT") or "465"
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")

DIGEST_PAGE_TITLE = "Social Media News Digest"
PUBLISHED_PAGE_URL = os.environ.get("PUBLISHED_PAGE_URL", "")

OUTPUT_DIR = "docs"
HISTORY_FILE = os.path.join(OUTPUT_DIR, "history.json")


# ---------------------------------------------------------------------------
# SOUTH AFRICA TIME + DECEMBER 2026 HOLIDAY SETTINGS
# ---------------------------------------------------------------------------
# The GitHub runner works in UTC, so every "what day is it?" question goes through
# _sa_today(). South Africa has no daylight saving, so a fixed UTC+2 is always right.
SA_TZ = datetime.timezone(datetime.timedelta(hours=2))


def _sa_today():
    # SIMULATE_DATE (YYYY-MM-DD) is only for testing banners/holiday logic early.
    fake = os.environ.get("SIMULATE_DATE")
    if fake:
        return datetime.date.fromisoformat(fake)
    return datetime.datetime.now(SA_TZ).date()


HOLIDAY_NOTICE_DAY = datetime.date(2026, 12, 18)    # the sign-off post
HOLIDAY_BREAK_START = datetime.date(2026, 12, 19)   # nothing is published from here...
HOLIDAY_BREAK_END = datetime.date(2027, 1, 10)      # ...until the 11th of January 2027
HOLIDAY_NOTICE = (
    "Happy holiday season, from all of us at SendCutPost.\n\n"
    "We're away from 21 December and back in the office on 11 January 2027. "
    "The digest returns then. Rest well."
)

# Holiday banner theme by date (inclusive). Weekend dates are included so a manual
# weekend run still gets the right banner. After 20 December the break begins.
HOLIDAY_THEMES = [
    (datetime.date(2026, 12, 1), datetime.date(2026, 12, 6), "sunrise"),   # week 1
    (datetime.date(2026, 12, 7), datetime.date(2026, 12, 13), "rest"),     # week 2
    (datetime.date(2026, 12, 14), datetime.date(2026, 12, 20), "family"),  # week 3
]


def _holiday_theme(day):
    for start, end, theme in HOLIDAY_THEMES:
        if start <= day <= end:
            return theme
    return None


def _on_holiday_break(day):
    return HOLIDAY_BREAK_START <= day <= HOLIDAY_BREAK_END


def _post_notice(day):
    """The special sign-off message goes into the posts on the 18th only."""
    return HOLIDAY_NOTICE if day == HOLIDAY_NOTICE_DAY else None


def _site_notice(day):
    """The web page carries the message from the 18th until the team is back."""
    if HOLIDAY_NOTICE_DAY <= day <= HOLIDAY_BREAK_END:
        return HOLIDAY_NOTICE
    return None


# ---------------------------------------------------------------------------
# STEP 1: Fetch + filter articles from RSS
# ---------------------------------------------------------------------------

def fetch_recent_articles():
    # On Mondays, reach back far enough to cover the weekend gap (script doesn't
    # run Sat/Sun), so Friday/weekend news isn't silently missed. Other weekdays
    # use the normal short lookback.
    is_monday = _sa_today().weekday() == 0  # Monday == 0
    lookback_hours = 78 if is_monday else LOOKBACK_HOURS  # ~3.25 days covers Fri-Sun

    cutoff = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(hours=lookback_hours)
    articles = []

    for feed_url in RSS_FEEDS:
        parsed = feedparser.parse(feed_url)
        for entry in parsed.entries:
            published = _get_published_dt(entry)
            if published and published < cutoff:
                continue

            title = entry.get("title", "").strip()
            summary_raw = entry.get("summary", "") or entry.get("description", "")
            # Leading/trailing space added so the " x " keyword (X/Twitter) still
            # matches when "X" is the very first or last word of the title.
            text_blob = f" {title} {summary_raw} ".lower()

            if not any(kw in text_blob for kw in SOCIAL_KEYWORDS):
                continue

            articles.append({
                "title": title,
                "link": entry.get("link", ""),
                "raw_summary": _clean_html(summary_raw),
                "source": parsed.feed.get("title", feed_url),
                "published": published.isoformat() if published else "",
            })

    return articles[:MAX_ARTICLES]


def _get_published_dt(entry):
    for field in ("published_parsed", "updated_parsed"):
        val = entry.get(field)
        if val:
            return datetime.datetime(*val[:6])
    return None


def _clean_html(raw):
    text = re.sub(r"<[^>]+>", "", raw or "")
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# STEP 2: Fetch full article text from the source URL
# ---------------------------------------------------------------------------

def fetch_full_text(url):
    """Download and extract the main article text. Falls back to '' on failure."""
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return ""
        extracted = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
        return extracted or ""
    except Exception as e:
        print(f"Full-text fetch failed for {url}: {e}")
        return ""


# ---------------------------------------------------------------------------
# STEP 3: Write an original executive summary (not quoted, not RSS snippet)
# ---------------------------------------------------------------------------

def summarize_article(article):
    full_text = fetch_full_text(article["link"])
    source_text = full_text if full_text else article["raw_summary"]

    if not ANTHROPIC_API_KEY:
        # Fallback with no API key: trim what we have (not ideal, but functional)
        trimmed = source_text[:220].rsplit(" ", 1)[0] + "…"
        return {"long": trimmed, "short": trimmed[:120]}

    # Cap input length to keep prompts small/cheap
    source_text = source_text[:6000]

    prompt = (
        "You are writing content for a daily social-media-news digest, read by busy "
        "marketing professionals. Read the article text below and respond in EXACTLY "
        "this plain-text format, with no other text before or after:\n\n"
        "SHORT: <one sentence here>\n"
        "LONG: <full summary here>\n\n"
        "For SHORT: a single, information-dense sentence (roughly 25-35 words) "
        "covering what happened, the platform/company involved, and the concrete "
        "detail that matters most. This will stand ALONE next to the headline in a "
        "LinkedIn post, so it must convey real substance by itself — not a teaser "
        "like 'here's what changed', but the actual news.\n\n"
        "For LONG: a ONE-MINUTE READ, about 150-180 words, written as a single "
        "paragraph (no line breaks within it). Dense enough that the reader "
        "genuinely understands what happened without opening the article.\n\n"
        "Both: written ENTIRELY IN YOUR OWN WORDS (no copying or close paraphrasing "
        "of source sentences, no direct quotes from the article), with concrete "
        "specifics (numbers, dates, features, names) rather than vague description. "
        "No throat-clearing openers, no restating the headline, no filler adjectives, "
        "no editorializing, no call-to-action. Quotation marks, apostrophes, and any "
        "punctuation are fine to use naturally within your sentences.\n\n"
        f"Title: {article['title']}\n\n"
        f"Article text:\n{source_text}"
    )

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": 700,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=45,
    )
    if response.status_code >= 400:
        print(f"Anthropic API error {response.status_code}: {response.text}")
    response.raise_for_status()
    data = response.json()
    raw_text = "".join(block.get("text", "") for block in data.get("content", [])).strip()

    # Plain-text SHORT:/LONG: format — no JSON, so quotes/apostrophes in the
    # generated text can never break parsing the way escaped JSON strings can.
    short_match = re.search(r'SHORT:\s*(.+?)(?=\n\s*LONG:|\Z)', raw_text, re.DOTALL)
    long_match = re.search(r'LONG:\s*(.+)', raw_text, re.DOTALL)
    short_val = short_match.group(1).strip() if short_match else ""
    long_val = long_match.group(1).strip() if long_match else ""

    if not short_val and not long_val:
        print(f"Summary parsing found neither field for '{article['title']}', using fallback")
        clean_fallback = source_text[:220].rsplit(" ", 1)[0] + "…"
        short_val = clean_fallback[:150]
        long_val = clean_fallback

    return {"long": long_val or short_val, "short": short_val or long_val[:150]}


# ---------------------------------------------------------------------------
# STEP 4: Rolling history (today + previous days, up to HISTORY_DAYS)
# ---------------------------------------------------------------------------

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def update_history(today_articles):
    today_str = _sa_today().isoformat()
    history = load_history()

    # Drop any existing entry for today (handles re-runs on the same day)
    history = [day for day in history if day["date"] != today_str]

    history.insert(0, {"date": today_str, "articles": today_articles})
    history = history[:HISTORY_DAYS]

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)

    return history


def _already_posted(day):
    """True if an earlier run today already published (used by the backup run)."""
    for entry in load_history():
        if entry.get("date") == day.isoformat() and entry.get("posted"):
            return True
    return False


def mark_posted(day):
    history = load_history()
    for entry in history:
        if entry.get("date") == day.isoformat():
            entry["posted"] = True
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


# ---------------------------------------------------------------------------
# STEP 5: Build HTML digest page (today + rolling history)
# ---------------------------------------------------------------------------

def build_html(history, notice=None):
    notice_html = ""
    if notice:
        notice_html = '<div class="notice">' + notice.replace("\n\n", "<br><br>") + '</div>'
    days_html = ""
    for day in history:
        date_label = datetime.date.fromisoformat(day["date"]).strftime("%A, %B %d, %Y")
        items_html = ""
        for a in day["articles"]:
            items_html += f"""
        <div class="item">
          <h3>{a['title']}</h3>
          <p>{a['summary']}</p>
          <a class="src" href="{a['link']}" target="_blank" rel="noopener">Read more at {a['source']} →</a>
        </div>"""
        if not day["articles"]:
            items_html = '<p class="none">No qualifying social media news that day.</p>'

        days_html += f"""
      <section class="day">
        <h2>{date_label}</h2>
        {items_html}
      </section>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{DIGEST_PAGE_TITLE}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Baloo+Thambi+2:wght@600&display=swap" rel="stylesheet">
<style>
  h1, .day h2, .item h3 {{ font-family: 'Baloo Thambi 2', -apple-system, Helvetica, Arial, sans-serif; font-weight: 600; }}
  body {{ font-family: -apple-system, Helvetica, Arial, sans-serif; max-width: 680px;
         margin: 40px auto; padding: 0 16px; color: #1a1a1a; line-height: 1.5; }}
  h1 {{ font-size: 1.5rem; margin-bottom: 4px; }}
  .subtitle {{ color: #666; margin-bottom: 32px; font-size: 0.9rem; }}
  .day {{ margin-bottom: 36px; }}
  .day h2 {{ font-size: 1.05rem; color: #333; border-bottom: 2px solid #0a66c2;
             padding-bottom: 6px; margin-bottom: 4px; }}
  .item {{ padding: 14px 0; border-bottom: 1px solid #eee; }}
  .item h3 {{ margin: 0 0 6px 0; font-size: 1.02rem; }}
  .item p {{ margin: 0 0 6px 0; }}
  .src {{ font-size: 0.82rem; color: #0a66c2; text-decoration: none; }}
  .src:hover {{ text-decoration: underline; }}
  .none {{ color: #999; font-style: italic; }}
  .notice {{ background: #fdf3e3; border-left: 4px solid #d6603e; padding: 14px 16px;
            margin-bottom: 28px; border-radius: 4px; }}
</style>
</head>
<body>
  <h1>{DIGEST_PAGE_TITLE}</h1>
  <div class="subtitle">Last {len(history)} day{'s' if len(history) != 1 else ''} of social media news, summarized</div>
  {notice_html}
  {days_html}
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# STEP 6: Post today's highlights to LinkedIn
# ---------------------------------------------------------------------------

def _to_bold_unicode(text):
    """Convert ASCII letters/digits to Unicode 'Mathematical Bold' lookalikes.
    LinkedIn's post API has no real bold formatting — this fakes it by using
    different (but visually bold) Unicode characters, which render as bold
    everywhere since they're just plain text under the hood."""
    bold_map = {}
    for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        bold_map[c] = chr(0x1D400 + i)
    for i, c in enumerate("abcdefghijklmnopqrstuvwxyz"):
        bold_map[c] = chr(0x1D41A + i)
    for i, c in enumerate("0123456789"):
        bold_map[c] = chr(0x1D7CE + i)
    return "".join(bold_map.get(ch, ch) for ch in text)


# Banner image: the digest title in Baloo Thambi 2 SemiBold, drawn as a picture,
# because LinkedIn/Facebook posts are plain text and can't use custom fonts or sizes.
BANNER_PATH = "/tmp/digest_banner.png"
BANNER_FONT_PATH = "/tmp/BalooThambi2.ttf"
BANNER_FONT_URL = ("https://raw.githubusercontent.com/google/fonts/main/ofl/"
                   "baloothambi2/BalooThambi2%5Bwght%5D.ttf")


def _banner_font(size, weight=600):
    from PIL import ImageFont
    if not os.path.exists(BANNER_FONT_PATH):
        r = requests.get(BANNER_FONT_URL, timeout=30)
        r.raise_for_status()
        with open(BANNER_FONT_PATH, "wb") as f:
            f.write(r.content)
    font = ImageFont.truetype(BANNER_FONT_PATH, size)
    font.set_variation_by_axes([weight])  # 600 = SemiBold
    return font


# ---------------------------------------------------------------------------
# HOLIDAY BANNERS (December 2026): beach / rest / family, same masthead title
# ---------------------------------------------------------------------------
_HS = 2  # drawn at 2x then shrunk, for smooth edges
_HW, _HH = 1200 * _HS, 627 * _HS


def _hfont(sz, w=600):
    return _banner_font(int(sz * _HS), w)


def _hP(pts):
    return [(x * _HS, y * _HS) for x, y in pts]


def _hlerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _hvgrad(d, y0, y1, c0, c1):
    for y in range(int(y0 * _HS), int(y1 * _HS)):
        t = (y - y0 * _HS) / max(1, (y1 - y0) * _HS - 1)
        d.line([(0, y), (_HW, y)], fill=_hlerp(c0, c1, t))


def _hheader(d, date, tagline, ink, accent):
    f = _hfont(118)
    d.text((_HW / 2, 118 * _HS), "Social Media", font=f, fill=ink, anchor="mm")
    d.text((_HW / 2, 228 * _HS), "News Digest", font=f, fill=ink, anchor="mm")
    d.text((_HW / 2, 312 * _HS), date, font=_hfont(34), fill=accent, anchor="mm")
    d.text((_HW / 2, 356 * _HS), tagline, font=_hfont(30, 500), fill=ink, anchor="mm")


def _hwaves(d, y0, y1, color, seed, n=60):
    rnd = random.Random(seed)
    for _ in range(n):
        x = rnd.randint(0, 1200)
        y = rnd.randint(int(y0) + 6, int(y1) - 4)
        l = rnd.randint(18, 48)
        d.line(_hP([(x, y), (x + l, y)]), fill=color, width=3 * _HS // 2 + 1)


def _hfoam(d, y, color):
    pts = [(x, y + 3 * math.sin(x / 22)) for x in range(0, 1201, 6)]
    d.line(_hP(pts), fill=color, width=5 * _HS)


def _hleaf(d, base, ang, length, droop, color):
    """A palm _hleaf: slender curved blade."""
    bx, by = base
    top, bot = [], []
    steps = 24
    for i in range(steps + 1):
        t = i / steps
        cx = bx + math.cos(math.radians(ang)) * length * t
        cy = by + math.sin(math.radians(ang)) * length * t + droop * t * t
        wdt = math.sin(math.pi * min(1, t * 1.05)) * 15
        nx, ny = -math.sin(math.radians(ang)), math.cos(math.radians(ang))
        top.append((cx + nx * wdt, cy + ny * wdt))
        bot.append((cx - nx * wdt, cy - ny * wdt))
    d.polygon(_hP(top + bot[::-1]), fill=color)


def _hperson(d, x, base, h, color, dress=False):
    r = h * 0.085
    head_y = base - h + r
    d.ellipse(_hP([(x - r, head_y - r), (x + r, head_y + r)]), fill=color)
    sh_y = base - h * 0.80
    hip_y = base - h * 0.46
    sw, hw = h * 0.12, h * 0.085
    d.polygon(_hP([(x - sw, sh_y), (x + sw, sh_y), (x + hw, hip_y), (x - hw, hip_y)]), fill=color)
    if dress:
        kw = h * 0.19
        d.polygon(_hP([(x - hw, hip_y - 4), (x + hw, hip_y - 4), (x + kw, base - h * 0.24), (x - kw, base - h * 0.24)]), fill=color)
        leg_top = base - h * 0.26
    else:
        leg_top = hip_y - 2
    lw = h * 0.055
    for dx in (-h * 0.045, h * 0.045):
        d.rectangle(_hP([(x + dx - lw / 2, leg_top), (x + dx + lw / 2, base)]), fill=color)
    return (x - sw, sh_y + 4), (x + sw, sh_y + 4)


def _harm(d, a, b, color, w):
    d.line(_hP([a, b]), fill=color, width=int(w * _HS))




# ---------------------------------------------------------------- week 1
def _scene_sunrise(d, date_str):
    ink, accent = (24, 45, 74), (214, 96, 62)
    _hvgrad(d, 0, 410, (255, 222, 192), (255, 243, 214))
    # sun + rays
    cx, cy, r = 1010, 410, 86
    d.ellipse(_hP([(cx - r, cy - r), (cx + r, cy + r)]), fill=(244, 140, 100))
    for a in range(200, 341, 14):
        ca, sa = math.cos(math.radians(a)), math.sin(math.radians(a))
        d.line(_hP([(cx + ca * 108, cy + sa * 108), (cx + ca * 138, cy + sa * 138)]), fill=(250, 176, 128), width=5 * _HS)
    # birds
    for bx, by in ((150, 150), (192, 124), (228, 160)):
        d.line(_hP([(bx - 14, by - 4), (bx, by + 4), (bx + 14, by - 4)]), fill=ink, width=3 * _HS)
    # sea
    _hvgrad(d, 410, 505, (112, 192, 186), (62, 148, 160))
    for i in range(6):
        y = 418 + i * 14
        hw = 74 - i * 10
        d.rounded_rectangle(_hP([(cx - hw, y), (cx + hw, y + 5)]), radius=2 * _HS, fill=(250, 190, 140))
    _hwaves(d, 410, 505, (160, 216, 208), 11)
    # sailboat
    d.polygon(_hP([(298, 463), (362, 463), (350, 476), (310, 476)]), fill=(255, 250, 240))
    d.line(_hP([(330, 463), (330, 412)]), fill=(90, 70, 60), width=3 * _HS)
    d.polygon(_hP([(333, 458), (333, 416), (366, 458)]), fill=(255, 252, 245))
    d.polygon(_hP([(327, 456), (327, 428), (303, 456)]), fill=(250, 226, 196))
    # sand
    _hvgrad(d, 505, 627, (244, 222, 180), (231, 198, 148))
    _hfoam(d, 506, (255, 255, 255))
    _hheader(d, date_str, "Slow mornings. Salt air.", ink, accent)


# ---------------------------------------------------------------- week 2
def _scene_rest(d, date_str):
    ink, accent = (22, 64, 76), (202, 92, 84)
    _hvgrad(d, 0, 410, (205, 222, 240), (255, 224, 202))
    cx, cy, r = 250, 410, 62
    d.ellipse(_hP([(cx - r, cy - r), (cx + r, cy + r)]), fill=(255, 205, 170))
    # palm leaves from the top-left corner
    greens = [(34, 98, 92), (48, 120, 108), (30, 86, 84), (56, 132, 112), (40, 108, 98)]
    for (ang, ln, dr), col in zip([(12, 190, 26), (30, 210, 34), (48, 240, 40), (66, 270, 44), (84, 290, 40)], greens):
        _hleaf(d, (-10, -10), ang, ln, dr, col)
    _hvgrad(d, 410, 505, (118, 196, 200), (78, 158, 170))
    _hwaves(d, 410, 505, (170, 222, 220), 22)
    _hvgrad(d, 505, 627, (240, 218, 176), (226, 196, 150))
    _hfoam(d, 506, (255, 255, 255))
    # umbrella + lounger on the right (pole stands clear of the lounger)
    d.line(_hP([(972, 606), (952, 434)]), fill=(120, 86, 60), width=6 * _HS)
    cols = [(236, 112, 99), (252, 244, 230)]
    for i in range(6):
        d.pieslice(_hP([(952 - 112, 434 - 112), (952 + 112, 434 + 112)]), 180 + i * 30, 210 + i * 30, fill=cols[i % 2])
    # lounger: backrest at the left, seat to the right
    d.polygon(_hP([(770, 574), (738, 522), (762, 513), (796, 568)]), fill=(68, 150, 160))
    d.polygon(_hP([(770, 574), (900, 574), (900, 586), (770, 586)]), fill=(68, 150, 160))
    d.line(_hP([(782, 579), (894, 579)]), fill=(252, 244, 230), width=3 * _HS)
    for lx in (778, 890):
        d.rectangle(_hP([(lx, 586), (lx + 5, 598)]), fill=(120, 86, 60))
    d.rectangle(_hP([(836, 560), (868, 574)]), fill=(236, 112, 99))        # book
    d.line(_hP([(852, 560), (852, 574)]), fill=(252, 244, 230), width=2 * _HS)
    for fx in (660, 692):                                                # flip-flops
        d.ellipse(_hP([(fx, 588), (fx + 20, 622)]), fill=(236, 112, 99))
        d.line(_hP([(fx + 10, 596), (fx + 3, 608)]), fill=(252, 244, 230), width=2 * _HS)
        d.line(_hP([(fx + 10, 596), (fx + 17, 608)]), fill=(252, 244, 230), width=2 * _HS)
    _hheader(d, date_str, "Rest is part of the work.", ink, accent)


# ---------------------------------------------------------------- week 3
def _scene_family(d, date_str):
    ink, accent = (62, 42, 62), (196, 84, 70)
    _hvgrad(d, 0, 410, (255, 236, 202), (255, 210, 164))
    cx, cy, r = 950, 410, 96
    d.ellipse(_hP([(cx - r, cy - r), (cx + r, cy + r)]), fill=(255, 186, 120))
    _hvgrad(d, 410, 505, (130, 176, 186), (90, 138, 160))
    for i in range(6):
        y = 418 + i * 14
        hw = 80 - i * 11
        d.rounded_rectangle(_hP([(cx - hw, y), (cx + hw, y + 5)]), radius=2 * _HS, fill=(252, 190, 140))
    _hwaves(d, 410, 505, (170, 206, 214), 33)
    _hvgrad(d, 505, 627, (246, 224, 186), (232, 200, 158))
    _hfoam(d, 506, (255, 255, 255))
    # sandcastle
    sc = (214, 176, 120)
    d.rectangle(_hP([(250, 578), (322, 602)]), fill=sc)
    d.rectangle(_hP([(250, 556), (270, 580)]), fill=sc)
    d.rectangle(_hP([(302, 556), (322, 580)]), fill=sc)
    d.rectangle(_hP([(277, 544), (295, 580)]), fill=sc)
    for bx in (250, 258, 266, 302, 310, 318, 277, 287):
        top = 550 if bx in (277, 287) else 552
        d.rectangle(_hP([(bx, top - 6), (bx + 5, top)]), fill=sc)
    d.line(_hP([(286, 538), (286, 520)]), fill=(120, 86, 60), width=2 * _HS)
    d.polygon(_hP([(287, 520), (303, 526), (287, 532)]), fill=(196, 84, 70))
    # kite (flown by the nearest child)
    kx, ky = 130, 180
    d.polygon(_hP([(kx, ky - 34), (kx + 24, ky), (kx, ky + 34), (kx - 24, ky)]), fill=(236, 112, 99))
    d.polygon(_hP([(kx, ky - 34), (kx + 24, ky), (kx, ky + 34)]), fill=(68, 150, 160))
    d.line(_hP([(kx, ky - 34), (kx, ky + 34)]), fill=(255, 244, 230), width=2 * _HS)
    d.line(_hP([(kx - 24, ky), (kx + 24, ky)]), fill=(255, 244, 230), width=2 * _HS)
    for i in range(1, 9):
        d.ellipse(_hP([(kx + 7 * math.sin(i * 1.2) - 4, ky + 34 + i * 9 - 4), (kx + 7 * math.sin(i * 1.2) + 4, ky + 34 + i * 9 + 4)]), fill=(255, 244, 230))
    # family silhouettes
    col = (70, 44, 70)
    base = 604
    a1 = _hperson(d, 380, base, 150, col, dress=True)
    c1 = _hperson(d, 452, base, 98, col)
    c2 = _hperson(d, 520, base, 90, col)
    a2 = _hperson(d, 610, base, 152, col)
    _harm(d, a1[1], (416, base - 54), col, 5)
    _harm(d, c1[0], (416, base - 54), col, 4)
    _harm(d, c1[1], (486, base - 46), col, 4)
    _harm(d, c2[0], (486, base - 46), col, 4)
    _harm(d, c2[1], (565, base - 46), col, 4)
    _harm(d, a2[0], (565, base - 46), col, 5)
    _harm(d, a1[0], (348, base - 134), col, 5)       # mother holds the kite string
    _harm(d, a2[1], (640, base - 70), col, 5)
    # kite string down to the mother's raised hand
    d.line(_hP([(130, 218), (348, base - 134)]), fill=(255, 244, 230), width=2 * _HS)
    _hheader(d, date_str, "Good people. Long afternoons.", ink, accent)




_HOLIDAY_SCENES = {"sunrise": _scene_sunrise, "rest": _scene_rest, "family": _scene_family}


def _draw_holiday_banner(theme, date_str):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (_HW, _HH))
    d = ImageDraw.Draw(img)
    _HOLIDAY_SCENES[theme](d, date_str)
    return img.resize((1200, 627), Image.LANCZOS)


def make_banner_image():
    """Draw today's masthead banner. Returns the file path, or None if anything
    goes wrong (the posts then fall back to text-only, so nothing breaks)."""
    try:
        day = _sa_today()
        theme = _holiday_theme(day)
        if theme:
            try:
                _draw_holiday_banner(theme, f"{day.strftime('%B')} {day.day}, {day.year}").save(BANNER_PATH)
                return BANNER_PATH
            except Exception as e:
                print(f"Holiday banner failed ({e}) — using the standard banner.")

        from PIL import Image, ImageDraw
        W, H = 1200, 627  # LinkedIn's recommended 1.91:1 link/image size
        paper, ink, blue = (250, 247, 240), (26, 26, 26), (10, 102, 194)
        img = Image.new("RGB", (W, H), paper)
        d = ImageDraw.Draw(img)

        # Double rules top and bottom, like a newspaper masthead
        for y, t in ((52, 6), (66, 2), (H - 58, 2), (H - 72, 6)):
            d.rectangle([70, y, W - 70, y + t - 1], fill=ink)

        title_font = _banner_font(150)
        d.text((W / 2, 210), "Social Media", font=title_font, fill=ink, anchor="mm")
        d.text((W / 2, 350), "News Digest", font=title_font, fill=ink, anchor="mm")

        today = _sa_today()
        date_str = f"{today.strftime('%B')} {today.day}, {today.year}"
        d.text((W / 2, 478), date_str, font=_banner_font(40), fill=blue, anchor="mm")

        img.save(BANNER_PATH)
        return BANNER_PATH
    except Exception as e:
        print(f"Banner image skipped ({e}) — posting text only.")
        return None


def build_linkedin_post(today_articles, include_url=True, with_header=True, notice=None):
    # Each bullet carries a real one-liner (li_summary), but deliberately does NOT
    # link to the original source directly — that would route readers away before
    # they ever see sendcutpost.co.za. Instead, everything points to the digest
    # page, which has the full summaries AND links out to each original source
    # from there. This keeps the traffic funnel on your own site first.
    # with_header=False is used when the banner image already carries the title.
    today = _sa_today().strftime("%B %d, %Y")
    lines = []
    if with_header:
        lines += [_to_bold_unicode(DIGEST_PAGE_TITLE), today, ""]
    for a in today_articles:
        bold_title = _to_bold_unicode(a['title'])
        lines.append(f"• {bold_title}: {a['li_summary']}")
    if notice:
        lines.append(("\n" if lines else "") + notice)
    if PUBLISHED_PAGE_URL and include_url:
        lines.append(f"\nFull digest + sources: {PUBLISHED_PAGE_URL}")
    return "\n".join(lines)


def post_to_linkedin(text, image_path=None, fallback_text=None):
    if not LINKEDIN_ACCESS_TOKEN or not LINKEDIN_AUTHOR_URN:
        print("LinkedIn credentials not set — skipping post. Digest text:\n")
        print(fallback_text or text)
        return

    # Uses LinkedIn's newer versioned REST APIs (rest/posts, rest/images), not the
    # legacy /v2/ugcPosts endpoint — the legacy endpoint rejects the member ID format
    # returned by the OpenID /userinfo lookup with an "author" field error.
    # LINKEDIN_API_VERSION should be bumped periodically — LinkedIn sunsets
    # versions after about a year. Check https://learn.microsoft.com/en-us/linkedin/
    # if this starts returning a 426 NONEXISTENT_VERSION error.
    LINKEDIN_API_VERSION = "202603"

    headers = {
        "Authorization": f"Bearer {LINKEDIN_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0",
        "LinkedIn-Version": LINKEDIN_API_VERSION,
    }

    # Optional: upload the banner image first (two steps: register, then upload)
    image_urn = None
    if image_path:
        try:
            init = requests.post(
                "https://api.linkedin.com/rest/images?action=initializeUpload",
                headers=headers,
                json={"initializeUploadRequest": {"owner": LINKEDIN_AUTHOR_URN}},
                timeout=30,
            )
            if init.status_code >= 300:
                print(f"LinkedIn image register failed: {init.status_code} {init.text}")
            else:
                value = init.json()["value"]
                with open(image_path, "rb") as f:
                    up = requests.put(
                        value["uploadUrl"],
                        data=f.read(),
                        headers={"Authorization": f"Bearer {LINKEDIN_ACCESS_TOKEN}",
                                 "Content-Type": "application/octet-stream"},
                        timeout=60,
                    )
                if up.status_code >= 300:
                    print(f"LinkedIn image upload failed: {up.status_code} {up.text}")
                else:
                    image_urn = value["image"]
        except Exception as e:
            print(f"LinkedIn image step error: {e}")

    body = {
        "author": LINKEDIN_AUTHOR_URN,
        # If the image didn't make it, use the version of the text that has its own title
        "commentary": text if image_urn else (fallback_text or text),
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    if image_urn:
        body["content"] = {"media": {"id": image_urn,
                                     "altText": "Social Media News Digest masthead"}}

    resp = requests.post("https://api.linkedin.com/rest/posts",
                         headers=headers, json=body, timeout=30)
    if resp.status_code >= 300:
        print(f"LinkedIn post failed: {resp.status_code} {resp.text}")
    else:
        print("Posted to LinkedIn successfully" + (" (with banner)." if image_urn else "."))


def post_to_facebook(text, image_path=None, fallback_text=None):
    if not FACEBOOK_PAGE_ID or not FACEBOOK_PAGE_TOKEN:
        print("Facebook credentials not set — skipping Facebook post.")
        return

    # Bump this occasionally — Meta retires old Graph API versions after ~2 years.
    FACEBOOK_API_VERSION = "v26.0"
    base = f"https://graph.facebook.com/{FACEBOOK_API_VERSION}/{FACEBOOK_PAGE_ID}"

    try:
        # Preferred: photo post (banner + caption)
        if image_path:
            with open(image_path, "rb") as f:
                resp = requests.post(
                    f"{base}/photos",
                    data={"caption": text, "access_token": FACEBOOK_PAGE_TOKEN},
                    files={"source": f},
                    timeout=60,
                )
            if resp.status_code < 300:
                print("Posted to Facebook successfully (with banner).")
                return
            print(f"Facebook photo post failed: {resp.status_code} {resp.text}")

        # Fallback: plain text post with a link preview card
        payload = {"message": fallback_text or text, "access_token": FACEBOOK_PAGE_TOKEN}
        if PUBLISHED_PAGE_URL:
            payload["link"] = PUBLISHED_PAGE_URL
        resp = requests.post(f"{base}/feed", data=payload, timeout=30)
        if resp.status_code >= 300:
            print(f"Facebook post failed: {resp.status_code} {resp.text}")
        else:
            print("Posted to Facebook successfully.")
    except Exception as e:
        # Never let a Facebook hiccup stop the LinkedIn post or the web page build.
        print(f"Facebook post error: {e}")


def build_whatsapp_post(today_articles, notice=None):
    # Same content as the other posts, but with WhatsApp's own *bold* markup,
    # which renders properly in a Channel (the banner image carries the title).
    lines = []
    for a in today_articles:
        lines.append(f"• *{a['title']}*: {a['li_summary']}")
        lines.append("")
    if notice:
        lines.append(notice)
        lines.append("")
    if PUBLISHED_PAGE_URL:
        lines.append(f"Full digest + sources: {PUBLISHED_PAGE_URL}")
    return "\n".join(lines).strip()


def send_github_nudge(today_articles, image_path=None, notice=None, test=False):
    """Open a GitHub issue holding the ready-to-paste WhatsApp post and tag the repo
    owner, so GitHub emails it to them. No mail server or password involved: it uses
    the workflow's own built-in token."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")          # "owner/name", set by GitHub
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER")
    if not (token and repo):
        print("GitHub nudge not configured — skipping.")
        return

    try:
        day = _sa_today()
        steps = []
        image_md = ""
        if image_path and os.path.exists(image_path):
            import shutil
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            # Saved into docs/ so the workflow publishes it on your GitHub Pages site.
            shutil.copyfile(image_path, os.path.join(OUTPUT_DIR, "digest-banner.png"))
            user, name = repo.split("/", 1)
            url = f"https://{user.lower()}.github.io/{name}/digest-banner.png?d={day.strftime('%Y%m%d')}"
            image_md = (f"![banner]({url})\n\n[Open the banner image]({url}) "
                        "(it goes live a minute or two after the run finishes)\n\n")
            steps.append("Save the banner image below.")
        steps.append("Copy the text in the box (use the copy icon at its top right).")
        steps.append("In your Channel: add the image, paste the text, send.")
        steps_md = "\n".join(f"{i}. {t}" for i, t in enumerate(steps, 1))

        intro = ("This is a test. Nothing was posted anywhere." if test
                 else "Your WhatsApp Channel post is ready.")
        body = (f"@{owner} {intro}\n\n{steps_md}\n\n{image_md}"
                f"```\n{build_whatsapp_post(today_articles, notice)}\n```\n")
        title = (f"TEST: WhatsApp nudge check ({day.strftime('%d %B')})" if test
                 else f"WhatsApp Channel post ready — {day.strftime('%A, %B %d')}")

        resp = requests.post(
            f"https://api.github.com/repos/{repo}/issues",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            json={"title": title, "body": body, "labels": ["whatsapp-nudge"]},
            timeout=30,
        )
        if resp.status_code >= 300:
            print(f"GitHub nudge failed: {resp.status_code} {resp.text}")
        else:
            print(f"GitHub nudge created: {resp.json().get('html_url')}")
    except Exception as e:
        # Never let a nudge problem stop the posts or the web page build.
        print(f"GitHub nudge error: {type(e).__name__}: {e}")


def _smtp_send(msg):
    """Send with retries. If the configured port keeps failing, try the other common
    one (465 SSL / 587 STARTTLS) before giving up. A wrong password is not retried."""
    import smtplib
    configured = int(SMTP_PORT)
    ports = [configured] + [p for p in (465, 587) if p != configured]
    last_error = None
    for port in ports:
        for attempt in (1, 2):
            stage = "connect"
            try:
                if port == 465:
                    server = smtplib.SMTP_SSL(SMTP_HOST, port, timeout=30)
                else:
                    server = smtplib.SMTP(SMTP_HOST, port, timeout=30)
                    stage = "hello"
                    server.ehlo()
                    stage = "starttls"
                    server.starttls()
                    stage = "hello"
                    server.ehlo()
                with server:
                    stage = "login"
                    server.login(SMTP_USER, SMTP_PASSWORD)
                    stage = "send"
                    server.send_message(msg)
                return port
            except smtplib.SMTPAuthenticationError:
                raise
            except (smtplib.SMTPException, OSError) as e:
                last_error = e
                print(f"Nudge email attempt failed (port {port}, try {attempt}, "
                      f"step: {stage}): {type(e).__name__}: {e}")
                time.sleep(5)
    raise last_error


def send_whatsapp_nudge(today_articles, image_path=None, notice=None):
    """Email the ready-to-paste WhatsApp Channel post (text in the body, banner
    attached). Skips quietly if the email settings aren't configured."""
    if not (NUDGE_EMAIL_TO and SMTP_HOST and SMTP_USER and SMTP_PASSWORD):
        print("Morning nudge email not configured — skipping.")
        return

    try:
        import smtplib
        from email.message import EmailMessage

        today = _sa_today().strftime("%A, %B %d")
        msg = EmailMessage()
        msg["Subject"] = f"WhatsApp Channel post ready — {today}"
        msg["From"] = SMTP_USER
        msg["To"] = NUDGE_EMAIL_TO
        msg.set_content(
            "Your WhatsApp Channel post is ready.\n\n"
            "1. Save the attached banner image.\n"
            "2. Copy the text below.\n"
            "3. In your Channel: add the image, paste the text, send.\n\n"
            "----------------------------------------\n\n"
            + build_whatsapp_post(today_articles, notice)
        )
        if image_path and os.path.exists(image_path):
            with open(image_path, "rb") as f:
                msg.add_attachment(f.read(), maintype="image", subtype="png",
                                   filename="digest-banner.png")

        port = _smtp_send(msg)
        print(f"Morning nudge email sent (port {port}).")
    except smtplib.SMTPAuthenticationError as e:
        print(f"Morning nudge email failed: login rejected ({e}). The app password may "
              "have been revoked or changed (for example after a Google password change). "
              "Make a new app password and update the SMTP_PASSWORD secret.")
    except Exception as e:
        # Never let an email problem stop the posts or the web page build.
        print(f"Morning nudge email failed: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    today = _sa_today()

    # Test switch (set from the "Run workflow" button): sends ONLY a sample nudge
    # email, with no posts, no web page update and no history change.
    if os.environ.get("NUDGE_TEST_ONLY") == "true":
        sample = [{
            "title": "TEST: this is a sample item",
            "li_summary": "If you can read this, the morning nudge email works. Nothing was posted anywhere.",
        }]
        test_banner = make_banner_image()
        send_github_nudge(sample, test_banner, test=True)
        send_whatsapp_nudge(sample, test_banner)
        return

    # Holiday break: nothing is published from 19 December until 10 January.
    if _on_holiday_break(today):
        print(f"Holiday break ({HOLIDAY_BREAK_START} to {HOLIDAY_BREAK_END}) — nothing published today.")
        return

    # Scheduled runs come in pairs (an early one and a backup). If the early one
    # already published today, the backup stands down. Manual runs always go ahead.
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule" and _already_posted(today):
        print("Already published today — backup run stands down.")
        return

    today_articles = fetch_recent_articles()
    for a in today_articles:
        summaries = summarize_article(a)
        a["summary"] = summaries["long"]       # full one-minute read, shown on the web page
        a["li_summary"] = summaries["short"]   # dense one-liner, shown in the LinkedIn post
        a.pop("raw_summary", None)  # no longer needed once we have a real summary

    history = update_history(today_articles)

    html = build_html(history, notice=_site_notice(today))
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(os.path.join(OUTPUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

    # Skip posting on a quiet news day rather than publishing an empty digest —
    # except on the sign-off day, when the holiday message must go out regardless.
    notice = _post_notice(today)
    if today_articles or notice:
        banner = make_banner_image()
        # With a banner the image carries the title, so the text starts at the bullets.
        # Without one (or if the upload fails) the text-only version has its own title.
        post_to_linkedin(
            build_linkedin_post(today_articles, with_header=False, notice=notice),
            image_path=banner,
            fallback_text=build_linkedin_post(today_articles, with_header=True, notice=notice),
        )
        post_to_facebook(
            build_linkedin_post(today_articles, with_header=False, notice=notice),
            image_path=banner,
            fallback_text=build_linkedin_post(today_articles, include_url=False,
                                              with_header=True, notice=notice),
        )
        mark_posted(today)
        send_github_nudge(today_articles, banner, notice)
        send_whatsapp_nudge(today_articles, banner, notice)
    else:
        print("No qualifying articles today — skipping posts to avoid a blank digest.")

    print(f"Digest built with {len(today_articles)} article(s) today, "
          f"{len(history)} day(s) in history.")


if __name__ == "__main__":
    main()
