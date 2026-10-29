#!/usr/bin/env python3
"""
Daily Social Media News Digest
--------------------------------
Modes (DIGEST_TEST_MODE):
  full                  Run the normal daily digest.
  email_test            Send only a sample WhatsApp nudge email.
  linkedin_text_test    Publish a clearly labelled text-only LinkedIn test.
  linkedin_banner_test  Publish a clearly labelled LinkedIn test with banner.

Run manually: python main.py
Scheduled by: .github/workflows/daily-digest.yml
"""

import os
import re
import json
import datetime
import html
import feedparser
import requests
import trafilatura

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

RSS_FEEDS = [
    "https://www.marketingdive.com/feeds/news/",
    "https://www.socialmediatoday.com/feeds/news/",
]
SOCIAL_KEYWORDS = [
    "social media", "instagram", "tiktok", "linkedin", "facebook", "meta",
    "twitter", " x ", "youtube", "snapchat", "pinterest", "reddit", "threads",
    "influencer", "algorithm", "creator economy", "platform update",
    "social-first", "social series", "social content", "social campaign",
    "social strategy", "user-generated content", " ugc ", "viral", "creator",
    "brand partnership", "sponsored content",
]
LOOKBACK_HOURS = 26
MAX_ARTICLES = 6
HISTORY_DAYS = 5

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = "claude-sonnet-5"

LINKEDIN_ACCESS_TOKEN = os.environ.get("LINKEDIN_ACCESS_TOKEN")
LINKEDIN_AUTHOR_URN = os.environ.get("LINKEDIN_AUTHOR_URN")

FACEBOOK_PAGE_ID = os.environ.get("FACEBOOK_PAGE_ID")
FACEBOOK_PAGE_TOKEN = os.environ.get("FACEBOOK_PAGE_TOKEN")

NUDGE_EMAIL_TO = os.environ.get("NUDGE_EMAIL_TO")
SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = os.environ.get("SMTP_PORT") or "465"
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")

DIGEST_PAGE_TITLE = "Social Media News Digest"
PUBLISHED_PAGE_URL = os.environ.get("PUBLISHED_PAGE_URL", "")
DIGEST_TEST_MODE = (os.environ.get("DIGEST_TEST_MODE") or "full").strip().lower()

OUTPUT_DIR = "docs"
HISTORY_FILE = os.path.join(OUTPUT_DIR, "history.json")


# ---------------------------------------------------------------------------
# STEP 1: Fetch + filter articles from RSS
# ---------------------------------------------------------------------------

def fetch_recent_articles():
    is_monday = datetime.date.today().weekday() == 0
    lookback_hours = 78 if is_monday else LOOKBACK_HOURS
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        - datetime.timedelta(hours=lookback_hours)
    )
    articles = []

    for feed_url in RSS_FEEDS:
        parsed = feedparser.parse(feed_url)
        for entry in parsed.entries:
            published = _get_published_dt(entry)
            if published and published < cutoff:
                continue

            title = entry.get("title", "").strip()
            summary_raw = entry.get("summary", "") or entry.get("description", "")
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
# STEP 2: Fetch full article text
# ---------------------------------------------------------------------------

def fetch_full_text(url):
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return ""
        extracted = trafilatura.extract(
            downloaded, include_comments=False, include_tables=False
        )
        return extracted or ""
    except Exception as e:
        print(f"Full-text fetch failed for {url}: {e}")
        return ""


# ---------------------------------------------------------------------------
# STEP 3: Summarise article with Anthropic
# ---------------------------------------------------------------------------

