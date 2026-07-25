// Real list_tools output from the example deployment's MCP servers (introspected live via our MCP
// client). In production the "Introspect" action fetches this through a backend
// endpoint; embedded here so the spike shows the real cross-check without a call.
// Reserved polartie_* session tools are kept — the model-tool filter drops them.
export const MCP_INTROSPECTION: Record<string, { tools: string[]; error?: string }> = {
  "https://malaga-airport-knowledge-mcp.hub01.polartie.com/mcp": {
    tools: [
      "ask", "query_documents", "health", "get_pipeline_status", "get_documents_status",
      "get_image", "get_image_meta", "list_images", "find_images", "render_page",
      "render_image_location", "polartie_ai_agent_session", "polartie_ai_agent_session_heartbeat",
    ],
  },
  "https://malaga-schedule-mcp.hub01.polartie.com/mcp": {
    tools: ["search_flights", "agent_session_link", "polartie_ai_agent_session", "polartie_ai_agent_session_heartbeat"],
  },
};
