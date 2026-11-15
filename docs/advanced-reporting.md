# Advanced reporting (Phase 1)

Five additional tools use the official `google.analytics.data_v1beta` client.
They are available in default read-only mode, use the existing request-scoped
credentials and ADK `FunctionTool` registration, and require only the
`analytics.readonly` scope. Existing tools and signatures are preserved.

| Tool | Official API | Result |
| --- | --- | --- |
| `get_report_metadata` | `getMetadata` | Standard and property-specific custom fields, descriptions, types |
| `check_compatibility` | `checkCompatibility` | Per-field compatibility and metadata |
| `run_pivot_report` | `runPivotReport` | Pivot headers, metric headers, rows and aggregates |
| `batch_run_reports` | `batchRunReports` | 1–5 reports in input order |
| `run_comparison_report` | `batchRunReports` | Labeled baseline and comparison period reports |

## Discover fields first

```json
{"property_id": "properties/123456"}
```

Pass this to `get_report_metadata`. Use the returned `api_name` values exactly,
including custom names such as `customEvent:plan` and `customEvent:score`.
Property `0` returns universal metadata without custom fields. Metadata is fetched
per operation, never cached across users or properties. All other tools validate
selected fields and filter field names against the actual property's metadata.

Pass `property_id`, `dimensions`, `metrics`, and optional `dimension_filter` and
`metric_filter` to `check_compatibility`. The result retains `INCOMPATIBLE` entries;
these are findings, not execution errors. Report execution also remains subject
to the official API's compatibility, access, quota and property-specific rules.

## Batch reports

Call `batch_run_reports` with:

```json
{
  "property_id": 123456,
  "reports": [
    {
      "date_ranges": [{"start_date": "30daysAgo", "end_date": "yesterday"}],
      "dimensions": ["country"],
      "metrics": ["sessions"],
      "dimension_filter": {
        "filter": {
          "field_name": "country",
          "in_list_filter": {"values": ["Bangladesh", "United States"]}
        }
      },
      "order_bys": [{"metric": {"metric_name": "sessions"}, "desc": true}],
      "limit": 100,
      "offset": 0,
      "return_property_quota": true
    }
  ]
}
```

Every report uses the top-level property; per-report property overrides and
unknown fields are rejected. Each report supports 0–9 dimensions, 1–10 metrics,
1–4 inclusive date ranges, optional filters, sorting, currency code and empty
rows. Duplicate field names are rejected. Limits are strict integers from 1 to
250,000 (default 10,000); offsets are nonnegative integers. No automatic
pagination occurs. Use `row_count` and your limit/offset to identify truncated
core reports. A batch validates every report before any report is executed.

Dates accept `YYYY-MM-DD`, `today`, `yesterday` and `NdaysAgo`. Relative dates are
resolved by GA4 in the property's timezone. Local validation checks date formats
and ordering when both endpoints are absolute or both relative; the API checks
mixed absolute/relative ordering. Range names must be unique and cannot start
with `date_range_` or `RESERVED_`.

Filter and order objects use the official **snake_case protobuf field names**.
Filters accept exactly one of `filter`, `and_group`, `or_group`, or
`not_expression`. Groups contain a nonempty `expressions` list. Each leaf uses
one predicate and a known field of the correct kind. Dimension fields may be
filtered without selecting them. Numeric filters require an operation and value;
list filters require nonempty values. Nesting is limited to 20 levels. Unknown
protobuf fields, invalid enum names and malformed oneof expressions are rejected.
See the [GA4 FilterExpression reference](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/FilterExpression).

## Pivot reports

Call `run_pivot_report` with:

```json
{
  "property_id": 123456,
  "report": {
    "date_ranges": [{"start_date": "30daysAgo", "end_date": "yesterday"}],
    "dimensions": ["country", "deviceCategory"],
    "metrics": ["sessions"]
  },
  "pivots": [
    {"field_names": ["country"], "limit": 50},
    {"field_names": ["deviceCategory"], "limit": 5}
  ]
}
```

Each selected dimension must appear in exactly one pivot axis. For multiple date
ranges, `dateRange` may additionally appear as an axis field. Use axis-level
`limit`, `offset`, `order_bys` and `metric_aggregations` (TOTAL, MINIMUM, MAXIMUM,
COUNT). Report-level pagination/sorting is rejected for pivots. The product of
axis limits must not exceed 250,000. Interpret row values using the official
`pivot_headers`; this tool preserves the API layout and does not flatten rows.

## Period comparisons

Call `run_comparison_report` with:

```json
{
  "property_id": 123456,
  "report": {"dimensions": ["country"], "metrics": ["sessions"], "limit": 100},
  "baseline_date_range": {"start_date": "2026-01-01", "end_date": "2026-01-31"},
  "comparison_date_range": {"start_date": "2026-02-01", "end_date": "2026-02-28"}
}
```

The result has `baseline` and `comparison` reports from one official batch call,
with identical fields, filters, sorting and pagination. Join rows by dimension
values, not row position: ordering and missing rows may differ between periods.
Limits apply separately. Missing rows do not imply zero values. There are no
automatic totals, deltas or percentages: rates, distinct counts and independently
truncated result sets cannot safely be summed. Native GA4 audience/segment
`comparisons` are outside this Phase 1 period-comparison interface.

## Structured output and errors

New tools publish MCP `output_schema` and return both `structured_content` and
identical JSON text for compatibility. Pydantic models describe metadata,
compatibility entries, headers, rows, batches and comparisons. Official API
metadata, quota and other additional response fields are retained. Metric values
stay **strings**, preserving 64-bit integer precision and the API's representation
of decimal values. Metric type enums use the official Python key `type_`.

Invalid arguments produce an MCP error with validation guidance; API failures
produce a sanitized message about access, compatibility and quota. Transport
exception text is never returned or logged, as it may contain authorization
headers. The server logs only the tool name and exception class. These tools do
not mutate GA4 configuration, request edit scopes, or log credentials/tokens.

## Verification

Tests mock the official Data API responses and clients, exercising validation,
custom fields, request construction, output schemas, MCP dispatch, pagination,
large integer precision and secret redaction without GA4 credentials.

```sh
pip install -e '.[dev]'
nox -s lint
nox -s 'tests*'
```

Nox runs the unit tests on supported Python interpreters available in the
environment (3.10–3.13). No live-property integration test is required.
