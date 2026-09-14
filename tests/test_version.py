"""``capsule_emit.__version__`` must match the installed distribution metadata.

The literal was hand-bumped and drifted (0.8.1 wheel reported "0.7.0"); this pins
the module attribute to the one version pyproject/PyPI actually publish.
"""

from importlib.metadata import version

import capsule_emit


def test_dunder_version_matches_distribution_metadata():
    assert capsule_emit.__version__ == version("capsule-emit")


def test_dunder_version_is_not_the_stale_literal():
    assert capsule_emit.__version__ != "0.7.0"
