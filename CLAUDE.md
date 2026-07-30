# HIPPIE Django Rewrite

## Project Context
Rewrite of HIPPIE (protein-protein interaction database) from PHP/MariaDB into Django.
Original system: PHP + MariaDB. Target: Django + MariaDB.

## Stack
- Python 3.x, Django
- Database: MariaDB (prod) / SQLite (dev)
- Package manager: pip / venv
- Lint: ruff
- Type check: pyright (if used)
- Tests: pytest-django

## Key Commands
All `manage.py` commands run from `hippie_django/`:
- Run migrations: `python manage.py migrate`
- Make migrations: `python manage.py makemigrations`
- Run tests: `python manage.py test`
- Start dev server: `python manage.py runserver`
- Create superuser: `python manage.py createsuperuser`

## Setup (first run)
```bash
python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
cd hippie_django
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

## Schema
- Reduced from 50 tables (schema reduction already designed)
- Core models: Protein, Isoform, Interaction, NonInteraction, BaitPreyAssociation
- See models.py for the canonical schema

## Code Style
- Type hints required on all new code
- No `Any` types
- ORM-only — no raw SQL unless absolutely necessary and documented
- Prefer class-based views for CRUD, function-based views for custom logic
- All migrations must be reviewed before applying — schema changes are sensitive

## Critical Rules
- Never drop or rename columns without a migration plan
- Always use select_related/prefetch_related for FK traversals
- The original PHP schema is the reference for data meaning, not structure

## Pre-commit / Linting
- pre-commit hooks active: ruff-check (with --fix), ruff-format, trailing-whitespace, end-of-file-fixer, check-yaml, check-added-large-files
- Install hooks after fresh clone: `pre-commit install`
- Run manually: `pre-commit run --all-files`
- No separate CI config exists — linting enforced locally via pre-commit only

## Performance Gotchas (browse_proteins_api hard-won lessons)
- `browse_proteins_api` serves ~20k proteins in 500-row chunks to the React frontend
- **Never annotate the count queryset** — Django wraps annotated counts in a subquery, making it O(n) expensive
- Pattern: filter lean → get ID slice → compute degree/avg_score via targeted `Interaction` aggregates on those IDs only
- `degree` = interactions where `protein_1=id` + `protein_2=id` − self-loops (counted twice otherwise)
- `avg_score` is derived from the same `Interaction` aggregates, not a correlated subquery
- Existing indexes on `(protein_1, score)` and `(protein_2, score)` — queries must ride these
- When `min_degree`/`min_score` filter is active, materialize candidate PIDs first, then compute stats — avoids GROUP BY across full table
- Use `order by pk` for stable offset/limit pagination across chunks

## Isoform Handling
- `Isoform` is a multi-table-inheritance subclass of `Protein` (`Isoform(Protein)`); an isoform row's PK is also a `Protein` PK, joined via the implicit `protein_ptr` FK
- On a queryset joined to `Isoform`, `isoform__isnull=True` means "canonical" (no isoform row for this protein), `isoform__isnull=False` means "is an isoform"
- Canonical proteins expand to isoforms via `Isoform.uniprot_accession` prefix match on the canonical `Protein.uniprot_accession` (see `_get_isoforms(protein_pk)` in views.py)
- Isoform input (e.g. `P37163-2`) is NOT expanded further — returned as-is
- Interaction lookups across isoforms: batch all (A-isoform × B-isoform) combos in a single query per pair
- Isoform filtering is a 3-way `isoform_mode` param (`general | isoforms | both`) on every query/browse/ML-splits endpoint, parsed by `query_filters.parse_isoform_mode()`. Two gate predicates in `query_filters.py`:
  - `canonical_or_queried_q(protein_pks)` — general mode: canonical-or-explicitly-queried
  - `isoform_only_q()` — isoforms mode: at least one endpoint is a non-canonical isoform
  - "both" mode applies neither predicate (no isoform filter)

## Project Structure
```
hippie_django/
  hippie/           # Django project settings, urls, wsgi
  hippie_website/   # Main app: models, views, forms, migrations
    services/       # Query logic, shared by the website and the MCP server
      queries.py    #   CommonFilters, identifier resolution, interaction rows
      pairs.py      #   pair lookup ("does A interact with B")
      detail.py     #   one interaction's full evidence (template ctx + JSON)
      vocab.py      #   filter option lists
      generate_splits.py
    filter_lookup.py  # name / PSI-MI code / category label / id → vocabulary PKs
    management/commands/  # hippie_update, import_pod_data, export_downloads, …
    migrations/     # squash carefully — schema is sensitive
  hippie_mcp/       # MCP server (ASGI, separate container) — see below
