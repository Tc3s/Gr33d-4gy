import sys
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.e2e.harness.env_factory import setup_test_environment, EnvironmentHarness


@pytest.fixture
def e2e_env(tmp_path: Path) -> EnvironmentHarness:
    """
    Hermetic sandbox fixture for opaque-box E2E tests.
    Sets up isolated HOME, PATH with mock agy, and credentials layout.
    """
    return setup_test_environment(tmp_path)


@pytest.fixture
def realworld_env(tmp_path: Path) -> EnvironmentHarness:
    """
    Hermetic sandbox fixture for real-world production workload scenarios.
    """
    return setup_test_environment(tmp_path)
