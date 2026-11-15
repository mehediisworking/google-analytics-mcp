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

"""Mocked official Data API and MCP contract tests for advanced reporting."""

import copy
import inspect
import io
import json
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

import jsonschema
from google.analytics import data_v1beta as ga
from mcp import types as mcp_types

from analytics_mcp import coordinator
from analytics_mcp.tools.reporting import advanced as a, core

RANGE = {"start_date": "2026-01-01", "end_date": "2026-01-31"}
REPORT = {
    "date_ranges": [RANGE],
    "dimensions": ["country"],
    "metrics": ["sessions"],
}


def report_response(value="9007199254740993"):
    return ga.RunReportResponse(
        dimension_headers=[ga.DimensionHeader(name="country")],
        metric_headers=[ga.MetricHeader(name="sessions", type_="TYPE_INTEGER")],
        rows=[
            ga.Row(
                dimension_values=[ga.DimensionValue(value="Bangladesh")],
                metric_values=[ga.MetricValue(value=value)],
            )
        ],
        row_count=1,
        metadata=ga.ResponseMetaData(time_zone="Asia/Dhaka"),
    )


class AdvancedTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patcher = patch.object(a, "create_data_api_client")
        self.client = self.patcher.start().return_value
        self.addCleanup(self.patcher.stop)
        self.client.get_metadata.return_value = ga.Metadata(
            name="properties/123/metadata",
            dimensions=[
                ga.DimensionMetadata(api_name="country"),
                ga.DimensionMetadata(
                    api_name="customEvent:plan", custom_definition=True
                ),
            ],
            metrics=[
                ga.MetricMetadata(api_name="sessions", type_="TYPE_INTEGER"),
                ga.MetricMetadata(
                    api_name="customEvent:score", custom_definition=True
                ),
            ],
        )
        self.client.batch_run_reports.return_value = ga.BatchRunReportsResponse(
            reports=[report_response()]
        )

    async def test_metadata_and_custom_fields(self):
        metadata = await a.get_report_metadata("properties/123")
        self.assertTrue(metadata["dimensions"][1]["custom_definition"])
        self.assertEqual(metadata["metrics"][0]["type_"], "TYPE_INTEGER")
        self.assertEqual(
            self.client.get_metadata.call_args.kwargs["request"].name,
            "properties/123/metadata",
        )
        report = dict(
            REPORT,
            dimensions=["customEvent:plan"],
            metrics=["customEvent:score"],
        )
        await a.batch_run_reports(123, [report])
        req = self.client.batch_run_reports.call_args.kwargs["request"]
        self.assertEqual(req.requests[0].metrics[0].name, "customEvent:score")
        await a.get_report_metadata(0)
        self.assertEqual(
            self.client.get_metadata.call_args.kwargs["request"].name,
            "properties/0/metadata",
        )

    async def test_compatibility_returns_incompatible_fields(self):
        self.client.check_compatibility.return_value = (
            ga.CheckCompatibilityResponse(
                dimension_compatibilities=[
                    ga.DimensionCompatibility(
                        dimension_metadata=ga.DimensionMetadata(
                            api_name="country"
                        ),
                        compatibility="INCOMPATIBLE",
                    )
                ]
            )
        )
        result = await a.check_compatibility(123, ["country"], ["sessions"])
        self.assertEqual(
            result["dimension_compatibilities"][0]["compatibility"],
            "INCOMPATIBLE",
        )
        self.client.check_compatibility.assert_called_once()

    async def test_batch_preserves_types_order_and_pagination(self):
        self.client.batch_run_reports.return_value = ga.BatchRunReportsResponse(
            reports=[report_response(), report_response("0")]
        )
        result = await a.batch_run_reports(
            123, [dict(REPORT, limit=250000, offset=10), REPORT]
        )
        req = self.client.batch_run_reports.call_args.kwargs["request"]
        self.assertEqual(req.property, "properties/123")
        self.assertEqual(req.requests[0].limit, 250000)
        self.assertEqual(req.requests[0].offset, 10)
        self.assertEqual(
            result["reports"][0]["rows"][0]["metric_values"][0]["value"],
            "9007199254740993",
        )
        self.assertEqual(
            result["reports"][1]["rows"][0]["metric_values"][0]["value"], "0"
        )
        self.assertEqual(
            result["reports"][0]["metadata"]["time_zone"], "Asia/Dhaka"
        )
        self.client.get_metadata.assert_called_once()
        self.client.batch_run_reports.assert_called_once()

    async def test_invalid_report_inputs_do_not_execute_reports(self):
        invalid = [dict(REPORT, limit=x) for x in (0, -1, 250001, True, "5")]
        invalid += [
            dict(REPORT, offset=-1),
            dict(REPORT, dimensions=["unknown"]),
            dict(REPORT, metrics=["country"]),
            dict(REPORT, metrics=[]),
            dict(REPORT, metrics=["sessions"] * 11),
            dict(REPORT, dimensions=["country", "country"]),
            dict(REPORT, date_ranges=[]),
            dict(REPORT, date_ranges=[RANGE] * 5),
            dict(REPORT, date_ranges=[dict(RANGE, start_date="2026-02-30")]),
            dict(REPORT, date_ranges=[dict(RANGE, end_date="2025-01-01")]),
            dict(
                REPORT,
                date_ranges=[dict(start_date="today", end_date="2daysAgo")],
            ),
            dict(REPORT, property="properties/456"),
            dict(REPORT, typo=1),
            dict(REPORT, order_bys=[{"metric": {"metric_name": "other"}}]),
        ]
        for report in invalid:
            with (
                self.subTest(report=report),
                self.assertRaises((ValueError, TypeError)),
            ):
                await a.batch_run_reports(123, [report])
        for reports in ([], [REPORT] * 6):
            with self.assertRaises(ValueError):
                await a.batch_run_reports(123, reports)
        self.client.batch_run_reports.assert_not_called()

    async def test_filters_validate_recursively_and_keep_input_unchanged(self):
        expression = {
            "and_group": {
                "expressions": [
                    {
                        "not_expression": {
                            "filter": {
                                "field_name": "country",
                                "in_list_filter": {"values": ["US"]},
                            }
                        }
                    }
                ]
            }
        }
        report = dict(REPORT, dimension_filter=expression)
        original = copy.deepcopy(report)
        await a.batch_run_reports(123, [report])
        self.assertEqual(report, original)
        invalid = [
            {},
            {"or_group": {"expressions": []}},
            {
                "not_expression": {
                    "filter": {"field_name": "sessions", "empty_filter": {}}
                }
            },
            {"filter": {"field_name": "country", "string_filter": {}}},
            {
                "filter": {
                    "field_name": "country",
                    "in_list_filter": {"values": []},
                }
            },
            {
                "filter": {
                    "field_name": "country",
                    "empty_filter": {},
                    "string_filter": {},
                }
            },
            {"filter": {"field_name": "country", "bad_filter": {}}},
        ]
        self.client.batch_run_reports.reset_mock()
        for expression in invalid:
            with (
                self.subTest(expression=expression),
                self.assertRaises((ValueError, TypeError)),
            ):
                await a.batch_run_reports(
                    123, [dict(REPORT, dimension_filter=expression)]
                )
        self.client.batch_run_reports.assert_not_called()

    async def test_numeric_filter_and_relative_dates(self):
        expression = {
            "filter": {
                "field_name": "sessions",
                "numeric_filter": {
                    "operation": "GREATER_THAN",
                    "value": {"int64_value": 5},
                },
            }
        }
        await a.batch_run_reports(
            123,
            [
                dict(
                    REPORT,
                    metric_filter=expression,
                    date_ranges=[
                        {"start_date": "30daysAgo", "end_date": "yesterday"}
                    ],
                )
            ],
        )
        self.client.batch_run_reports.assert_called_once()
        with self.assertRaises(ValueError):
            await a.batch_run_reports(
                123,
                [
                    dict(
                        REPORT,
                        metric_filter={
                            "filter": {
                                "field_name": "sessions",
                                "numeric_filter": {"operation": "GREATER_THAN"},
                            }
                        },
                    )
                ],
            )

    async def test_pivot_request_and_response(self):
        self.client.run_pivot_report.return_value = ga.RunPivotReportResponse(
            pivot_headers=[ga.PivotHeader(row_count=2)],
            metric_headers=[
                ga.MetricHeader(name="sessions", type_="TYPE_INTEGER")
            ],
        )
        result = await a.run_pivot_report(
            123, REPORT, [{"field_names": ["country"], "limit": 20}]
        )
        req = self.client.run_pivot_report.call_args.kwargs["request"]
        self.assertIsInstance(req, ga.RunPivotReportRequest)
        self.assertEqual(req.pivots[0].limit, 20)
        self.assertEqual(result["pivot_headers"][0]["row_count"], 2)
        self.assertEqual(result["rows"], [])

    async def test_pivot_rejects_bad_axes_and_limits(self):
        for axes in (
            [],
            [{"field_names": ["country"], "limit": 0}],
            [{"field_names": ["other"], "limit": 10}],
            [{"field_names": ["country"], "limit": 10}] * 2,
        ):
            with self.subTest(axes=axes), self.assertRaises(ValueError):
                await a.run_pivot_report(123, REPORT, axes)
        multi = dict(REPORT, dimensions=["country", "customEvent:plan"])
        with self.assertRaisesRegex(ValueError, "Product"):
            await a.run_pivot_report(
                123,
                multi,
                [
                    {"field_names": ["country"], "limit": 501},
                    {"field_names": ["customEvent:plan"], "limit": 500},
                ],
            )
        with self.assertRaisesRegex(ValueError, "pivot-level"):
            await a.run_pivot_report(123, dict(REPORT, limit=10), [])
        self.client.run_pivot_report.assert_not_called()

    async def test_comparison_is_one_batch_with_identical_fields(self):
        self.client.batch_run_reports.return_value = ga.BatchRunReportsResponse(
            reports=[report_response("0"), report_response("50")]
        )
        spec = {k: v for k, v in REPORT.items() if k != "date_ranges"}
        result = await a.run_comparison_report(
            123,
            spec,
            RANGE,
            {"start_date": "2026-02-01", "end_date": "2026-02-28"},
        )
        req = self.client.batch_run_reports.call_args.kwargs["request"]
        self.assertEqual(req.requests[0].dimensions, req.requests[1].dimensions)
        self.assertEqual(
            req.requests[1].date_ranges[0].start_date, "2026-02-01"
        )
        self.assertEqual(
            result["baseline"]["rows"][0]["metric_values"][0]["value"], "0"
        )
        self.client.batch_run_reports.assert_called_once()

    async def test_bad_property_ids_and_sanitized_api_errors(self):
        for value in (True, -1, "properties/a/123", "abc"):
            with self.assertRaises(ValueError):
                await a.get_report_metadata(value)
        self.client.get_metadata.assert_not_called()
        self.client.get_metadata.side_effect = RuntimeError(
            "Authorization: Bearer SECRET_TOKEN"
        )
        server = coordinator.create_server()
        handler = server.get_request_handler("tools/call").handler
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = await handler(
                None,
                mcp_types.CallToolRequestParams(
                    name="get_report_metadata", arguments={"property_id": 123}
                ),
            )
        self.assertTrue(result.is_error)
        self.assertNotIn(
            "SECRET_TOKEN", stderr.getvalue() + result.content[0].text
        )
        self.assertIn("property access", result.content[0].text)

    async def test_mcp_schemas_annotations_and_structured_content(self):
        server = coordinator.create_server()
        tools = (
            await server.get_request_handler("tools/list").handler(None, None)
        ).tools
        by_name = {t.name: t for t in tools}
        for function in a.TOOLS:
            tool = by_name[function.__name__]
            self.assertTrue(tool.annotations.read_only_hint)
            self.assertGreater(len(tool.description), 100)
            jsonschema.Draft202012Validator.check_schema(tool.input_schema)
            jsonschema.Draft202012Validator.check_schema(tool.output_schema)
        jsonschema.validate(
            {"property_id": 123, "reports": [REPORT]},
            by_name["batch_run_reports"].input_schema,
        )
        result = await server.get_request_handler("tools/call").handler(
            None,
            mcp_types.CallToolRequestParams(
                name="batch_run_reports",
                arguments={"property_id": 123, "reports": [REPORT]},
            ),
        )
        self.assertFalse(result.is_error)
        self.assertEqual(
            result.structured_content, json.loads(result.content[0].text)
        )
        jsonschema.validate(
            result.structured_content,
            by_name["batch_run_reports"].output_schema,
        )
        # Legacy tools retain their signatures and remain available read-only.
        self.assertIn("run_report", by_name)
        self.assertEqual(
            list(inspect.signature(core.run_report).parameters),
            [
                "property_id",
                "date_ranges",
                "dimensions",
                "metrics",
                "dimension_filter",
                "metric_filter",
                "order_bys",
                "limit",
                "offset",
                "currency_code",
                "return_property_quota",
            ],
        )

    async def test_compatibility_rejects_invalid_fields_and_filters(self):
        for dimensions, metrics, filters in (
            (["country"] * 10, ["sessions"], {}),
            (["country"], ["sessions"] * 11, {}),
            (["notRegistered"], ["sessions"], {}),
            (
                ["country"],
                ["sessions"],
                {
                    "metric_filter": {
                        "filter": {"field_name": "country", "empty_filter": {}}
                    }
                },
            ),
        ):
            with (
                self.subTest(dimensions=dimensions),
                self.assertRaises(ValueError),
            ):
                await a.check_compatibility(123, dimensions, metrics, **filters)
        self.client.check_compatibility.assert_not_called()

    async def test_multirange_pivot_preserves_axes_sort_and_aggregations(self):
        self.client.run_pivot_report.return_value = ga.RunPivotReportResponse()
        report = dict(REPORT, date_ranges=[RANGE, dict(RANGE, name="second")])
        await a.run_pivot_report(
            123,
            report,
            [
                {
                    "field_names": ["country"],
                    "limit": 10,
                    "offset": 2,
                    "order_bys": [
                        {"metric": {"metric_name": "sessions"}, "desc": True}
                    ],
                    "metric_aggregations": ["TOTAL"],
                },
                {"field_names": ["dateRange"], "limit": 2},
            ],
        )
        req = self.client.run_pivot_report.call_args.kwargs["request"]
        self.assertEqual(req.pivots[0].offset, 2)
        self.assertEqual(
            req.pivots[0].metric_aggregations, [ga.MetricAggregation.TOTAL]
        )
        self.assertTrue(req.pivots[0].order_bys[0].desc)
        self.assertEqual(req.pivots[1].field_names, ["dateRange"])

    async def test_numeric_filter_edge_cases(self):
        invalid = [
            {"numeric_filter": {"operation": "GREATER_THAN", "value": {}}},
            {"numeric_filter": {"operation": 999, "value": {"int64_value": 1}}},
            {
                "numeric_filter": {
                    "operation": "GREATER_THAN",
                    "value": {"double_value": float("inf")},
                }
            },
            {
                "between_filter": {
                    "from_value": {"int64_value": "9007199254740993"},
                    "to_value": {"int64_value": "9007199254740992"},
                }
            },
        ]
        for predicate in invalid:
            with (
                self.subTest(predicate=predicate),
                self.assertRaises(ValueError),
            ):
                await a.batch_run_reports(
                    123,
                    [
                        dict(
                            REPORT,
                            metric_filter={
                                "filter": dict(
                                    field_name="sessions", **predicate
                                )
                            },
                        )
                    ],
                )
        self.client.batch_run_reports.assert_not_called()

    async def test_comparison_rejects_overrides_and_schema_rejects_bad_limits(
        self,
    ):
        with self.assertRaisesRegex(ValueError, "baseline_date_range"):
            await a.run_comparison_report(123, REPORT, RANGE, RANGE)
        with self.assertRaises(ValueError):
            await a.run_comparison_report(
                123,
                {"metrics": ["sessions"]},
                RANGE,
                {"start_date": "bad", "end_date": "today"},
            )
        tool = next(
            t
            for t in coordinator._READ_ONLY_MCP_TOOLS
            if t.name == "batch_run_reports"
        )
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                {"property_id": 123, "reports": [dict(REPORT, limit=0)]},
                tool.input_schema,
            )
        self.client.batch_run_reports.assert_not_called()
