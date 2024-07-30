import logging
from dataclasses import asdict
from typing import Any

import sentry_sdk
from django.conf import settings

from sentry import features, options
from sentry import ratelimits as ratelimiter
from sentry.conf.server import SEER_SIMILARITY_MODEL_VERSION
from sentry.eventstore.models import Event
from sentry.grouping.grouping_info import get_grouping_info_from_variants
from sentry.grouping.result import CalculatedHashes
from sentry.models.group import Group
from sentry.models.project import Project
from sentry.seer.similarity.similar_issues import get_similarity_data_from_seer
from sentry.seer.similarity.types import SimilarIssuesEmbeddingsRequest
from sentry.seer.similarity.utils import (
    event_content_is_seer_eligible,
    filter_null_from_event_title,
    get_stacktrace_string,
    killswitch_enabled,
)
from sentry.utils import metrics
from sentry.utils.circuit_breaker2 import CircuitBreaker
from sentry.utils.safe import get_path

logger = logging.getLogger("sentry.events.grouping")


def should_call_seer_for_grouping(event: Event, primary_hashes: CalculatedHashes) -> bool:
    """
    Use event content, feature flags, rate limits, killswitches, seer health, etc. to determine
    whether a call to Seer should be made.
    """

    project = event.project

    if not _project_has_similarity_grouping_enabled(project):
        return False

    if _has_customized_fingerprint(event, primary_hashes):
        return False

    if not event_content_is_seer_eligible(event):
        return False

    # **Do not add any new checks after this.** The rate limit check MUST remain the last of all the
    # checks.
    #
    # (Checking the rate limit for calling Seer also increments the counter of how many times we've
    # tried to call it, and if we fail any of the other checks, it shouldn't count as an attempt.
    # Thus we only want to run the rate limit check if every other check has already succeeded.)
    if (
        killswitch_enabled(project.id, event)
        or _circuit_breaker_broken(event, project)
        or _ratelimiting_enabled(event, project)
    ):
        return False

    return True


def _project_has_similarity_grouping_enabled(project: Project) -> bool:
    has_seer_grouping_flag_on = features.has("projects:similarity-embeddings-grouping", project)

    # TODO: This is a hack to get ingest to turn on for projects as soon as they're backfilled. When
    # the backfill script completes, we turn on this option, enabling ingest immediately rather than
    # forcing the project to wait until it's been manually added to a feature handler. Once all
    # projects have been backfilled, the option (and this check) can go away.
    has_been_backfilled = project.get_option("sentry:similarity_backfill_completed")

    return has_seer_grouping_flag_on or has_been_backfilled


# TODO: Here we're including events with hybrid fingerprints (ones which are `{{ default }}`
# combined with some other value). To the extent to which we're then using this function to decide
# whether or not to call Seer, this means that the calculations giving rise to the default part of
# the value never involve Seer input. In the long run, we probably want to change that.
def _has_customized_fingerprint(event: Event, primary_hashes: CalculatedHashes) -> bool:
    fingerprint = event.data.get("fingerprint", [])

    if "{{ default }}" in fingerprint:
        # No custom fingerprinting at all
        if len(fingerprint) == 1:
            return False

        # Hybrid fingerprinting ({{ default }} + some other value(s))
        else:
            metrics.incr(
                "grouping.similarity.did_call_seer",
                sample_rate=1.0,
                tags={"call_made": False, "blocker": "hybrid-fingerprint"},
            )
            return True

    # Fully customized fingerprint (from either us or the user)
    fingerprint_variant = primary_hashes.variants.get(
        "custom-fingerprint"
    ) or primary_hashes.variants.get("built-in-fingerprint")

    if fingerprint_variant:
        metrics.incr(
            "grouping.similarity.did_call_seer",
            sample_rate=1.0,
            tags={"call_made": False, "blocker": fingerprint_variant.type},
        )
        return True

    return False


# If you'd set LeakyBucket to burst rate X and drip rate to Y per second, it's would give you
# similar result as setting SlidingWindow to quota X and window length X/Y seconds and granularity
# of at least 1/Y seconds

# max 30/60 sec
# sustained 5/60 sec (drip)
# window 6 * 60 sec
# granularity 12 sec (1 drop every 12 sec = 5 drops/min)


