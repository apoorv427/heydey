"""S6a gate — run: python -m heydey.s6a_gate

The MCP SECURITY SUBSTRATE, proven against a real subprocess connector before
any real external tool is allowed near the corpus (Contract C's immovable
ordering). Assertions map 1:1 to the contract:

  [1] roundtrip        hand-built stdio host: handshake + classified manifest
  [2] injection guard  poisoned result stored + tagged, EXCLUDED from context
  [3] outbound tray    write-classed tool refused without an approved row;
                       runs once approved; pull tools run free
  [4] cred isolation   workspace A's connector + credential invisible to B
  [5] egress gate      local_only profile refuses cloud with connector content

Scope honesty: real Shopify/Sheets connectors, Playbooks, Live Map and the
<10-min Foundry onboarding (Showcase 2) are S6b/S6c — NOT claimed here.
"""

import sys

from . import approvals, connectors, workspaces
from .connectors import EgressError, egress_allows
from .mcp_host import ApprovalRequired, MCPHost

DEMO_CMD = [sys.executable, "-m", "heydey.demo_connector"]


def _ensure_workspace(workspace_id: str) -> None:
    try:
        workspaces.create_workspace(workspace_id)
    except workspaces.WorkspaceExists:
        pass


def main() -> int:  # noqa: C901 — a gate reads top to bottom
    print("=" * 68)
    print("S6a GATE — MCP host + Contract-C security substrate")
    print("=" * 68)
    results: dict[str, bool] = {}

    _ensure_workspace("s6a-client-a")
    conn = workspaces.connect("s6a-client-a")

    with MCPHost(workspace_id="s6a-client-a", connector_id="demo", command=DEMO_CMD) as host:
        manifest = host.list_tools()
        classes = {t["name"]: t["approval_class"] for t in manifest}
        results["roundtrip"] = (classes.get("list_orders") == "none"
                                and classes.get("create_discount") == "outbound")
        print(f"\n[1] roundtrip: {len(manifest)} tools · " +
              " · ".join(f"{k}={v}" for k, v in classes.items()) +
              f"  -> {'OK' if results['roundtrip'] else 'FAIL'}")

        poisoned = host.call_tool(conn, "poisoned_feed")
        excluded = ("ignore previous instructions" not in poisoned["context_block"].lower()
                    and f"[connector_result_stored:{poisoned['stored_id']}]"
                    in poisoned["context_block"])
        results["injection"] = poisoned["injection_risk"] and excluded
        print(f"\n[2] injection guard: risk={poisoned['injection_risk']} · "
              f"context carries stored-ref not payload={excluded}"
              f"  -> {'OK' if results['injection'] else 'FAIL'}")
        print(f"    context_block: {poisoned['context_block'][:96]}…")

        try:
            host.call_tool(conn, "create_discount", {"code": "GATE10"})
            refused = False
        except ApprovalRequired:
            refused = True
        approval_id = approvals.create_approval(
            conn, action_class="outbound",
            payload={"kind": "connector_call", "title": "Create discount GATE10",
                     "connector_id": "demo", "tool": "create_discount"})
        approvals.decide(conn, approval_id, "approved", workspace_id="s6a-client-a")
        after = host.call_tool(conn, "create_discount", {"code": "GATE10"},
                               approval_id=approval_id)
        free = host.call_tool(conn, "list_orders")
        results["tray"] = refused and "created" in after["context_block"] and bool(
            free["context_block"])
        print(f"\n[3] outbound tray: unapproved refused={refused} · approved ran · "
              f"pull ran free  -> {'OK' if results['tray'] else 'FAIL'}")

    ref = connectors.register(conn, "s6a-client-a", "shopify", scopes="read_orders")
    _ensure_workspace("s6a-client-b")
    conn_b = workspaces.connect("s6a-client-b")
    invisible = (connectors.list_connectors(conn_b, "s6a-client-b") == []
                 and connectors.get(conn_b, "s6a-client-a", "shopify") is None)
    results["isolation"] = ref == "heydey.s6a-client-a.shopify" and invisible
    print(f"\n[4] cred isolation: ref={ref} · invisible to client-b={invisible}"
          f"  -> {'OK' if results['isolation'] else 'FAIL'}")
    conn_b.close()
    conn.close()

    try:
        egress_allows("local_only", "deepseek/deepseek-chat", uses_connector_content=True)
        blocked = False
    except EgressError:
        blocked = True
    local_ok = True
    try:
        egress_allows("local_only", "llama3.1:8b", uses_connector_content=True)
    except EgressError:
        local_ok = False
    results["egress"] = blocked and local_ok
    print(f"\n[5] egress gate: cloud+connector blocked={blocked} · local lane allowed={local_ok}"
          f"  -> {'OK' if results['egress'] else 'FAIL'}")

    overall = all(results.values())
    print("\n" + "=" * 68)
    print(f"S6a GATE: {'PASS ✅' if overall else 'FAIL ❌'}  (" +
          " · ".join(f"{k} {'✓' if v else '✗'}" for k, v in results.items()) +
          ")   [S6b: real connectors + Playbooks · S6c: Foundry/Showcase-2 — open]")
    print("=" * 68)
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
