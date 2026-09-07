"""Asynchronous Python client for Russound RIO."""


class RussoundError(Exception):
    """A generic error."""


class CommandError(RussoundError):
    """A command sent to the controller caused an error."""


class MediaManagementInitializationTimeoutError(RussoundError):
    """A streamer acknowledged MMInit but did not return its menu page."""

    def __init__(self, source_device: str) -> None:
        """Initialize the error for the unresponsive source device."""
        super().__init__(
            f"Media Management initialization timed out waiting for a menu from "
            f"{source_device}"
        )
        self.source_device = source_device


class UncachedVariableError(RussoundError):
    """A variable was not found in the cache."""


class UnsupportedFeatureError(RussoundError):
    """A requested command is not supported on this controller."""


class UnsupportedRussoundVersionError(RussoundError):
    """The client implements an unsupported version of the Russound RIO API."""