def summarize_article(article):
    full_text = fetch_full_text(article["link"])
    source_text = full_text if full_text else article["raw_summary"]

    if not ANTHROPIC_API_KEY:
        trimmed = source_text[:220].rsplit(" ", 1)[0] + "…"
        return {"long": trimmed, "short": trimmed[:120]}

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
        "LinkedIn post, so it must convey real substance by itself.\n\n"
        "For LONG: a ONE-MINUTE READ, about 150-180 words, written as a single "
        "paragraph. Dense enough that the reader understands what happened.\n\n"
        "Both: written entirely in your own words, with concrete specifics rather "
        "than vague description. No filler, editorializing, or call-to-action.\n\n"
        f"Title: {article['title']}\n\nArticle text:\n{source_text}"
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
        print(f"Anthropic API error {response.status_code}: {response.text[:1000]}")
    response.raise_for_status()
    data = response.json()
    raw_text = "".join(
        block.get("text", "") for block in data.get("content", [])
    ).strip()

    short_match = re.search(
        r"SHORT:\s*(.+?)(?=\n\s*LONG:|\Z)", raw_text, re.DOTALL
    )
    long_match = re.search(r"LONG:\s*(.+)", raw_text, re.DOTALL)
    short_val = short_match.group(1).strip() if short_match else ""
    long_val = long_match.group(1).strip() if long_match else ""

    if not short_val and not long_val:
        print(f"Summary parsing failed for '{article['title']}', using fallback")
        clean_fallback = source_text[:220].rsplit(" ", 1)[0] + "…"
        short_val = clean_fallback[:150]
        long_val = clean_fallback

    return {"long": long_val or short_val, "short": short_val or long_val[:150]}


# ---------------------------------------------------------------------------
# STEP 4: Rolling history
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
    history = [day for day in history if day.get("date") != today_str]
    history.insert(0, {"date": today_str, "articles": today_articles})
    history = history[:HISTORY_DAYS]

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)
    return history


# ---------------------------------------------------------------------------
# STEP 5: Build HTML digest page
# ---------------------------------------------------------------------------

def build_html(history):
    days_html = ""
    for day in history:
        date_label = datetime.date.fromisoformat(day["date"]).strftime(
            "%A, %B %d, %Y"
        )
        items_html = ""
        for a in day["articles"]:
            title = html.escape(str(a.get("title", "")))
            summary = html.escape(str(a.get("summary", "")))
            link = html.escape(str(a.get("link", "")), quote=True)
            source = html.escape(str(a.get("source", "source")))
            items_html += f"""
        <div class="item">
          <h3>{title}</h3>
          <p>{summary}</p>
          <a class="src" href="{link}" target="_blank" rel="noopener">Read more at {source} →</a>
        </div>"""
        if not day["articles"]:
            items_html = '<p class="none">No qualifying social media news that day.</p>'
        days_html += f"""
      <section class="day">
        <h2>{date_label}</h2>
        {items_html}
      </section>"""

    safe_page_title = html.escape(DIGEST_PAGE_TITLE)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{safe_page_title}</title>
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
</style>
</head>
<body>
  <h1>{safe_page_title}</h1>
  <div class="subtitle">Last {len(history)} day{'s' if len(history) != 1 else ''} of social media news, summarized</div>
  {days_html}
