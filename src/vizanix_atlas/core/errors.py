"""Typed error taxonomy.

Atlas processes untrusted payloads from many venues, and a single venue's failure
must never become a global failure (see ``docs/QUALITY.md``). Errors are therefore
classified so that a caller can decide whether a condition is retryable, whether
it should trip a circuit breaker, and whether it should be recorded against a
venue's health rather than aborting a generation.

``except Exception`` is not used anywhere in the collection path. Callers catch
:class:`AtlasError` subclasses and record them.
"""

from __future__ import annotations

from typing import Any


class AtlasError(Exception):
    """Base class for every error Atlas raises deliberately.

    Attributes:
        retryable: Whether repeating the identical operation could plausibly
            succeed. Used by the retry policy; a non-retryable error is recorded
            immediately rather than re-attempted.
        counts_against_health: Whether the condition reflects on the venue's
            observed availability. Programming errors and configuration errors do
            not, because attributing them to a venue would be misleading.
    """

    retryable: bool = False
    counts_against_health: bool = True

    def __init__(self, message: str, /, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def __str__(self) -> str:
        if not self.context:
            return self.message
        rendered = " ".join(f"{k}={v!r}" for k, v in sorted(self.context.items()))
        return f"{self.message} ({rendered})"

    @property
    def error_type(self) -> str:
        """Stable machine-readable discriminator used in metrics and logs."""
        return type(self).__name__


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #


class TransportError(AtlasError):
    """Base class for failures that occurred before a response was parsed."""


class NetworkTimeout(TransportError):
    """A connect, read or write deadline elapsed."""

    retryable = True


class DnsFailure(TransportError):
    """The venue hostname could not be resolved."""

    retryable = True


class ConnectionFailure(TransportError):
    """The connection could not be established or was reset mid-transfer."""

    retryable = True


class HttpError(TransportError):
    """The venue returned an unsuccessful HTTP status.

    ``5xx`` responses are treated as retryable; ``4xx`` responses are not,
    because repeating a malformed or refused request does not help.
    """

    def __init__(self, message: str, /, status_code: int, **context: Any) -> None:
        super().__init__(message, status_code=status_code, **context)
        self.status_code = status_code
        self.retryable = status_code >= 500

    @property
    def error_type(self) -> str:
        """Include the status class so health metrics separate 4xx from 5xx."""
        return f"HttpError{self.status_code}"


class RateLimited(TransportError):
    """The venue signalled that the request rate was too high.

    Attributes:
        retry_after_seconds: The venue's own instruction when it supplied one.
            Atlas honours it in preference to its own backoff schedule.
    """

    retryable = True

    def __init__(
        self, message: str, /, retry_after_seconds: float | None = None, **context: Any
    ) -> None:
        super().__init__(message, retry_after_seconds=retry_after_seconds, **context)
        self.retry_after_seconds = retry_after_seconds


class ResponseTooLarge(TransportError):
    """A response exceeded the configured body budget and was abandoned.

    Atlas caps response sizes so that an unexpected venue change cannot exhaust a
    runner's memory. This is not retryable: the same request would return the same
    oversized body.
    """


class GeoRestricted(TransportError):
    """The venue refused the request because of the collector's network location.

    Distinguished from a generic ``HttpError`` because it is a property of where
    Atlas is running rather than of the venue's health or of the request, and
    because retrying from the same address cannot succeed.
    """

    counts_against_health = False


# --------------------------------------------------------------------------- #
# Venue application layer
# --------------------------------------------------------------------------- #


class ExchangeApplicationError(AtlasError):
    """The venue returned a transport-level success carrying an error document.

    Several venues answer ``200 OK`` with a non-zero code in the body. Treating
    those as successes would silently corrupt a generation.
    """

    def __init__(self, message: str, /, venue_code: str | None = None, **context: Any) -> None:
        super().__init__(message, venue_code=venue_code, **context)
        self.venue_code = venue_code


class SchemaMismatch(AtlasError):
    """A payload did not have the shape the adapter was written against.

    Raised when a required field disappears, changes type, or an enumerated field
    carries an unrecognised value that changes the record's meaning. Recorded per
    adapter so that venue schema drift is visible rather than absorbed.
    """


class UnsupportedCapability(AtlasError):
    """An operation was requested that the venue does not expose.

    Not a fault: it is the expected answer when, for example, funding is requested
    from a spot-only venue. Never counted against venue health.
    """

    counts_against_health = False


class CircuitOpen(AtlasError):
    """The adapter's circuit breaker is open, so the request was not attempted.

    The original failures are already recorded; re-counting the suppressed
    requests would exaggerate the failure rate.
    """

    counts_against_health = False


# --------------------------------------------------------------------------- #
# Semantic layer
# --------------------------------------------------------------------------- #


class InvalidObservation(AtlasError):
    """An observation failed validation and must be quarantined, not coerced.

    Attributes:
        reason: A stable exclusion reason recorded alongside the derived state so
            that every dropped observation remains auditable.
    """

    counts_against_health = False

    def __init__(self, message: str, /, reason: str, **context: Any) -> None:
        super().__init__(message, reason=reason, **context)
        self.reason = reason


class IdentityResolutionFailure(AtlasError):
    """An asset or instrument could not be resolved to a canonical identity.

    Atlas prefers this over guessing. The caller falls back to a venue-scoped
    identity so that the observation is still recorded without being merged into
    an unrelated asset.
    """

    counts_against_health = False


class ConversionUnavailable(AtlasError):
    """A quote currency could not be converted with acceptable provenance.

    Raised instead of emitting a USD-normalised figure that would look
    authoritative while resting on an unobserved rate.
    """

    counts_against_health = False


# --------------------------------------------------------------------------- #
# Configuration, dataset and publication
# --------------------------------------------------------------------------- #


class ConfigurationError(AtlasError):
    """Declared configuration is missing, malformed or internally inconsistent."""

    counts_against_health = False


class MetricUnknown(AtlasError):
    """A metric name was referenced that the metric registry does not define."""

    counts_against_health = False


class MqlError(AtlasError):
    """Base class for Market Query Language failures."""

    counts_against_health = False


class MqlSyntaxError(MqlError):
    """A query could not be parsed.

    Attributes:
        position: Zero-based offset into the query text, for caret rendering.
    """

    def __init__(self, message: str, /, position: int | None = None, **context: Any) -> None:
        super().__init__(message, position=position, **context)
        self.position = position


class MqlSemanticError(MqlError):
    """A query parsed but referenced something that does not exist or is not allowed."""


class ValidationFailure(AtlasError):
    """A candidate generation failed its validity gate and must not be published."""

    counts_against_health = False


class ChecksumMismatch(AtlasError):
    """A file's content hash did not match the value recorded in the manifest."""

    counts_against_health = False


class PublicationFailure(AtlasError):
    """A dataset could not be published.

    Raised before the ``latest`` pointer is advanced, so that a partial upload
    never becomes the canonical generation.
    """

    counts_against_health = False


class DatasetUnavailable(AtlasError):
    """A requested dataset could not be located or opened."""

    counts_against_health = False


class AmbiguousAsset(AtlasError):
    """A user-supplied symbol matched more than one canonical asset.

    Attributes:
        candidates: The matching canonical asset IDs, so the caller can choose.
    """

    counts_against_health = False

    def __init__(self, message: str, /, candidates: list[str], **context: Any) -> None:
        super().__init__(message, candidates=candidates, **context)
        self.candidates = candidates


class AssetNotFound(AtlasError):
    """No canonical asset matched the supplied symbol or identifier."""

    counts_against_health = False
