default:
    @just --list

check:
    uv run ruff check src tests
    uv run ruff format --check src tests
    uv run pyright
    uv run pytest -q

format:
    uv run ruff format src tests

run:
    uv run reading-relay run
