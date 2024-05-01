from collections import defaultdict
from datetime import datetime, timedelta, timezone

import sentry_sdk
from rest_framework.request import Request
from rest_framework.response import Response
from sentry_relay.consts import SPAN_STATUS_CODE_TO_NAME
from snuba_sdk import Column, Condition, Function, Op

from sentry.api.api_owners import ApiOwner
from sentry.api.api_publish_status import ApiPublishStatus
from sentry.api.base import region_silo_endpoint
from sentry.api.bases.project import ProjectEndpoint
from sentry.api.utils import handle_query_errors
from sentry.search.events.builder.spans_indexed import SpansIndexedQueryBuilder
from sentry.search.events.builder.spans_metrics import SpansMetricsQueryBuilder
from sentry.search.events.types import ParamsType, QueryBuilderConfig
from sentry.snuba.dataset import Dataset
from sentry.snuba.referrer import Referrer

VALID_AVERAGE_COLUMNS = {"span.self_time", "span.duration"}


@region_silo_endpoint
class ProjectTransactionDetailsEndpoint(ProjectEndpoint):
    owner = ApiOwner.PERFORMANCE
    publish_status = {
        "GET": ApiPublishStatus.PRIVATE,
    }

    def get(self, request: Request, project, transaction_id):
        given_start_str = request.GET.get("start_timestamp", "")
        given_end_str = request.GET.get("end_timestamp", "")
        try:
            start = datetime.fromtimestamp(float(given_start_str))
            end = datetime.fromtimestamp(float(given_end_str))
        except TypeError:
            return Response({"detail": "missing start_timestamp or end_timestamp"}, status=400)

        spans_data = _query_all_spans_in_transaction(
            project.organization, project, transaction_id, start, end
        )
        sentry_sdk.set_measurement("transaction_endpoint.span_query.num_spans", len(spans_data))
        if len(spans_data) == 0:
            return Response(status=404)

        segment_spans_generator = (span for span in spans_data if span["is_segment"] != 0)
        segment_span = next(segment_spans_generator, None)
        if segment_span is None:
            return Response({"detail": "No transaction span found"}, status=500)
        if next(segment_spans_generator, None) is not None:
            with sentry_sdk.push_scope() as scope:
                scope.set_extra("transaction_id", transaction_id)
                message = "Found transaction with multiple segment spans"
                sentry_sdk.capture_message(message, level="warning")

        spans_data = list(
            filter(lambda span: span["span_id"] != segment_span["span_id"], spans_data)
        )

        segment_tags_items = zip(segment_span["tags.key"], segment_span["tags.value"])
        segment_sentry_tags = dict(
            zip(segment_span["sentry_tags.key"], segment_span["sentry_tags.value"])
        )
        segment_measurements = dict(
            zip(segment_span["measurements.key"], segment_span["measurements.value"])
        )

        transaction_name = segment_sentry_tags.get("transaction")
        parent_span_id = segment_span.get("parent_span_id", None)
        if parent_span_id == "00":
            parent_span_id = None

        # Assemble them into the same format as the event details endpoint
        data = {
            "id": transaction_id,
            "groupID": None,
            "eventID": transaction_id,
            "projectID": project.id,
            "entries": [
                {
                    "type": "spans",
                    "data": _span_data_to_event_spans(spans_data),
                }
            ],
            "dist": None,
            "message": "",
            "title": transaction_name,
            "location": transaction_name,
            "user": {
                # missing from indexed spans dataset
            },
            "contexts": {
                "browser": {
                    "name": segment_sentry_tags.get("browser.name"),
                    # other props missing from indexed spans dataset
                },
                "client_os": {
                    "name": segment_sentry_tags.get("os.name"),
                    # other props missing from indexed spans dataset
                },
                "trace": {
                    "trace_id": segment_span.get("trace_id"),
                    "span_id": segment_span.get("span_id"),
                    "parent_span_id": parent_span_id,
                    "op": segment_span.get("op"),
                    "status": SPAN_STATUS_CODE_TO_NAME[segment_span.get("status", 2)],
                    "exclusive_time": segment_span.get("exclusive_time"),
                    "hash": segment_span.get("group"),
                    "type": "trace",
                },
            },
            "sdk": {
                "name": segment_sentry_tags.get("sdk.name"),
                "version": segment_sentry_tags.get("sdk.version"),
                # other props missing from indexed spans dataset
            },
            "context": {},  # missing from indexed spans dataset
            "packages": {},  # # missing from indexed spans dataset
            "type": "transaction",
            "metadata": {
                "location": transaction_name,
                "title": transaction_name,
            },
            "tags": [
                {"key": key, "value": value}
                for key, value in list(segment_tags_items) + list(segment_sentry_tags.items())
            ],
            "platform": segment_span.get("platform"),  # column is always null in Clickhouse
            "dateReceived": datetime.fromtimestamp(
                segment_span.get("precise.start_ts"), tz=timezone.utc
            ).isoformat(),
            "errors": [],
            "occurrence": None,
            "_meta": {},
            "start_timestamp": segment_span.get("precise.start_ts"),
            "timestamp": segment_span.get("precise.finish_ts"),
            "measurements": segment_measurements,
            "breakdowns": {
                "span_ops": _span_ops_breakdown(spans_data),
            },
            "release": {
                "version": segment_sentry_tags.get("release"),
                # other props missing from indexed spans dataset
            },
            "projectSlug": project.slug,
        }

        average_columns = request.GET.getlist("averageColumn", [])
        if (
            all(col in VALID_AVERAGE_COLUMNS for col in average_columns)
            and len(average_columns) > 0
        ):
            _add_comparison_to_event(data, project.organization.id, project, average_columns)

        return Response(data)


