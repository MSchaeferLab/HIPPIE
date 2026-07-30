"""Protocol-level tests: the tools as an MCP client actually sees them.

``test_mcp_tools`` calls the tool functions directly. These go through the SDK's
in-memory client instead, which is the only way to cover the parts the decorator
generates rather than the parts we wrote: the tool listing, the input schemas
derived from the type hints, and whether a result carries
``structured_content``.

The schema tests need no database. The one round-trip test does, and it uses
``TransactionTestCase`` on purpose: the SDK runs a synchronous tool on a worker
thread, which gets its own database connection, and an uncommitted
``TestCase`` transaction is invisible from there.
"""

from django.test import SimpleTestCase, TransactionTestCase
from mcp import Client

from hippie_mcp.server import mcp

from .factories import make_interaction, make_protein, recompute_stats

EXPECTED_TOOLS = {
    "resolve_protein",
    "get_interactions",
    "check_pairs",
    "get_interaction_detail",
    "list_filter_options",
}


class McpSchemaTests(SimpleTestCase):
    """What the decorator generated from the tool signatures."""

    async def _tools(self) -> dict:
        async with Client(mcp, raise_exceptions=True) as client:
            result = await client.list_tools()
        return {tool.name: tool for tool in result.tools}

    async def test_all_tools_are_registered(self):
        self.assertEqual(set(await self._tools()), EXPECTED_TOOLS)

    async def test_every_tool_has_a_description(self):
        for name, tool in (await self._tools()).items():
            with self.subTest(tool=name):
                self.assertTrue(
                    (tool.description or "").strip(),
                    f"{name} has no description — the model relies on it",
                )

    async def test_required_arguments_are_exactly_the_mandatory_ones(self):
        tools = await self._tools()
        expected = {
            "resolve_protein": ["identifier"],
            "get_interactions": ["proteins"],
            "check_pairs": ["pairs"],
            "get_interaction_detail": ["interaction_id"],
            "list_filter_options": [],
        }
        for name, required in expected.items():
            with self.subTest(tool=name):
                self.assertEqual(tools[name].input_schema.get("required", []), required)

    async def test_filter_arguments_are_present_and_optional(self):
        schema = (await self._tools())["get_interactions"].input_schema
        for arg in ("sources", "experiments", "interaction_types", "tissues"):
            with self.subTest(arg=arg):
                self.assertIn(arg, schema["properties"])
                self.assertNotIn(arg, schema.get("required", []))

    async def test_filter_argument_descriptions_mention_category_labels(self):
        """The 'you may pass a category label' contract has to reach the model."""
        schema = (await self._tools())["get_interactions"].input_schema
        description = schema["properties"]["experiments"].get("description", "")
        self.assertIn("category", description.lower())

    async def test_enum_arguments_are_constrained(self):
        schema = (await self._tools())["get_interactions"].input_schema
        for arg, values in (
            ("show", {"interactions", "noninteractions", "both"}),
            ("isoform_mode", {"general", "isoforms", "both"}),
            ("reviewed", {"both", "reviewed", "unreviewed"}),
            ("format", {"summary", "rows"}),
        ):
            with self.subTest(arg=arg):
                prop = schema["properties"][arg]
                enum = prop.get("enum") or prop.get("const")
                self.assertIsNotNone(enum, f"{arg} is unconstrained")
                self.assertEqual(set(enum), values)

    async def test_limit_is_bounded_in_the_schema(self):
        prop = (await self._tools())["get_interactions"].input_schema["properties"][
            "limit"
        ]
        self.assertEqual(prop.get("minimum"), 1)
        self.assertEqual(prop.get("maximum"), 200)

    async def test_tools_declare_an_output_schema(self):
        for name, tool in (await self._tools()).items():
            with self.subTest(tool=name):
                self.assertIsNotNone(
                    tool.output_schema,
                    f"{name} returns no output schema, so clients get no "
                    f"structured_content",
                )

    async def test_server_advertises_usage_instructions(self):
        async with Client(mcp, raise_exceptions=True) as client:
            self.assertIn("HIPPIE", client.instructions or "")


class McpRoundTripTests(TransactionTestCase):
    """One real call per tool, over the client, against committed data."""

    def setUp(self):
        self.tp53 = make_protein(
            "TP53", uniprot_name="P53_HUMAN", gene_id=7157, accession="P04637"
        )
        self.mdm2 = make_protein(
            "MDM2", uniprot_name="MDM2_HUMAN", gene_id=4193, accession="Q00987"
        )
        self.interaction = make_interaction(self.tp53, self.mdm2, score=0.97)
        recompute_stats()

    async def test_call_returns_content_and_structured_content(self):
        async with Client(mcp, raise_exceptions=True) as client:
            result = await client.call_tool("resolve_protein", {"identifier": "P04637"})

        self.assertFalse(result.is_error)
        # content is what the model reads; structured_content is for the client app.
        self.assertTrue(result.content)
        self.assertIsNotNone(result.structured_content)
        self.assertTrue(result.structured_content["found"])
        self.assertEqual(result.structured_content["matches"][0]["symbol"], "TP53")

    async def test_every_tool_answers_over_the_protocol(self):
        calls = {
            "resolve_protein": {"identifier": "P04637"},
            "get_interactions": {"proteins": ["P04637"]},
            "check_pairs": {"pairs": [["P04637", "Q00987"]]},
            "get_interaction_detail": {"interaction_id": self.interaction.pk},
            "list_filter_options": {},
        }
        async with Client(mcp, raise_exceptions=True) as client:
            for name, args in calls.items():
                with self.subTest(tool=name):
                    result = await client.call_tool(name, args)
                    self.assertFalse(result.is_error, f"{name} errored")
                    self.assertIsNotNone(result.structured_content)
                    self.assertIn("summary", result.structured_content)

    async def test_bad_arguments_are_rejected_by_validation(self):
        async with Client(mcp) as client:
            result = await client.call_tool(
                "get_interactions", {"proteins": ["P04637"], "show": "nonsense"}
            )
        self.assertTrue(result.is_error)

    async def test_unresolvable_filter_surfaces_over_the_protocol(self):
        async with Client(mcp, raise_exceptions=True) as client:
            result = await client.call_tool(
                "get_interactions",
                {"proteins": ["P04637"], "experiments": ["not-a-method"]},
            )
        self.assertEqual(result.structured_content["error"], "unresolved_filter")