data/               # raw import data lives here
```

## Service layer
Query logic lives in `hippie_website/services/`, not in views. Views own request
parsing and response construction only. The MCP tools call the same functions, so
the website and an agent cannot disagree about what a query means — the drift
check for that is comparing `/api/query/` counts against `get_interactions`.

`views.py` re-imports the extracted helpers under their original private names
(`_protein_display`, `_resolve_interaction_pair`, `_filter_option_lists`, …)
because the existing tests import them from there. Keep those aliases when
moving more logic out.

## MCP server (`hippie_mcp`)
Five read-only tools over streamable HTTP: `resolve_protein`,
`get_interactions`, `check_pairs`, `get_interaction_detail`,
`list_filter_options`. Served by uvicorn in its own container
(`uvicorn hippie_mcp.asgi:app`), proxied by Apache at `/mcp`.

- **MCP SDK is 2.0** — `from mcp.server import MCPServer`. The v1 `FastMCP` API
  and `mcp.server.fastmcp` no longer exist; wire types are snake_case
  (`input_schema`, not `inputSchema`).
- Tools are `def`, not `async def`, so the SDK runs them on a worker thread and
  the sync ORM is legal. Every tool body is wrapped in `hippie_mcp.db.with_db`
  for connection hygiene — a pooled thread otherwise holds a connection past
  MariaDB's `wait_timeout`.
- Return `dict[str, object]`, not `dict` — a bare `dict` yields no output schema
  and no `structured_content`.
- Tool results are capped (`shaping.py`) and always report the full match count.
  Never let a truncated result look complete.
- A filter value that resolves to nothing **fails the call**. An unresolved
  filter would collapse to "no filter" and silently return a wider result set.
- `asgi.py` calls `django.setup()` before importing anything model-touching, and
  its lifespan must enter `mcp.session_manager.run()` — a mounted sub-app's own
  lifespan never runs, and without it every request dies with
  `RuntimeError: Task group is not initialized`.
- `transport_security` is derived from `DJANGO_ALLOWED_HOSTS`. Without it, DNS
  rebinding protection answers every request behind a real hostname with
  `421 Misdirected Request`.

## Workflow Patterns
- Feature branches → PR → merge to main (no direct pushes to main observed)
- Claude-generated branches named `claude/<slug>-<id>` (e.g. `claude/add-detection-info-onZzl`)
- Commit messages: imperative mood, plain English; detailed body when behavior is non-obvious
- SQLite used in dev (`db.sqlite3` in hippie_django/); MariaDB for prod

<!-- code-review-graph MCP tools -->
## MCP Tools: code-review-graph

**IMPORTANT: This project has a knowledge graph. ALWAYS use the
code-review-graph MCP tools BEFORE using Grep/Glob/Read to explore
the codebase.** The graph is faster, cheaper (fewer tokens), and gives
you structural context (callers, dependents, test coverage) that file
scanning cannot.

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes` or `query_graph` instead of Grep
- **Understanding impact**: `get_impact_radius` instead of manually tracing imports
- **Code review**: `detect_changes` + `get_review_context` instead of reading entire files
- **Finding relationships**: `query_graph` with callers_of/callees_of/imports_of/tests_for
- **Architecture questions**: `get_architecture_overview` + `list_communities`

Fall back to Grep/Glob/Read **only** when the graph doesn't cover what you need.

### Key Tools

| Tool | Use when |
|------|----------|
| `detect_changes` | Reviewing code changes — gives risk-scored analysis |
| `get_review_context` | Need source snippets for review — token-efficient |
| `get_impact_radius` | Understanding blast radius of a change |
| `get_affected_flows` | Finding which execution paths are impacted |
| `query_graph` | Tracing callers, callees, imports, tests, dependencies |
| `semantic_search_nodes` | Finding functions/classes by name or keyword |
| `get_architecture_overview` | Understanding high-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |

### Workflow

1. The graph auto-updates on file changes (via hooks).
2. Use `detect_changes` for code review.
3. Use `get_affected_flows` to understand impact.
4. Use `query_graph` pattern="tests_for" to check coverage.

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke office-hours
- Bugs, errors, "why is this broken", 500 errors → invoke investigate
- Ship, deploy, push, create PR → invoke ship
- QA, test the site, find bugs → invoke qa
- Code review, check my diff → invoke review
- Update docs after shipping → invoke document-release
- Weekly retro → invoke retro
- Design system, brand → invoke design-consultation
- Visual audit, design polish → invoke design-review
- Architecture review → invoke plan-eng-review
- Save progress, checkpoint, resume → invoke checkpoint
- Code quality, health check → invoke health