@sentry_sdk.tracing.trace
def _query_all_spans_in_transaction(organization, project, transaction_id, start, end):
    params: ParamsType = {
        "start": start - timedelta(seconds=1),
        "end": end + timedelta(seconds=1),
        "project_id": [project.id],
        "project_objects": [project],
        "organization_id": organization.id,
    }

    # Look up the spans for this transaction
    query = SpansIndexedQueryBuilder(
        Dataset.SpansIndexed,
        params,
        query=f"transaction_id:{transaction_id}",
        selected_columns=[
            "transaction_id",
            "transaction_op",
            "trace_id",
            "span_id",
            "profile_id",
            "parent_span_id",
            "segment_id",
            "is_segment",
            "segment_name",
            "precise.start_ts",
            "precise.finish_ts",
            "exclusive_time",
            "op",
            "group",
            "span_status",
            "span_kind",
            "description",
            "status",
            "platform",
            "user",
            "measurements.key",
            "measurements.value",
        ],
        orderby=["-is_segment", "precise.start_ts", "id"],
        limit=10000,
    )
    # These columns are incorrectly translated by the query builder - add
    # them directly as a workaround
    query.columns += [
        Column("tags.key"),
        Column("tags.value"),
        Column("sentry_tags.key"),
        Column("sentry_tags.value"),
    ]
    results = query.run_query(referrer=Referrer.API_ORGANIZATION_TRANSACTION_DETAILS.value)
    return results["data"]


def _span_data_to_event_spans(span_data):
    def _normalize_group(group):
        if group == "00":
            return None
        return group

    return [
        {
            "timestamp": span["precise.finish_ts"],
            "start_timestamp": span["precise.start_ts"],
            "exclusive_time": span["exclusive_time"],
            "description": span["description"],
            "op": span["op"],
            "span_id": span["span_id"],
            "parent_span_id": span["parent_span_id"],
            "trace_id": span["trace_id"],
            "tags": dict(zip(span["tags.key"], span["tags.value"])),
            "data": {},  # missing from indexed spans data set
            "sentry_tags": dict(zip(span["sentry_tags.key"], span["sentry_tags.value"])),
            "hash": _normalize_group(span["group"]),
            "same_process_as_parent": span.get("same_process_as_parent"),
        }
        for span in span_data
    ]


@sentry_sdk.tracing.trace
def _span_ops_breakdown(spans_data):
    # Roughly replicates the logic in Relay. See:
    # https://github.com/getsentry/relay/blob/b2fcde7ddb829e53f8b312bc25b2dc24eaae3b84/relay-event-normalization/src/normalize/breakdowns.rs#L87
    known_ops = {"db", "http", "resource", "browser", "ui"}
    intervals_by_op = defaultdict(list)

    for span in spans_data:
        # Normalize known span ops, since those get reported individualy
        span_op = span.get("op", "")
        for op in known_ops:
            if span_op.startswith(op):
                span_op = op
                break

        intervals_by_op[span_op].append(
            (span.get("precise.start_ts"), span.get("precise.finish_ts"))
        )

    total_time = 0.0
    breakdown = {}
    for op, intervals in intervals_by_op.items():
        duration = _duration_from_intervals(intervals)
        total_time += duration
        if op in known_ops:
            breakdown[f"ops.{op}"] = {
                "value": round(duration, 3),
                "unit": "milliseconds",
            }

    breakdown["total.time"] = {
        "value": round(total_time, 3),
        "unit": "milliseconds",
    }
    return breakdown


def _duration_from_intervals(intervals):
    intervals.sort(key=lambda x: x[0])

    duration = 0.0
    i = 0
    while i < len(intervals):
        start, end = intervals[i]
        for j in range(i, len(intervals)):
            next_span_start, next_span_end = intervals[j]
            if next_span_start < end:
                end = next_span_end
                i = j
        duration += (end - start) * 1000.0  # convert to ms
        i += 1

    return duration


@sentry_sdk.tracing.trace
def _add_comparison_to_event(data, organization_id, project, average_columns):
    group_to_span_map = defaultdict(list)
    end = datetime.now()
    start = end - timedelta(hours=24)
    spans = data["entries"][0]["data"]
    for span in spans:
        group = span.get("sentry_tags", {}).get("group")
        if group is not None:
            group_to_span_map[group].append(span)

    sentry_sdk.set_measurement("query.groups", len(group_to_span_map))
    if len(group_to_span_map) == 0:
        return

    with handle_query_errors():
        builder = SpansMetricsQueryBuilder(
            dataset=Dataset.PerformanceMetrics,
            params={
                "start": start,
                "end": end,
                "project_objects": [project],
                "organization_id": organization_id,
            },
            selected_columns=[
                "span.group",
                *[f"avg({average_column})" for average_column in average_columns],
            ],
            config=QueryBuilderConfig(transform_alias_to_input_format=True),
            # orderby shouldn't matter, just picking something so results are consistent
            orderby=["span.group"],
        )
        builder.add_conditions(
            [
                Condition(
                    Column(builder.resolve_column_name("span.group")),
                    Op.IN,
                    Function("tuple", list(group_to_span_map.keys())),
                )
            ]
        )
        result = builder.process_results(
            builder.run_query(Referrer.API_PERFORMANCE_ORG_TRANSACTION_AVERAGE_SPAN.value)
        )
        sentry_sdk.set_measurement("query.groups_found", len(result["data"]))
        for row in result["data"]:
            group = row["span.group"]
            for span in group_to_span_map[group]:
                average_results = {}
                for col in row:
                    if col.startswith("avg") and row[col] > 0:
                        average_results[col] = row[col]
                if average_results:
                    span["span.averageResults"] = average_results