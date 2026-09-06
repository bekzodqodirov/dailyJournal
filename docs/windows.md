# Running MIYA on Windows

`make` does not exist on Windows, so run the same steps directly. In
`cmd.exe`, from the repository root:

```bat
copy .env.example .env
notepad .env
docker compose up -d db api
docker compose run --rm api alembic upgrade head
docker compose up -d bot worker userbot
curl http://127.0.0.1:8000/health
```

The PowerShell equivalents differ in two places: use `curl.exe`, not `curl`
(which is an alias for `Invoke-WebRequest`), and `Select-String` in place of
`findstr`.

## Port 5432 is often already taken

A locally installed PostgreSQL holds it, and the `db` container then fails to
bind:

```
Error response from daemon: failed to set up container networking:
Bind for 0.0.0.0:5432 failed: port is already allocated
```

The damage is worse than it looks. Docker leaves that container **running and
healthy but never attaches it to the compose network**, so the next command
fails somewhere else entirely:

```
psycopg.OperationalError: [Errno -5] No address associated with hostname
```

That is `db` failing to resolve, not a database problem. `docker compose ps`
gives it away: the healthy `db` row shows `5432/tcp` instead of
`127.0.0.1:5432->5432/tcp`, meaning it has no published port and no network.

Set `DB_PORT` in `.env` to a free port and start over:

```bat
docker compose down
netstat -ano | findstr :5432
rem add DB_PORT=5433 to .env
docker compose up -d db api
```

`DB_PORT` changes only the host binding, which exists for running `psql` from
outside the containers. The containers themselves always reach the database as
`db:5432`, so `DATABASE_URL` must not be touched.

## Line endings

`.env` must use LF. A checkout made before this repository had a
`.gitattributes` can still hold CRLF files, `.env` inherits them from
`.env.example`, and Compose passes the trailing `\r` straight into every
value. A bot token with an invisible carriage return fails to authenticate
against Telegram with no useful error anywhere — the worst failure mode there
is for a first-time setup.

To check, print the environment as the container sees it:

```bat
docker compose run --rm --no-deps api sh -c "env | grep DATABASE_URL | cat -A"
```

`cat -A` marks the end of each line with `$`. A `^M$` means CRLF; a bare `$` is
clean. To repair such a checkout, make git re-materialise every file under the
current `.gitattributes`:

```bat
git rm --cached -r .
git reset --hard
```

Then recreate `.env` from the freshly normalised `.env.example`.

## Slow connections

The first `docker compose build` downloads a few hundred megabytes and can take
15 minutes or more. It is designed to survive interruption: pip keeps a
BuildKit cache mount, so a build that dies half way keeps every wheel it
already fetched and re-running `docker compose build` resumes rather than
starting over.
