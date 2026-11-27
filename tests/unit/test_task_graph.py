import pytest

from crew.domain.enums import TaskStatus
from crew.domain.errors import PlanValidationError
from crew.domain.task_graph import Node, is_blocked, is_ready, order_shared_files, validate_graph


def test_topological_order_respects_dependencies():
    order = validate_graph([Node("c", ("a", "b")), Node("a", ()), Node("b", ("a",))])
    assert order.index("a") < order.index("b") < order.index("c")


@pytest.mark.parametrize(
    ("nodes", "message"),
    [
        ([Node("a", ("b",)), Node("b", ("a",))], "cycle"),
        ([Node("a", ("a",))], "depends on itself"),
        ([Node("a", ("missing",))], "unknown task"),
        ([Node("a", ()), Node("a", ())], "duplicate"),
        ([Node("Bad Id", ())], "must match"),
        ([], "no tasks"),
    ],
)
def test_invalid_graphs_are_rejected(nodes, message):
    with pytest.raises(PlanValidationError, match=message):
        validate_graph(nodes)


def test_ready_only_when_all_dependencies_completed():
    statuses = {"a": TaskStatus.COMPLETED, "b": TaskStatus.RUNNING}
    assert is_ready([], statuses)
    assert is_ready(["a"], statuses)
    assert not is_ready(["a", "b"], statuses)


def test_blocked_when_a_dependency_failed():
    statuses = {"a": TaskStatus.FAILED, "b": TaskStatus.COMPLETED}
    assert is_blocked(["a"], statuses)
    assert not is_blocked(["b"], statuses)


def test_reserved_prefix_is_rejected():
    with pytest.raises(PlanValidationError, match="reserved prefix"):
        validate_graph([Node("crew-fix-1", ())])


def test_tasks_sharing_a_file_are_chained_nearest_first():
    order = ["a", "b", "c", "d"]
    deps = {"a": [], "b": [], "c": [], "d": []}
    files = {"a": ["pom.xml", "A.java"], "b": ["./pom.xml"], "c": ["pom.xml"], "d": ["D.java"]}
    assert order_shared_files(order, deps, files) == {
        "b": {"a": ["pom.xml"]},
        "c": {"b": ["pom.xml"]},  # a is reached through b
    }


def test_existing_ordering_and_directories():
    order = ["a", "b", "c"]
    deps = {"a": [], "b": ["a"], "c": []}
    files = {"a": ["src/api/"], "b": ["src/api/routes.py"], "c": ["src/api/routes.py"]}
    # b already depends on a; c overlaps b (file) and a (directory) but needs only b.
    assert order_shared_files(order, deps, files) == {"c": {"b": ["src/api/routes.py"]}}


def test_no_shared_files_adds_nothing():
    assert order_shared_files(["a", "b"], {}, {"a": ["x.py"], "b": ["xy.py"]}) == {}
