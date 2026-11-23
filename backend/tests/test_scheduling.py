"""Scheduled runs.

Deliberately small. APScheduler owns the trigger arithmetic, the wait, and the
lateness policy, and re-testing a dependency's own test suite is waste — its
behaviour was measured before the decision to adopt it (`docs/target/reuse.md`
§1.33), and that measurement is what these tests do *not* repeat.

What is left to test is the part that is ours, and specifically the parts that
fail **silently**. A scheduler is the worst possible thing to have untested,
because when it breaks nothing else in the platform looks any different: the app
starts, the list page renders, and the runs simply never happen. So the coverage
below is chosen by "would anyone notice", not by "is it public":

* a rule that a canvas author picks becomes the trigger they meant;
* a due schedule actually starts a run — the one test that proves the feature
  exists at all;
* a workflow deleted out from under a schedule is noticed;
* UTC is really applied, because that was a **measured** trap: modifying a job
  with a trigger that omits the timezone gave a next fire time in the server's
  local zone even though the scheduler was built with UTC.

Three tests here are reverse-validated: see the notes on each.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.hooks import MAX_PAYLOAD_BYTES
from app.main import app
from app.scheduling.rules import FREQUENCIES, ScheduleRule, ScheduleRuleError

UTC = timezone.utc


# ---------------------------------------------------------------------------
# The vocabulary. Pure, no database, so it runs in milliseconds.
# ---------------------------------------------------------------------------


class TestTheRuleVocabulary:
    def test_a_weekly_rule_becomes_the_trigger_the_author_meant(self):
        """The weekdays must survive translation, in the right order.

        Monday is 0 here and in APScheduler, which is *not* what a crontab means —
        APScheduler documents that mismatch as a historical mistake it cannot fix.
        Pinning the mapping in a test is what stops a future "cleanup" from
        silently shifting every weekday by one.
        """
        trigger = ScheduleRule(
            "weekly", weekday=(0, 2, 4), hour=(9, 17), minute=30
        ).to_trigger()
        assert str(trigger) == (
            "cron[day_of_week='mon,wed,fri', hour='9,17', minute='30']"
        ), str(trigger)

    def test_every_offered_frequency_can_be_built(self):
        for frequency in FREQUENCIES:
            trigger = ScheduleRule(frequency, hour=(9,), minute=0).to_trigger()
            assert trigger is not None, frequency

    def test_the_timezone_is_utc_and_not_inherited(self):
        """Reverse-validated: this is the measured trap.

        Removing `timezone=UTC` from `to_trigger` makes this fail — checked by
        doing it. The symptom it prevents is a schedule that fires on the server's
        local clock, which shows up as "it ran at the wrong hour" for exactly the
        people whose servers are not in UTC.
        """
        trigger = ScheduleRule("daily", hour=(9,), minute=0).to_trigger()
        assert trigger.timezone is not None
        assert str(trigger.timezone) == "UTC"

    def test_a_combination_that_could_never_fire_is_refused(self):
        """A weekly rule with a day-of-month would be accepted and then never match.

        Refusing it at save time is the whole reason `ScheduleRuleError` exists: a
        rule that cannot fire, stored successfully, is a schedule that fails with
        no symptom at all.
        """
        with pytest.raises(ScheduleRuleError, match="weekday"):
            ScheduleRule("weekly", day=15)

    def test_a_round_trip_through_json_gives_the_same_rule(self):
        original = ScheduleRule("weekly", weekday=(1, 3), hour=(8,), minute=0)
        assert ScheduleRule.from_dict(original.to_dict()).to_dict() == original.to_dict()

    def test_an_unreadable_stored_rule_names_the_field(self):
        """Read back out of a hand-edited row, the message has to be actionable."""
        with pytest.raises(ScheduleRuleError, match="frequency"):
            ScheduleRule.from_dict({"nonsense": 1})


# ---------------------------------------------------------------------------
# The feature. Everything above passes even if no run is ever started.
# ---------------------------------------------------------------------------


def _graph(workflow_id: str) -> dict:
    return {
        "id": workflow_id,
        "name": "scheduled",
        "nodes": [
            {"id": "start", "type": "start", "label": "Start", "config": {}, "position": {"x": 0, "y": 0}},
            {
                "id": "say",
                "type": "prompt",
                "label": "Say",
                "config": {"template": "tick"},
                "position": {"x": 200, "y": 0},
            },
            {"id": "end", "type": "end", "label": "End", "config": {}, "position": {"x": 400, "y": 0}},
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "say"},
            {"id": "e2", "source": "say", "target": "end"},
        ],
    }


@pytest.fixture
def client(monkeypatch):
    """A client whose lifespan actually runs, so the scheduler really starts.

    Not a shortcut: the scheduler is started in the lifespan, and a test that
    called the service directly would not catch it failing to start — which is the
    failure with no symptom.
    """
    monkeypatch.setenv("SANDBOX_MODE", "inprocess")
    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    from app.settings import get_settings

    get_settings.cache_clear()
    with TestClient(app, client=("127.0.0.1", 55555)) as test_client:
        yield test_client
    get_settings.cache_clear()


@pytest.fixture
def workflow(client: TestClient) -> str:
    response = client.put("/api/workflows/wf-sched", json=_graph("wf-sched"))
    assert response.status_code in (200, 201), response.text
    return "wf-sched"


class TestAScheduleStartsARun:
    """The one end-to-end test. If this passes and nothing else does, the feature works."""

    def test_a_due_schedule_starts_a_run(self, client: TestClient, workflow: str):
        """Force the job's next fire time into the past, then let it fire.

        The rule is `daily`, which will not come round during a test, so the job is
        re-pointed with `next_run_time` instead of waiting. That is the same lever
        an operator would pull to test a schedule, and it exercises the real path:
        APScheduler's loop calls our job function, which calls `RunManager.create`
        and `start`.
        """
        response = client.post(
            "/api/schedules",
            json={
                "workflow_id": workflow,
                "name": "tick",
                "rule": {"frequency": "daily", "hour": [9], "minute": 0},
                "inputs": {},
            },
        )
        assert response.status_code == 201, response.text
        schedule_id = response.json()["id"]

        # What a schedule's row looks like before it has ever fired.
        assert client.get(f"/api/schedules/{schedule_id}").json()["last_run_id"] == ""

        from app.scheduling.service import get_scheduler

        scheduler = get_scheduler()
        assert scheduler.running, "the lifespan did not start the scheduler"
        scheduler._scheduler.modify_job(
            schedule_id, next_run_time=datetime.now(UTC) - timedelta(seconds=1)
        )

        deadline = time.monotonic() + 30
        run_id = ""
        while time.monotonic() < deadline:
            run_id = client.get(f"/api/schedules/{schedule_id}").json()["last_run_id"]
            if run_id:
                break
            time.sleep(0.2)

        assert run_id, (
            "the schedule never started a run. The loop is the whole feature, so "
            "this failing means scheduled runs do not happen at all"
        )
        # And it is an ordinary run: it appears in the run history the same page
        # shows every other run from.
        assert client.get(f"/api/runs/{run_id}").status_code == 200

    def test_a_disabled_schedule_has_no_job(self, client: TestClient, workflow: str):
        """`enabled=false` is a job removal, not a flag the job checks.

        Worth pinning because the two are easy to confuse: a job that stayed
        registered and merely skipped itself would still hold a next fire time, so
        the schedule list would keep promising a run that never comes.
        """
        created = client.post(
            "/api/schedules",
            json={
                "workflow_id": workflow,
                "rule": {"frequency": "daily", "hour": [9]},
                "enabled": False,
            },
        )
        assert created.status_code == 201, created.text
        schedule_id = created.json()["id"]

        from app.scheduling.service import get_scheduler

        assert get_scheduler()._scheduler.get_job(schedule_id) is None


class TestAWorkflowDeletedUnderASchedule:
    def test_the_schedule_is_orphaned_rather_than_deleted(
        self, client: TestClient, workflow: str
    ):
        """Delete the workflow, reconcile, and the schedule survives as a marker.

        **Reverse-validated**: skipping the `reconcile` call leaves `status` at
        `active`, which makes this fail. Deleting the schedule instead would be the
        tempting simplification and it loses the user's intent — the repair is to
        point it at another workflow, which is only possible if it still exists.
        """
        created = client.post(
            "/api/schedules",
            json={"workflow_id": workflow, "rule": {"frequency": "daily", "hour": [9]}},
        )
        assert created.status_code == 201, created.text
        schedule_id = created.json()["id"]

        assert client.delete(f"/api/workflows/{workflow}").status_code in (200, 204)

        result = client.post("/api/schedules/reconcile").json()
        assert result["orphaned"] >= 1, result

        row = client.get(f"/api/schedules/{schedule_id}").json()
        assert row["status"] == "orphaned", row
        assert row["validity"], "an orphaned schedule must say why it will not fire"
        assert row["next_run_at"] is None, "an orphaned schedule must stop being due"

    def test_the_repair_is_repointing_it(self, client: TestClient, workflow: str):
        """The other half of the previous test: the repair must actually work.

        Orphaning is only a good design if re-pointing revives the schedule, and
        that is a claim about code rather than about intent.
        """
        created = client.post(
            "/api/schedules",
            json={"workflow_id": workflow, "rule": {"frequency": "daily", "hour": [9]}},
        )
        schedule_id = created.json()["id"]
        assert client.delete(f"/api/workflows/{workflow}").status_code in (200, 204)
        client.post("/api/schedules/reconcile")
        assert client.get(f"/api/schedules/{schedule_id}").json()["status"] == "orphaned"

        client.put("/api/workflows/wf-sched", json=_graph("wf-sched"))
        response = client.patch(
            f"/api/schedules/{schedule_id}", json={"workflow_id": "wf-sched"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "active"
        assert response.json()["next_run_at"] is not None


class TestTheApiRefusesWhatItCannotHonour:
    def test_a_schedule_for_a_workflow_that_does_not_exist_is_refused(
        self, client: TestClient
    ):
        """A typo'd id would otherwise be discovered at the first fire time.

        Which is the worst moment: nobody is watching, and the only record is a
        failed run in a list that is not being read.
        """
        response = client.post(
            "/api/schedules",
            json={"workflow_id": "nope", "rule": {"frequency": "daily", "hour": [9]}},
        )
        assert response.status_code == 422, response.text

    def test_an_unbuildable_rule_answers_422_rather_than_500(
        self, client: TestClient, workflow: str
    ):
        response = client.post(
            "/api/schedules",
            json={"workflow_id": workflow, "rule": {"frequency": "weekly", "day": 3}},
        )
        assert response.status_code == 422, response.text
        assert "weekday" in response.text

    def test_the_editor_options_come_from_the_server(self, client: TestClient):
        """One source for the vocabulary, so a new frequency is one edit."""
        body = client.get("/api/schedules/options").json()
        assert body["frequencies"] == list(FREQUENCIES)
        assert body["timezone"] == "UTC"
        assert body["kinds"] == ["schedule", "webhook"]


# ---------------------------------------------------------------------------
# Webhooks: the event-driven half.
#
# Same row, same workflow lookup, same run-starting path as a schedule — a
# different firing mechanism. The tests below are about the differences, plus the
# one security decision the design rests on.
# ---------------------------------------------------------------------------


def _hook(client: TestClient, workflow_id: str, **extra: object) -> dict:
    response = client.post(
        "/api/schedules",
        json={"workflow_id": workflow_id, "kind": "webhook", "name": "hook", **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestAWebhookStartsARun:
    def test_a_post_starts_a_run_and_returns_its_id(
        self, client: TestClient, workflow: str
    ):
        """The feature, end to end: an external system POSTs, a run exists.

        The body becomes the run's inputs, which is what makes a webhook useful
        rather than merely present — the event's own data reaches the workflow.
        """
        hook = _hook(client, workflow)
        assert hook["hook_url"] == f"/api/hooks/{hook['token']}"
        # A webhook has no clock, so it must not pretend to have a next fire time.
        assert hook["next_run_at"] is None
        assert hook["description"] == "on an inbound POST"

        response = client.post(hook["hook_url"], json={"order": "A-17"})
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]

        # It is an ordinary run: the same history every other run appears in.
        run = client.get(f"/api/runs/{run_id}")
        assert run.status_code == 200, run.text
        assert run.json()["inputs"] == {"order": "A-17"}

        # And the trigger's own row remembers what it produced.
        after = client.get(f"/api/schedules/{hook['id']}").json()
        assert after["last_run_id"] == run_id
        assert after["last_started_at"] is not None

    def test_the_request_body_wins_over_the_stored_inputs(
        self, client: TestClient, workflow: str
    ):
        """Stored inputs are defaults; the payload is the caller's specifics.

        Pinned because the opposite precedence looks equally reasonable and would
        make a payload key silently ignored — the kind of bug that is diagnosed by
        reading source, not by reading logs.
        """
        hook = _hook(client, workflow, inputs={"order": "default", "kept": "yes"})
        run_id = client.post(hook["hook_url"], json={"order": "A-17"}).json()["run_id"]

        inputs = client.get(f"/api/runs/{run_id}").json()["inputs"]
        assert inputs == {"order": "A-17", "kept": "yes"}, inputs

    def test_a_body_is_optional(self, client: TestClient, workflow: str):
        """"Run this" with no payload is the simplest integration there is.

        Refusing it would make the easiest thing to build the one that does not
        work, so an absent or empty body means no inputs rather than a 422.
        """
        hook = _hook(client, workflow)
        for kwargs in ({}, {"json": None}, {"content": b""}):
            response = client.post(hook["hook_url"], **kwargs)
            assert response.status_code == 202, (kwargs, response.text)

    def test_a_non_object_body_becomes_no_inputs(
        self, client: TestClient, workflow: str
    ):
        """A run's inputs are a mapping by contract, so an array is dropped.

        Wrapping it under a guessed key would be inventing intent, and the caller
        would never know their payload had been reshaped.
        """
        hook = _hook(client, workflow)
        run_id = client.post(hook["hook_url"], json=[1, 2, 3]).json()["run_id"]
        assert client.get(f"/api/runs/{run_id}").json()["inputs"] == {}

    def test_an_oversized_payload_is_refused(self, client: TestClient, workflow: str):
        """An unauthenticated caller must not be able to write an unbounded body.

        The payload is persisted, streamed to the UI, and handed to every node, so
        its size is not only a request concern.
        """
        hook = _hook(client, workflow)
        huge = "x" * (MAX_PAYLOAD_BYTES + 1)
        response = client.post(hook["hook_url"], json={"blob": huge})
        assert response.status_code == 413, response.status_code


class TestTheHookTokenIsTheWholeCredential:
    """The security of this endpoint rests on one property, so it is tested directly."""

    def test_an_unknown_token_is_404(self, client: TestClient):
        assert client.post("/api/hooks/not-a-real-token").status_code == 404

    def test_a_disabled_hook_and_an_unknown_one_are_indistinguishable(
        self, client: TestClient, workflow: str
    ):
        """**Reverse-validated**: answering 403 for the disabled one makes this fail.

        Telling an unauthorised caller "that token is real but switched off" hands
        them a working token for free. A probe that can tell the difference can
        search for valid tokens by brute force; one that cannot has learned nothing
        from a miss.
        """
        hook = _hook(client, workflow)
        client.patch(f"/api/schedules/{hook['id']}", json={"enabled": False})

        disabled = client.post(hook["hook_url"])
        unknown = client.post("/api/hooks/totally-different-token")
        assert disabled.status_code == unknown.status_code == 404
        assert disabled.json() == unknown.json()

    def test_an_orphaned_hook_is_also_404(self, client: TestClient, workflow: str):
        """A hook whose workflow was deleted must stop firing, not fire and fail."""
        hook = _hook(client, workflow)
        assert client.delete(f"/api/workflows/{workflow}").status_code in (200, 204)
        client.post("/api/schedules/reconcile")

        assert client.post(hook["hook_url"]).status_code == 404

    def test_a_hook_is_reachable_without_a_platform_token(self, client: TestClient, workflow: str):
        """The endpoint is public **by design**, because the sender has no token of ours.

        Guarded instead by a 256-bit token. This test exists because the exemption
        is the load-bearing part of that design: if `/api/hooks/` stopped being
        public, every real caller would start getting 401s and the fix would look
        like "the sender is misconfigured".
        """
        hook = _hook(client, workflow)
        from app.security.auth import PUBLIC_PREFIXES

        assert hook["hook_url"].startswith(PUBLIC_PREFIXES)
        # No Authorization header anywhere in this request.
        assert client.post(hook["hook_url"]).status_code == 202

    def test_rotating_the_token_invalidates_the_old_url(
        self, client: TestClient, workflow: str
    ):
        """The remedy for a leaked URL, and it has to actually revoke.

        Without rotation the only fix would be deleting the trigger, which loses
        its run history and its configured inputs.
        """
        hook = _hook(client, workflow)
        old_url = hook["hook_url"]

        rotated = client.post(f"/api/schedules/{hook['id']}/rotate")
        assert rotated.status_code == 200, rotated.text
        new_url = rotated.json()["hook_url"]
        assert new_url != old_url

        assert client.post(old_url).status_code == 404, "the old token still works"
        assert client.post(new_url).status_code == 202

    def test_a_schedule_has_no_token_and_a_hook_has_no_rule(
        self, client: TestClient, workflow: str
    ):
        """The two kinds are exclusive, and the API says so rather than guessing."""
        timed = client.post(
            "/api/schedules",
            json={"workflow_id": workflow, "rule": {"frequency": "daily", "hour": [9]}},
        ).json()
        assert timed["token"] == ""
        assert "hook_url" not in timed

        no_rule = client.post(
            "/api/schedules",
            json={"workflow_id": workflow, "kind": "schedule"},
        )
        assert no_rule.status_code == 422
        assert "rule" in no_rule.text

        rule_on_hook = client.post(
            "/api/schedules",
            json={
                "workflow_id": workflow,
                "kind": "webhook",
                "rule": {"frequency": "daily", "hour": [9]},
            },
        )
        assert rule_on_hook.status_code == 422, (
            "a rule on a webhook would be stored and never consulted, which is "
            "worse than refusing it — the editor would show it as if it worked"
        )


class TestAWebhookDoesNotBecomeATimer:
    def test_a_hook_registers_no_apscheduler_job(self, client: TestClient, workflow: str):
        hook = _hook(client, workflow)

        from app.scheduling.service import get_scheduler

        scheduler = get_scheduler()
        assert scheduler.running
        assert scheduler._scheduler.get_job(hook["id"]) is None

        timed = client.post(
            "/api/schedules",
            json={"workflow_id": workflow, "rule": {"frequency": "daily", "hour": [9]}},
        ).json()
        assert scheduler._scheduler.get_job(timed["id"]) is not None

    def test_a_hook_does_not_collect_a_rule_error(self, client: TestClient, workflow: str):
        """**Reverse-validated**, and the first version of this test was too weak.

        Removing the `kind` filter from `live_schedules` did **not** fail the job
        test above, because `sync` catches the unparseable rule and skips the row —
        so no job appears either way and the assertion held for the wrong reason.

        The damage is on the row: `sync` calls `record_error` before skipping, so a
        webhook would accumulate a fabricated "rule is missing 'frequency'" on every
        sync, and its `failure_count` would climb forever for no reason. That is the
        thing worth asserting, because it is what an operator would see and have to
        debug.
        """
        hook = _hook(client, workflow)
        assert hook["last_error"] == "" and hook["failure_count"] == 0

        from app.scheduling.service import get_scheduler

        scheduler = get_scheduler()
        # Several syncs, because one would not show an accumulation.
        for _ in range(3):
            scheduler.sync()

        after = client.get(f"/api/schedules/{hook['id']}").json()
        assert after["last_error"] == "", (
            f"a webhook collected a rule error it can never have: {after['last_error']!r}"
        )
        assert after["failure_count"] == 0, after["failure_count"]
        assert scheduler._scheduler.get_job(hook["id"]) is None
