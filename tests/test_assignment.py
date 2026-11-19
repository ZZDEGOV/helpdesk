"""v3.4: assignment, saved views, bulk actions, and the "someone else is here"
presence warning."""
from datetime import datetime, timedelta, timezone

from app import assignment, auth, presence, repo, views
from app.db import connect
from app.migrate import run as migrate

from .conftest import NR, new_ticket
from .test_digest import subscribe, user_id
from .test_groups import team  # noqa: F401  (fixture)


def ticket(tid) -> dict:
    with connect() as conn:
        return dict(repo.get_ticket(conn, int(tid)))


# ---------------------------------------------------------------- assignment

def test_assign_to_a_groupmate(team):
    tid = new_ticket(team["user"], "Reader down", group_id=team["gid"])
    r = team["user"].post(f"/tickets/{tid}/assign",
                          data={"assignee_id": user_id("dclark")}, **NR)
    assert r.status_code == 303
    assert ticket(tid)["assignee_id"] == user_id("dclark")
    with connect() as conn:
        assert any(e["type"] == "assigned" and "dclark" in e["body"]
                   for e in repo.get_events(conn, int(tid)))
    assert "assigned to dclark" in team["other"].get(f"/tickets/{tid}").text
    # The teammate can hand it back: anyone who can work it can reassign it.
    team["other"].post(f"/tickets/{tid}/assign", data={"assignee_id": ""}, **NR)
    assert ticket(tid)["assignee_id"] is None


def test_only_people_who_can_see_it_can_be_assigned(team):
    tid = new_ticket(team["user"], "Reader down", group_id=team["gid"])
    r = team["user"].post(f"/tickets/{tid}/assign",
                          data={"assignee_id": user_id("jlopez")}, **NR)
    assert r.status_code == 403
    assert ticket(tid)["assignee_id"] is None
    # Root can't work tickets, so it is never assignable either.
    assert team["user"].post(f"/tickets/{tid}/assign", data={"assignee_id": user_id("root")},
                             **NR).status_code == 403
    # Admins see everything, so they can always be assigned.
    team["user"].post(f"/tickets/{tid}/assign", data={"assignee_id": user_id("ZachZ")}, **NR)
    assert ticket(tid)["assignee_id"] == user_id("ZachZ")
    with connect() as conn:
        t = repo.get_ticket(conn, int(tid))
        by_user = {u["username"] for u in assignment.assignable(
            conn, t, by=auth.get_user(conn, user_id("mreed")))}
        by_admin = {u["username"] for u in assignment.assignable(
            conn, t, by=auth.get_user(conn, user_id("ZachZ")))}
    assert by_user == {"mreed", "dclark", "ZachZ"}
    assert by_admin == {"mreed", "dclark", "ZachZ", "jlopez"}      # admins: anyone who works tickets


def test_disabled_accounts_are_not_assignable(team):
    tid = new_ticket(team["user"], "Reader down", group_id=team["gid"])
    team["root"].post(f"/root/users/{user_id('dclark')}/update",
                      data={"display_name": "dclark", "role": "user"}, **NR)  # is_active off
    assert team["user"].post(f"/tickets/{tid}/assign", data={"assignee_id": user_id("dclark")},
                             **NR).status_code == 403


def test_assignee_keeps_access_when_the_ticket_leaves_the_group(team):
    tid = new_ticket(team["user"], "Handed over", group_id=team["gid"])
    team["user"].post(f"/tickets/{tid}/assign", data={"assignee_id": user_id("dclark")}, **NR)
    team["user"].post(f"/tickets/{tid}/group", data={"group_id": ""}, **NR)
    assert team["other"].get(f"/tickets/{tid}", **NR).status_code == 200
    assert f"/tickets/{tid}" in team["other"].get("/").text
    assert int(tid) in {t["id"] for t in team["other"].get("/api/v1/tickets").json()}
    # ...and loses it once unassigned.
    team["user"].post(f"/tickets/{tid}/assign", data={"assignee_id": ""}, **NR)
    assert team["other"].get(f"/tickets/{tid}", **NR).status_code == 404


def test_assign_to_me_link(team):
    tid = new_ticket(team["user"], "Up for grabs", group_id=team["gid"])
    page = team["other"].get(f"/tickets/{tid}").text
    assert "Assign to me" in page
    team["other"].post(f"/tickets/{tid}/assign", data={"assignee_id": user_id("dclark")}, **NR)
    assert "Assign to me" not in team["other"].get(f"/tickets/{tid}").text


