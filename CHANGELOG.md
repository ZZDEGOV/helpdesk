# Changelog

## v3.4 — Team workflow

Schema version 6 (applied automatically on start): `tickets.assignee_id` (the
column was reserved since v1 and is reused if present), and new `saved_views` and
`ticket_presence` tables. Existing tickets start unassigned; nobody loses access to
anything.

### Assignment
- *Logged by* (the owner, who created the ticket) and *assigned to* (who is working
  it) are now separate. Set it from the ticket's **People** panel (or **Assign to
  me**), or straight away on the new-ticket form.
- A user can give a ticket to anyone who can already see it: its owner, its group's
  members, or an admin. An admin can give it to anyone who works tickets. Anyone who
  can work it can reassign it. Every change is on the timeline.
- Being assigned a ticket lets you see it, so a hand-off survives the ticket leaving
  the group. Deleting an account unassigns its tickets.
- The ticket list has an *Assigned* column and an assignee filter. Digest emails gain
  an *Assigned* column, and the *team* scope includes tickets assigned to the person.

### A simpler ticket list
- The six stat cards are replaced by one row of **view tabs** with live counts:
  *All open*, *Needs action*, *Assigned to me*, *Unassigned*, *Overdue*, *Untouched
  3+ days*. Anyone can save the current filters as their own tab (up to 20).
- Search, status and sort are always shown. Priority, category, group, assignee,
  last activity and overdue sit under **Filters**, with a count of how many are set.
  Filters apply as you pick them; there's no Apply button.
- Follow-ups due are a slim banner instead of a panel.
- Fewer columns: ref and details sit under the subject, and priority is only flagged
  when it's P1/P2. Narrower screens (a portrait monitor, a half-width window) drop
  *Active time*, then *Customer*, and phones get one card per ticket.
- The "Resolved / 7d" figure moved off this page; it's still on root's dashboard.

### Bulk actions
Tick tickets on the list (shift-click for a range) and a bar appears to set status,
assign, share with a group, or add a tag. Picking from a menu applies it straight away.
Each ticket gets the same permission checks as its own page; the rest are skipped
and counted in the confirmation.

### Ticket page
- **People** panel: *Assigned to* and *Shared with* together, saved as soon as you
  pick.
- "Paste a whole thread" folds into the correspondence panel.
- Picking a saved reply loads it without a separate Load button.
- The stylesheet URL now changes with each release, so nobody sees new pages with
  old cached styles after an upgrade.

### "Someone else is here"
An open ticket page checks in every 20 seconds. It shows who else has the ticket
open, warns when one of them has a reply in progress, and says when someone else has
added to the ticket since you opened it. The ticket list marks tickets someone is
viewing. Root's views are not shown to others.

### Accessibility
- Skip-to-content link and labelled navigation landmarks.
- Labels on every filter, timeline and ticket-page field that only had a placeholder.
- The lightest grey text now meets WCAG AA contrast.
- The `n` and `/` shortcuts no longer fire with Ctrl/Alt/⌘, so Ctrl+N opens a
  browser window again.
- Overdue dates are announced as overdue, not just shown in red.

### Wording
"Owner" is shown as *Logged by*, and the list filter *Mine only* is now
*Logged by me*, to keep it distinct from *Assigned to me*.

## v3.3 — Groups and per-person digest schedules

Schema version 5 (applied automatically on start): new `user_groups` and
`group_members` tables, a `group_id` on tickets, and a schedule on each digest
subscriber. No existing ticket changes visibility until someone shares it.

### Groups
Root creates groups (teams) and chooses their members under **Root → Groups**.
A ticket filed under a group can be seen and worked by all of its members;
a ticket in no group stays private to its owner and the admins, as before.
The new-ticket form and each ticket page have a *Shared with* box, and the ticket
list can be filtered to *Mine only* or one group. Root can share a member's
existing tickets with a group in one step.

### Digest
- **Each subscriber has their own schedule**, edited in place on the Email digest
  page. The single shared schedule is gone; existing subscribers keep it as their
  starting point.
- New scope: *their own and their groups' tickets*.
- **Group digests:** a group can have its own schedule, sent to every member with
  an email address and listing that group's open tickets.
- New-ticket emails now reach anyone whose settings let them see the ticket, and
  nobody gets two emails about the same ticket.

## v3.2 — Drop-folder delivery

New `HELPDESK_MAIL_MODE=folder`: each digest email is written as a `.json` file
(`to`, `subject`, `html`) into `HELPDESK_MAIL_FOLDER`. With a OneDrive-synced folder
mounted there, a Power Automate *When a file is created* flow sends the email and
deletes the file. For networks where an HTTP-triggered flow isn't permitted. The
Email digest page shows how many emails are still waiting to be picked up. No schema
change.

## v3.1 — Open-ticket digest

Schema version 4 (applied automatically on start): accounts gain an optional email
address, and there is a new `digest_subscribers` table.

### Email digest
Root can email a list of the open tickets to chosen accounts (**Root → Email
digest**). Root adds each subscriber by hand and picks what they see: every open
ticket, or only their own. It goes out on the weekdays and hour root sets, on
**Send now**, and optionally when a ticket is created. **Preview** shows a
subscriber's email without sending it.

Delivery is set by `HELPDESK_MAIL_MODE`: `outbox` (the default: writes `.eml` files,
sends nothing), `webhook` (a Power Automate flow) or `smtp`.

### Staging safety
`HELPDESK_ENV=staging` puts a banner on every page and forces mail into the outbox,
so a staging copy of production data cannot email anyone.

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
