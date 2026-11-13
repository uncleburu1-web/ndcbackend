# News data files

- `2026-11-election-news.json` — 50 researched 2027 election news items (checked 12 Nov 2026): INEC preparations, the
  voters' register and PVCs, the courts and tribunals, candidate lists, campaigns across the parties, peace and security,
  and the 2026 Ekiti and Osun governorship results.
- `ndcconnect-announcements.json` — 3 announcements about NDCConnect itself (welcome, PVC, new Settings), each with a
  graphic in `images/` (1600 × 900 PNG, drawn by `make_graphics.py`; no photos of people, no third-party marks).

Load them with a system admin's email:

    python manage.py import_news --as admin@example.com --dry-run --list        # check and report, saves nothing
    python manage.py import_news --as admin@example.com                         # 50 news items
    python manage.py import_news --as admin@example.com --announcements         # 3 announcements with images

## What the import does

- Each item is published as an official article through `announcements.publish(..., notify=False)`: same validation,
  source rule and audit entry as one typed in by an admin. **Nobody is notified.**
- News items keep their source's date (08:00 Lagos time), so the feed stays in real order. Announcements are published
  now, the first in the file newest.
- Idempotent: a news item is matched by `source_url`, an announcement by its title, against every official article
  (any status). A match is left exactly as it is, so an admin's later edit or removal is never undone.
- The whole file is checked before anything is saved (https source links, known states, categories, lengths, dates
  not in the future, images next to the file).
- Images go through `news.services.set_image` with local media, or a server-side Cloudinary upload (as
  `migrate_media_to_cloudinary` does) when `MEDIA_BACKEND=cloudinary`.

## How the research was done

- Only facts the cited source states, written in our own words; every item links to its source. Allegations are
  attributed and denials or the other side's response included where the source gives them.
- Neutral across parties: APC, ADC, NDC, APM, PDP, LP and AAC campaign items all appear, plus INEC, the courts and
  civil society. No item says who should win.
- `election_updates` (Official Election Updates) is used for INEC and court facts; everything else is
  `election_news` (General Election News).
