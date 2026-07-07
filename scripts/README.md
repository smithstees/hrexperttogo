# SEO audit automation

Automated weekly SEO audit for **hrexperttogo.com**, tuned for the target
audience of parents supporting new-grad and early-career job seekers.

## What it does

Runs every Sunday at 12:00 UTC (8am ET in summer, 7am ET in winter) and:

1. **Audits every HTML page** for:
   - Missing / too-short / too-long titles and meta descriptions
   - Missing or duplicate `<h1>`
   - Missing canonical, Open Graph, Twitter, JSON-LD structured data
   - Images without `alt` text
   - Broken internal links
   - Missing pages in `sitemap.xml` and stale `<lastmod>` dates
   - Missing `lang` on `<html>`
2. **Auto-commits safe fixes** to `main`:
   - Canonical, OG, Twitter, `robots` meta tags
   - JSON-LD (`Organization`, `ProfessionalService`, `Person`, `Service`)
   - Alt text derived from image filenames
   - Refreshed `sitemap.xml`
   - `lang="en"` on `<html>`
3. **Opens a pull request** with audience-tuned title / meta description
   changes (anything that alters visible copy) for you to review and merge.
4. **Posts a GitHub Issue** with the full report each run. Because your
   GitHub account already emails you when new issues are opened, the report
   arrives in your inbox with no SMTP setup required.

## Files

- `scripts/seo_audit.py` — the audit + fixer. Supports `--mode safe-fix`,
  `--mode content-pr`, `--mode audit-only`.
- `scripts/requirements.txt` — pinned Python deps.
- `.github/workflows/seo-audit.yml` — the weekly schedule + manual trigger.
- `reports/seo-report.md` and `reports/seo-report.json` — regenerated each run.

## Running locally

```bash
python -m pip install -r scripts/requirements.txt
python scripts/seo_audit.py --mode audit-only     # no changes, just report
python scripts/seo_audit.py --mode safe-fix       # apply mechanical fixes
python scripts/seo_audit.py --mode content-pr     # apply title/desc changes
```

Reports land in `reports/`.

## Email delivery

No secrets required. The workflow posts a GitHub Issue with the full report,
and GitHub emails you whenever a new issue is opened (default notification
settings). To make sure you get the email:

1. Go to <https://github.com/settings/notifications>
2. Under **Watching**, ensure email is enabled for issues in repositories
   you're watching.
3. Confirm you're watching this repo (**Watch** button on the repo page,
   set to **All Activity** or at least **Issues**).

The report is also uploaded as a downloadable workflow artifact each run.

## Target audience keywords

The audit tunes titles and descriptions toward:

- career coaching for college graduates
- career coach for new grads
- help my college graduate find a job
- virtual career coaching
- resume help for recent graduates
- interview coaching for college students
- salary negotiation coaching
- job search coach for early career professionals
- career coach for parents of college students
- SHRM-certified career coach

Edit `AUDIENCE_KEYWORDS` and `CONTENT_SUGGESTIONS` in `seo_audit.py` to
adjust.

## Cadence

Currently **weekly** on Sundays. To switch to bi-weekly, change the cron
expression in `.github/workflows/seo-audit.yml`:

```yaml
# Every other Sunday at 12:00 UTC
- cron: "0 12 * * 0"
```

GitHub cron doesn't support "every other week" natively, so bi-weekly is
best handled by keeping the weekly schedule and only committing when
issues exist — which this script already does.
