# Cloud facade: sensitive BotFather chat

The remote agent facade refuses message/history/search, poll, profile, avatar,
photo and attachment operations targeting Telegram's `@BotFather` account. This
keeps its secret-bearing chat out of ordinary agent-facing output. The local
owner's normal Telegram MCP and its account session remain unchanged.

Full-account search excludes that chat before caching new results. The facade
also filters older cached global search results and fails closed on an unexpected
response shape. Restricted search skips it. Search responses identify this
reduced coverage; upgrading invalidates old search cursors, so start those
queries again without a cursor. The status field
`sensitive_botfather_excluded` reports the boundary separately from the
configured `all_chats` grant.

Text returned by the facade also replaces recognisable plaintext bot-token
patterns, including ordinary Bot API URLs. This is only an extra filter. It
cannot reliably identify secrets encoded or split across messages, images,
files, arbitrary software output or forwarded attachments. Agents with direct
SSH or filesystem access under the service's Unix identity can bypass the MCP
facade; this design is not an OS isolation boundary or a vault. Do not claim
that tokens are protected from such access. Never read BotFather history to
test the guard. Tests use synthetic messages and tokens only.
