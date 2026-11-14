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

## Member news (`2026-11-member-news.json`)

108 community news items (checked 14 Nov 2026) for seven named members to post as themselves, 10–20 each: party
campaigns (APC, ADC, NDC, APM, PDP, SDP, LP, AAC, APGA, YPP), INEC and the courts, police and election security,
state races (Lagos, Rivers, Oyo, Kano, Plateau, Gombe, Adamawa and more), and fact-checks of viral election claims.
None repeats a story in the official file above (no shared source link).

The authors are written as handles `m1`–`m7` (and the reviewers as `m7`, with `m6` for m7's own items), so no
member's email address is stored in this public repository. Map them when you run it:

    A=m1=<email>,m2=<email>,m3=<email>,m4=<email>,m5=<email>,m6=<email>,m7=<email>
    python manage.py import_member_news --authors "$A" --dry-run --list   # check accounts and report, saves nothing
    python manage.py import_member_news --authors "$A"                    # post and approve
    python manage.py import_member_news --authors "$A" --pending          # post only; approve in the app (3 per author)
    python manage.py import_member_news --authors "$A" --show-names       # show each author's name on their articles

- Each item goes through `news.services.submit` as its author (profile complete, not paused, declarations, length,
  https source) and then `services.approve` by the file's `reviewer` — or the `second_reviewer` for the reviewer's own
  items, because nobody reviews their own article. Both must be news reviewers or system admins
  (`--review-as` / `--second-reviewer`, a handle or an email, override the file). Approval notifications are not sent.
- Every account is checked before anything is saved; a missing author or an unfinished profile stops the run.
- Bylines stay hidden ("a member") unless `--show-names` is given, as in the app.
- Idempotent by author + title: an item that author already posted (any status) is left alone.
- Same research rules as above: own words, sources linked, allegations attributed with the response, neutral.
