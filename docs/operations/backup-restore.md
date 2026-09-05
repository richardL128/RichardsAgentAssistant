# LifeAgent backup and restore

LifeAgent uses host-run encrypted PostgreSQL backups for data that must survive
the Docker stack being down. The default schedule is a macOS launchd job at
03:00 America/Toronto. Artifact retention runs separately inside Procrastinate
at 03:30 local time.

## Files and defaults

- Backup directory: `/Users/richardliu/Backups/LifeAgent`
- Backup env file: `/Users/richardliu/.config/lifeagent/backup.env`
- Restore identity file: `/Users/richardliu/.config/age/keys.txt`
- Encryption: `age` public-key encryption
- Retention: encrypted daily backups older than 30 days are deleted

The env file is outside the repo because it can contain deployment credentials.
It should be readable only by Richard:

```sh
mkdir -p /Users/richardliu/.config/lifeagent
chmod 700 /Users/richardliu/.config/lifeagent
cat >/Users/richardliu/.config/lifeagent/backup.env <<'EOF'
DATABASE_URL=postgresql://lifeagent:lifeagent@localhost:5432/lifeagent
LIFEAGENT_BACKUP_DIR=/Users/richardliu/Backups/LifeAgent
LIFEAGENT_AGE_RECIPIENT=age1...
EOF
chmod 600 /Users/richardliu/.config/lifeagent/backup.env
```

Create the age identity outside the repo and record only the public recipient in
the backup env file:

```sh
mkdir -p /Users/richardliu/.config/age
chmod 700 /Users/richardliu/.config/age
age-keygen -o /Users/richardliu/.config/age/keys.txt
chmod 600 /Users/richardliu/.config/age/keys.txt
```

## Manual backup

Validate the destination without writing a backup:

```sh
scripts/backup_database.sh --dry-run
```

Run the backup:

```sh
scripts/backup_database.sh
```

Override any default for a one-off run:

```sh
scripts/backup_database.sh \
  --database-url 'postgresql://lifeagent:lifeagent@localhost:5432/lifeagent' \
  --output-dir /Users/richardliu/Backups/LifeAgent \
  --recipient 'age1...' \
  --retention-days 30
```

The script writes `lifeagent-YYYYMMDDTHHMMSS-0400.dump.age` files using
`pg_dump --format=custom`, then encrypts the stream with `age`.

## launchd schedule

Install the included user launch agent:

```sh
cp scripts/com.richard.lifeagent.backup.plist \
  /Users/richardliu/Library/LaunchAgents/com.richard.lifeagent.backup.plist
launchctl bootstrap gui/$(id -u) \
  /Users/richardliu/Library/LaunchAgents/com.richard.lifeagent.backup.plist
launchctl enable gui/$(id -u)/com.richard.lifeagent.backup
launchctl kickstart -k gui/$(id -u)/com.richard.lifeagent.backup
```

Check logs:

```sh
tail -n 100 /Users/richardliu/Library/Logs/lifeagent-backup.log
tail -n 100 /Users/richardliu/Library/Logs/lifeagent-backup.err.log
```

Remove the schedule:

```sh
launchctl bootout gui/$(id -u) \
  /Users/richardliu/Library/LaunchAgents/com.richard.lifeagent.backup.plist
rm /Users/richardliu/Library/LaunchAgents/com.richard.lifeagent.backup.plist
```

## Restore drill

Restore into a disposable database first. This keeps the running development
database out of the blast radius.

```sh
createdb lifeagent_restore
scripts/restore_database.sh \
  --backup-file /Users/richardliu/Backups/LifeAgent/lifeagent-YYYYMMDDTHHMMSS-0400.dump.age \
  --target-database-url 'postgresql://lifeagent:lifeagent@localhost:5432/lifeagent_restore' \
  --confirm-target-db lifeagent_restore \
  --dry-run
```

If the dry run passes, perform the restore:

```sh
scripts/restore_database.sh \
  --backup-file /Users/richardliu/Backups/LifeAgent/lifeagent-YYYYMMDDTHHMMSS-0400.dump.age \
  --target-database-url 'postgresql://lifeagent:lifeagent@localhost:5432/lifeagent_restore' \
  --confirm-target-db lifeagent_restore
```

Sanity-check durable tables:

```sh
psql 'postgresql://lifeagent:lifeagent@localhost:5432/lifeagent_restore' \
  -c "SELECT count(*) FROM agent_runs"
psql 'postgresql://lifeagent:lifeagent@localhost:5432/lifeagent_restore' \
  -c "SELECT count(*) FROM audit_events"
psql 'postgresql://lifeagent:lifeagent@localhost:5432/lifeagent_restore' \
  -c "SELECT count(*) FROM deliveries WHERE receipt_artifact_key IS NOT NULL"
```

The restore script refuses to restore into the current `DATABASE_URL` by
default. That check compares the normalized database identity instead of raw
URL text: PostgreSQL driver variants such as `postgresql+psycopg`, query
parameters, passwords, default port `5432`, and loopback spellings such as
`localhost` and `127.0.0.1` do not bypass the guard. If Richard intentionally
wants to overwrite the running database, he must pass both
`--confirm-target-db lifeagent` and `--allow-current-database`.
