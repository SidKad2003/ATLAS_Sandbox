"""Shared fixtures for svc-sim contract tests. Run against the LIVE stack (`make test`)."""
import os
import uuid

import pytest

from svc_sim_common.mcp_client import MCPTools

# Synthetic customer, same style as the upstream load-generator people.json (fake card).
SYNTHETIC_PERSON = {
    "email": "contract-test@example.com",
    "address": {
        "streetAddress": "1 Test Street",
        "city": "Mountain View",
        "state": "CA",
        "country": "United States",
        "zipCode": "94043",
    },
    "credit_card": {
        "creditCardNumber": "4432-8015-6152-0454",
        "creditCardExpirationMonth": 1,
        "creditCardExpirationYear": 2039,
        "creditCardCvv": 672,
    },
}

REFERENCE_PRODUCT = "OLJCESPC7Z"
TEST_RUN_ID = "contract-test"


def pytest_collection_modifyitems(items):
    for item in items:
        item.add_marker(pytest.mark.asyncio(loop_scope="session"))


@pytest.fixture(scope="session")
def shop() -> MCPTools:
    return MCPTools("shop-mcp", os.environ.get("SHOP_MCP_URL", "http://shop-mcp:8000"))


@pytest.fixture(scope="session")
def store_ops() -> MCPTools:
    return MCPTools("store-ops-mcp", os.environ.get("STORE_OPS_MCP_URL", "http://store-ops-mcp:8000"))


@pytest.fixture(scope="session")
def obs() -> MCPTools:
    return MCPTools("observability-mcp", os.environ.get("OBSERVABILITY_MCP_URL", "http://observability-mcp:8000"))


@pytest.fixture
def session_id() -> str:
    return f"contract-{uuid.uuid4()}"


@pytest.fixture(scope="session")
def person() -> dict:
    return SYNTHETIC_PERSON
