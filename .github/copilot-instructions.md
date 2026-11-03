<!--
This file was generated as a starter because no repository files were discoverable
during analysis. Update the "Examples" and "Key files" sections below with
concrete paths from this project so AI coding agents can be immediately productive.
-->
# Copilot agent instructions — Project-specific guidance

Purpose
- Help an AI coding agent become productive quickly in this repository.

What to open first
- Look for: `README.md`, `src/` or `lib/`, `tests/`, `Makefile` or `pyproject.toml`.
- If these files don't exist, ask the human: "Which directory contains the source code?"

Big picture guidance
- Describe high-level architecture here (e.g., "single-package Python CLI", "React frontend + Flask API", "microservices in services/*").
- If the repo contains multiple components, list them in order of importance with their entrypoints.

Build / run / test (examples)
- Common commands to try (adapt to repo):
  - `make build` or `make test`
  - `python -m pytest` or `pytest tests/`
  - `npm install && npm test` (for JS/TS projects)
  - `./gradlew build` or `mvn -DskipTests package` (for Java)
- If these are wrong, update this section with the actual commands (CI YAML, Makefile, or package manifest).

Project conventions and patterns
- Common file/directory names to check: `src/`, `app/`, `services/`, `pkg/`, `tests/`, `infra/`.
- Preferred style/tooling: note any linters, formatters, or typed languages found (e.g., `black`, `flake8`, `prettier`, `tsconfig.json`).
- Describe commit message or PR expectations if present (CONTRIBUTING.md or CI checks).

Integration points
- List external services, APIs, or databases the project depends on (from env files, configs, or docs).
- Note any credentials or secrets patterns (e.g., `.env`, `secrets.yml`, GitHub Actions secrets).

Examples (update these with real paths from the repo)
- Architecture example: `src/main.py` starts the application and loads `config/*.yml`.
- Test example: `tests/test_api.py` shows how components are instantiated in tests.

How to propose changes
- Make focused edits, run the tests locally, and run linters used by CI.
- When adding new behavior, include a unit test demonstrating intended behavior.

If you (human) see this file
- Please replace the placeholder examples above with real file paths and commands.
- If you want me to re-run discovery, reply with a path to the repository root or grant file access.

Questions for the author
- Where is the main application entry point? (provide path)
- What are the canonical build and test commands?
- Are there any non-standard conventions (monorepo layout, generated code directories)?

Thank you — after you update the Examples/Key files I will re-open and create
targeted, actionable instructions with concrete patterns and references.
