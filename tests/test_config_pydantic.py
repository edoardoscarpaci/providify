"""Failing tests (RED mode) for Plan 006 — the pydantic `model_validate` hand-off.

Encodes `plans/006-configuration-binding.md` Step 17. Guarded by
`pytest.importorskip("pydantic")` so this file degrades gracefully in
environments (like this one, at the time of writing) that have not yet added
`pydantic>=2.13` to `[dependency-groups].dev` (plan Step 16).

Covered:
    - a BaseModel target binds via the model_validate path
    - a pydantic ValidationError is wrapped into ConfigBindingError with
      __cause__ preserved
    - no coercion happens on the pydantic path (an int field receiving "20"
      is handed through as the string)
    - `import providify` alone does not import pydantic (subprocess check)
"""

from __future__ import annotations

import subprocess
import sys

import pytest

pydantic = pytest.importorskip("pydantic")

from providify import ConfigBindingError, DictSource  # noqa: E402
from providify.config import bind_config_object  # noqa: E402


class _Settings(pydantic.BaseModel):
    url: str
    pool_size: int = 5


class TestPydanticModelValidatePath:
    def test_base_model_target_binds_via_model_validate(self) -> None:
        result = bind_config_object(_Settings, [DictSource({"url": "postgres://x"})])

        assert isinstance(result, _Settings)
        assert result.url == "postgres://x"

    def test_pydantic_validation_error_wraps_into_config_binding_error(self) -> None:
        with pytest.raises(ConfigBindingError) as exc:
            bind_config_object(_Settings, [DictSource({})])  # missing required url

        assert isinstance(exc.value.__cause__, pydantic.ValidationError)

    def test_no_coercion_on_pydantic_path_int_field_receives_raw_string(self) -> None:
        """providify performs no coercion on the pydantic path — the string
        "20" is handed to pydantic verbatim, and pydantic itself coerces it.
        """
        result = bind_config_object(
            _Settings, [DictSource({"url": "x", "pool_size": "20"})]
        )

        assert result.pool_size == 20  # pydantic did the coercion, not providify


class TestCoreImportDoesNotPullInPydantic:
    def test_import_providify_alone_does_not_import_pydantic(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys, providify; "
                "assert 'pydantic' not in sys.modules, "
                "'providify must not import pydantic at import time'",
            ],
            capture_output=True,
            text=True,
        )

        assert proc.returncode == 0, proc.stderr
