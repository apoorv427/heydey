"""CI grep for the banned-dependency wall (build doc §3 / CLAUDE.md).

Wrapper-free is a lock (L6): no agent frameworks, no heavy infra, no hosted
vector DBs, no graph libs. This test greps imports + declared dependencies.
"""

import json
import re
import tomllib
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = API_DIR.parent

BANNED_MODULES = frozenset(
    {
        "langchain",
        "langgraph",
        "crewai",
        "autogen",
        "celery",
        "redis",
        "minio",
        "psycopg",
        "psycopg2",
        "asyncpg",
        "logto",
        "neo4j",
        "networkx",
        "docker",
        "qdrant_client",
        "pinecone",
        "weaviate",
        "chromadb",
    }
)

_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w\.]*)")


def _python_sources():
    yield API_DIR / "heydey_supervisor.py"
    yield from (API_DIR / "heydey").rglob("*.py")
    yield from (API_DIR / "tests").rglob("*.py")


def test_no_banned_imports():
    offenders = []
    for source in _python_sources():
        for line_number, line in enumerate(source.read_text().splitlines(), 1):
            match = _IMPORT_RE.match(line)
            if match and match.group(1).split(".")[0] in BANNED_MODULES:
                offenders.append(f"{source}:{line_number}: {line.strip()}")
    assert not offenders, "banned imports found:\n" + "\n".join(offenders)


def test_no_banned_deps_declared():
    pyproject = tomllib.loads((API_DIR / "pyproject.toml").read_text())
    declared = list(pyproject["project"]["dependencies"])
    for extra in pyproject["project"].get("optional-dependencies", {}).values():
        declared.extend(extra)
    for dependency in declared:
        name = re.split(r"[=<>!\[ ]", dependency, 1)[0].lower().replace("-", "_")
        assert name not in BANNED_MODULES, f"banned dependency declared: {dependency}"

    package_json = REPO_ROOT / "webapp" / "package.json"
    if package_json.is_file():
        data = json.loads(package_json.read_text())
        node_deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        for name in node_deps:
            assert name.lower() not in BANNED_MODULES, f"banned npm dependency: {name}"
