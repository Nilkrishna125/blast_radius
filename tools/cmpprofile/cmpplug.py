"""pytest plugin for the comparison-swap profile: names the bucket for each phase."""
import pytest
import sitecustomize


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    sitecustomize.CURRENT[0] = item.nodeid + "|setup"
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    sitecustomize.CURRENT[0] = item.nodeid
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    sitecustomize.CURRENT[0] = item.nodeid + "|setup"
    yield
    sitecustomize.CURRENT[0] = "between"


def pytest_sessionfinish(session, exitstatus):
    sitecustomize._dump()


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    # importing a test module runs its module-level code: every test in the file depends on it
    previous = sitecustomize.CURRENT[0]
    if isinstance(collector, pytest.Module):
        sitecustomize.CURRENT[0] = "collect:" + collector.nodeid
    yield
    sitecustomize.CURRENT[0] = previous
