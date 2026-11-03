# Backups and restore tests (spec 14 §4.11)

## What is backed up

| What | How often | Kept | Notes |
|---|---|---|---|
| PostgreSQL database | daily, encrypted | 35 days | `pg_dump --format=custom`, encrypted before it leaves the server |
| Private media (`PRIVATE_MEDIA_ROOT`: member photos, legal-notice attachments) | daily, encrypted | 35 days | same tool and key as the database |
| Public media (`MEDIA_ROOT`) | daily | 35 days | public anyway; backed up for convenience |

Backups live in a different account or provider region from the servers, with write-once (object lock) storage where available.

## Keys

- `PII_ENCRYPTION_KEYS` (VIN/NIN encryption) and the **backup encryption key** are stored in the secret manager, **never** next to the backups. A stolen backup alone reveals no NIN or VIN.
- At least two named people can reach both keys. Rotating `PII_ENCRYPTION_KEYS`: put the new key first and keep the old one until every row is re-encrypted.

## Restore test (quarterly)

1. Create a scratch database (never the live one) and restore the latest backup into it:
   `pg_restore --no-owner --dbname=$SCRATCH_DATABASE_URL latest.dump`
2. Point a shell at it: `DATABASE_URL=$SCRATCH_DATABASE_URL python manage.py check` and `python manage.py migrate --plan` (no unapplied migrations expected).
3. Compare row counts for the main tables (users, follows, memberships, donations, audit log) with the live database at the backup's time. They should match within the day's activity.
4. Run `DATABASE_URL=$SCRATCH_DATABASE_URL python manage.py verify_audit` (the chain must be intact).
5. Restore one private media file and open it.
6. Drop the scratch database and delete any downloaded copies.
7. Record it on the live system:
   `python manage.py restore_drill --ok "Restored 2 Nov backup to scratch; counts matched; audit intact"`

The admin readiness panel warns when the last recorded restore test is older than 100 days. Notes must not contain personal data.

## If a backup is lost or exposed

Log it as an incident at `/admin/privacy/incidents` (data categories: everything in the backup; processor: hosting). The NDPC clock starts when you became aware.
