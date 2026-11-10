# Candidate data files

`ndc_2027.json` holds researched profiles of Nigeria Democratic Congress candidates for the 2027 general election
(checked 10 Nov 2026). Load it with:

    python manage.py import_candidates --dry-run --list   # check and report, saves nothing
    python manage.py import_candidates                    # import

## What the import does

- Every record becomes a **draft, not verified**, through `candidates.services.create_candidate` (same validation, slugs
  and audit entries as a profile an admin types in). Nothing is verified or published: an admin checks each draft and
  uses the normal Verify → Publish buttons.
- The research record (status, confidence, sources, what's missing, flags) goes into `verification_notes`, which is
  internal and never in the public API. The admin candidate page shows it as "Research notes to check before verifying".
  Verifying replaces it with the admin's own note.
- Idempotent: each record has a stable `key`, written into the notes as `import-key: ndc-2027:<key>`. Running again
  updates only the importer's own unreviewed drafts. It never touches a record that has been verified, published,
  unpublished or archived, or one an admin made by hand for the same name, office, year and state.
- The whole file is checked before any write (positions, states, year, limits, https links, duplicate keys or people,
  at least one source), and the import runs in one transaction: one bad record means nothing is saved.

## How the research was done

- Sources: INEC's final governorship list as reported on 11 Oct 2026, and national news reports (Channels TV, The
  Guardian, ThisDay, Vanguard, Leadership, Daily Trust, BusinessDay, Peoples Gazette, NAN, Legit.ng, Blueprint and
  others). Each record lists its sources.
- Only facts the sources state. Empty biography or priorities mean nothing was documented; nothing was filled in.
  Biographies and summaries are written in our own words. Links are the news reports used; no social accounts were
  added because none could be confirmed.
- `research.status`: `inec_listed` (on INEC's published list), `party_announced` (primary result or party statement in
  the news), `disputed` (a court case, rival claimant or conflicting report). `disputed` and `low` confidence records are
  "flagged for careful review" in the command's output.
- `not_imported` lists people we looked at and left out, with the reason (not on INEC's list, conflicting reports,
  factional lists, no Vice President position in the model). No LGA chairman or councillor candidates were found:
  local elections are run by each State Independent Electoral Commission on its own timetable.

Facts change quickly during a campaign (substitutions, court rulings). Check each record against INEC's current list
before verifying it.
