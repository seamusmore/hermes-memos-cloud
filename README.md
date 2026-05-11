# hermes-memos-cloud

MemOS Cloud memory provider plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent). Provides persistent cross-session memory via the MemOS Cloud API.

## Features

- **Auto-write**: Every conversation turn is automatically submitted to MemOS Cloud for summarization, vectorization, and storage
- **Auto-recall**: Relevant memories are retrieved before each turn and injected into system context
- **Manual search**: `memos_search` tool for on-demand memory search
- **Cross-platform sharing**: Same user's memories shared across platforms (Feishu, WeChat, etc.) via `user_id_map`
- **Optional agent isolation**: Configure `agent_id` to isolate memories per agent/profile

## Installation

### Recommended (via Hermes CLI)

```bash
hermes plugins install https://github.com/seamusmore/hermes-memos-cloud.git
hermes plugins enable memos-cloud
```

Then restart the gateway for the plugin to take effect.

### Manual (alternative)

```bash
# Clone into Hermes user plugins directory
mkdir -p ~/.hermes/plugins/
git clone https://github.com/seamusmore/hermes-memos-cloud.git \
  ~/.hermes/plugins/memos-cloud
```

Then configure and restart (see below).

## Configuration

### 1. API Key

Add to `~/.hermes/.env`:

```bash
MEMOS_API_KEY=your_api_key
MEMOS_BASE_URL=https://memos.memtensor.cn/api/openmem/v1
```

Get your API key: https://memos-dashboard.openmem.net/apikeys

### 2. User ID Mapping (cross-platform)

Create `~/.hermes/memos-cloud.json`:

```json
{
  "user_id_map": {
    "FEISHU_USER_ID": "unified_user_id",
    "WECHAT_USER_ID@im.wechat": "unified_user_id"
  }
}
```

- `user_id_map` maps platform-specific user IDs to a unified identifier
- This enables the same user to share memories across Feishu, WeChat, and other platforms
- Without mapping, each platform uses its raw user ID separately

### 3. Enable in config.yaml

```yaml
memory:
  memory_enabled: true
  provider: memos-cloud
```

- `provider: memos-cloud` tells Hermes to use the MemOS Cloud plugin for memory
- If omitted, Hermes may use the default built-in memory provider
- Restart the gateway after changing this setting

### 4. Optional Agent ID

If you want to isolate memories for a specific agent/profile, add `agent_id`:

```json
{
  "user_id_map": {
    "FEISHU_USER_ID": "unified_user_id"
  },
  "agent_id": "lyra"
}
```

- `agent_id` is optional. When set, it is sent to the MemOS API for memory isolation
- Different `agent_id` values cannot see each other's memories for the same user
- If omitted, no agent isolation is applied (memories are shared for the same user)
- Each profile should have its own `memos-cloud.json`, so `agent_id` is per-profile by design

## Tools

| Tool | Description |
|------|-------------|
| `memos_search` | Search relevant memories, supports `query` and `limit` parameters |
| `memos_forget` | Delete memories (requires web console) |

## Management

- Memory dashboard: https://memos-dashboard.openmem.net

## Troubleshooting

### Memories not shared across platforms

1. Verify `user_id_map` mapping is correct
2. Check MemOS Dashboard to confirm memories are stored

### Memories leaking between agents

1. Check if `agent_id` is configured in each profile's `memos-cloud.json`
2. Ensure different agents use different `agent_id` values

### View logs

```bash
grep "MemOS" ~/.hermes/logs/agent.log | tail -20
```

## License

MIT
