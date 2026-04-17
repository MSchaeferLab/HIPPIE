# HIPPIE Django Rewrite

## Project Context
Rewrite of HIPPIE (protein-protein interaction database) from PHP/MariaDB into Django.
Original system: PHP + MariaDB. Target: Django + PostgreSQL.

## Stack
- Python 3.x, Django
- Database: PostgreSQL (migrating from MariaDB)
- Package manager: pip / venv
- Lint: ruff
- Type check: pyright (if used)
- Tests: pytest-django

## Key Commands
- Run migrations: `python manage.py migrate`
- Make migrations: `python manage.py makemigrations`
- Run tests: `python manage.py test`
- Start dev server: `python manage.py runserver`

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
