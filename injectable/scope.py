from __future__ import annotations

import uuid
import threading
from contextlib import contextmanager, asynccontextmanager
from contextvars import ContextVar
from typing import Generator, AsyncGenerator,Any


class ScopeContext:
    """
    Manages REQUEST and SESSION scoped instance caches.

    Supports both sync and async contexts:
        Sync:  with sc.request(): ...
        Async: async with sc.request(): ...

    Uses contextvars.ContextVar instead of threading.local —
    each asyncio Task gets its own copy of context variables,
    preventing scope bleed across concurrent coroutines.

    Equivalent to Jakarta's RequestContext / SessionContext.
    """

    def __init__(self) -> None:
        # ContextVar — each task/thread gets its own value ✅
        # threading.local — all coroutines on same thread share value ❌
        self._request_id: ContextVar[str | None] = ContextVar(
            "request_id", default=None
        )
        self._session_id: ContextVar[str | None] = ContextVar(
            "session_id", default=None
        )

        # Actual instance caches — keyed by context ID
        # Lock protects concurrent writes to these shared dicts
        self._request_caches: dict[str, dict[Any, object]] = {}
        self._session_caches: dict[str, dict[Any, object]] = {}
        self._lock = threading.Lock()

    # ── Request scope — sync ──────────────────────────────────────

    @contextmanager
    def request(self) -> Generator[str, None, None]:
        """
        Activates a request scope context (sync version).

        Usage:
            with container.scope_context.request():
                svc = container.get(MyService)  # @RequestScoped
        """
        request_id = str(uuid.uuid4())

        with self._lock:
            self._request_caches[request_id] = {}

        # ContextVar.set() returns a Token — used to restore previous value
        # This is critical for nested request contexts
        token = self._request_id.set(request_id)

        try:
            yield request_id
        finally:
            # Restore previous request_id — handles nested contexts correctly
            self._request_id.reset(token)
            with self._lock:
                self._request_caches.pop(request_id, None)

    # ── Request scope — async ─────────────────────────────────────

    @asynccontextmanager
    async def arequest(self) -> AsyncGenerator[str, None]:
        """
        Activates a request scope context (async version).

        Each asyncio Task that enters this context gets its own
        isolated request_id — concurrent requests never bleed.

        Usage:
            async with container.scope_context.arequest():
                svc = container.get(MyService)  # @RequestScoped — isolated ✅
        """
        request_id = str(uuid.uuid4())

        with self._lock:
            self._request_caches[request_id] = {}

        # ✅ ContextVar.set() — isolated per asyncio Task
        token = self._request_id.set(request_id)

        try:
            yield request_id
        finally:
            self._request_id.reset(token)
            with self._lock:
                self._request_caches.pop(request_id, None)

    # ── Session scope — sync ──────────────────────────────────────

    @contextmanager
    def session(self, session_id: str | None = None) -> Generator[str, None, None]:
        """
        Activates a session scope context (sync version).
        Reuses existing session cache if session_id already exists.

        Usage:
            with container.scope_context.session("user-abc") as sid:
                profile = container.get(UserProfile)  # @SessionScoped
        """
        sid = session_id or str(uuid.uuid4())

        with self._lock:
            if sid not in self._session_caches:
                self._session_caches[sid] = {}

        token = self._session_id.set(sid)

        try:
            yield sid
        finally:
            self._session_id.reset(token)

    # ── Session scope — async ─────────────────────────────────────

    @asynccontextmanager
    async def asession(self, session_id: str | None = None) -> AsyncGenerator[str, None]:
        """
        Activates a session scope context (async version).

        Usage:
            async with container.scope_context.asession("user-abc") as sid:
                async with container.scope_context.arequest():
                    profile = container.get(UserProfile)  # session-scoped ✅
                    logger  = container.get(RequestLogger) # request-scoped ✅
        """
        sid = session_id or str(uuid.uuid4())

        with self._lock:
            if sid not in self._session_caches:
                self._session_caches[sid] = {}

        token = self._session_id.set(sid)

        try:
            yield sid
        finally:
            self._session_id.reset(token)

    def invalidate_session(self, session_id: str) -> None:
        """Destroys a session cache — call on logout or session expiry."""
        with self._lock:
            self._session_caches.pop(session_id, None)

    # ── Cache accessors ───────────────────────────────────────────

    def clear_request_cache(self) -> None:
        self._request_caches.clear()

    def clear_session_cache(self) -> None:
        self._session_caches.clear()

    def clear_caches(self) -> None:
        self.clear_request_cache()
        self.clear_session_cache()

    def get_request_cache(self) -> dict[Any, object] | None:
        """
        Returns the cache for the currently active request.
        ContextVar.get() returns the value for THIS task/thread only.
        """
        request_id = self._request_id.get()     # ✅ isolated per task
        if request_id is None:
            return None
        return self._request_caches.get(request_id)

    def get_session_cache(self) -> dict[Any, object] | None:
        """Returns the cache for the currently active session."""
        session_id = self._session_id.get()     # ✅ isolated per task
        if session_id is None:
            return None
        return self._session_caches.get(session_id)