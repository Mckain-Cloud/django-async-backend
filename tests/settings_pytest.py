"""Settings for the pytest sample run started by tests/test_pytest_plugin.py.

Reuses whatever settings module the outer test run uses and gives the
pytest run its own test database name, so both can run at the same time.
"""

import os
from importlib import import_module

_base = import_module(os.environ.get("DAB_BASE_SETTINGS", "settings"))
globals().update({k: v for k, v in vars(_base).items() if k.isupper()})

for _alias in ("default", "other"):
    DATABASES[_alias] = {  # noqa: F821
        **DATABASES[_alias],  # noqa: F821
        "TEST": {
            **DATABASES[_alias].get("TEST", {}),
            "NAME": "test_postgres_pytest",
        },  # noqa: F821
    }
