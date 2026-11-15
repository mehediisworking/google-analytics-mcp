# Copyright 2026 Google LLC All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Read-only advanced GA4 Data API reporting and property-aware validation."""

import asyncio
from datetime import date
from decimal import Decimal
import re
from typing import Any, Literal

from google.analytics import data_v1beta as ga
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from analytics_mcp.tools.client import create_data_api_client
from analytics_mcp.tools.utils import proto_to_dict


class InputModel(BaseModel):
    """Reject misspelled request fields instead of silently ignoring them."""

    model_config = ConfigDict(extra="forbid")


class DateRange(InputModel):
    """An inclusive GA4 date range in the property's timezone."""

    start_date: str = Field(
        description="YYYY-MM-DD, today, yesterday or NdaysAgo"
    )
    end_date: str = Field(
        description="Inclusive end; same formats as start_date"
    )
    name: str | None = Field(
        default=None, description="Optional GA4 range label"
    )


class ReportSpec(InputModel):
    """A single core report; field names use Data API snake_case."""

    date_ranges: list[DateRange] = Field(min_length=1, max_length=4)
    dimensions: list[str] = Field(default_factory=list, max_length=9)
    metrics: list[str] = Field(min_length=1, max_length=10)
    dimension_filter: dict[str, Any] | None = Field(
        default=None,
        description="GA4 FilterExpression using dimension names only",
    )
    metric_filter: dict[str, Any] | None = Field(
        default=None, description="GA4 FilterExpression using metric names only"
    )
    order_bys: list[dict[str, Any]] = Field(
        default_factory=list,
        description="GA4 OrderBy objects for selected fields",
    )
    limit: StrictInt = Field(default=10000, ge=1, le=250000)
    offset: StrictInt = Field(default=0, ge=0)
    currency_code: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    keep_empty_rows: bool = False
    return_property_quota: bool = True


class PivotSpec(InputModel):
    """A GA4 pivot axis. The product of axis limits cannot exceed 250,000."""

    field_names: list[str] = Field(min_length=1, max_length=9)
    limit: StrictInt = Field(ge=1, le=250000)
    offset: StrictInt = Field(default=0, ge=0)
    order_bys: list[dict[str, Any]] = Field(default_factory=list)
    metric_aggregations: list[
        Literal["TOTAL", "MINIMUM", "MAXIMUM", "COUNT"]
    ] = Field(default_factory=list)


class APIObject(BaseModel):
    """Preserve additional official API response fields without data loss."""

    model_config = ConfigDict(extra="allow")


class DimensionMetadata(APIObject):
    api_name: str
    ui_name: str = ""
    description: str = ""
    custom_definition: bool = False


class MetricMetadata(DimensionMetadata):
    type_: str = "METRIC_TYPE_UNSPECIFIED"


class MetadataResult(APIObject):
    name: str = ""
    dimensions: list[DimensionMetadata] = Field(default_factory=list)
    metrics: list[MetricMetadata] = Field(default_factory=list)


class DimensionCompatibility(APIObject):
    compatibility: str
    dimension_metadata: DimensionMetadata


class MetricCompatibility(APIObject):
    compatibility: str
    metric_metadata: MetricMetadata


class CompatibilityResult(APIObject):
    dimension_compatibilities: list[DimensionCompatibility] = Field(
        default_factory=list
    )
    metric_compatibilities: list[MetricCompatibility] = Field(
        default_factory=list
    )


class Header(APIObject):
    name: str


class MetricHeader(Header):
    type_: str = "METRIC_TYPE_UNSPECIFIED"


class Value(APIObject):
    value: str = ""


class Row(APIObject):
    dimension_values: list[Value] = Field(default_factory=list)
    metric_values: list[Value] = Field(default_factory=list)


class ReportResult(APIObject):
    dimension_headers: list[Header] = Field(default_factory=list)
    metric_headers: list[MetricHeader] = Field(default_factory=list)
    rows: list[Row] = Field(default_factory=list)
    row_count: int = 0


class PivotDimensionHeader(APIObject):
    dimension_values: list[Value] = Field(default_factory=list)


class PivotHeader(APIObject):
    pivot_dimension_headers: list[PivotDimensionHeader] = Field(
        default_factory=list
    )
    row_count: int = 0


