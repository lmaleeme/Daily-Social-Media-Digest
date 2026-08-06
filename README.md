# Daily Social Media News Digest

Fetches social-media-relevant news from your RSS sources, summarizes each in one
tight sentence, publishes a page (for embedding in Zoho Sites), and posts the
highlights to LinkedIn — automatically, every day.

## How it works

```
RSS feeds → filter for social/platform relevance → summarize (Claude) →
  docs/index.html (published via GitHub Pages) + LinkedIn post
```

## One-time setup (about 15–20 minutes)

### 1. Create the GitHub repo
1. Create a new **public** repo on GitHub (Pages requires public on the free tier, or
   use GitHub Pro for a private one).
2. Upload these files, keeping the folder structure:
   - `main.py`
   - `.github/workflows/daily-digest.yml`
   - `README.md`

### 2. Turn on GitHub Pages
1. In the repo: **Settings → Pages**.
2. Under "Build and deployment", set **Source: Deploy from a branch**.
3. Branch: `main`, folder: `/docs`. Save.
4. GitHub gives you a URL like `https://yourname.github.io/repo-name/` — that's
   your digest page.

### 3. Get an Anthropic API key (for summarization)
1. Go to https://console.anthropic.com → **API Keys** → create one.
2. In your repo: **Settings → Secrets and variables → Actions → New repository secret**.
   - Name: `ANTHROPIC_API_KEY`, value: your key.

### 4. Get LinkedIn API access (for posting)
LinkedIn's posting API requires an approved app — this is the fiddliest part:
1. Create an app at https://www.linkedin.com/developers/apps.
2. Request the **"Share on LinkedIn"** product (or **"Community Management API"**
   for a company page) — approval can take a bit, and LinkedIn has tightened
   access for new apps, so check current requirements in their docs.
3. Complete OAuth to get a **user access token** with the `w_member_social` scope
   (or the org equivalent for a company page).
4. Get your **author URN**:
   - Personal profile: `urn:li:person:{your-id}`
   - Company page: `urn:li:organization:{your-page-id}`
5. Add two more repo secrets:
   - `LINKEDIN_ACCESS_TOKEN`
   - `LINKEDIN_AUTHOR_URN`

Access tokens expire (personal ones ~60 days) — you'll need to refresh this
periodically unless you set up the refresh-token flow.

### 5. Set your published page URL
**Settings → Secrets and variables → Actions → Variables tab → New variable**:
- Name: `PUBLISHED_PAGE_URL`, value: your GitHub Pages URL from step 2.
(This just gets included as a link in the LinkedIn post.)

### 6. Embed the digest in Zoho Sites
In Zoho Sites, add an **HTML/iframe embed block** with:
```html
<iframe src="https://yourname.github.io/repo-name/" width="100%" height="800" style="border:none;"></iframe>
```

### 7. Test it
In GitHub: **Actions tab → Daily Social Media Digest → Run workflow** — this
triggers it manually so you can confirm everything works before waiting for
the schedule.

## Customizing

Open `main.py` and edit the config block at the top:
- `RSS_FEEDS` — add more sources (Social Media Today, Adweek, etc.)
- `SOCIAL_KEYWORDS` — tune what counts as "social media relevant" news
- `MAX_ARTICLES` — how many stories per digest
- `LOOKBACK_HOURS` — the article recency window
- the cron line in `daily-digest.yml` — what time (UTC) it runs daily

## Running locally (to test before deploying)

```bash
pip install feedparser requests
export ANTHROPIC_API_KEY=sk-ant-...
python main.py
```

This writes `docs/index.html` and `docs/latest.json`, and prints the LinkedIn
post text (it'll only actually post if LinkedIn credentials are set).
