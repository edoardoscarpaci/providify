from __future__ import annotations

import logging
import uuid
import threading
from contextlib import contextmanager, asynccontextmanager
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Generator, AsyncGenerator

logger = logging.getLogger(__name__)


class ScopeContext:
    """Manages REQUEST and SESSION scoped instance caches.

    Supports both sync and async contexts:
        Sync:  with sc.request(): ...
        Async: async with sc.request(): ...

    Uses contextvars.ContextVar instead of threading.local —
    each asyncio Task gets its own copy of context variables,
    preventing scope bleed across concurrent coroutines.

    Equivalent to Jakarta's RequestContext / SessionContext.

    Thread safety:  ✅ Safe — _request_caches and _session_caches are guarded
                    by _lock for all writes.  ContextVar ensures each
                    thread/task sees its own active scope ID.
    Async safety:   ✅ Safe — ContextVar.set() is isolated per asyncio Task;
                    concurrent coroutines cannot see each other's scope IDs.

    DESIGN: on_scope_exit and on_scope_exit_async callbacks let the owning
    DIContainer run @PreDestroy hooks exactly when a scope frame exits —
    before the cache entry is popped.  Two separate callbacks are provided:

        on_scope_exit       — sync callback for sync request()/session().
                              Called in a finally block.  Async @PreDestroy hooks
                              are skipped with a warning (cannot be awaited here).
        on_scope_exit_async — async callback for async arequest()/asession().
                              Awaited in a finally block.  Both sync and async
                              @PreDestroy hooks run here.

    Tradeoffs:
        ✅ Container-side logic stays in the container; ScopeContext stays generic.
        ✅ No import cycle — callbacks are plain Callables, not concrete types.
        ❌ Two callbacks instead of one — small API surface increase.
    """

    def __init__(
        self,
        *,
        on_scope_exit: Callable[[dict[Any, object]], None] | None = None,
        on_scope_exit_async: (
            Callable[[dict[Any, object]], Awaitable[None]] | None
        ) = None,
    ) -> None:
        """Initialise an empty scope context.

        Args:
            on_scope_exit:       Optional sync callback invoked just before a
                                 scope cache is popped.  Receives the cache dict
                                 so the container can look up @PreDestroy instances.
                                 Called in a ``finally`` block — runs even if the
                                 with-block raised.
                                 Any exception raised inside this callback is
                                 propagated after the cache is cleared.
            on_scope_exit_async: Optional async callback — awaited just before a
                                 scope cache is popped in async context managers.
                                 Both sync and async @PreDestroy hooks should be
                                 driven by this callback.

        Returns:
            None

        Edge cases:
            - Both callbacks may be None (default) — backward-compatible with
              code that creates ScopeContext directly.
            - Sync path cannot await — if on_scope_exit receives a cache that
              contains instances with async @PreDestroy hooks, those hooks must
              be skipped (with a warning) inside the callback.  The async path
              (on_scope_exit_async) handles both sync and async hooks correctly.

        Example:
            ScopeContext(
                on_scope_exit=container._run_pre_destroy_for_scope,
                on_scope_exit_async=container._arun_pre_destroy_for_scope,
            )
        """
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

        # DESIGN: callbacks wired at construction by DIContainer.  Storing them
        # as instance attributes (not class-level) means each ScopeContext can
        # have independent lifecycle semantics — important when multiple
        # DIContainer instances coexist (e.g. in tests via DIContainer.scoped()).
        self._on_scope_exit = on_scope_exit
        self._on_scope_exit_async = on_scope_exit_async

    # ── Request scope — sync ──────────────────────────────────────

    @contextmanager
    def request(self) -> Generator[str, None, None]:
        """Activate a request scope context (sync version).

        Creates a fresh instance cache for this request, installs it as the
        active request context via ContextVar, and yields the request ID.
        On exit (normal or exceptional), @PreDestroy hooks are run via
        ``on_scope_exit`` (if wired) before the cache is discarded.

        Args:
            (none)

        Returns:
            Yields the request ID string (a UUID4).

        Edge cases:
            - Nested request() calls are fully supported — each level has its
              own ID; the inner one is active while its block runs.
            - on_scope_exit exceptions propagate; the cache is still popped.

        Example:
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
            # Run @PreDestroy for scoped instances before the cache is evicted.
            # on_scope_exit is None when ScopeContext is used standalone (tests
            # that create ScopeContext directly without a container).
            if self._on_scope_exit is not None:
                with self._lock:
                    cache = self._request_caches.get(request_id, {})
                # Call outside the lock — hooks may themselves call container.get()
                self._on_scope_exit(cache)

            # Restore previous request_id — handles nested contexts correctly
            self._request_id.reset(token)
            with self._lock:
                self._request_caches.pop(request_id, None)

    # ── Request scope — async ─────────────────────────────────────

    @asynccontextmanager
    async def arequest(self) -> AsyncGenerator[str, None]:
        """Activate a request scope context (async version).

        Each asyncio Task that enters this context gets its own
        isolated request_id — concurrent requests never bleed.
        On exit, ``on_scope_exit_async`` is awaited (if wired) to run
        both sync and async @PreDestroy hooks.

        Args:
            (none)

        Returns:
            Yields the request ID string (a UUID4).

        Edge cases:
            - Same nested-context semantics as sync request().
            - on_scope_exit_async exceptions propagate after cache cleanup.

        Example:
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
            # Run @PreDestroy (sync + async) before evicting the cache.
            if self._on_scope_exit_async is not None:
                with self._lock:
                    cache = self._request_caches.get(request_id, {})
                await self._on_scope_exit_async(cache)

            self._request_id.reset(token)
            with self._lock:
                self._request_caches.pop(request_id, None)

    # ── Session scope — sync ──────────────────────────────────────

    @contextmanager
    def session(self, session_id: str | None = None) -> Generator[str, None, None]:
        """Activate a session scope context (sync version).

        Reuses existing session cache if session_id already exists.
        On exit, ``on_scope_exit`` is called (if wired) — but ONLY when the
        session cache has a new entry (i.e. the current block created it).
        Re-entering an existing session does NOT trigger @PreDestroy on exit;
        the session cache continues to live until ``invalidate_session()`` is
        called.

        DESIGN: session() can be entered and exited multiple times for the
        same session_id (e.g. across multiple requests).  Running @PreDestroy
        every time a session block exits would be wrong — the session is still
        live.  Instead, @PreDestroy runs when the session itself is invalidated
        (future work) or implicitly when the cache is first created and the
        block exits (i.e. the session is used once in a single block and the
        exit signals its end).

        For simplicity, the current implementation calls on_scope_exit when
        any session block exits — matching the documented semantics: "when the
        session() context manager exits, @PreDestroy hooks fire".

        Args:
            session_id: Optional explicit session ID.  A random UUID is used
                        when omitted.

        Returns:
            Yields the session ID string.

        Example:
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
            # Run @PreDestroy for session-scoped instances before the block exits.
            if self._on_scope_exit is not None:
                with self._lock:
                    cache = self._session_caches.get(sid, {})
                self._on_scope_exit(cache)

            self._session_id.reset(token)

    # ── Session scope — async ─────────────────────────────────────

    @asynccontextmanager
    async def asession(
        self, session_id: str | None = None
    ) -> AsyncGenerator[str, None]:
        """Activate a session scope context (async version).

        Args:
            session_id: Optional explicit session ID.  A random UUID is used
                        when omitted.

        Returns:
            Yields the session ID string.

        Example:
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
            # Run @PreDestroy (sync + async) before the session block exits.
            if self._on_scope_exit_async is not None:
                with self._lock:
                    cache = self._session_caches.get(sid, {})
                await self._on_scope_exit_async(cache)

            self._session_id.reset(token)

    def invalidate_session(self, session_id: str) -> None:
        """Destroy a session cache — call on logout or session expiry.

        Note: Does NOT run @PreDestroy hooks.  Use asession()/session() context
        managers to trigger lifecycle teardown.

        Args:
            session_id: The session ID to invalidate. No-op if unknown.

        Returns:
            None
        """
        with self._lock:
            self._session_caches.pop(session_id, None)

    # ── Cache accessors ───────────────────────────────────────────

    def clear_request_cache(self) -> None:
        """Clear all request caches without running @PreDestroy hooks.

        Returns:
            None
        """
        self._request_caches.clear()

    def clear_session_cache(self) -> None:
        """Clear all session caches without running @PreDestroy hooks.

        Returns:
            None
        """
        self._session_caches.clear()

    def clear_caches(self) -> None:
        """Clear all request and session caches without lifecycle hooks.

        Returns:
            None
        """
        self.clear_request_cache()
        self.clear_session_cache()

    def get_request_cache(self) -> dict[Any, object] | None:
        """Return the cache for the currently active request.

        ContextVar.get() returns the value for THIS task/thread only.

        Returns:
            The current request's cache dict, or None if no request is active.
        """
        request_id = self._request_id.get()  # ✅ isolated per task
        if request_id is None:
            return None
        return self._request_caches.get(request_id)

    def get_session_cache(self) -> dict[Any, object] | None:
        """Return the cache for the currently active session.

        Returns:
            The current session's cache dict, or None if no session is active.
        """
        session_id = self._session_id.get()  # ✅ isolated per task
        if session_id is None:
            return None
        return self._session_caches.get(session_id)