def test_assign_while_creating(team):
    mine = new_ticket(team["user"], "Doing it myself", assignee_id=str(user_id("mreed")))
    assert ticket(mine)["assignee_id"] == user_id("mreed")
    mate = new_ticket(team["user"], "For dclark", group_id=team["gid"],
                      assignee_id=str(user_id("dclark")))
    assert ticket(mate)["assignee_id"] == user_id("dclark")

    # A user can't hand a ticket to someone outside the group it's shared with...
    r = team["user"].post("/tickets/new", data={"subject": "Nope", "priority": "3",
                                                "assignee_id": user_id("dclark")}, **NR)
    assert r.status_code == 403
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tickets WHERE subject = 'Nope'").fetchone()[0] == 0

    # ...but an admin can assign to anyone, and that person can then see it.
    tid = new_ticket(team["admin"], "From the manager", assignee_id=str(user_id("jlopez")))
    assert ticket(tid)["assignee_id"] == user_id("jlopez")
    assert team["outsider"].get(f"/tickets/{tid}", **NR).status_code == 200
    assert team["other"].get(f"/tickets/{tid}", **NR).status_code == 404


def test_new_ticket_form_offers_people_by_group(team):
    page = team["user"].get("/tickets/new").text
    assert f'<option value="{user_id("dclark")}" data-groups="{team["gid"]}"' in page
    assert "jlopez" not in page                       # not someone mreed could assign
    assert "jlopez" in team["admin"].get("/tickets/new").text


def test_saving_an_unchanged_assignee_is_harmless(team):
    tid = new_ticket(team["user"], "Handed over", group_id=team["gid"])
    team["user"].post(f"/tickets/{tid}/assign", data={"assignee_id": user_id("dclark")}, **NR)
    # dclark leaves the group but stays assigned (so is no longer "assignable")...
    team["root"].post(f"/root/groups/{team['gid']}/members/{user_id('dclark')}/remove",
                      data={}, **NR)
    # ...and pressing Save on the unchanged dropdown doesn't error.
    r = team["user"].post(f"/tickets/{tid}/assign", data={"assignee_id": user_id("dclark")}, **NR)
    assert r.status_code == 303


def test_deleting_an_account_unassigns_its_tickets(team):
    tid = new_ticket(team["user"], "Handed over", group_id=team["gid"])
    team["user"].post(f"/tickets/{tid}/assign", data={"assignee_id": user_id("dclark")}, **NR)
    team["root"].post(f"/root/users/{user_id('dclark')}/delete", data={}, **NR)
    assert ticket(tid)["assignee_id"] is None


def test_list_filters_by_assignee(team):
    mine = new_ticket(team["user"], "Assigned to mreed", group_id=team["gid"],
                      assignee_id=str(user_id("mreed")))
    theirs = new_ticket(team["other"], "Assigned to dclark", group_id=team["gid"],
                        assignee_id=str(user_id("dclark")))
    nobody = new_ticket(team["user"], "Nobody yet", group_id=team["gid"])

    page = team["user"].get("/?assignee=me").text
    assert f"/tickets/{mine}" in page and f"/tickets/{theirs}" not in page
    page = team["user"].get("/?assignee=none").text
    assert f"/tickets/{nobody}" in page and f"/tickets/{mine}" not in page
    page = team["user"].get(f"/?assignee={user_id('dclark')}").text
    assert f"/tickets/{theirs}" in page and f"/tickets/{nobody}" not in page
    # An outsider asking for dclark's tickets still only gets what they can see.
    assert f"/tickets/{theirs}" not in team["outsider"].get(
        f"/?assignee={user_id('dclark')}").text


def test_idle_filter(team):
    old = new_ticket(team["user"], "Gone quiet")
    fresh = new_ticket(team["user"], "Just touched")
    stale = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (stale, int(old)))
        conn.commit()
    page = team["user"].get("/?idle=3").text
    assert f"/tickets/{old}" in page and f"/tickets/{fresh}" not in page


def test_digest_shows_who_is_assigned(team):
    tid = new_ticket(team["user"], "Reader down", group_id=team["gid"])
    team["user"].post(f"/tickets/{tid}/assign", data={"assignee_id": user_id("dclark")}, **NR)
    subscribe(team["root"], "ZachZ")
    page = team["root"].get(f"/root/digest/preview/{user_id('ZachZ')}").text
    assert "ASSIGNED" in page and "dclark" in page


# ---------------------------------------------------------------- saved views

