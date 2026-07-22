// A representative PolarTie robot config.json (SessionConfig-shaped) for the
// visualisation spike. Deliberately includes one foot-gun: send_email is enabled
// but notification_service_url is empty — and the prompt promises to email the
// caller, so both the dependency graph and the logical prompt check light up.
export const SAMPLE_CONFIG = {
  display_name: "Málaga Airport Info Desk",
  prompt:
    "You are the information desk agent at Málaga Airport. Greet callers warmly and answer in the caller's language (Spanish or English). If a caller asks, you can email them a summary of their enquiry. For lost baggage, transfer them to the baggage desk; for lost property, send them to the lost-property desk. Keep replies short.",
  voice: "alloy",
  default_language_iso: "es",
  notification_service_url: "",
  custom_config: { gpt_model: "gpt-realtime-1.5", reconfigure_with_ai: true },
  tools: {
    hangup: { enabled: true, prompts: { description: { message: "End the call when the caller is done." } } },
    transfer: { enabled: true, prompts: { description: { message: "Transfer the call to a specialist desk." } } },
    show_text: { enabled: true, prompts: { description: { message: "Show text on the caller's screen." } } },
    show_table: { enabled: true, prompts: { description: { message: "Show a table on the caller's screen." } } },
    knowledge_base_query_lightrag: { enabled: true, prompts: { description: { message: "Look up airport facts." } } },
    send_email: { enabled: true, prompts: { description: { message: "Email the caller a summary of the enquiry." } } },
    stay_silent: { enabled: true, prompts: { description: { message: "Stay silent when it's not your turn." } } },
    take_camera_snapshot: { enabled: false, prompts: {} },
  },
  transfer_topics: [
    { topic_id: "lost_luggage", function_tag: "baggage", prompt: "Route to the baggage desk." },
    { topic_id: "special_assistance", function_tag: "pmr", prompt: "Route to reduced-mobility assistance." },
  ],
  knowledge_base: [
    { knowledge_id: "airport_facts", index_mode: "lightrag", function_tag: "facts", prompt: "Airport facts knowledge base." },
  ],
  lightrag: { postgres: { host: "pg.internal", port: 5432, database: "rag", user: "rag" } },
  mcp_servers: [
    { url: "https://sopilot.internal/mcp", authorization: "Bearer ***" },
    { url: "https://booking.example/mcp" },
  ],
  visual_hints: [{ function_tag: "map", url: "https://cdn.example/terminal-map.png", prompt: "Show the terminal map." }],
};
