"""Groups: members see and work the tickets filed under their group, and a
group can have its own email digest. Root manages all of it."""
from datetime import datetime, timedelta, timezone

import pytest

from app import digest, groups, repo
from app.db import connect
from app.web import LOCAL_TZ

from .conftest import NR, create_account, new_ticket
from .test_digest import outbox, subscribe, user_id


@pytest.fixture
def team(env):
    """env plus two more users. mreed and dclark are in Fare Systems; jlopez is not."""
    env["other"] = create_account(env["root"], "dclark", "user")
    env["outsider"] = create_account(env["root"], "jlopez", "user")
    env["root"].post("/root/groups/create", data={"name": "Fare Systems"}, **NR)
    with connect() as conn:
        env["gid"] = conn.execute("SELECT id FROM user_groups").fetchone()["id"]
    for name in ("mreed", "dclark"):
        env["root"].post(f"/root/groups/{env['gid']}/members",
                         data={"user_id": user_id(name)}, **NR)
    return env


def give_email(root, username: str) -> None:
    uid = user_id(username)
    with connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    root.post(f"/root/users/{uid}/update", data={
        "display_name": user["display_name"], "role": user["role"], "is_active": "1",
        "email": f"{username.lower()}@example.gov"}, **NR)


# ---------------------------------------------------------------- managing groups

def test_only_root_manages_groups(team):
    gid = team["gid"]
    for who in ("admin", "user"):
        assert team[who].get("/root/groups", **NR).status_code == 403
        assert team[who].post("/root/groups/create", data={"name": "X"}, **NR).status_code == 403
        assert team[who].post(f"/root/groups/{gid}/members",
                              data={"user_id": user_id("jlopez")}, **NR).status_code == 403
        assert team[who].post(f"/root/groups/{gid}/delete", data={}, **NR).status_code == 403
    page = team["root"].get("/root/groups").text
    assert "Fare Systems" in page and "dclark" in page


def test_group_names_are_unique_and_required(team):
    r = team["root"].post("/root/groups/create", data={"name": "fare systems"}, **NR)
    assert "error=" in r.headers["location"]
    r = team["root"].post("/root/groups/create", data={"name": "   "}, **NR)
    assert "error=" in r.headers["location"]
    with connect() as conn:
        assert len(groups.list_groups(conn)) == 1
    team["root"].post(f"/root/groups/{team['gid']}/rename", data={"name": "Fares"}, **NR)
    with connect() as conn:
        assert groups.get_group(conn, team["gid"])["name"] == "Fares"


# ---------------------------------------------------------------- visibility

def test_group_members_see_and_work_group_tickets(team):
    tid = new_ticket(team["user"], "Validator offline", group_id=team["gid"])
    mate, outsider = team["other"], team["outsider"]

    assert f"/tickets/{tid}" in mate.get("/").text
    assert mate.get(f"/tickets/{tid}", **NR).status_code == 200
    assert mate.post(f"/tickets/{tid}/status", data={"status": "open"}, **NR).status_code == 303
    with connect() as conn:
        assert repo.get_ticket(conn, int(tid))["status"] == "open"

    assert f"/tickets/{tid}" not in outsider.get("/").text
    assert outsider.get(f"/tickets/{tid}", **NR).status_code == 404
    assert outsider.post(f"/tickets/{tid}/status", data={"status": "closed"},
                         **NR).status_code == 404


def test_ticket_without_a_group_stays_private(team):
    tid = new_ticket(team["user"], "Just mine")
    assert team["other"].get(f"/tickets/{tid}", **NR).status_code == 404
    assert f"/tickets/{tid}" not in team["other"].get("/").text
    assert team["admin"].get(f"/tickets/{tid}", **NR).status_code == 200


