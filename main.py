#!/usr/bin/env python3
"""
Daily Social Media News Digest
--------------------------------
1. Pulls recent articles from configured RSS feeds
2. Filters for items relevant to social media / platform news
3. Summarizes each in 1-2 tight sentences (Anthropic API)
4. Builds a static HTML digest page (for GitHub Pages -> embed in Zoho Sites)
5. Posts a short highlights digest to LinkedIn

Run manually:  python main.py
Scheduled by:  .github/workflows/daily-digest.yml
"""

import os
import re
import json
import datetime
import feedparser
import requests

# ---------------------------------------------------------------------------
# CONFIG — edit this section to add/remove sources or tune behavior
# ---------------------------------------------------------------------------

RSS_FEEDS = [
    "https://www.marketingdive.com/feeds/news/",
    # Add more feeds here, e.g.:
    # "https://www.socialmediatoday.com/rss/",
    # "https://www.adweek.com/feed/",
]

# Only keep articles whose title/summary mentions one of these (case-insensitive).
# This is what narrows a general marketing feed down to "social media news".
SOCIAL_KEYWORDS = [
    "social media", "instagram", "tiktok", "linkedin", "facebook", "meta",
    "twitter", " x ", "youtube", "snapchat", "pinterest", "reddit",
    "influencer", "algorithm", "creator economy", "platform update",
]

LOOKBACK_HOURS = 26  # slight buffer past 24h so a daily run never misses items
MAX_ARTICLES = 6     # cap on how many stories go into one digest

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = "claude-sonnet-4-6"

LINKEDIN_ACCESS_TOKEN = os.environ.get("LINKEDIN_ACCESS_TOKEN")
LINKEDIN_AUTHOR_URN = os.environ.get("LINKEDIN_AUTHOR_URN")  # e.g. "urn:li:person:xxxx" or "urn:li:organization:xxxx"

DIGEST_PAGE_TITLE = "Daily Social Media News Digest"
# Set this to your published GitHub Pages URL once repo is live, used in the LinkedIn post.
PUBLISHED_PAGE_URL = os.environ.get("PUBLISHED_PAGE_URL", "")

OUTPUT_DIR = "docs"


# ---------------------------------------------------------------------------
# STEP 1: Fetch + filter articles
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
# STEP 2: Summarize each article (short, skimmable)
# ---------------------------------------------------------------------------

def summarize_article(article):
    if not ANTHROPIC_API_KEY:
        # Fallback: no key configured, just trim the raw description
        return article["raw_summary"][:180].rsplit(" ", 1)[0] + "…"

    prompt = (
        "Summarize this social media / marketing news item in ONE tight sentence "
        "(max ~25 words). Assume the reader wants to stay informed but has no time "
        "for the full article. Be concrete: name the platform/company and the "
        "concrete change or news, not vague framing.\n\n"
        f"Title: {article['title']}\n"
        f"Description: {article['raw_summary']}"
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
            "max_tokens": 100,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    return "".join(block.get("text", "") for block in data.get("content", [])).strip()


# ---------------------------------------------------------------------------
# STEP 3: Build HTML digest page
# ---------------------------------------------------------------------------

def build_html(articles):
    today = datetime.date.today().strftime("%B %d, %Y")
    rows = ""
    for a in articles:
        rows += f"""
        <div class="item">
          <h3><a href="{a['link']}" target="_blank" rel="noopener">{a['title']}</a></h3>
          <p>{a['summary']}</p>
          <span class="src">{a['source']}</span>
        </div>"""

    if not articles:
        rows = "<p>No qualifying social media news in the last 24 hours.</p>"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{DIGEST_PAGE_TITLE} — {today}</title>
<style>
  body {{ font-family: -apple-system, Helvetica, Arial, sans-serif; max-width: 680px;
         margin: 40px auto; padding: 0 16px; color: #1a1a1a; line-height: 1.5; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 0; }}
  .date {{ color: #666; margin-top: 4px; margin-bottom: 24px; font-size: 0.9rem; }}
  .item {{ padding: 14px 0; border-bottom: 1px solid #eee; }}
  .item h3 {{ margin: 0 0 6px 0; font-size: 1.05rem; }}
  .item a {{ color: #0a66c2; text-decoration: none; }}
  .item a:hover {{ text-decoration: underline; }}
  .item p {{ margin: 0 0 6px 0; }}
  .src {{ font-size: 0.8rem; color: #888; }}
</style>
</head>
<body>
  <h1>{DIGEST_PAGE_TITLE}</h1>
  <div class="date">{today}</div>
  {rows}
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# STEP 4: Post highlights to LinkedIn
# ---------------------------------------------------------------------------

def build_linkedin_post(articles):
    today = datetime.date.today().strftime("%B %d, %Y")
    lines = [f"📱 Social Media News — {today}\n"]
    for a in articles:
        lines.append(f"• {a['summary']}")
    if PUBLISHED_PAGE_URL:
        lines.append(f"\nFull digest: {PUBLISHED_PAGE_URL}")
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
    articles = fetch_recent_articles()
    for a in articles:
        a["summary"] = summarize_article(a)

    html = build_html(articles)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(os.path.join(OUTPUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

    # Keep a JSON copy too, useful for debugging or feeding other tools
    with open(os.path.join(OUTPUT_DIR, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(articles, f, indent=2)

    li_text = build_linkedin_post(articles)
    post_to_linkedin(li_text)

    print(f"Digest built with {len(articles)} article(s).")


if __name__ == "__main__":
    main()
