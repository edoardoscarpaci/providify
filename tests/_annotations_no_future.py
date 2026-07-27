"""Helper module WITHOUT ``from __future__ import annotations``.

Deliberately not using PEP-563 postponed evaluation — every annotation on the
classes/functions declared here is therefore a real, already-evaluated object
at import time (not a string), exercising the "step 1: not a str" branch of
the ``resolve_one`` decision procedure (Plan 001 §7.2). Any test that imports
this module wants to prove that per-parameter resolution behaves identically
whether annotations arrive as strings (PEP 563) or as live objects.

Do not add ``from __future__ import annotations`` to this file — that is the
entire point of its existence.
"""

from providify.decorator.scope import Provider, Singleton
from providify.type import Inject


@Singleton
class Thing:
    """A trivially resolvable dependency type, real object at import time.

    SINGLETON (not DEPENDENT) so that `make_thing` below — a
    `@Provider(singleton=True)` — does not trip the container's (pre-existing,
    Phase 1-6) scope-leak validator by capturing a narrower-scoped dep as a
    bare, unmarked parameter.
    """


class AnnotatedBase:
    """Cross-module MRO test subject: a class-var injection point declared here.

    Used by the class-attribute resolver's reverse-MRO walk — the defining
    class's module (this one) must supply the globalns for its own
    annotations, not the subclass's module.
    """

    dep: Inject[Thing]


@Provider(singleton=True)
def make_thing(dep: Thing) -> Thing:
    """A plain provider-shaped function whose annotations are real objects."""
    return dep