def test_api_counts_and_customer_pages_follow_group_visibility(team):
    tid = new_ticket(team["user"], "Shared", group_id=team["gid"],
                     customer_email="rider@example.com")
    mate, outsider = team["other"], team["outsider"]
    assert int(tid) in {t["id"] for t in mate.get("/api/v1/tickets").json()}
    assert int(tid) not in {t["id"] for t in outsider.get("/api/v1/tickets").json()}
    assert mate.get(f"/api/v1/tickets/{tid}").status_code == 200
    assert outsider.get(f"/api/v1/tickets/{tid}").status_code == 404
    with connect() as conn:
        assert repo.dashboard_stats(conn, user_id("dclark"))["counts"]["new"] == 1
        assert repo.dashboard_stats(conn, user_id("jlopez"))["counts"]["new"] == 0
        customer_id = repo.get_ticket(conn, int(tid))["customer_id"]
    assert f"/tickets/{tid}" in mate.get(f"/customers/{customer_id}").text
    assert f"/tickets/{tid}" not in outsider.get(f"/customers/{customer_id}").text


def test_removing_a_member_ends_their_access(team):
    tid = new_ticket(team["user"], "Shared", group_id=team["gid"])
    team["root"].post(f"/root/groups/{team['gid']}/members/{user_id('dclark')}/remove",
                      data={}, **NR)
    assert team["other"].get(f"/tickets/{tid}", **NR).status_code == 404
    assert team["user"].get(f"/tickets/{tid}", **NR).status_code == 200    # still the owner


def test_deleting_a_group_keeps_tickets_but_makes_them_private(team):
    tid = new_ticket(team["user"], "Shared", group_id=team["gid"])
    team["root"].post(f"/root/groups/{team['gid']}/delete", data={}, **NR)
    with connect() as conn:
        ticket = repo.get_ticket(conn, int(tid))
        assert ticket is not None and ticket["group_id"] is None
        assert groups.list_groups(conn) == []
    assert team["other"].get(f"/tickets/{tid}", **NR).status_code == 404
    assert team["user"].get(f"/tickets/{tid}", **NR).status_code == 200


def test_list_filter_mine_and_by_group(team):
    mine = new_ticket(team["user"], "Mine", group_id=team["gid"])
    theirs = new_ticket(team["other"], "Theirs", group_id=team["gid"])
    private = new_ticket(team["user"], "Private")
    page = team["user"].get("/?show=mine").text
    assert f"/tickets/{mine}" in page and f"/tickets/{private}" in page
    assert f"/tickets/{theirs}" not in page
    page = team["user"].get(f"/?show={team['gid']}").text
    assert f"/tickets/{mine}" in page and f"/tickets/{theirs}" in page
    assert f"/tickets/{private}" not in page
    # Filtering by a group you aren't in shows nothing rather than leaking it.
    assert f"/tickets/{theirs}" not in team["outsider"].get(f"/?show={team['gid']}").text


# ---------------------------------------------------------------- filing and sharing

def test_cannot_file_under_a_group_you_are_not_in(team):
    r = team["outsider"].post("/tickets/new", data={"subject": "Sneaky", "priority": "3",
                                                    "group_id": team["gid"]}, **NR)
    assert r.status_code == 403
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0
    # An admin sees everything already, so may file under any group.
    assert new_ticket(team["admin"], "From admin", group_id=team["gid"]).isdigit()


def test_single_group_is_preselected_on_the_new_ticket_form(team):
    assert f'<option value="{team["gid"]}" selected>' in team["user"].get("/tickets/new").text
    assert "Shared with" not in team["outsider"].get("/tickets/new").text


def test_owner_shares_and_unshares_a_ticket(team):
    tid = new_ticket(team["user"], "Was private")
    assert team["other"].get(f"/tickets/{tid}", **NR).status_code == 404

    team["user"].post(f"/tickets/{tid}/group", data={"group_id": team["gid"]}, **NR)
    assert team["other"].get(f"/tickets/{tid}", **NR).status_code == 200
    with connect() as conn:
        assert any("Shared with changed" in (e["body"] or "")
                   for e in repo.get_events(conn, int(tid)))

    # A teammate can work it but can't pull it out of the team.
    assert team["other"].post(f"/tickets/{tid}/group", data={"group_id": ""},
                              **NR).status_code == 403
    team["user"].post(f"/tickets/{tid}/group", data={"group_id": ""}, **NR)
    assert team["other"].get(f"/tickets/{tid}", **NR).status_code == 404


