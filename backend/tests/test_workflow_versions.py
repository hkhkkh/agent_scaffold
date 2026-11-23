"""Definition versioning: a saved definition stays runnable after the canvas moves on.

Task 28. The feature exists so that "what did this agent look like six months ago"
has an answer, and so a run can be reproduced from the definition it actually used
rather than from whatever is on the canvas now.

The tests below are the ones task 28's own verification table asks for, plus the
three reverse-validation cases it names. Deliberately not exhaustive: the value here
is in the boundary (what counts as a new version, what does not) and in the two ways
versioning can lie — a version that quietly returns the newest definition, and a
hash that ignores the thing it is supposed to guard.

Storage is verified through the API and the repository rather than by inspecting
rows, so the tests do not have to know the schema.
"""

from __future__ import annotations

import pytest

from app.db.repository import (
    WorkflowVersionNotFound,
    delete_workflow,
    get_workflow,
    get_workflow_version,
    list_workflow_versions,
    save_workflow,
)
from app.graph.schema import EdgeSpec, NodeSpec, WorkflowGraph
from app.graph.subgraphs import definition_digest


def _graph(workflow_id: str = "wf-v", *, template: str = "v1") -> WorkflowGraph:
    return WorkflowGraph(
        id=workflow_id,
        name="versioned",
        nodes=[
            NodeSpec(id="start", type="start", label="Start"),
            NodeSpec(id="tpl", type="prompt", label="Say", config={"template": template}),
            NodeSpec(id="end", type="end", label="End"),
        ],
        edges=[
            EdgeSpec(id="e1", source="start", sourceHandle="out", target="tpl", targetHandle="in"),
            EdgeSpec(id="e2", source="tpl", sourceHandle="out", target="end", targetHandle="in"),
        ],
    )


class TestEverySaveIsKept:
    def test_saving_twice_creates_two_versions(self):
        first = save_workflow(_graph())
        assert first.version == 1

        second = save_workflow(_graph(template="v2"))
        assert second.version == 2

        versions = list_workflow_versions("wf-v")
        assert [item["version"] for item in versions] == [2, 1]

    def test_an_old_version_is_still_readable_after_an_edit(self):
        """★ Task 28's headline requirement: "旧定义永远能重跑".

        **Reverse-validated**: making `get_workflow_version` ignore the number and
        return the current definition fails this — checked by doing it. That is the
        failure worth guarding against, because a version endpoint that quietly
        answers with the latest definition looks like it works until someone needs
        to reproduce a run.
        """
        save_workflow(_graph(template="ORIGINAL"))
        save_workflow(_graph(template="CHANGED"))

        old = get_workflow_version("wf-v", 1)
        new = get_workflow_version("wf-v", 2)

        assert old.nodes[1].config["template"] == "ORIGINAL"
        assert new.nodes[1].config["template"] == "CHANGED"

    def test_the_current_definition_is_the_newest_one(self):
        save_workflow(_graph(template="ORIGINAL"))
        save_workflow(_graph(template="CHANGED"))

        assert get_workflow("wf-v").nodes[1].config["template"] == "CHANGED"

    def test_a_version_that_does_not_exist_is_an_error_not_a_fallback(self):
        """Answering with a *different* definition than asked for is worse than
        failing, so this raises rather than returning the newest."""
        save_workflow(_graph())

        with pytest.raises(WorkflowVersionNotFound, match="no version 99"):
            get_workflow_version("wf-v", 99)

    def test_version_numbers_are_assigned_by_the_server(self):
        """Not taken from the client.

        `graph.version` is whatever the canvas last saw. Honouring it would let two
        open editors both save "version 3", and the history would then have two
        different definitions claiming the same number.
        """
        graph = _graph()
        graph.version = 42  # a client claiming a number

        saved = save_workflow(graph)
        assert saved.version == 1

        again = _graph(template="v2")
        again.version = 42
        assert save_workflow(again).version == 2

    def test_the_versions_are_isolated_per_workflow(self):
        save_workflow(_graph("wf-a"))
        save_workflow(_graph("wf-a", template="a2"))
        save_workflow(_graph("wf-b"))

        assert [item["version"] for item in list_workflow_versions("wf-a")] == [2, 1]
        assert [item["version"] for item in list_workflow_versions("wf-b")] == [1]


