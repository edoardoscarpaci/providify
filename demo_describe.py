"""
Quick demo of Binding.describe() — run this from the project root:

    poetry run python demo_describe.py

Shows a three-tier service graph:
  AppService (SINGLETON)
    └── OrderRepository (DEPENDENT)
          └── DatabaseConnection (SINGLETON)

Then a second tree with a scope-leak warning:
  ReportService (SINGLETON)
    └── RequestCache (REQUEST)   ← ⚠️ SCOPE LEAK

And finally a two-binding diamond (shared DatabaseConnection):
  Dashboard (DEPENDENT)
    ├── UserService (DEPENDENT)
    │   └── DatabaseConnection (SINGLETON)
    └── OrderService (DEPENDENT)
        └── DatabaseConnection (SINGLETON)

NOTE: All classes are at module level because get_type_hints() resolves
annotations from the module namespace, not local function scope.
"""

from __future__ import annotations

from providify import (
    Component,
    DIContainer,
    Inject,
    RequestScoped,
    Singleton,
)


# ─────────────────────────────────────────────────────────────────
#  Infrastructure layer
# ─────────────────────────────────────────────────────────────────


@Singleton
class DatabaseConnection:
    """Pretends to hold a DB connection — singleton so it's shared app-wide."""


# ─────────────────────────────────────────────────────────────────
#  Repository layer
# ─────────────────────────────────────────────────────────────────


@Component
class OrderRepository:
    """Fetches orders — DEPENDENT (new instance per resolution)."""

    def __init__(self, db: Inject[DatabaseConnection]) -> None:
        self.db = db


@Component
class UserRepository:
    """Fetches users — DEPENDENT."""

    def __init__(self, db: Inject[DatabaseConnection]) -> None:
        self.db = db


# ─────────────────────────────────────────────────────────────────
#  Service layer
# ─────────────────────────────────────────────────────────────────


@Singleton
class AppService:
    """Top-level singleton service that depends on OrderRepository."""

    def __init__(self, repo: Inject[OrderRepository]) -> None:
        self.repo = repo


@RequestScoped
class RequestCache:
    """Lives only for the duration of a single request."""


@Singleton
class ReportService:
    """SINGLETON that mistakenly injects a REQUEST-scoped cache — scope leak!"""

    def __init__(self, cache: Inject[RequestCache]) -> None:
        self.cache = cache


@Component
class UserService:
    """Depends on UserRepository which depends on DatabaseConnection."""

    def __init__(self, repo: Inject[UserRepository]) -> None:
        self.repo = repo


@Component
class OrderService:
    """Depends on OrderRepository which depends on DatabaseConnection."""

    def __init__(self, repo: Inject[OrderRepository]) -> None:
        self.repo = repo


@Component
class Dashboard:
    """Diamond: depends on both UserService and OrderService, both share DatabaseConnection."""

    def __init__(
        self,
        users: Inject[UserService],
        orders: Inject[OrderService],
    ) -> None:
        self.users = users
        self.orders = orders


# ─────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────


def main() -> None:
    container = DIContainer()

    # Register everything
    container.register(DatabaseConnection)
    container.register(OrderRepository)
    container.register(UserRepository)
    container.register(AppService)
    container.register(RequestCache)
    container.register(ReportService)
    container.register(UserService)
    container.register(OrderService)
    container.register(Dashboard)

    separator = "─" * 60

    # ── Tree 1: three-tier chain ─────────────────────────────────
    from providify.binding import ClassBinding

    app_binding = ClassBinding(AppService, AppService)

    print(separator)
    print("Tree 1 — AppService (three-tier chain)")
    print(separator)
    print(app_binding.describe(container))
    print()

    # ── Tree 2: scope-leak warning ───────────────────────────────
    report_binding = ClassBinding(ReportService, ReportService)

    print(separator)
    print("Tree 2 — ReportService (scope leak: SINGLETON → REQUEST)")
    print(separator)
    print(report_binding.describe(container))
    print()

    # ── Tree 3: diamond (shared DatabaseConnection) ──────────────
    dashboard_binding = ClassBinding(Dashboard, Dashboard)

    print(separator)
    print("Tree 3 — Dashboard (diamond pattern)")
    print(separator)
    print(dashboard_binding.describe(container))
    print()

    print(separator)
    print("Tree 4 — Container")
    print(separator)
    print(container.describe())
    print()

    # ── Serialised dict ──────────────────────────────────────────
    import json

    print(separator)
    print("AppService as JSON")
    print(separator)
    print(json.dumps(app_binding.describe(container).to_dict(), indent=2))
    print()

    import json

    print(separator)
    print("Container as JSON")
    print(separator)
    print(json.dumps(container.describe().to_dict(), indent=2))
    print()


if __name__ == "__main__":
    main()
