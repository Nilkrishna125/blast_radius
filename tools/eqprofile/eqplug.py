"""pytest plugin: write the equality profile when each (xdist worker) session ends."""
import sitecustomize


def pytest_sessionfinish(session, exitstatus):
    if getattr(sitecustomize, "OUT", None):
        sitecustomize._dump()