def test_normalize_is_order_and_default_insensitive():
    assert views.normalize("sort=priority&q=fare&status=open") == views.normalize("q=fare")
    assert views.normalize("status=") != views.normalize("")       # all vs. open (the default)
    assert views.normalize("overdue=on&notice=hi&bogus=1") == "overdue=1&status=open"
    assert views.normalize("assignee=me&status=open") == "assignee=me&status=open"


def test_built_in_views_show_counts_and_the_current_one(team):
    new_ticket(team["user"], "Mine", assignee_id=str(user_id("mreed")))
    new_ticket(team["user"], "Nobody's")
    page = team["user"].get("/?assignee=me").text
    assert 'aria-current="page">Assigned to me <span class="n">1</span>' in page
    assert 'Unassigned <span class="n">1</span>' in page
    assert 'aria-current="page">All open' in team["user"].get("/").text
    # Root can't work tickets: no "Assigned to me" for root.
    assert "Assigned to me" not in team["root"].get("/").text


def test_save_and_delete_a_personal_view(team):
    r = team["user"].post("/views", data={"name": "ADA, mine", "query":
                                          "status=open&q=ramp&sort=priority"}, **NR)
    assert r.headers["location"] == "/?q=ramp&status=open"
    page = team["user"].get("/?q=ramp").text
    assert "ADA, mine" in page and 'aria-current="page">ADA, mine' in page
    assert "ADA, mine" not in team["other"].get("/").text            # personal

    # Same name replaces rather than duplicating.
    team["user"].post("/views", data={"name": "ada, MINE", "query": "q=lift"}, **NR)
    with connect() as conn:
        rows = views.saved(conn, user_id("mreed"))
    assert len(rows) == 1 and rows[0]["query"] == "q=lift&status=open"

    # Someone else can't delete it.
    team["other"].post(f"/views/{rows[0]['id']}/delete", data={}, **NR)
    with connect() as conn:
        assert len(views.saved(conn, user_id("mreed"))) == 1
    team["user"].post(f"/views/{rows[0]['id']}/delete", data={}, **NR)
    with connect() as conn:
        assert views.saved(conn, user_id("mreed")) == []


def test_view_limit(team):
    for i in range(views.MAX_VIEWS):
        team["user"].post("/views", data={"name": f"v{i}", "query": f"q={i}"}, **NR)
    r = team["user"].post("/views", data={"name": "one too many", "query": "q=x"}, **NR)
    assert "error=" in r.headers["location"]


# ---------------------------------------------------------------- bulk actions

def test_bulk_status_skips_tickets_you_cannot_see(team):
    a = new_ticket(team["user"], "A", group_id=team["gid"])
    b = new_ticket(team["other"], "B", group_id=team["gid"])
    hidden = new_ticket(team["outsider"], "Not visible to mreed")
    r = team["user"].post("/tickets/bulk", data={
        "ids": [a, b, hidden], "action": "status", "status": "waiting_customer",
        "back": "status=open&q=x"}, **NR)
    assert r.headers["location"].startswith("/?q=x&status=open&notice=")
    assert "2+tickets+updated" in r.headers["location"] and "1+skipped" in r.headers["location"]
    assert ticket(a)["status"] == ticket(b)["status"] == "waiting_customer"
    assert ticket(hidden)["status"] == "new"


def test_bulk_assign_checks_each_ticket(team):
    shared = new_ticket(team["user"], "Shared", group_id=team["gid"])
    private = new_ticket(team["user"], "Private")
    r = team["user"].post("/tickets/bulk", data={
        "ids": [shared, private], "action": "assign", "assignee": str(user_id("dclark"))}, **NR)
    assert "1+skipped" in r.headers["location"]          # dclark can't see the private one
    assert ticket(shared)["assignee_id"] == user_id("dclark")
    assert ticket(private)["assignee_id"] is None

    team["user"].post("/tickets/bulk", data={"ids": [shared, private], "action": "assign",
                                             "assignee": "me"}, **NR)
    assert ticket(shared)["assignee_id"] == ticket(private)["assignee_id"] == user_id("mreed")


def test_bulk_sharing_follows_the_single_ticket_rules(team):
    own = new_ticket(team["user"], "Mine")
    theirs = new_ticket(team["other"], "Theirs", group_id=team["gid"])
    r = team["user"].post("/tickets/bulk", data={
        "ids": [own, theirs], "action": "group", "group": "none"}, **NR)
    assert "1+ticket+updated" in r.headers["location"] and "1+skipped" in r.headers["location"]
    assert ticket(theirs)["group_id"] == team["gid"]       # not mreed's to unshare
    # Can't share into a group you're not in.
    assert team["outsider"].post("/tickets/bulk", data={
        "ids": [own], "action": "group", "group": str(team["gid"])}, **NR).status_code == 403


