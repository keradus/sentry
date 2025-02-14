import logging
from collections.abc import Callable, MutableMapping
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, TypeVar

import sentry_sdk

logger = logging.getLogger(__name__)


class TimeoutException(Exception):
    def __init__(self, elapsed: timedelta, timeout: timedelta, *args: object) -> None:
        super().__init__(*args)
        self._elapsed = elapsed
        self._timeout = timeout


class TimeChecker:
    """Interface to check whether a timeout has been hit.

    The class is initialized with the provided hard timeout. If the timedelta is
    not bigger than `0`, no checks are performed.  Calling `check` checks the
    timeout, and raises a `TimeoutException` if it's hit. The timeout starts at
    the moment the class is initialized.
    """

    def __init__(self, hard_timeout: timedelta) -> None:
        self._hard_timeout = hard_timeout
        self._start = datetime.now(timezone.utc)

    def check(self) -> None:
        if self._hard_timeout <= timedelta(0):
            return

        now = datetime.now(timezone.utc)
        elapsed = now - self._start
        if elapsed >= self._hard_timeout:
            raise TimeoutException(elapsed, self._hard_timeout)


class ExperimentalConfigBuilder(Protocol):
    def __call__(self, timeout: TimeChecker, *args, **kwargs) -> Any:
        pass


#: Timeout for an experimental feature build.
_FEATURE_BUILD_TIMEOUT = timedelta(seconds=20)


def add_experimental_config(
    config: MutableMapping[str, Any],
    key: str,
    function: ExperimentalConfigBuilder,
    *args: Any,
    **kwargs: Any,
) -> None:
    """Try to set `config[key] = function(*args, **kwargs)`.
    If the result of the function call is None, the key is not set.
    If the function call raises an exception, we log it to sentry and the key remains unset.
    NOTE: Only use this function if you expect Relay to behave reasonably
    if ``key`` is missing from the config.
    """

    if subconfig := build_safe_config(key, function, *args, **kwargs):
        config[key] = subconfig


R = TypeVar("R")
R_default = TypeVar("R_default")


def build_safe_config(
    key: str,
    function: Callable[..., R],
    *args: Any,
    default_return: R_default | None = None,
    **kwargs: Any,
) -> R | R_default | None:
    """
    Runs a config builder function with a timeout.
    If the function call raises an exception, we log it to sentry and return value passed as
    `default_return` parameter (by default this is `None`).
    """
    timeout = TimeChecker(_FEATURE_BUILD_TIMEOUT)

    with sentry_sdk.start_span(op=f"project_config.build_safe_config.{key}"):
        try:
            return function(timeout, *args, **kwargs)
        except TimeoutException as e:
            logger.exception(
                "Project config feature build timed out: %s",
                key,
                extra={"hard_timeout": e._timeout, "elapsed": e._elapsed},
            )
        except Exception:
            logger.exception("Exception while building Relay project config field")

    return default_return
