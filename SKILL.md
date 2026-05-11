---
name: hermes-memos-cloud
description: MemOS Cloud memory provider plugin for Hermes Agent — persistent cross-session memory
---

# hermes-memos-cloud

MemOS Cloud memory provider plugin for Hermes Agent. Provides persistent cross-session memory via the MemOS Cloud API.

## When to Use

- Installing or configuring the memos-cloud plugin on a new Hermes instance
- Debugging memory sharing/isolation issues
- Setting up cross-platform memory sharing (Feishu, WeChat, etc.)

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

## Configuration

### 1. API Key (required)

Add to `~/.hermes/.env`:
```bash
MEMOS_API_KEY=your_api_key
MEMOS_BASE_URL=https://memos.memtensor.cn/api/openmem/v1
```

Get API key: https://memos-dashboard.openmem.net/apikeys

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
- Enables the same user to share memories across Feishu, WeChat, and other platforms
- Without mapping, each platform uses its raw user ID separately

### 3. Optional Agent ID

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

### 4. Enable in config.yaml

```yaml
memory:
  memory_enabled: true
  provider: memos-cloud
  identity_name: Lyra  # Display name
```

## Tools

| Tool | Description |
|------|-------------|
| `memos_search` | Search memories with `query` and optional `limit` |
| `memos_forget` | Search memories to delete (deletion via web console) |

## Troubleshooting

### Memories not shared across platforms
- Verify `user_id_map` has all platform IDs mapping to the same unified ID
- Check MemOS Dashboard: https://memos-dashboard.openmem.net

### Memories leaking between agents
- Check if `agent_id` is configured in each profile's `memos-cloud.json`
- Ensure different agents use different `agent_id` values

### View logs
```bash
grep "MemOS" ~/.hermes/logs/agent.log | tail -20
```

## Key Files

- Plugin: `~/.hermes/plugins/memos-cloud/__init__.py`
- Config: `~/.hermes/memos-cloud.json`
- API Key: `~/.hermes/.env` (MEMOS_API_KEY)
- Logs: `~/.hermes/logs/agent.log`
