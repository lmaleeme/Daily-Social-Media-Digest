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
import datetime
import feedparser
import requests
import trafilatura

# ---------------------------------------------------------------------------
# CONFIG — edit this section to add/remove sources or tune behavior
# ---------------------------------------------------------------------------

RSS_FEEDS = [
    "https://www.marketingdive.com/feeds/news/", "https://www.socialmediatoday.com/feeds/news/",
    # Add more feeds here, e.g.:
    # "https://www.socialmediatoday.com/rss/",
    # "https://www.adweek.com/feed/",
]

# Only keep articles whose title/summary mentions one of these (case-insensitive).
SOCIAL_KEYWORDS = [
    "social media", "instagram", "tiktok", "linkedin", "facebook", "meta",
    "twitter", " x ", "youtube", "snapchat", "pinterest", "reddit",
    "influencer", "algorithm", "creator economy", "platform update",
]

LOOKBACK_HOURS = 26  # slight buffer past 24h so a daily run never misses items
MAX_ARTICLES = 6     # cap on how many stories go into one day's digest
HISTORY_DAYS = 5     # how many days of digests to keep on the page

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = "claude-sonnet-4-6"

LINKEDIN_ACCESS_TOKEN = os.environ.get("LINKEDIN_ACCESS_TOKEN")
LINKEDIN_AUTHOR_URN = os.environ.get("LINKEDIN_AUTHOR_URN")

DIGEST_PAGE_TITLE = "Social Media News Digest"
PUBLISHED_PAGE_URL = os.environ.get("PUBLISHED_PAGE_URL", "")

OUTPUT_DIR = "docs"
HISTORY_FILE = os.path.join(OUTPUT_DIR, "history.json")


# ---------------------------------------------------------------------------
# STEP 1: Fetch + filter articles from RSS
# ---------------------------------------------------------------------------

def fetch_recent_articles():
    cutoff = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(hours=LOOKBACK_HOURS)
    articles = []

    for feed_url in RSS_FEEDS:
        parsed = feedparser.parse(feed_url)
        for entry in parsed.entries:
            published = _get_published_dt(entry)
            if published and published < cutoff:
                continue

            title = entry.get("title", "").strip()
            summary_raw = entry.get("summary", "") or entry.get("description", "")
            text_blob = f"{title} {summary_raw}".lower()

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
        return source_text[:220].rsplit(" ", 1)[0] + "…"

    # Cap input length to keep prompts small/cheap
    source_text = source_text[:6000]

    prompt = (
        "You are writing an executive summary for a daily social-media-news digest, "
        "read by busy marketing professionals. Read the article text below and write "
        "a summary ENTIRELY IN YOUR OWN WORDS — do not copy or closely paraphrase "
        "sentences from the source, and do not use direct quotes.\n\n"
        "Target length: a ONE-MINUTE READ — about 150-180 words, 4-6 sentences. "
        "This is not a teaser; it should be dense enough that the reader genuinely "
        "understands what happened without opening the article, and only clicks "
        "through if they want more depth or the exact source.\n\n"
        "Include, as relevant: what happened and to which platform/company; concrete "
        "specifics (numbers, dates, features, names) rather than vague description; "
        "the stated reason or context behind it; and why it matters for someone "
        "working in marketing or social media (the practical implication).\n\n"
        "Do not include fluff: no throat-clearing openers ('In a recent development...'), "
        "no restating the headline, no filler adjectives, no editorializing or opinion, "
        "no call-to-action. Every sentence should carry information the reader would "
        "otherwise have had to read the article to get.\n\n"
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
            "max_tokens": 350,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=45,
    )
    response.raise_for_status()
    data = response.json()
    return "".join(block.get("text", "") for block in data.get("content", [])).strip()


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
    today_str = datetime.date.today().isoformat()
    history = load_history()

    # Drop any existing entry for today (handles re-runs on the same day)
    history = [day for day in history if day["date"] != today_str]

    history.insert(0, {"date": today_str, "articles": today_articles})
    history = history[:HISTORY_DAYS]

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)

    return history


# ---------------------------------------------------------------------------
# STEP 5: Build HTML digest page (today + rolling history)
# ---------------------------------------------------------------------------

def build_html(history):
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
<style>
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
</style>
</head>
<body>
  <h1>{DIGEST_PAGE_TITLE}</h1>
  <div class="subtitle">Last {len(history)} day{'s' if len(history) != 1 else ''} of social media news, summarized</div>
  {days_html}
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# STEP 6: Post today's highlights to LinkedIn
# ---------------------------------------------------------------------------

def build_linkedin_post(today_articles):
    # Summaries are now full 1-minute reads (~150-180 words), too long to stack
    # several into one LinkedIn post. Keep LinkedIn as true highlights — just the
    # headlines — and send readers to the page for the full summaries.
    today = datetime.date.today().strftime("%B %d, %Y")
    lines = [f"📱 Social Media News — {today}\n"]
    for a in today_articles:
        lines.append(f"• {a['title']}")
    if PUBLISHED_PAGE_URL:
        lines.append(f"\nFull summaries: {PUBLISHED_PAGE_URL}")
    return "\n".join(lines)


def post_to_linkedin(text):
    if not LINKEDIN_ACCESS_TOKEN or not LINKEDIN_AUTHOR_URN:
        print("LinkedIn credentials not set — skipping post. Digest text:\n")
        print(text)
        return

    resp = requests.post(
        "https://api.linkedin.com/v2/ugcPosts",
        headers={
            "Authorization": f"Bearer {LINKEDIN_ACCESS_TOKEN}",
            "Content-Type": "application/json",
            "X-Restli-Protocol-Version": "2.0.0",
        },
        json={
            "author": LINKEDIN_AUTHOR_URN,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": text},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
            },
        },
        timeout=30,
    )
    if resp.status_code >= 300:
        print(f"LinkedIn post failed: {resp.status_code} {resp.text}")
    else:
        print("Posted to LinkedIn successfully.")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    today_articles = fetch_recent_articles()
    for a in today_articles:
        a["summary"] = summarize_article(a)
        a.pop("raw_summary", None)  # no longer needed once we have a real summary

    history = update_history(today_articles)

    html = build_html(history)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(os.path.join(OUTPUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

    li_text = build_linkedin_post(today_articles)
    post_to_linkedin(li_text)

    print(f"Digest built with {len(today_articles)} article(s) today, "
          f"{len(history)} day(s) in history.")


if __name__ == "__main__":
    main()