# TODO: Switch to using a slide window and/or leaky bucket rate limiter. (For the latter, we can
# mimic a leaky bucket with a burst rate of X/min and a drip rate of Y/min by making a sliding window with
# quota X, window length X/Y min (because it's the amount of time it would take the full quota to drip
# out), and granularity of at least 1/Y seconds (because you need Y granules to have one
# drip/granule). For example, for a max burst rate of 30/min and a sustained rate (drip rate) of
# 5/min, the window would need to be 6 minutes long (to drip the all 30 requests from a full bucket)
# and - assuming we want to drip one request at a time - the granularity would need to be 12 seconds
# (because 1 drip every 12 sec adds up to our 5/min drip rate)
def _ratelimiting_enabled(event: Event, project: Project) -> bool:
    """
    Check both the global and project-based Seer similarity ratelimits.
    """

    global_ratelimit = options.get("seer.similarity.global-rate-limit")
    per_project_ratelimit = options.get("seer.similarity.per-project-rate-limit")

    global_limit_per_sec = global_ratelimit["limit"] / global_ratelimit["window"]
    project_limit_per_sec = per_project_ratelimit["limit"] / per_project_ratelimit["window"]

    logger_extra = {"event_id": event.event_id, "project_id": project.id}

    if ratelimiter.backend.is_limited("seer:similarity:global-limit", **global_ratelimit):
        logger_extra["limit_per_sec"] = global_limit_per_sec
        logger.warning("should_call_seer_for_grouping.global_ratelimit_hit", extra=logger_extra)

        metrics.incr(
            "grouping.similarity.global_ratelimit_hit",
            tags={"limit_per_sec": global_limit_per_sec},
        )
        metrics.incr(
            "grouping.similarity.did_call_seer",
            sample_rate=1.0,
            tags={"call_made": False, "blocker": "global-rate-limit"},
        )

        return True

    if ratelimiter.backend.is_limited(
        f"seer:similarity:project-{project.id}-limit", **per_project_ratelimit
    ):
        logger_extra["limit_per_sec"] = project_limit_per_sec
        logger.warning("should_call_seer_for_grouping.project_ratelimit_hit", extra=logger_extra)

        metrics.incr(
            "grouping.similarity.project_ratelimit_hit",
            tags={"limit_per_sec": project_limit_per_sec},
        )
        metrics.incr(
            "grouping.similarity.did_call_seer",
            sample_rate=1.0,
            tags={"call_made": False, "blocker": "project-rate-limit"},
        )

        return True

    return False


def _circuit_breaker_broken(event: Event, project: Project) -> bool:
    breaker_config = options.get("seer.similarity.circuit-breaker-config")
    circuit_breaker = CircuitBreaker(settings.SEER_SIMILARITY_CIRCUIT_BREAKER_KEY, breaker_config)
    circuit_broken = not circuit_breaker.should_allow_request()

    if circuit_broken:
        logger.warning(
            "should_call_seer_for_grouping.broken_circuit_breaker",
            extra={
                "event_id": event.event_id,
                "project_id": project.id,
                **breaker_config,
            },
        )
        metrics.incr(
            "grouping.similarity.broken_circuit_breaker",
        )
        metrics.incr(
            "grouping.similarity.did_call_seer",
            sample_rate=1.0,
            tags={"call_made": False, "blocker": "circuit-breaker"},
        )

    return circuit_broken


def get_seer_similar_issues(
    event: Event,
    primary_hashes: CalculatedHashes,
    num_neighbors: int = 1,
) -> tuple[dict[str, Any], Group] | tuple[None, None]:
    """
    Ask Seer for the given event's nearest neighbor(s) and return the seer response data, sorted
    with the best matches first, along with the group Seer decided the event should go in, if any,
    or None if no neighbor was near enough.
    """

    event_hash = primary_hashes.hashes[0]
    stacktrace_string = get_stacktrace_string(
        get_grouping_info_from_variants(primary_hashes.variants)
    )

    request_data: SimilarIssuesEmbeddingsRequest = {
        "event_id": event.event_id,
        "hash": event_hash,
        "project_id": event.project.id,
        "stacktrace": stacktrace_string,
        "message": filter_null_from_event_title(event.title),
        "exception_type": get_path(event.data, "exception", "values", -1, "type"),
        "k": num_neighbors,
        "referrer": "ingest",
    }

    # Similar issues are returned with the closest match first
    seer_results = get_similarity_data_from_seer(request_data)
    similar_issues_metadata = {
        "results": [asdict(result) for result in seer_results],
        "similarity_model_version": SEER_SIMILARITY_MODEL_VERSION,
    }
    parent_group = (
        Group.objects.filter(id=seer_results[0].parent_group_id).first() if seer_results else None
    )

    logger.info(
        "get_seer_similar_issues.results",
        extra={
            "event_id": event.event_id,
            "project_id": event.project.id,
            "hash": event_hash,
            "results": seer_results,
            "group_returned": bool(parent_group),
        },
    )

    return (similar_issues_metadata, parent_group)


def maybe_check_seer_for_matching_group(
    event: Event, primary_hashes: CalculatedHashes
) -> Group | None:
    seer_matched_group = None

    if should_call_seer_for_grouping(event, primary_hashes):
        metrics.incr(
            "grouping.similarity.did_call_seer",
            # TODO: Consider lowering this (in all the spots this metric is
            # collected) once we roll Seer grouping out more widely
            sample_rate=1.0,
            tags={"call_made": True, "blocker": "none"},
        )
        try:
            # If no matching group is found in Seer, we'll still get back result
            # metadata, but `seer_matched_group` will be None
            seer_response_data, seer_matched_group = get_seer_similar_issues(event, primary_hashes)
            event.data["seer_similarity"] = seer_response_data

        # Insurance - in theory we shouldn't ever land here
        except Exception as e:
            sentry_sdk.capture_exception(
                e, tags={"event": event.event_id, "project": event.project.id}
            )

    return seer_matched_group