</body>
</html>"""


# ---------------------------------------------------------------------------
# STEP 6: Social post content and banner
# ---------------------------------------------------------------------------

def _to_bold_unicode(text):
    bold_map = {}
    for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        bold_map[c] = chr(0x1D400 + i)
    for i, c in enumerate("abcdefghijklmnopqrstuvwxyz"):
        bold_map[c] = chr(0x1D41A + i)
    for i, c in enumerate("0123456789"):
        bold_map[c] = chr(0x1D7CE + i)
    return "".join(bold_map.get(ch, ch) for ch in text)


BANNER_PATH = "/tmp/digest_banner.png"
BANNER_FONT_PATH = "/tmp/BalooThambi2.ttf"
BANNER_FONT_URL = (
    "https://raw.githubusercontent.com/google/fonts/main/ofl/"
    "baloothambi2/BalooThambi2%5Bwght%5D.ttf"
)


def _banner_font(size, weight=600):
    from PIL import ImageFont
    if not os.path.exists(BANNER_FONT_PATH):
        r = requests.get(BANNER_FONT_URL, timeout=30)
        r.raise_for_status()
        with open(BANNER_FONT_PATH, "wb") as f:
            f.write(r.content)
    font = ImageFont.truetype(BANNER_FONT_PATH, size)
    try:
        font.set_variation_by_axes([weight])
    except Exception:
        pass
    return font


def make_banner_image():
    try:
        from PIL import Image, ImageDraw
        width, height = 1200, 627
        paper, ink, blue = (250, 247, 240), (26, 26, 26), (10, 102, 194)
        img = Image.new("RGB", (width, height), paper)
        draw = ImageDraw.Draw(img)
        for y, thickness in ((52, 6), (66, 2), (height - 58, 2), (height - 72, 6)):
            draw.rectangle(
                [70, y, width - 70, y + thickness - 1], fill=ink
            )
        title_font = _banner_font(150)
        draw.text((width / 2, 210), "Social Media", font=title_font, fill=ink, anchor="mm")
        draw.text((width / 2, 350), "News Digest", font=title_font, fill=ink, anchor="mm")
        today = datetime.date.today()
        date_str = f"{today.strftime('%B')} {today.day}, {today.year}"
        draw.text((width / 2, 478), date_str, font=_banner_font(40), fill=blue, anchor="mm")
        img.save(BANNER_PATH)
        return BANNER_PATH
    except Exception as e:
        print(f"Banner image skipped ({e}) — posting text only.")
        return None


def build_linkedin_post(today_articles, include_url=True, with_header=True):
    today = datetime.date.today().strftime("%B %d, %Y")
    lines = []
    if with_header:
        lines += [_to_bold_unicode(DIGEST_PAGE_TITLE), today, ""]
    for article in today_articles:
        lines.append(
            f"• {_to_bold_unicode(article['title'])}: {article['li_summary']}"
        )
    if PUBLISHED_PAGE_URL and include_url:
        lines.append(f"\nFull digest + sources: {PUBLISHED_PAGE_URL}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# STEP 7: LinkedIn publishing via self-service Share on LinkedIn API
# ---------------------------------------------------------------------------

def post_to_linkedin(text, image_path=None, fallback_text=None):
    if not LINKEDIN_ACCESS_TOKEN or not LINKEDIN_AUTHOR_URN:
        print("LinkedIn credentials missing: set LINKEDIN_ACCESS_TOKEN and LINKEDIN_AUTHOR_URN.")
        return False

    if not LINKEDIN_AUTHOR_URN.startswith("urn:li:person:"):
        print("LinkedIn author URN must be a person URN (urn:li:person:...).")
        print("Check LINKEDIN_AUTHOR_URN; do not put the access token in this field.")
        return False

    base_url = "https://api.linkedin.com/v2"
    headers = {
        "Authorization": f"Bearer {LINKEDIN_ACCESS_TOKEN}",
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }
    image_asset = None

    if image_path and os.path.isfile(image_path):
        try:
            register_payload = {
                "registerUploadRequest": {
                    "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
                    "owner": LINKEDIN_AUTHOR_URN,
                    "serviceRelationships": [{
                        "relationshipType": "OWNER",
                        "identifier": "urn:li:userGeneratedContent",
                    }],
                }
            }
            reg = requests.post(
                f"{base_url}/assets?action=registerUpload",
                headers=headers,
                json=register_payload,
                timeout=30,
            )
            if reg.status_code not in (200, 201):
                print(
                    f"LinkedIn image registration failed: {reg.status_code} "
                    f"{reg.text[:1200]}"
                )
            else:
                value = reg.json()["value"]
                mechanism = value["uploadMechanism"][
                    "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"
                ]
                upload_url = mechanism["uploadUrl"]
                image_asset = value["asset"]
                upload_headers = dict(mechanism.get("headers", {}))
                upload_headers.setdefault("Content-Type", "application/octet-stream")

                with open(image_path, "rb") as image_file:
                    upload = requests.put(
                        upload_url,
                        headers=upload_headers,
                        data=image_file,
                        timeout=60,
                    )
                if upload.status_code not in (200, 201, 202):
                    print(
                        f"LinkedIn image upload failed: {upload.status_code} "
                        f"{upload.text[:1200]}"
                    )
                    image_asset = None
                else:
                    print("LinkedIn banner uploaded successfully.")
        except (requests.RequestException, KeyError, ValueError, OSError) as exc:
            image_asset = None
            print(f"LinkedIn image step failed: {exc}")

    post_text = text if image_asset else (fallback_text or text)
    share_content = {
        "shareCommentary": {"text": post_text},
        "shareMediaCategory": "IMAGE" if image_asset else "NONE",
    }
    if image_asset:
        share_content["media"] = [{
            "status": "READY",
            "media": image_asset,
            "title": {"text": "Social Media News Digest"},
            "description": {"text": "Social Media News Digest banner"},
        }]

    payload = {
        "author": LINKEDIN_AUTHOR_URN,
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": share_content
        },
        "visibility": {
            "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
        },
    }

    try:
        response = requests.post(
            f"{base_url}/ugcPosts",
            headers=headers,
            json=payload,
            timeout=30,
        )
        if response.status_code == 201:
            post_id = response.headers.get("X-RestLi-Id", "(not returned)")
            print(
                "LinkedIn post published successfully"
                + (" with banner." if image_asset else " (text only).")
            )
            print(f"LinkedIn post ID: {post_id}")
            return True

        print(
            f"LinkedIn post failed: HTTP {response.status_code}: "
            f"{response.text[:1800]}"
        )
        if response.status_code == 403:
            print(
                "Permission check: confirm the app has Share on LinkedIn access, "
                "the token was authorised with w_member_social, and the author URN "
                "belongs to the same member who authorised the token."
            )
        return False
    except requests.RequestException as exc:
        print(f"LinkedIn post request failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# STEP 8: Facebook publishing (normal full mode only)
# ---------------------------------------------------------------------------

def post_to_facebook(text, image_path=None, fallback_text=None):
    if not FACEBOOK_PAGE_ID or not FACEBOOK_PAGE_TOKEN:
        print("Facebook credentials not set — skipping Facebook post.")
        return

    facebook_api_version = "v26.0"
    base = f"https://graph.facebook.com/{facebook_api_version}/{FACEBOOK_PAGE_ID}"
    try:
        if image_path:
            with open(image_path, "rb") as image_file:
                response = requests.post(
                    f"{base}/photos",
                    data={"caption": text, "access_token": FACEBOOK_PAGE_TOKEN},
                    files={"source": image_file},
                    timeout=60,
                )
            if response.status_code < 300:
                print("Posted to Facebook successfully (with banner).")
                return
            print(f"Facebook photo post failed: {response.status_code} {response.text[:1000]}")

        payload = {
            "message": fallback_text or text,
            "access_token": FACEBOOK_PAGE_TOKEN,
        }
        if PUBLISHED_PAGE_URL:
            payload["link"] = PUBLISHED_PAGE_URL
        response = requests.post(f"{base}/feed", data=payload, timeout=30)
        if response.status_code >= 300:
            print(f"Facebook post failed: {response.status_code} {response.text[:1000]}")
        else:
            print("Posted to Facebook successfully.")
    except Exception as exc:
        print(f"Facebook post error: {exc}")


# ---------------------------------------------------------------------------
# STEP 9: WhatsApp post and email nudge
# ---------------------------------------------------------------------------

def build_whatsapp_post(today_articles):
    lines = []
    for article in today_articles:
        lines.append(f"• *{article['title']}*: {article['li_summary']}")
        lines.append("")
    if PUBLISHED_PAGE_URL:
        lines.append(f"Full digest + sources: {PUBLISHED_PAGE_URL}")
    return "\n".join(lines).strip()


def send_whatsapp_nudge(today_articles, image_path=None):
    if not (NUDGE_EMAIL_TO and SMTP_HOST and SMTP_USER and SMTP_PASSWORD):
        print("Morning nudge email not configured — skipping.")
        return False

    try:
        import smtplib
        from email.message import EmailMessage

        today = datetime.date.today().strftime("%A, %B %d")
        message = EmailMessage()
        message["Subject"] = f"WhatsApp Channel post ready — {today}"
        message["From"] = SMTP_USER
        message["To"] = NUDGE_EMAIL_TO
        message.set_content(
            "Your WhatsApp Channel post is ready.\n\n"
            "1. Save the attached banner image.\n"
            "2. Copy the text below.\n"
            "3. In your Channel: add the image, paste the text, send.\n\n"
            "----------------------------------------\n\n"
            + build_whatsapp_post(today_articles)
        )
        if image_path and os.path.exists(image_path):
            with open(image_path, "rb") as image_file:
                message.add_attachment(
                    image_file.read(),
                    maintype="image",
                    subtype="png",
                    filename="digest-banner.png",
                )

        port = int(SMTP_PORT)
        if port == 465:
            server = smtplib.SMTP_SSL(SMTP_HOST, port, timeout=30)
        else:
            server = smtplib.SMTP(SMTP_HOST, port, timeout=30)
            server.starttls()
        with server:
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(message)
        print("Morning nudge email sent.")
        return True
    except Exception as exc:
        print(f"Morning nudge email failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# STEP 10: Explicit test modes and main workflow
# ---------------------------------------------------------------------------

def run_email_test():
    sample = [{
        "title": "TEST: sample WhatsApp Channel post",
        "li_summary": (
            "TEST ONLY — if you received this email, the nudge email setup works. "
            "No social posts or website updates were made."
        ),
    }]
    banner = make_banner_image()
    send_whatsapp_nudge(sample, banner)


def run_linkedin_test(with_banner):
    if with_banner:
        banner = make_banner_image()
        text = (
            "TEST POST — Social Media News Digest\n\n"
            "This is a test of LinkedIn text publishing and banner image upload. "
            "Please disregard; this is not a news update."
        )
        fallback = (
            "TEST POST — Social Media News Digest\n\n"
            "This is a text-only test of LinkedIn publishing. Please disregard."
        )
        post_to_linkedin(text, image_path=banner, fallback_text=fallback)
    else:
        text = (
            "TEST POST — Social Media News Digest\n\n"
            "This is a text-only test of LinkedIn publishing. Please disregard; "
            "this is not a news update."
        )
        post_to_linkedin(text)


def main():
    print(f"Digest run mode: {DIGEST_TEST_MODE}")

    # Isolated test modes exit before RSS, Anthropic, history, HTML, Facebook,
    # or normal WhatsApp processing can run.
    if DIGEST_TEST_MODE == "email_test":
        run_email_test()
        return
    if DIGEST_TEST_MODE == "linkedin_text_test":
        run_linkedin_test(with_banner=False)
        return
    if DIGEST_TEST_MODE == "linkedin_banner_test":
        run_linkedin_test(with_banner=True)
        return
    if DIGEST_TEST_MODE != "full":
        raise ValueError(
            f"Unknown DIGEST_TEST_MODE '{DIGEST_TEST_MODE}'. "
            "Use full, email_test, linkedin_text_test, or linkedin_banner_test."
        )

    today_articles = fetch_recent_articles()
    for article in today_articles:
        summaries = summarize_article(article)
        article["summary"] = summaries["long"]
        article["li_summary"] = summaries["short"]
        article.pop("raw_summary", None)

    history = update_history(today_articles)
    html_page = build_html(history)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(os.path.join(OUTPUT_DIR, "index.html"), "w", encoding="utf-8") as html_file:
        html_file.write(html_page)

    if today_articles:
        banner = make_banner_image()
        post_to_linkedin(
            build_linkedin_post(today_articles, with_header=False),
            image_path=banner,
            fallback_text=build_linkedin_post(today_articles, with_header=True),
        )
        post_to_facebook(
            build_linkedin_post(today_articles, with_header=False),
            image_path=banner,
            fallback_text=build_linkedin_post(
                today_articles, include_url=False, with_header=True
            ),
        )
        send_whatsapp_nudge(today_articles, banner)
    else:
        print("No qualifying articles today — skipping LinkedIn and Facebook posts.")

    print(
        f"Digest built with {len(today_articles)} article(s) today, "
        f"{len(history)} day(s) in history."
    )


if __name__ == "__main__":
    main()