class PivotResult(APIObject):
    pivot_headers: list[PivotHeader] = Field(default_factory=list)
    metric_headers: list[MetricHeader] = Field(default_factory=list)
    rows: list[Row] = Field(default_factory=list)
    aggregates: list[Row] = Field(default_factory=list)


class BatchResult(APIObject):
    reports: list[ReportResult] = Field(default_factory=list)


class ComparisonResult(BaseModel):
    """Two labeled reports, without unsafe aggregation of non-additive metrics."""

    baseline: ReportResult
    comparison: ReportResult


def _property(property_id: int | str) -> str:
    if isinstance(property_id, bool) or not re.fullmatch(
        r"(?:properties/)?[0-9]+", str(property_id)
    ):
        raise ValueError("property_id must be a numeric ID or properties/ID")
    return f"properties/{int(str(property_id).removeprefix('properties/'))}"


async def _call(method: str, request):
    def sync_call():
        return getattr(create_data_api_client(), method)(request=request)

    try:
        return await asyncio.to_thread(sync_call)
    except Exception:
        # Never surface transport exceptions: they can contain request headers.
        raise RuntimeError(
            f"GA4 {method} failed. Check property access, request compatibility "
            "and Data API quota; retry transient failures."
        ) from None


async def get_report_metadata(property_id: int | str) -> dict[str, Any]:
    """Get official GA4 dimension/metric names, types and descriptions.

    Args:
        property_id: Numeric property ID or properties/ID. Use 0 for universal
            metadata only; use a real property to include its custom definitions.

    Returns:
        Metadata including dimensions and metrics with api_name and
        custom_definition. Use these exact names in reports and filters.
    """
    response = await _call(
        "get_metadata",
        ga.GetMetadataRequest(name=f"{_property(property_id)}/metadata"),
    )
    return MetadataResult.model_validate(proto_to_dict(response)).model_dump()


def _names(names, allowed, label):
    if len(names) != len(set(names)):
        raise ValueError(f"{label} must not contain duplicates")
    if any(name not in allowed for name in names):
        raise ValueError(
            f"Unknown {label}; use get_report_metadata for this property"
        )


def _filter(expression, allowed, label, depth=0):
    if expression is None:
        if depth:
            raise ValueError("Filter groups require non-null child expressions")
        return
    if depth > 20 or not isinstance(expression, dict) or len(expression) != 1:
        raise ValueError(
            f"{label} must contain exactly one expression (depth <= 20)"
        )
    kind, value = next(iter(expression.items()))
    if kind in ("and_group", "or_group"):
        if not isinstance(value, dict) or set(value) != {"expressions"}:
            raise ValueError(f"{label} group requires expressions")
        children = value["expressions"]
        if not isinstance(children, list) or not children:
            raise ValueError(f"{label} group expressions must be non-empty")
        for child in children:
            _filter(child, allowed, label, depth + 1)
    elif kind == "not_expression":
        if value is None:
            raise ValueError("not_expression requires a child expression")
        _filter(value, allowed, label, depth + 1)
    elif kind == "filter":
        if (
            not isinstance(value, dict)
            or value.get("field_name") not in allowed
        ):
            raise ValueError(
                f"{label} field_name must be a known {label} field"
            )
        predicates = set(value) - {"field_name"}
        if len(predicates) != 1:
            raise ValueError(f"{label} filter requires exactly one predicate")
        predicate = next(iter(predicates))
        if predicate not in {
            "string_filter",
            "in_list_filter",
            "numeric_filter",
            "between_filter",
            "empty_filter",
        }:
            raise ValueError(f"Invalid {label} predicate")
        if label in {"metric_filter", "metrics"} and predicate not in {
            "numeric_filter",
            "between_filter",
            "empty_filter",
        }:
            raise ValueError(
                "Metric filters require numeric, between or empty predicates"
            )
        if not isinstance(value[predicate], dict):
            raise ValueError("Filter predicate must be an object")
        ga.FilterExpression(
            expression
        )  # Reject unknown fields and invalid enums.
        parsed = ga.Filter(value)
        if predicate == "string_filter":
            ga.Filter.StringFilter.MatchType(parsed.string_filter.match_type)
        if predicate == "numeric_filter":
            ga.Filter.NumericFilter.Operation(parsed.numeric_filter.operation)
        if predicate == "string_filter" and not parsed.string_filter.match_type:
            raise ValueError("string_filter requires a match_type")
        if predicate == "in_list_filter" and not parsed.in_list_filter.values:
            raise ValueError("in_list_filter requires non-empty values")
        if predicate == "numeric_filter":
            if (
                not parsed.numeric_filter.operation
                or "value" not in parsed.numeric_filter
            ):
                raise ValueError("numeric_filter requires operation and value")
        if predicate == "between_filter":
            if (
                "from_value" not in parsed.between_filter
                or "to_value" not in parsed.between_filter
            ):
                raise ValueError(
                    "between_filter requires from_value and to_value"
                )
        numeric_values = []
        if predicate == "numeric_filter":
            numeric_values = [value[predicate]["value"]]
        elif predicate == "between_filter":
            numeric_values = [
                value[predicate]["from_value"],
                value[predicate]["to_value"],
            ]
        for numeric in numeric_values:
            if len(numeric) != 1 or set(numeric) - {
                "int64_value",
                "double_value",
            }:
                raise ValueError(
                    "NumericValue requires exactly one numeric value"
                )
            number = next(iter(numeric.values()))
            if isinstance(number, bool) or not Decimal(str(number)).is_finite():
                raise ValueError("NumericValue must be finite")
        if predicate == "between_filter":
            lower, upper = (next(iter(n.values())) for n in numeric_values)
            if Decimal(str(lower)) > Decimal(str(upper)):
                raise ValueError(
                    "between_filter from_value must be <= to_value"
                )
    else:
        raise ValueError(f"Invalid {label} expression kind")


