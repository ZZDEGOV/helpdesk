# Changelog

## v3.0 — Team server

Runs in Docker on the lab server instead of one person's PC. No schema change;
existing databases load as-is.

### Ticket visibility
Users now see only the tickets they own. Admins and root see all. A ticket you
can't see returns *Not found* everywhere — its page, every action on it, the API,
and timeline/question actions (which used to trust a hidden `ticket_id` form field).
Dashboard counts, follow-ups, customer pages and a ticket's "other tickets" list
are scoped the same way. The customer directory itself stays shared.

### Fixes
- `{{owner}}` in saved replies is now the person drafting the reply. It used to be
  the global `HELPDESK_OWNER` setting, so every reply was signed "Me".
- Two people creating tickets at the same moment could both get the same
  `HD-YYYY-NNNN` ref, and one would fail. Ref allocation now holds the write lock.
- Bad form input (e.g. an unknown status) shows an error page instead of a crash.
- The test suite could wipe the real database: some test files imported the app
  before redirecting it to a temp file. `tests/conftest.py` now redirects first and
  refuses to run against `data/`.

### Structure
- `main.py` split into routers (`app/routers/`). Access rules are FastAPI
  dependencies declared on each route (`deps.py`) instead of a path-matching
  middleware.
- Form bodies and API responses are Pydantic models (`schemas.py`); settings use
  `pydantic-settings`. `/docs` documents every route — sign-in required.
- New read-only JSON API under `/api/v1`.
- Migrations run at startup (lifespan), not at import. An hourly in-process sweep
  purges expired trash and sessions instead of doing it on page loads.
- Daily automatic backups (`python -m app.backup`), and `/health` for Docker.

### Removed
`run.py`, `run.bat`, `update.ps1`, and the retired settings `HELPDESK_PASSWORD`,
`HELPDESK_SECRET_KEY`, `HELPDESK_OWNER`, `HELPDESK_HOST`, `HELPDESK_PORT`.
Dependencies updated to current releases; dev-only ones moved to
`requirements-dev.txt`.

## v2.1 — Deletion permissions

Deletion is now split three ways. **Striking through timeline entries is unaffected** —
it isn't deletion, and remains available to everyone who works tickets.

| | User | Admin | Root |
|---|---|---|---|
| Strike through a timeline entry | yes | yes | no (read-only) |
| Queue a ticket/customer for deletion | **no** | yes | no |
| Restore from the queue | no | yes | yes |
| See the deletion queue | **no** | yes | yes |
| Delete immediately and permanently | no | **no** | yes |
| Delete a user account | no | no | yes |

### User
Deletion removed entirely — the delete panels and the Trash link are gone, and the
routes return 403 rather than merely hiding the buttons.

### Admin
Queues a deletion: the record moves to the trash, stays restorable, and is purged after
the retention window. The "Delete now" button in the trash is root-only.

### Root
- **All tickets** page: every ticket including queued ones, with immediate permanent
  deletion. Useful for clearing test data.
- **All customers** page: read-only, with immediate deletion that takes the customer's
  tickets with them.
- **Account deletion**, with a dropdown to reassign the departing user's tickets to
  someone else or leave them unowned.

Root's instant deletes live under `/root/...` rather than on the ticket pages, so the
v2.0 rule that root cannot POST to any ticket route is still enforced unchanged.

**Guards:** you can't delete the account you're signed in as, and the last remaining root
can be neither deleted nor demoted. Deleting a user keeps their timeline entries and
audit rows — the history survives, the link to the account is nulled.

Every destructive action is written to the audit log.


## v2.0 — Accounts

**Migrates automatically on startup.** All existing tickets, customers, and timeline
entries are preserved and assigned to `ZachZ`.

### Sign-in replaces the shared password
- Real accounts: username, display name, password. No email anywhere.
- Passwords hashed with scrypt (standard library), unique salt per user.
- **First run** shows a setup page that creates the root account. The old
  `HELPDESK_PASSWORD` setting is retired.
- Sessions live in the database, so restarting the server no longer signs everyone out.
- Accounts lock for 15 minutes after 5 failed attempts.
- Login errors don't reveal whether a username exists.

### Roles
| Role | Can do |
|---|---|
| `root` | Manage accounts, view analytics and the audit log. Reads tickets but cannot change them. |
| `admin` | Full ticket access. |
| `user` | Full ticket access. (Scoping arrives in v2.1.) |

Root's read-only status is enforced in the middleware — every mutating request outside
`/root` is refused with a 403, not merely hidden in the UI. Root's ticket views are
written to the audit log.

The last remaining root account can't be demoted or disabled.

### Passwords without email
Root sets a temporary password; the account is flagged and the user must choose their
own at first sign-in. Root never learns anyone's working password. Resets follow the
same path and sign the user out of all sessions.

### Root dashboard
Per-user open, overdue, resolved-in-7-days, median first reply, median resolution, and
follow-ups due. Response times use the pausable clock, so time blocked on someone else
doesn't count against anyone. Plus account management and an audit log.

### Attribution
Every timeline entry now records who created it, via a request-scoped context variable.
Tickets have an `owner_id`, set to whoever created them.

### New status: Waiting on external
For vendors like Bytemark, as distinct from another internal team. The clock pauses
while in this status, and an inbound vendor message clears it back to Open.

**Adding a status is now a config-only change.** Previously the status list was
duplicated inside SQL string literals, so a new status was accepted by the database but
silently dropped from the default ticket view. All those filters are now generated from
`config.OPEN_STATUSES` and `config.ACTIVE_STATUSES`. The `CHECK` constraints have also
been removed from the tickets table — SQLite can't alter them, so they made every future
status change require a table rebuild. Validation moved to `repo.set_status`, which reads
the list from config. A test asserts that every status in `OPEN_STATUSES` appears in the
Open filter, so this specific regression can't recur.

### To add a status
1. Add the key to `config.STATUSES` and a label to `config.STATUS_LABELS`
2. Add it to `OPEN_STATUSES` if it counts as unfinished
3. Add it to `ACTIVE_STATUSES` only if the SLA clock should keep running
4. Optionally add a `.s-yourstatus` colour rule in `style.css`

Nothing else needs touching.

## v1.2
Unified correspondence timeline, thread splitter, open questions, follow-ups, multiple
customer contacts, merge, soft delete with retention.

## v1.1
Blank filter fix, inbound reply logging, migration runner.

## v1.0
Ticket tracker, pausable SLA clock, customers, saved replies.
