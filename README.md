# Transit Systems Help Desk

The team's ticket tracker. Log customer issues, keep the whole email trail on one
timeline, and track response time with an SLA clock that pauses while a ticket is
waiting on someone else.

It runs as a single Docker container on the lab server. Everything is stored in one
SQLite database inside a Docker volume.

---

## Running it

### Prerequisites

Docker Engine with the Compose plugin. Don't use **Docker Desktop**: it needs a paid
license for organizations our size.

- **Linux server:** install the `docker-ce` and `docker-compose-plugin` packages
  ([instructions](https://docs.docker.com/engine/install/)).
- **Windows server:** install Docker Engine inside a WSL2 distro (e.g. Ubuntu) and
  follow the Linux instructions there.

[Portainer CE](https://docs.portainer.io/start/install-ce) is an optional web UI
on top of Docker Engine. To deploy through it, create a *Stack* from this
repository and it will use `compose.yaml`.

### First start

```sh
git clone https://github.com/ZZDEGOV/helpdesk.git
cd helpdesk
docker compose up -d --build
```

Open `http://<server>:8000`. On a brand-new database you're asked to create the
**root** account. Root manages accounts and can read tickets, but can't work them,
so create a separate admin account for yourself afterwards.

To change the port, edit the left side of `"8000:8000"` in `compose.yaml`.

### Bringing over an existing database

On the machine with the old install, take a snapshot. This is safe while the app is
running:

```sh
python -m app.backup create        # writes data/backups/helpdesk-<timestamp>.db
```

Copy that file to the server, then load it into the container:

```sh
docker compose cp helpdesk-<timestamp>.db helpdesk:/data/import.db
docker compose exec helpdesk python -m app.backup restore /data/import.db --force
docker compose exec helpdesk rm /data/import.db
docker compose restart
```

`--force` is needed when the container's database already has tickets in it.
Migrations run automatically on restart.

### Updating

```sh
git pull
docker compose exec helpdesk python -m app.backup create   # just in case
docker compose up -d --build
```

### Backups

The app writes a snapshot to `/data/backups` once a day and keeps the last 14.
Those snapshots live on the **same disk** as the database, so they don't protect
against losing the server. Copy them somewhere else on a schedule:

```sh
docker compose cp helpdesk:/data/backups ./helpdesk-backups
```

To restore one: stop people working, copy it in and run
`python -m app.backup restore … --force`, as in
[Bringing over an existing database](#bringing-over-an-existing-database).

### Configuration

Set these under `environment:` in `compose.yaml`. All are optional.

| Variable | Default | |
|---|---|---|
| `HELPDESK_TIMEZONE` | `America/New_York` | How times are displayed and entered |
| `HELPDESK_WORK_END` | `17` | Hour a target *date* becomes due |
| `HELPDESK_SESSION_DAYS` | `7` | How long a sign-in lasts |
| `HELPDESK_MAX_FAILED_LOGINS` | `5` | Failed attempts before an account locks |
| `HELPDESK_LOCKOUT_MINUTES` | `15` | How long it stays locked |
| `HELPDESK_RETENTION_DAYS` | `7` | Days deleted records stay in the trash |
| `HELPDESK_BACKUP_KEEP` | `14` | Daily snapshots to keep |
| `HELPDESK_SECURE_COOKIES` | `false` | Set `true` once served over HTTPS |
| `HELPDESK_OWNER_HINTS` | | Extra names that mark a pasted email as ours |

The app is served over plain HTTP, so passwords cross the LAN unencrypted. If that
matters for your network, put a reverse proxy with a certificate in front (e.g.
Caddy) and set `HELPDESK_SECURE_COOKIES=true`.

---

## Using it

### Accounts and who sees what

| | Sees tickets | Works tickets | Trash | Accounts |
|---|---|---|---|---|
| **User** | Their own | Yes | No | No |
| **Admin** | All | Yes | Queue deletions, restore | No |
| **Root** | All (read-only, audited) | No | Restore, delete permanently | Yes |

A ticket belongs to whoever created it. The customer directory is shared by
everyone. Root creates accounts with a temporary password, and the user picks their
own at first sign-in. There's no email, so password resets also go through root.

### Statuses and the clock

The active-time clock runs only while a ticket is **New** or **Open**. It pauses
while the ticket is *Waiting on customer / internal / external*, so time spent
blocked on someone else doesn't count against you. Logging an inbound message from
whoever it's waiting on moves it back to Open.

### Day to day

| To… | Use |
|---|---|
| Log a ticket | **+ New ticket**, or press `n` |
| Search | The search box, or press `/` |
| Record an email, call or note | **Log correspondence** on the ticket |
| Import a whole Outlook thread | **Paste a whole thread**: it's split into entries for you to check before saving |
| Fix a wrong entry | **strike** (it stays visible, struck through) or **set time** |
| Track what you asked the customer | **Open questions**, or tick *Track questions asked* when logging |
| Get reminded | **Follow-up** |

**Saved replies** (the *Replies* page) fill in `{{first_name}}`, `{{full_name}}`,
`{{ref}}`, `{{subject}}`, `{{due_date}}`, `{{category}}`, `{{external_ref}}` and
`{{owner}}`, which is your display name. A placeholder that isn't recognized stays
visible as `{{like_this}}`, so you notice it before sending.

### API

After signing in, `/docs` lists every route. The read-only JSON API lives under
`/api/v1` (`/me`, `/tickets`, `/tickets/{id}`). It uses your browser session and
the same visibility rules.

---

## Development

Python 3.14.

```sh
python -m venv .venv
.venv/Scripts/activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload     # http://localhost:8000, data in ./data
python -m pytest tests/ -q
```

The tests always use a throwaway database. `tests/conftest.py` sets that up and
refuses to run against `./data`.

```
app/
  main.py        app assembly: routers, error handling, startup, /docs
  routers/       one module per area (tickets, customers, root, api, ...)
  deps.py        dependencies: DB connection, signed-in user, permissions
  schemas.py     Pydantic models for forms and API responses
  config.py      settings (env vars) and domain constants: statuses, channels
  repo.py        data access and the SLA clock
  auth.py        passwords, sessions, roles, audit log
  migrate.py     schema migrations, applied at startup
  backup.py      snapshot / restore CLI
  web.py         Jinja setup and date helpers
  templates/     pages
tests/
```

**Adding a status** only takes a change to `config.py`. The steps are in the comment
above `STATUSES`. **Changing the schema** means appending a migration to
`migrate.py` and updating `schema.sql`.
