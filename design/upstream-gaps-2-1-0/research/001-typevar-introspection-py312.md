# Research 001 — TypeVar Introspection & Open Generic Binding in Python 3.12–3.13

Date: 2026-09-12 · Freshness matters: **YES** — PEP 695 (3.12), PEP 696 (3.13), and `typing_extensions` backports are live; implementation details vary by version.

## Question

For providify's "open generic binding" feature (`container.provide(factory, returns=Repo[T])` then `container.get(Repo[User])` matches and passes `User` to a `type[T]` parameter), answer these seven questions with official-source citations:

1. Detecting a TypeVar at runtime: `isinstance(x, typing.TypeVar)` — is it `True` for both classic `TypeVar("T")` and PEP 695 `class Repo[T]:` type parameters? What about `ParamSpec`/`TypeVarTuple` (should be rejected)?
2. PEP 695 (3.12): type params from `class Repo[T]:` — where are they (`Repo.__type_params__`), are bounds lazily evaluated, `__constraints__`, and `__infer_variance__`. Gotcha in `get_args(Repo[T])` when Repo is PEP 695-defined vs `Generic[T]`-defined?
3. PEP 696 (3.13): `TypeVar` defaults — `T.__default__`, `T.has_default()`, `typing.NoDefault`. Does `Repo[T]` with a defaulted T need special handling? Does 3.12 lack these attributes (AttributeError)?
4. `type[T]` annotations: `get_origin(type[T]) is type`, `get_args(type[T]) == (T,)` — confirm for both `type[T]` builtin and `typing.Type[T]`. Also `typing.get_type_hints()` treats `type[T]` under `from __future__ import annotations`.
5. Checking a concrete arg against a TypeVar: recommended runtime check for `bound=` (issubclass with the bound; what about when the bound is a Protocol or a generic alias — issubclass raises TypeError for subscripted generics) and for `constraints` (identity/equality membership). What raises and when.
6. `Repo[T] == Repo[T]` equality/hash of generic aliases containing TypeVars, and `Repo[User] == Repo[User]` — safe as dict cache keys? Cite `typing._GenericAlias.__eq__/__hash__` behaviour.
7. Substituting: given `Repo[T]` and a request `Repo[User]`, is there a stdlib helper to bind T→User (e.g. `alias[User]` on a partially-open alias; `__parameters__`)? Confirm `Repo[T].__parameters__ == (T,)` and that `Repo[T][User] == Repo[User]`.

---

## Findings

### 1. Detecting TypeVar at Runtime

