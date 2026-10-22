"""Cloud power providers for rjob-autostop (and later the VM client).

Each module exposes:
    state(cfg) -> str     RUNNING | STOPPED | STOPPING | STARTING | ... (provider states, upper case)
    start(cfg) -> None    asynchronous; returns once the provider accepted the request
    stop(cfg) -> None     asynchronous; returns once the provider accepted the request
and raises ProviderError on failure. cfg is the [<provider>] table of the config.
Only power operations: a provider never creates, deletes or modifies resources.
"""
# Created by: Alex Myasnikov
# Date: October 22, 2026


class ProviderError(Exception):
    """The provider refused or failed a request."""