def _orders(orders, dimensions, metrics):
    for order in orders:
        parsed = ga.OrderBy(order)
        if "dimension" in parsed:
            if parsed.dimension.dimension_name not in dimensions:
                raise ValueError("OrderBy dimension must be selected")
        elif "metric" in parsed:
            if parsed.metric.metric_name not in metrics:
                raise ValueError("OrderBy metric must be selected")
        elif "pivot" in parsed:
            if parsed.pivot.metric_name not in metrics or any(
                s.dimension_name not in dimensions
                for s in parsed.pivot.pivot_selections
            ):
                raise ValueError("Pivot OrderBy must reference selected fields")
        else:
            raise ValueError("OrderBy requires dimension, metric or pivot")
        if len(set(order) - {"desc"}) != 1:
            raise ValueError("OrderBy requires exactly one ordering kind")


def _date_value(value):
    if value == "today":
        return ("relative", 0)
    if value == "yesterday":
        return ("relative", -1)
    if re.fullmatch(r"[0-9]+daysAgo", value):
        return ("relative", -int(value[:-7]))
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError(
            "Dates must use YYYY-MM-DD, today, yesterday or NdaysAgo"
        )
    return ("absolute", date.fromisoformat(value).toordinal())


def _validate_spec(spec, metadata):
    _names(
        spec.dimensions,
        {d["api_name"] for d in metadata["dimensions"]},
        "dimensions",
    )
    _names(
        spec.metrics, {m["api_name"] for m in metadata["metrics"]}, "metrics"
    )
    _filter(
        spec.dimension_filter,
        {d["api_name"] for d in metadata["dimensions"]},
        "dimension_filter",
    )
    _filter(
        spec.metric_filter,
        {m["api_name"] for m in metadata["metrics"]},
        "metric_filter",
    )
    for dr in spec.date_ranges:
        start, end = _date_value(dr.start_date), _date_value(dr.end_date)
        if start[0] == end[0] and start[1] > end[1]:
            raise ValueError("start_date must be on or before end_date")
        if dr.name and (dr.name.startswith(("date_range_", "RESERVED_"))):
            raise ValueError("Date range name uses a reserved prefix")
    labels = [dr.name for dr in spec.date_ranges if dr.name]
    if len(labels) != len(set(labels)):
        raise ValueError("Date range names must be unique")
    _orders(spec.order_bys, spec.dimensions, spec.metrics)


def _request(spec, property_name):
    values = spec.model_dump(exclude_none=True)
    values["dimensions"] = [{"name": name} for name in spec.dimensions]
    values["metrics"] = [{"name": name} for name in spec.metrics]
    return ga.RunReportRequest(property=property_name, **values)