def test_bulk_tag_and_input_errors(team):
    a = new_ticket(team["user"], "A")
    team["user"].post("/tickets/bulk", data={"ids": [a], "action": "tag", "tag": "Fares"}, **NR)
    with connect() as conn:
        assert repo.get_tags(conn, int(a)) == ["fares"]
    for data in ({"action": "status"}, {"ids": [a], "action": "status"},
                 {"ids": [a], "action": "assign"}, {"ids": [a], "action": "tag", "tag": " "}):
        r = team["user"].post("/tickets/bulk", data=data, **NR)
        assert "error=" in r.headers["location"], data


def test_root_cannot_bulk_edit(team):
    a = new_ticket(team["user"], "A")
    assert team["root"].post("/tickets/bulk", data={"ids": [a], "action": "status",
                                                    "status": "closed"}, **NR).status_code == 403
    assert ticket(a)["status"] == "new"


# ---------------------------------------------------------------- presence

def check_in(client, tid, **data):
    r = client.post(f"/tickets/{tid}/presence", data=data)
    assert r.status_code == 200, r.text
    return r.json()


def test_presence_shows_who_else_is_on_a_ticket(team):
    tid = new_ticket(team["user"], "Shared", group_id=team["gid"])
    assert check_in(team["user"], tid)["others"] == []
    check_in(team["other"], tid, writing="1")
    assert check_in(team["user"], tid)["others"] == [{"name": "dclark", "writing": True}]
    check_in(team["other"], tid)                            # stopped writing
    assert check_in(team["user"], tid)["others"] == [{"name": "dclark", "writing": False}]
    # Shows on the ticket list too.
    assert "dclark viewing now" in team["user"].get("/").text

    team["other"].post(f"/tickets/{tid}/presence/leave", **NR)
    assert check_in(team["user"], tid)["others"] == []


def test_presence_goes_stale(team):
    tid = new_ticket(team["user"], "Shared", group_id=team["gid"])
    check_in(team["other"], tid)
    old = (datetime.now(timezone.utc)
           - timedelta(seconds=presence.VIEWING_SECONDS + 5)).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute("UPDATE ticket_presence SET seen_at = ?", (old,))
        conn.commit()
    assert check_in(team["user"], tid)["others"] == []


def test_presence_reports_changes_by_others_since_load(team):
    tid = new_ticket(team["user"], "Shared", group_id=team["gid"])
    loaded = check_in(team["user"], tid)
    assert loaded["changed_by"] is None
    since = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds")
    team["user"].post(f"/tickets/{tid}/note", data={"body": "my own note"}, **NR)
    assert check_in(team["user"], tid, since=since)["changed_by"] is None   # mine don't count
    team["other"].post(f"/tickets/{tid}/message", data={"kind": "out_customer",
                                                        "body": "Replied to the rider"}, **NR)
    assert check_in(team["user"], tid, since=since)["changed_by"] == "dclark"


def test_presence_respects_visibility_and_root_is_not_recorded(team):
    tid = new_ticket(team["user"], "Shared", group_id=team["gid"])
    assert team["outsider"].post(f"/tickets/{tid}/presence", **NR).status_code == 404
    check_in(team["root"], tid)
    assert check_in(team["user"], tid)["others"] == []


# ---------------------------------------------------------------- migration

def test_migration_6_adds_the_column_to_an_older_database(env):
    tid = new_ticket(env["user"], "Before the upgrade")
    with connect() as conn:
        conn.execute("DROP INDEX idx_tickets_assignee")
        conn.execute("ALTER TABLE tickets DROP COLUMN assignee_id")
        conn.execute("DROP TABLE saved_views")
        conn.execute("DROP TABLE ticket_presence")
        conn.execute("UPDATE meta SET value = '5' WHERE key = 'schema_version'")
        conn.commit()
    migrate(verbose=False)
    with connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tickets)")}
        assert "assignee_id" in cols
        assert conn.execute("SELECT value FROM meta WHERE key = 'schema_version'"
                            ).fetchone()["value"] == "6"
    assert ticket(tid)["assignee_id"] is None
    env["user"].post(f"/tickets/{tid}/assign", data={"assignee_id": user_id("mreed")}, **NR)
    assert ticket(tid)["assignee_id"] == user_id("mreed")
    # And running it again is harmless.
    with connect() as conn:
        conn.execute("UPDATE meta SET value = '5' WHERE key = 'schema_version'")
        conn.commit()
    migrate(verbose=False)
