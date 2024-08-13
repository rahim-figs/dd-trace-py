"""
This custom pytest plugin implements tracing for pytest by using pytest hooks. The plugin registers tracing code
to be run at specific points during pytest execution. The most important hooks used are:

    * pytest_sessionstart: during pytest session startup, a custom trace filter is configured to the global tracer to
        only send test spans, which are generated by the plugin.
    * pytest_runtest_protocol: this wraps around the execution of a pytest test function, which we trace. Most span
        tags are generated and added in this function. We also store the span on the underlying pytest test item to
        retrieve later when we need to report test status/result.
    * pytest_runtest_makereport: this hook is used to set the test status/result tag, including skipped tests and
        expected failures.

"""
import os
from pathlib import Path
from typing import Dict  # noqa:F401

import pytest

from ddtrace.contrib.pytest._utils import _pytest_version_supports_itr


DDTRACE_HELP_MSG = "Enable tracing of pytest functions."
NO_DDTRACE_HELP_MSG = "Disable tracing of pytest functions."
DDTRACE_INCLUDE_CLASS_HELP_MSG = "Prepend 'ClassName.' to names of class-based tests."
PATCH_ALL_HELP_MSG = "Call ddtrace.patch_all before running tests."


def _is_enabled_early(early_config):
    """Checks if the ddtrace plugin is enabled before the config is fully populated.

    This is necessary because the module watchdog for coverage collection needs to be enabled as early as possible.

    Note: since coverage is used for ITR purposes, we only check if the plugin is enabled if the pytest version supports
    ITR
    """
    if not _pytest_version_supports_itr():
        return False

    if (
        "--no-ddtrace" in early_config.invocation_params.args
        or early_config.getini("no-ddtrace")
        or "ddtrace" in early_config.inicfg
        and early_config.getini("ddtrace") is False
    ):
        return False

    return "--ddtrace" in early_config.invocation_params.args or early_config.getini("ddtrace")


def is_enabled(config):
    """Check if the ddtrace plugin is enabled."""
    return (config.getoption("ddtrace") or config.getini("ddtrace")) and not config.getoption("no-ddtrace")


def pytest_addoption(parser):
    """Add ddtrace options."""
    group = parser.getgroup("ddtrace")

    group._addoption(
        "--ddtrace",
        action="store_true",
        dest="ddtrace",
        default=False,
        help=DDTRACE_HELP_MSG,
    )

    group._addoption(
        "--no-ddtrace",
        action="store_true",
        dest="no-ddtrace",
        default=False,
        help=NO_DDTRACE_HELP_MSG,
    )

    group._addoption(
        "--ddtrace-patch-all",
        action="store_true",
        dest="ddtrace-patch-all",
        default=False,
        help=PATCH_ALL_HELP_MSG,
    )

    group._addoption(
        "--ddtrace-include-class-name",
        action="store_true",
        dest="ddtrace-include-class-name",
        default=False,
        help=DDTRACE_INCLUDE_CLASS_HELP_MSG,
    )

    parser.addini("ddtrace", DDTRACE_HELP_MSG, type="bool")
    parser.addini("no-ddtrace", DDTRACE_HELP_MSG, type="bool")
    parser.addini("ddtrace-patch-all", PATCH_ALL_HELP_MSG, type="bool")
    parser.addini("ddtrace-include-class-name", DDTRACE_INCLUDE_CLASS_HELP_MSG, type="bool")


def pytest_load_initial_conftests(early_config, parser, args):
    if _is_enabled_early(early_config):
        # Enables experimental use of ModuleCodeCollector for coverage collection.
        from ddtrace.internal.ci_visibility.coverage import USE_DD_COVERAGE
        from ddtrace.internal.logger import get_logger
        from ddtrace.internal.utils.formats import asbool

        log = get_logger(__name__)

        COVER_SESSION = asbool(os.environ.get("_DD_COVER_SESSION", "false"))

        if USE_DD_COVERAGE:
            from ddtrace.ext.git import extract_workspace_path
            from ddtrace.internal.coverage.code import ModuleCodeCollector
            from ddtrace.internal.coverage.installer import install

            try:
                workspace_path = Path(extract_workspace_path())
            except ValueError:
                workspace_path = Path(os.getcwd())

            log.warning("Installing ModuleCodeCollector with include_paths=%s", [workspace_path])

            install(include_paths=[workspace_path], collect_import_time_coverage=True)
            if COVER_SESSION:
                ModuleCodeCollector.start_coverage()
        else:
            if COVER_SESSION:
                log.warning(
                    "_DD_COVER_SESSION must be used with _DD_USE_INTERNAL_COVERAGE but not DD_CIVISIBILITY_ITR_ENABLED"
                )


def pytest_configure(config):
    config.addinivalue_line("markers", "dd_tags(**kwargs): add tags to current span")
    if is_enabled(config):
        from ddtrace.internal.utils.formats import asbool

        if asbool(os.environ.get("_DD_CIVISIBILITY_USE_PYTEST_V2", "false")):
            from ddtrace.internal.logger import get_logger

            log = get_logger(__name__)

            log.warning("The new ddtrace pytest plugin is in beta and is not currently supported")
            from ._plugin_v2 import _PytestDDTracePluginV2

            config.pluginmanager.register(_PytestDDTracePluginV2(), "_datadog-pytest-v2")
            return

        from ._plugin_v1 import _PytestDDTracePluginV1

        config.pluginmanager.register(_PytestDDTracePluginV1(), "_datadog-pytest-v1")


@pytest.hookimpl
def pytest_addhooks(pluginmanager):
    from ddtrace.contrib.pytest import newhooks

    pluginmanager.add_hookspecs(newhooks)


@pytest.fixture(scope="function")
def ddspan(request):
    """Return the :class:`ddtrace._trace.span.Span` instance associated with the
    current test when Datadog CI Visibility is enabled.
    """
    from ddtrace.contrib.pytest._plugin_v1 import _extract_span
    from ddtrace.internal.ci_visibility import CIVisibility as _CIVisibility

    if _CIVisibility.enabled:
        return _extract_span(request.node)


@pytest.fixture(scope="session")
def ddtracer():
    """Return the :class:`ddtrace.tracer.Tracer` instance for Datadog CI
    visibility if it is enabled, otherwise return the default Datadog tracer.
    """
    import ddtrace
    from ddtrace.internal.ci_visibility import CIVisibility as _CIVisibility

    if _CIVisibility.enabled:
        return _CIVisibility._instance.tracer
    return ddtrace.tracer


@pytest.fixture(scope="session", autouse=True)
def patch_all(request):
    """Patch all available modules for Datadog tracing when ddtrace-patch-all
    is specified in command or .ini.
    """
    import ddtrace

    if request.config.getoption("ddtrace-patch-all") or request.config.getini("ddtrace-patch-all"):
        ddtrace.patch_all()