async def check_compatibility(
    property_id: int | str,
    dimensions: list[str],
    metrics: list[str],
    dimension_filter: dict[str, Any] | None = None,
    metric_filter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Check field compatibility through the official GA4 Data API.

    Args:
        property_id: Numeric property ID or properties/ID.
        dimensions: Up to 9 exact standard or custom dimension API names.
        metrics: Up to 10 exact standard or custom metric API names.
        dimension_filter: Optional snake_case GA4 FilterExpression for dimensions.
        metric_filter: Optional snake_case GA4 FilterExpression for metrics.

    Returns:
        Per-field dimension_compatibilities and metric_compatibilities, each
        with compatibility and official field metadata. Incompatible fields
        are returned as data, not raised as an execution error.
    """
    if len(dimensions) > 9 or len(metrics) > 10:
        raise ValueError("At most 9 dimensions and 10 metrics are allowed")
    metadata = await get_report_metadata(property_id)
    for names, key, expression in (
        (dimensions, "dimensions", dimension_filter),
        (metrics, "metrics", metric_filter),
    ):
        allowed = {item["api_name"] for item in metadata[key]}
        _names(names, allowed, key)
        _filter(expression, allowed, key)
    response = await _call(
        "check_compatibility",
        ga.CheckCompatibilityRequest(
            property=_property(property_id),
            dimensions=[ga.Dimension(name=n) for n in dimensions],
            metrics=[ga.Metric(name=n) for n in metrics],
            dimension_filter=dimension_filter,
            metric_filter=metric_filter,
        ),
    )
    return CompatibilityResult.model_validate(
        proto_to_dict(response)
    ).model_dump()


async def batch_run_reports(
    property_id: int | str, reports: list[dict[str, Any]]
) -> dict[str, Any]:
    """Run 1–5 core reports for one property in a single official batch call.

    Args:
        property_id: Numeric property ID or properties/ID shared by all reports.
        reports: 1–5 ReportSpec objects. Each requires date_ranges (1–4) and
            metrics (1–10); dimensions (0–9), filters, order_bys, limit (1–250000),
            offset (>=0), currency_code and return_property_quota are optional.
            Unknown fields and per-report property overrides are rejected.

    Returns:
        Reports in request order with headers, string-valued rows, row_count,
        metadata and quota. Pagination is per report; no automatic paging.
    """
    if not 1 <= len(reports) <= 5:
        raise ValueError("reports must contain 1 to 5 reports")
    specs = [ReportSpec.model_validate(report) for report in reports]
    property_name = _property(property_id)
    metadata = await get_report_metadata(property_id)
    for spec in specs:
        _validate_spec(spec, metadata)
    request = ga.BatchRunReportsRequest(
        property=property_name,
        requests=[_request(spec, property_name) for spec in specs],
    )
    response = await _call("batch_run_reports", request)
    result = BatchResult.model_validate(proto_to_dict(response))
    if len(result.reports) != len(specs):
        raise RuntimeError(
            "GA4 batch response count does not match request count"
        )
    return result.model_dump()


async def run_pivot_report(
    property_id: int | str, report: dict[str, Any], pivots: list[dict[str, Any]]
) -> dict[str, Any]:
    """Run an official GA4 pivot report with validated pivot axes.

    Args:
        property_id: Numeric property ID or properties/ID.
        report: ReportSpec: date_ranges, dimensions and metrics plus optional
            filters, currency_code, keep_empty_rows and return_property_quota.
            Use pivot-level limit, offset and order_bys, not report-level ones.
        pivots: Non-empty list of PivotSpec objects: field_names and positive
            limit required; offset, order_bys and metric_aggregations optional.
            Each selected dimension must occur in exactly one axis; dateRange
            may additionally be used for multiple date ranges. Product of
            limits must be <=250000.

    Returns:
        Official pivot_headers, metric_headers, rows, aggregates, metadata and
        quota. Read rows using pivot_headers; they are not flattened or summed.
    """
    if set(report) & {"limit", "offset", "order_bys"}:
        raise ValueError("Use pivot-level limit, offset and order_bys")
    spec = ReportSpec.model_validate(report)
    axes = [PivotSpec.model_validate(p) for p in pivots]
    if not axes:
        raise ValueError("pivots must contain at least one axis")
    fields = [name for axis in axes for name in axis.field_names]
    expected = set(spec.dimensions)
    if len(spec.date_ranges) > 1 and "dateRange" in fields:
        expected.add("dateRange")
    if len(fields) != len(set(fields)) or set(fields) != expected:
        raise ValueError(
            "Pivot fields must cover selected dimensions exactly once"
        )
    product = 1
    for axis in axes:
        product *= axis.limit
        _orders(axis.order_bys, axis.field_names, spec.metrics)
    if product > 250000:
        raise ValueError("Product of pivot limits must be <=250000")
    metadata = await get_report_metadata(property_id)
    _validate_spec(spec, metadata)
    values = ga.RunReportRequest.to_dict(_request(spec, _property(property_id)))
    for key in (
        "limit",
        "offset",
        "order_bys",
        "cohort_spec",
        "metric_aggregations",
    ):
        values.pop(key, None)
    values["pivots"] = [axis.model_dump() for axis in axes]
    response = await _call("run_pivot_report", ga.RunPivotReportRequest(values))
    return PivotResult.model_validate(proto_to_dict(response)).model_dump()


async def run_comparison_report(
    property_id: int | str,
    report: dict[str, Any],
    baseline_date_range: dict[str, Any],
    comparison_date_range: dict[str, Any],
) -> dict[str, Any]:
    """Compare two periods with identical report fields and filters.

    Args:
        property_id: Numeric property ID or properties/ID.
        report: ReportSpec without date_ranges. Shared dimensions, metrics,
            filters, order_bys, limit and offset are applied to both periods.
        baseline_date_range: DateRange with start_date and end_date; inclusive.
        comparison_date_range: DateRange for the period being compared.

    Returns:
        Labeled baseline and comparison reports from one batch request. Match
        rows by dimension values, not row position. Limits apply separately;
        missing rows are not zeros. No summing rates or distinct-user counts.
    """
    if "date_ranges" in report:
        raise ValueError("Use baseline_date_range and comparison_date_range")
    result = await batch_run_reports(
        property_id,
        [
            dict(report, date_ranges=[baseline_date_range]),
            dict(report, date_ranges=[comparison_date_range]),
        ],
    )
    if len(result["reports"]) != 2:
        raise RuntimeError("GA4 comparison response must contain two reports")
    return ComparisonResult(
        baseline=result["reports"][0], comparison=result["reports"][1]
    ).model_dump()


TOOLS = [
    get_report_metadata,
    check_compatibility,
    run_pivot_report,
    batch_run_reports,
    run_comparison_report,
]
OUTPUT_MODELS = dict(
    zip(
        (tool.__name__ for tool in TOOLS),
        (
            MetadataResult,
            CompatibilityResult,
            PivotResult,
            BatchResult,
            ComparisonResult,
        ),
    )
)


def enrich_schema(tool):
    """Add nested report schemas beyond ADK's dictionary parameter inference."""
    tool.input_schema["additionalProperties"] = False
    properties = tool.input_schema["properties"]
    properties["property_id"][
        "description"
    ] = "Numeric GA4 property ID or properties/ID; metadata property 0 is universal"
    if tool.name == "check_compatibility":
        properties["dimensions"]["maxItems"] = 9
        properties["metrics"]["maxItems"] = 10
    report = ReportSpec.model_json_schema()
    definitions = report.pop("$defs", {})
    tool.input_schema.setdefault("$defs", {}).update(definitions)
    if tool.name == "batch_run_reports":
        properties["reports"].update(
            type="array", minItems=1, maxItems=5, items=report
        )
    elif tool.name == "run_pivot_report":
        properties["report"] = report
        for name in ("limit", "offset", "order_bys"):
            report["properties"].pop(name)
        properties["pivots"].update(
            type="array", minItems=1, items=PivotSpec.model_json_schema()
        )
    elif tool.name == "run_comparison_report":
        report["properties"].pop("date_ranges")
        report["required"].remove("date_ranges")
        properties["report"] = report
        for key in ("baseline_date_range", "comparison_date_range"):
            properties[key] = DateRange.model_json_schema()
    tool.output_schema = OUTPUT_MODELS[tool.name].model_json_schema()