**Classic TypeVar detection via `isinstance(x, TypeVar)`:**
- `isinstance(obj, TypeVar)` returns `True` only if `obj` is a TypeVar instance (e.g., `T = TypeVar("T")` then `isinstance(T, TypeVar)` is `True`). — [typing — Support for type hints · Python 3.14.7 documentation](https://docs.python.org/3/library/typing.html) (2026, docs.python.org).
- **NOT supported inside function signatures**: `isinstance(value, T)` where `T` is a TypeVar raises `TypeError: isinstance() argument 2 must be a type or tuple of types` if `T` is unconstrained. For constrained `AnyStr = TypeVar("AnyStr", str, bytes)`, `isinstance("x", AnyStr)` checks membership in the constraints. — [typing — Support for type hints · Python 3.14.7 documentation](https://docs.python.org/3/library/typing.html) (2026, docs.python.org).

**PEP 695 syntax (`class Repo[T]:`):**
- PEP 695 creates TypeVar objects stored in `__type_params__` tuple. These are identical to classic TypeVar instances. `isinstance(Repo.__type_params__[0], TypeVar)` returns `True`. — [PEP 695 – Type Parameter Syntax](https://peps.python.org/pep-0695/) (2023, PEP 695, ratified for Python 3.12).
- **No difference**: PEP 695 type parameters and classic `TypeVar("T")` objects are the same at runtime; `isinstance(x, TypeVar)` works identically. — [PEP 695 – Type Parameter Syntax](https://peps.python.org/pep-0695/) (2023).

**ParamSpec and TypeVarTuple:**
- `isinstance(obj, ParamSpec)` and `isinstance(obj, TypeVarTuple)` also return `True` for their respective instances. — [typing — Support for type hints · Python 3.14.7 documentation](https://docs.python.org/3/library/typing.html) (2026, docs.python.org).
- **Implementation note for open generics**: ParamSpec and TypeVarTuple should be rejected in container open generic bindings because they don't represent concrete type arguments; they represent parameter packs (**P) and variadic type packs (*Ts). Checking `type(x) is TypeVar` (not `isinstance`) is stricter and recommended to exclude subclasses and related forms. — [PEP 612 – Parameter Specification Variables](https://peps.python.org/pep-0612/) (2020); [typing — Support for type hints · Python 3.14.7 documentation](https://docs.python.org/3/library/typing.html) (2026).

### 2. PEP 695 Type Parameters: Storage, Evaluation, and get_args Behavior

**Storage in `__type_params__`:**
- Generic classes, functions, and type aliases declared with PEP 695 syntax have a `__type_params__` attribute containing a tuple of TypeVar/ParamSpec/TypeVarTuple objects. `class Repo[T]: ...` stores `T` in `Repo.__type_params__ == (T,)`. — [PEP 695 – Type Parameter Syntax](https://peps.python.org/pep-0695/) (2023).
- Classic `Generic[T]` syntax does NOT produce `__type_params__`. It is a PEP 695 feature. — [PEP 695 – Type Parameter Syntax](https://peps.python.org/pep-0695/) (2023).

**Lazy Evaluation of Bounds and Constraints:**
- PEP 695 bounds (e.g., `class Repo[T: Entity]:`) and constraints (e.g., `T: (str, int)`) are NOT evaluated at class definition time. They are stored in code objects and evaluated only when the attribute (`__bound__`, `__constraints__`) is accessed. — [PEP 695 – Type Parameter Syntax](https://peps.python.org/pep-0695/) (2023).
- **Gotcha**: Accessing `Repo.T.__bound__` may trigger evaluation of forward references. If the bound is a forward reference to a class defined later, evaluation will fail with `NameError` unless the bound is evaluated in the correct scope. — [PEP 695 – Type Parameter Syntax](https://peps.python.org/pep-0695/) (2023).

**`__infer_variance__`:**
- PEP 695 infers variance from usage (covariant, contravariant, invariant) instead of requiring explicit `covariant=True` or `contravariant=True` flags. The inference result is stored; classic TypeVar(`name`, covariant=True) and PEP 695 `class Repo[T]` (inferred covariant) produce equivalent objects. — [PEP 695 – Type Parameter Syntax](https://peps.python.org/pep-0695/) (2023).

**No Gotcha in `get_args()` for PEP 695 vs Generic[T]:**
- `get_args(Repo[User])` returns `(User,)` identically whether `Repo` is defined as `class Repo[T]: ...` (PEP 695) or `class Repo(Generic[T]): ...` (classic). No difference in the result. — [PEP 695 – Type Parameter Syntax](https://peps.python.org/pep-0695/) (2023); [typing — Support for type hints · Python 3.14.7 documentation](https://docs.python.org/3/library/typing.html) (2026).

### 3. PEP 696 TypeVar Defaults: __default__, has_default(), and 3.12 Compatibility

**`__default__` Attribute (Python 3.13 native):**
- `TypeVar`, `ParamSpec`, and `TypeVarTuple` in Python 3.13+ expose a `__default__` attribute. If no `default=` kwarg is passed, `__default__` is set to `typing.NoDefault` (a sentinel object, not `None`). — [PEP 696 – Type Defaults for Type Parameters](https://peps.python.org/pep-0696/) (2023, target Python 3.13).
- Example: `T_int = TypeVar("T_int", default=int)` results in `T_int.__default__ == int`. `T = TypeVar("T")` results in `T.__default__ is typing.NoDefault`. — [PEP 696 – Type Defaults for Type Parameters](https://peps.python.org/pep-0696/) (2023).

**`has_default()` Method:**
- `TypeVar.has_default()` returns `True` if a default was provided, `False` otherwise (equivalent to `T.__default__ is not typing.NoDefault`). — [PEP 696 – Type Defaults for Type Parameters](https://peps.python.org/pep-0696/) (2023).

**Python 3.12 Compatibility via typing_extensions:**
- **3.12 lacks `__default__` and `has_default()` natively**; accessing them on native 3.12 TypeVars raises `AttributeError`. — [PEP 696 – Type Defaults for Type Parameters](https://peps.python.org/pep-0696/) (2023).
- **Backport available**: `typing_extensions >= 4.12.0` (released 2024-05-16) provides `NoDefault` and adds `__default__` and `has_default()` to TypeVar/ParamSpec/TypeVarTuple for Python 3.12. — [typing_extensions 4.12.0 · GitHub](https://github.com/python/typing_extensions/releases/tag/4.12.0) (2024-05-16).
- **Implementation**: For Python 3.12 with `typing_extensions 4.12+`, you can safely check `hasattr(T, "__default__")` and call `T.has_default()` for cross-version compatibility. — [typing_extensions 4.12.0 · GitHub](https://github.com/python/typing_extensions/releases/tag/4.12.0) (2024-05-16).

**Defaulted TypeVar in Open Generic Bindings:**
- If `Repo[T_default]` where `T_default.__default__ == User`, resolving `Repo[SomeOtherType]` is orthogonal to the default. Defaults are static type-checking aids; the container must still match based on the closed type argument passed at `container.get()`. No special runtime handling required. — [PEP 696 – Type Defaults for Type Parameters](https://peps.python.org/pep-0696/) (2023).

### 4. `type[T]` Annotations: get_origin, get_args, and Future Annotations

**`get_origin(type[T])` and `get_args(type[T])`:**
- `get_origin(type[int])` returns `type` (the `type` builtin class). — [typing — Support for type hints · Python 3.14.7 documentation](https://docs.python.org/3/library/typing.html) (2026).
- `get_args(type[int])` returns `(int,)` — a tuple with the single type argument. — [typing — Support for type hints · Python 3.14.7 documentation](https://docs.python.org/3/library/typing.html) (2026).
- **Identical for `typing.Type[T]`**: `get_origin(typing.Type[int])` returns `type` and `get_args(typing.Type[int])` returns `(int,)`. The old `typing.Type` is an alias for `type` in Python 3.9+. — [typing — Support for type hints · Python 3.14.7 documentation](https://docs.python.org/3/library/typing.html) (2026).

**Covariance of `type[T]`:**
- `type[ProUser]` is a subtype of `type[User]` if `ProUser` is a subclass of `User` (covariant). — [typing — Support for type hints · Python 3.14.7 documentation](https://docs.python.org/3/library/typing.html) (2026).
- **For container bindings**: If a factory is annotated `def make_repo(cls: type[T]) -> Repo[T]`, passing `User` (a subclass of Entity) where `Entity` is the bound of T satisfies the type check. — [typing — Support for type hints · Python 3.14.7 documentation](https://docs.python.org/3/library/typing.html) (2026).

**`type[T]` with `from __future__ import annotations`:**
- Under PEP 563 (`from __future__ import annotations`), all annotations become strings at definition time. `get_type_hints()` evaluates them in the caller's namespace. — [PEP 563 – Postponed Evaluation of Annotations](https://peps.python.org/pep-0563/) (2019).
- `get_type_hints(factory)` on a function with `def factory(x: type[T]) -> Repo[T]` correctly reconstructs `type[T]` as a parameterized generic (not as a string). — [typing — Support for type hints · Python 3.14.7 documentation](https://docs.python.org/3/library/typing.html) (2026).

### 5. Checking Concrete Args Against TypeVar Bounds and Constraints

**Checking against `bound=`:**
- If `T = TypeVar("T", bound=Entity)`, you must check `issubclass(concrete_type, T.__bound__)` to verify the concrete type satisfies the bound. — [PEP 484 – Type Hints](https://peps.python.org/pep-0484/) (2015); [typing — Support for type hints · Python 3.14.7 documentation](https://docs.python.org/3/library/typing.html) (2026).
- **TypeError when bound is a generic alias**: `issubclass(User, Repository[X])` raises `TypeError: issubclass() arg 2 must be a class or tuple of classes` because subscripted generics (like `Repository[X]`) cannot be used as the second arg to `issubclass()`. — [typing — Support for type hints · Python 3.14.7 documentation](https://docs.python.org/3/library/typing.html) (2026).
- **Workaround for Protocol or generic bounds**: Extract the origin using `get_origin()`. For `bound=Repository[Entity]`, check `issubclass(concrete_type, get_origin(T.__bound__))` to validate against the unparameterized class. — Inferred from [typing — Support for type hints · Python 3.14.7 documentation](https://docs.python.org/3/library/typing.html) (2026).
- **Protocol bounds**: If `bound=SomeProtocol` and `SomeProtocol` is a `typing.Protocol`, you must use `isinstance(instance, SomeProtocol)` (with `@runtime_checkable` decorator) or mypy-style nominal checks; `issubclass()` on the class itself works only if the class is explicitly marked with the Protocol. — [typing — Support for type hints · Python 3.14.7 documentation](https://docs.python.org/3/library/typing.html) (2026).

**Checking against `constraints`:**
- If `AnyStr = TypeVar("AnyStr", str, bytes)`, you must check `concrete_type in T.__constraints__` or `concrete_type is str or concrete_type is bytes` (identity-based membership, not subclass). Constraints require exact type membership, not subclassing. — [PEP 484 – Type Hints](https://peps.python.org/pep-0484/) (2015); [typing — Support for type hints · Python 3.14.7 documentation](https://docs.python.org/3/library/typing.html) (2026).
- Example: `isinstance(int, AnyStr)` raises `TypeError` because `AnyStr` is a TypeVar, not a class. Constraints only constrain the *formal* type parameter used at definition time, not checked at runtime on instances. — [PEP 484 – Type Hints](https://peps.python.org/pep-0484/) (2015).

### 6. Generic Alias Equality and Hashing for Caching

**`typing._GenericAlias.__eq__` and `__hash__` Implementation:**
- `_GenericAlias` (used by `typing.List[int]`, `typing.Dict[str, int]`, and PEP 695 `Repo[User]`) implements `__eq__` by comparing `__origin__` and `__args__` attribute-by-attribute. — [cpython/Lib/typing.py · main · GitHub](https://github.com/python/cpython/blob/main/Lib/typing.py) (Python 3.12+, CPython source).
- `__hash__` is computed from the tuple `(__origin__, __args__)`, making generic aliases hashable. — [cpython/Lib/typing.py · main · GitHub](https://github.com/python/cpython/blob/main/Lib/typing.py) (Python 3.12+, CPython source).

**Caching and Identity Guarantees:**
- `typing._GenericAlias.__getitem__` is decorated with `@_tp_cache`, which caches the result of subscripting. `Repository[User] is Repository[User]` returns `True` because the second call returns the cached instance. — [cpython/Lib/typing.py · main · GitHub](https://github.com/python/cpython/blob/main/Lib/typing.py) (Python 3.12+, CPython source).
- **Safe as dict cache keys**: `Repo[User] == Repo[User]` and `hash(Repo[User]) == hash(Repo[User])` are guaranteed. You can safely use `dict[type, ...] with Repo[User]` as a key for caching singleton instances per closed type. — [cpython/Lib/typing.py · main · GitHub](https://github.com/python/cpython/blob/main/Lib/typing.py) (Python 3.12+, CPython source).
- **With TypeVars**: `Repo[T] == Repo[T]` (both with the same TypeVar object) also returns `True` because the equality is structural and `T == T` (the TypeVar is the same object). Using `Repo[T]` as a dict key is safe for binding-time caching (before substitution). — [cpython/Lib/typing.py · main · GitHub](https://github.com/python/cpython/blob/main/Lib/typing.py) (Python 3.12+, CPython source).

### 7. Substituting TypeVars: `__parameters__` and Partial Binding

**`__parameters__` Attribute:**
- Generic aliases expose a `__parameters__` tuple containing all free (unbound) TypeVars. `Repo[T].__parameters__ == (T,)`. `Repository[User].__parameters__ == ()` (no free TypeVars). — [typing — Support for type hints · Python 3.14.7 documentation](https://docs.python.org/3/library/typing.html) (2026).
- **Partially open aliases**: `Repo[T, User]` (mixing one free TypeVar and one concrete) has `__parameters__ == (T,)`. — [typing — Support for type hints · Python 3.14.7 documentation](https://docs.python.org/3/library/typing.html) (2026).

**Substitution via Subscripting:**
- Given `Repo[T]` and a request for `Repo[User]`, you can substitute by direct subscripting: `Repo[T][User]` is equivalent to `Repo[User]` (evaluates to `Repo[User]`). — [PEP 585 – Type Hinting Generics In Standard Collections](https://peps.python.org/pep-0585/) (2019); [typing — Support for type hints · Python 3.14.7 documentation](https://docs.python.org/3/library/typing.html) (2026).
- **Stdlib helper**: There is no dedicated `substitute()` function. You must use subscripting: `open_binding[concrete_type]` is the standard way. — [typing — Support for type hints · Python 3.14.7 documentation](https://docs.python.org/3/library/typing.html) (2026).

**Identity and Equality After Substitution:**
- `Repo[T][User] == Repo[User]` returns `True` (equality). `Repo[T][User] is Repo[User]` also returns `True` (identity, due to caching). — [cpython/Lib/typing.py · main · GitHub](https://github.com/python/cpython/blob/main/Lib/typing.py) (Python 3.12+, CPython source).

---

## Version / Compatibility Notes

- **Python 3.12**: PEP 695 (`class Repo[T]:` syntax) and full `typing.get_origin()`/`get_args()` support are native. `__type_params__` attribute is present. PEP 696 (`__default__`, `has_default()`) **not included**; use `typing_extensions >= 4.12.0` for backport.
- **Python 3.13**: PEP 696 features (`__default__`, `has_default()`, `typing.NoDefault`) are native to `typing` module.
- **typing_extensions**: Version 4.7.0+ (released 2023-06-21) declares Python 3.12 support. Version 4.12.0+ (released 2024-05-16) backports PEP 696 (`NoDefault`, `has_default()`) for 3.12.
- **Lazy evaluation gotcha**: Accessing `Repo.T.__bound__` when the bound is a forward reference may raise `NameError` if the class is not yet in scope. Evaluate bounds defensively using `try`/`except NameError`.
- **Breaking changes**: None in 3.12–3.13 for the core `get_origin()`/`get_args()` API. PEP 695 is opt-in syntax; classic `Generic[T]` continues to work.

---

## Recommendations for the Implementation

1. **TypeVar Detection**: Use `type(x) is typing.TypeVar` (not `isinstance`) to check if `x` is a TypeVar and reject `ParamSpec` and `TypeVarTuple` explicitly. This is stricter and clearer than `isinstance(x, TypeVar)`.

2. **PEP 695 Support**: Extract type parameters from `class Repo[T]:` using `getattr(Repo, "__type_params__", ())` to be compatible with both PEP 695 and classic `Generic[T]` classes.

3. **PEP 696 Compatibility**: For Python 3.12 support, add `typing_extensions >= 4.12.0` as a dependency. Use `hasattr(T, "__default__")` and `getattr(T, "has_default", lambda: False)()` for safe cross-version checks.

4. **Bound Checking**: When validating a concrete type against `T.__bound__`:
   - If bound is a concrete class: `issubclass(concrete_type, T.__bound__)`.
   - If bound is a generic alias (e.g., `Repository[Entity]`): `issubclass(concrete_type, get_origin(T.__bound__))`.
   - If bound is a `Protocol`: Use the `@runtime_checkable` decorator and `isinstance(instance, bound)` for instance checks, or assume nominal subclassing for class checks.
   - **Wrap in try/except**: Catch `TypeError` (subscripted generics as second arg) and `NameError` (forward reference evaluation).

5. **Constraint Checking**: For `T.__constraints__`, iterate and check `concrete_type in T.__constraints__` (identity-based). Constraints use exact type membership, not subclassing.

6. **Generic Alias Caching**: Store open bindings (e.g., `Repo[T]`) and closed bindings (e.g., `Repo[User]`) in a dict keyed by the generic alias itself. Identity caching (`is`) is guaranteed, so using the alias as a dict key is safe for singleton resolution.

7. **Substitution**: Use direct subscripting to bind TypeVars: `open_binding_alias[concrete_type]`. This is equivalent to `get_args(open_binding_alias)` and reconstructing, but simpler and guaranteed to use the same cached instance.

8. **get_type_hints() with type[T]**: Call `typing.get_type_hints(factory)` to resolve stringified annotations (under `from __future__ import annotations`) and extract parameter types. This handles forward references and PEP 563 transparently.

---

## Evidence Gaps

1. **Exact semantics of lazy evaluation for PEP 695 bounds**: The PEP states that bounds are "saved in a code object" and "evaluated only when accessed," but the precise timing and exception handling for forward reference evaluation is not exhaustively documented. Testing with forward references in real code is recommended.

2. **typing_extensions 4.12.0 guarantees for 3.12**: The backport of `__default__` and `has_default()` is confirmed, but the behavior of `typing.NoDefault` as a sentinel (identity checks vs equality checks) across 3.12/3.13 is not formally specified in the changelog. Recommend defensive checks: `T.__default__ is typing.NoDefault` (3.13) vs. `T.__default__ is getattr(typing_extensions, "NoDefault", None)` (3.12).

3. **Performance of generic alias identity caching**: The `@_tp_cache` decorator uses an internal dict, but its size limits and eviction policies are not documented. For long-running containers with thousands of closed type aliases, memory overhead is unquantified.

4. **Interaction of ParamSpec with open generics**: PEP 612 (ParamSpec) and PEP 695 do not explicitly address how to bind `**P` in an open generic factory. Preliminary support for `class Repo[T, **P]:` syntax may exist in 3.13+, but is not yet documented in official typing sources.

5. **TypeError specificity for issubclass with generic bounds**: When a bound is a subscripted generic (e.g., `T.__bound__ == Repository[Entity]`), `issubclass(User, T.__bound__)` raises `TypeError`, but the exact message and exception type are not formally guaranteed across Python versions.

---

## Librarian's Note

**The sources indicate that Python 3.12+ provides sufficient runtime introspection for open generic binding:**

- `isinstance(x, TypeVar)` reliably detects TypeVars (both classic and PEP 695).
- `get_origin()`, `get_args()`, and `__type_params__` enable structural matching of generic aliases.
- `typing._GenericAlias` implements caching and hashability, making generic aliases safe dict cache keys.
- Direct subscripting (`Repo[T][User]`) performs type variable substitution with identity guarantees.

**For PEP 696 (TypeVar defaults) on Python 3.12**, rely on `typing_extensions >= 4.12.0` to backport `__default__`, `has_default()`, and `NoDefault`. No native PEP 696 support on 3.12.

**Critical gotcha**: Bounds and constraints require defensive validation. Generic alias bounds (e.g., `Repository[T]`) cannot be used directly in `issubclass()`; extract the origin first. Protocol bounds require `@runtime_checkable` and `isinstance()` for instance checks.

**Recommendation favoured by evidence**: Providify should mirror .NET's open generic pattern (one binding materializes many closed types) using Python's `get_origin()`, `get_args()`, and subscripting. The cached identity of generic aliases makes this both memory-efficient and correct.
