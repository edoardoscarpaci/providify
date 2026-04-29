"""Tests for F10: DEPENDENT scope tracking with flush_dependents."""

from __future__ import annotations

from providify import (
    Component,
    DIContainer,
    PreDestroy,
)


class Tracker:
    destroyed: list[str] = []

    @classmethod
    def reset(cls) -> None:
        cls.destroyed.clear()


@Component(track=True)
class TrackedBean:
    def __init__(self) -> None:
        self.name = "tracked"

    @PreDestroy
    def teardown(self) -> None:
        Tracker.destroyed.append(self.name)


@Component
class UntrackedBean:
    @PreDestroy
    def teardown(self) -> None:
        Tracker.destroyed.append("untracked")


def test_pre_destroy_fires_on_flush_dependents(container: DIContainer):
    Tracker.reset()
    container.register(TrackedBean)

    bean = container.get(TrackedBean)
    assert bean is not None

    container.flush_dependents()
    assert "tracked" in Tracker.destroyed


def test_untracked_dependent_not_in_flush(container: DIContainer):
    Tracker.reset()
    container.register(UntrackedBean)

    container.get(UntrackedBean)
    container.flush_dependents()

    assert "untracked" not in Tracker.destroyed


def test_flush_dependents_clears_list(container: DIContainer):
    Tracker.reset()
    container.register(TrackedBean)

    container.get(TrackedBean)
    container.flush_dependents()
    destroyed_count = len(Tracker.destroyed)

    # Second flush: list is empty, no double-firing
    container.flush_dependents()
    assert len(Tracker.destroyed) == destroyed_count


def test_multiple_tracked_instances_all_flushed(container: DIContainer):
    Tracker.reset()
    container.register(TrackedBean)

    # Three separate DEPENDENT instances
    for i in range(3):
        b = container.get(TrackedBean)
        b.name = f"bean-{i}"

    container.flush_dependents()
    assert len(Tracker.destroyed) == 3


def test_context_manager_flushes_on_exit():
    Tracker.reset()

    @Component(track=True)
    class Temp:
        @PreDestroy
        def bye(self) -> None:
            Tracker.destroyed.append("temp")

    with DIContainer() as c:
        c.register(Temp)
        c.get(Temp)

    assert "temp" in Tracker.destroyed