class TestWhatCountsAsANewVersion:
    """A new version is written on every save. That is the simplest rule, and it is
    also the one that cannot lose an edit.

    The alternative — write a version only when the content hash changed — sounds
    tidier and quietly discards the fact that someone pressed Save. It also breaks
    the one thing a history is *for*: reconstructing what a person did, in order.
    """

    def test_a_cosmetic_save_still_records_a_version(self):
        first = save_workflow(_graph(template="same"))
        second = save_workflow(_graph(template="same"))

        assert second.version == first.version + 1
        # Same content, so the same hash — the hash describes the definition, the
        # version number describes the act of saving.
        assert list_workflow_versions("wf-v")[0]["definition_hash"] == list_workflow_versions("wf-v")[1]["definition_hash"]

    def test_the_hash_changes_when_the_definition_does(self):
        save_workflow(_graph(template="one"))
        save_workflow(_graph(template="two"))

        versions = list_workflow_versions("wf-v")
        assert versions[0]["definition_hash"] != versions[1]["definition_hash"]

    def test_the_hash_matches_the_digest_the_pins_use(self):
        """One hash function, not two.

        The sub-flow pins and the version history must agree about what "the same
        definition" means. Two functions would drift, and the drift would surface as
        a pin that looks stale against a version that looks identical.
        """
        saved = save_workflow(_graph())

        assert list_workflow_versions("wf-v")[0]["definition_hash"] == definition_digest(saved)


class TestDeletingAWorkflowTakesItsHistory:
    def test_versions_are_removed_with_the_workflow(self):
        """Otherwise a deleted definition leaves history growing on disk with no
        reader: versions exist to reproduce a run of *this* workflow."""
        save_workflow(_graph())
        save_workflow(_graph(template="v2"))

        delete_workflow("wf-v")

        from app.db.repository import WorkflowNotFound

        with pytest.raises(WorkflowNotFound):
            list_workflow_versions("wf-v")

    def test_runs_and_their_steps_are_removed_with_the_workflow(self):
        """The half that has no public reader, so it is asserted on rows.

        Runs and steps are the bulk of what a workflow accumulates, and no
        endpoint can show an orphaned step — ``/runs/<id>/steps`` answers only for
        a run that still exists. The failure mode is therefore a database that
        grows with nobody noticing, which is the opposite of what the retention
        work was for.

        Both tables, and the rows are asserted to exist before the delete: the bug
        this guards is deleting the runs and leaving the steps behind. That is not
        hypothetical — the first version of the function deleted steps by hand in
        a loop, and the shorthand that replaced it has to do the same job.
        """
        from sqlmodel import select

        from app.db.models import Run as RunRow
        from app.db.models import RunStep as RunStepRow
        from app.db.session import session_scope

        save_workflow(_graph())

        with session_scope() as session:
            session.add(RunRow(id="run-1", workflow_id="wf-v"))
            session.add(RunStepRow(run_id="run-1", node_id="tpl"))

        with session_scope() as session:
            assert session.exec(select(RunRow).where(RunRow.id == "run-1")).all()
            assert session.exec(
                select(RunStepRow).where(RunStepRow.run_id == "run-1")
            ).all()

        delete_workflow("wf-v")

        with session_scope() as session:
            assert not session.exec(
                select(RunRow).where(RunRow.workflow_id == "wf-v")
            ).all()
            assert not session.exec(
                select(RunStepRow).where(RunStepRow.run_id == "run-1")
            ).all()


class TestThroughTheApi:
    """The endpoint is the part a person or a script actually uses."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app, client=("127.0.0.1", 55555)) as test_client:
            yield test_client

    def _save(self, client, template: str) -> dict:
        body = _graph(template=template).model_dump(mode="json")
        response = client.put("/api/workflows/wf-v", json=body)
        assert response.status_code in (200, 201), response.text
        return response.json()

    def test_listing_versions(self, client):
        self._save(client, "one")
        self._save(client, "two")

        body = client.get("/api/workflows/wf-v/versions").json()
        assert [item["version"] for item in body["items"]] == [2, 1]
        # Summaries, not payloads: a list of fifty versions should not ship fifty
        # whole graphs.
        assert "graph" not in body["items"][0]
        assert body["items"][0]["node_count"] == 3

    def test_fetching_one_version(self, client):
        self._save(client, "one")
        self._save(client, "two")

        body = client.get("/api/workflows/wf-v/versions/1").json()
        assert body["nodes"][1]["config"]["template"] == "one"

    def test_an_unknown_version_is_404(self, client):
        self._save(client, "one")
        assert client.get("/api/workflows/wf-v/versions/99").status_code == 404

    def test_an_unknown_workflow_is_404_for_both_endpoints(self, client):
        assert client.get("/api/workflows/nope/versions").status_code == 404
        assert client.get("/api/workflows/nope/versions/1").status_code == 404