def test_root_can_share_a_members_existing_tickets(team):
    old = new_ticket(team["user"], "Filed before the group")
    other_owner = new_ticket(team["outsider"], "Not theirs")
    r = team["root"].post(f"/root/groups/{team['gid']}/adopt",
                          data={"user_id": user_id("mreed")}, **NR)
    assert "notice=" in r.headers["location"]
    assert team["other"].get(f"/tickets/{old}", **NR).status_code == 200
    assert team["other"].get(f"/tickets/{other_owner}", **NR).status_code == 404
    # Only for people actually in the group.
    r = team["root"].post(f"/root/groups/{team['gid']}/adopt",
                          data={"user_id": user_id("jlopez")}, **NR)
    assert "error=" in r.headers["location"]


# ---------------------------------------------------------------- digests

def test_team_scope_subscriber_gets_own_and_group_tickets(team):
    new_ticket(team["user"], "Team ticket", group_id=team["gid"])
    new_ticket(team["other"], "My private one")
    new_ticket(team["outsider"], "Someone elses")
    subscribe(team["root"], "dclark", scope="team")
    page = team["root"].get(f"/root/digest/preview/{user_id('dclark')}").text
    assert "Team ticket" in page and "My private one" in page
    assert "Someone elses" not in page


def test_group_digest_goes_to_every_member_with_an_email(team):
    give_email(team["root"], "mreed")                # dclark has no email
    new_ticket(team["user"], "Group ticket", group_id=team["gid"])
    new_ticket(team["outsider"], "Not the groups")

    preview = team["root"].get(f"/root/groups/{team['gid']}/digest/preview").text
    assert "Group ticket" in preview and "Not the groups" not in preview

    r = team["root"].post(f"/root/groups/{team['gid']}/digest/send", data={}, **NR)
    assert "notice=" in r.headers["location"] and "skipped" in r.headers["location"]
    mail = outbox()
    assert [m["To"] for m in mail] == ["mreed@example.gov"]
    assert mail[0]["Subject"] == "Help Desk · Fare Systems: 1 open ticket"


def test_group_digest_runs_on_the_groups_own_schedule(team):
    give_email(team["root"], "mreed")
    give_email(team["root"], "dclark")
    now = datetime.now(timezone.utc)
    day = (now.astimezone(LOCAL_TZ).weekday() + 2) % 7
    team["root"].post(f"/root/groups/{team['gid']}/digest",
                      data={"days": [str(day)], "hour": "0"}, **NR)
    assert digest.run_if_due(now + timedelta(days=1)) is None
    result = digest.run_if_due(now + timedelta(days=2))
    assert sorted(result.sent) == ["dclark@example.gov", "mreed@example.gov"]
    assert digest.run_if_due(now + timedelta(days=2)) is None

    # Someone added later is included without touching the schedule.
    team["root"].post(f"/root/groups/{team['gid']}/members",
                      data={"user_id": user_id("jlopez")}, **NR)
    give_email(team["root"], "jlopez")
    assert len(digest.run_if_due(now + timedelta(days=9)).sent) == 3


def test_new_group_ticket_notifies_members_once_and_not_the_creator(team):
    for name in ("mreed", "dclark", "jlopez"):
        give_email(team["root"], name)
    team["root"].post(f"/root/groups/{team['gid']}/digest",
                      data={"hour": "8", "on_new_ticket": "1"}, **NR)
    # dclark is also a personal subscriber with new-ticket emails: still one email.
    subscribe(team["root"], "dclark", scope="team")
    team["root"].post(f"/root/digest/subscribers/{user_id('dclark')}/update",
                      data={"scope": "team", "hour": "8", "on_new_ticket": "1"}, **NR)

    new_ticket(team["user"], "Gate 3 reader down", group_id=team["gid"])
    assert [m["To"] for m in outbox()] == ["dclark@example.gov"]

    new_ticket(team["user"], "Private, no group")
    assert len(outbox()) == 1                  # nobody else can see it
