# Help Desk — Phase 1

A standalone ticket tracker. No email integration; everything is entered by hand.
Phase 2 adds Power Automate intake, Phase 3 adds sending.

---

## Setup

Run these on the machine that will **host** the app.

**1. Get the code onto the machine.** Unzip to `C:\dev\helpdesk`.

**2. Open PowerShell in that folder** (Shift + right-click → "Open PowerShell window here").

**3. Check Python:**
```powershell
python --version
```
Needs 3.11 or higher. If it's missing, install from python.org and tick
**"Add Python to PATH"** during install.

**4. Create the virtual environment:**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```
If you get an execution-policy error:
```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```
then run the activate line again. Your prompt should now start with `(.venv)`.

**5. Install dependencies:**
```powershell
pip install -r requirements.txt
```

**6. Create your config:**
```powershell
Copy-Item .env.example .env
notepad .env
```
Set `HELPDESK_PASSWORD` to something only you know. Leave the rest as-is for now.

**7. Allow the port through the firewall.** This is what lets your other machine
connect. Run PowerShell **as Administrator**:
```powershell
New-NetFirewallRule -DisplayName "Help Desk" -Direction Inbound -LocalPort 8000 `
  -Protocol TCP -Action Allow -Profile Domain,Private
```
Note `-Profile Domain,Private` — this deliberately does not open the port on public
networks.

**8. Start it:**
```powershell
python run.py
```
Or double-click `run.bat`. It prints both URLs — the `localhost` one for this machine,
and a `192.168.x.x` one to use from your other machine.

**9. Confirm it works from the other machine** by opening that second URL.

Leave the window open while you're using it. Closing it stops the server; your data is
safe in `data/helpdesk.db`.

---

## Daily use

| Action | Where |
|---|---|
| Log a new ticket | **+ New ticket**, or press `n` |
| Find something | Search box, or press `/` |
| See what needs work | "Needs action" card on the dashboard |
| Change status | Buttons at the top of a ticket |
| Record research | "Add a note" |
| Draft a reply | Load a saved reply → Copy → paste into Outlook |
| Record that you replied | "Log as sent" |

**Statuses and the clock.** The active-time bar only runs while a ticket is **New** or
**Open**. Move it to *Waiting on customer* or *Waiting on internal* and the clock
pauses. This is deliberate — time spent blocked on someone else shouldn't count against
you, and it keeps the overdue list trustworthy instead of something you learn to ignore.

**Saved replies** support `{{first_name}}`, `{{full_name}}`, `{{ref}}`, `{{subject}}`,
`{{due_date}}`, `{{owner}}`, `{{category}}`, `{{external_ref}}`. Anything unrecognized
stays visible as `{{like_this}}` so you spot it before sending rather than sending a
sentence with a hole in it.

**Customers** are created automatically from the email address on a ticket. Reuse the
same address and their history links up on its own.

---

## Backups

Everything lives in one file: `data\helpdesk.db`. Copy it somewhere safe on a schedule.
Simplest option — point it at your work OneDrive folder in `.env`:

```
HELPDESK_DB_PATH=C:\Users\you\OneDrive - Org\Helpdesk\db\helpdesk.db
```

That gets you company-controlled storage and version history for free. One caveat: run
the app from **one machine only** if you do this. Two machines writing to a synced
SQLite file will corrupt it.

---

## Notes

**Security.** The app binds to `0.0.0.0`, so anyone on your work LAN can reach the port.
The password gate is what stands between them and customer PII — set it. Sessions are
in-memory, so restarting the server signs you out. That's fine for one user.

**Backwards compatibility.** The schema already has the columns Phase 2 needs
(`external_ref`, `entry_id`, `conversation_id`, `raw`) and an `extractions` table for
tracking suggestion accuracy later. Adding email intake won't require a migration.

**Multi-user later.** There's a `users` table with one row and an `assignee_id` on
tickets, both unused. Phase 5 fills them in rather than restructuring.

---

## Development

```powershell
python -m pytest tests/ -q
```

16 tests, mostly covering the SLA clock — pausing, resuming, and not accruing while
blocked. That logic is easy to break and hard to notice when broken, so run these after
any change to `repo.py`.

```
app/
  config.py      settings from .env
  db.py          connection + init
  schema.sql     tables
  repo.py        all data access, SLA clock
  main.py        routes
  templates/     Jinja2 views
  static/        stylesheet
tests/           pytest suite
run.py           start the server
```
