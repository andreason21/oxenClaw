"""Built-in dashboard / desktop-client channel.

A no-op channel plugin: outbound `send` calls are accepted and recorded in
the agent's conversation history (the dashboard polls `chat.history` to
render replies), but nothing is forwarded to an external service.

Lets the dashboard and native desktop client work as the default chat
surface without requiring Slack or any other external channel to be
configured. Required in environments where outbound messaging platforms
are blocked (corporate networks, air-gapped installs).
"""
