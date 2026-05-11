"""MemOS Cloud Memory Provider for Hermes Agent.

Integrates with MemOS Cloud (https://memos-dashboard.openmem.net) for
persistent cross-session memory: automatic recall before each turn,
automatic storage after each turn, and manual search/forget tools.
"""

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

import requests
import yaml

logger = logging.getLogger(__name__)

MEMOS_BASE_URL = "https://memos.memtensor.cn/api/openmem/v1"


# Class-level lock for cross-instance prefetch persistence
# Fixes GitHub issue #9973: prefetch cache lost on every gateway turn
# Uses file-based storage to survive multi-process Gateway
# Stores only the LATEST prefetch result (overwritten each turn)
_prefetch_cache_lock = threading.Lock()


class MemOSMemoryProvider:
    """Hermes MemoryProvider that talks to MemOS Cloud."""

    def __init__(self):
        # Instance-level cache for deduplicating memos_search tool calls within the same turn
        # This is separate from the class-level _prefetch_cache for cross-instance persistence
        self._last_prefetch_query: str = ""
        self._last_prefetch_result_cached: str = ""

    # ── Required properties / methods ──

    @property
    def name(self) -> str:
        return "memos-cloud"

    def is_available(self) -> bool:
        """Check config only — no network calls."""
        return bool(os.environ.get("MEMOS_API_KEY"))

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._api_key: str = os.environ.get("MEMOS_API_KEY", "")
        self._base_url: str = os.environ.get(
            "MEMOS_BASE_URL", MEMOS_BASE_URL
        ).rstrip("/")
        self._session_id = session_id
        self._hermes_home: str = kwargs.get("hermes_home", os.path.expanduser("~/.hermes"))
        self._platform: str = kwargs.get("platform", "cli")
        self._agent_context: str = kwargs.get("agent_context", "primary")
        self._agent_identity: str = kwargs.get("agent_identity", "default")
        self._user_id: str = kwargs.get("user_id", "")

        # Load identity_name from profile config.yaml if available
        # This allows readable names like "Lyra" instead of "default" in MemOS
        self._display_name = self._load_identity_name()

        # Load user_id mapping from config or environment
        # Format: {"ou_xxx": "user001", "o9cq80xV_xxx@im.wechat": "user001"}
        # Environment variable: MEMORY_USER_ID_MAP='{"ou_xxx":"user001","o9cq80xV_xxx":"user001"}'
        self._user_id_map = self._load_user_id_map()

        # Load optional agent_id from config
        # agent_id is a plain string; no mapping needed since each profile
        # reads its own memos-cloud.json.
        self._config = self._load_config()
        self._agent_id = self._config.get("agent_id")

        # MemOS identity model:
        # - user_id identifies the human user (mapped via user_id_map)
        # - agent_id is optional; only sent if explicitly configured
        identity = self._display_name or self._agent_identity
        if self._user_id:
            # Apply user_id mapping if configured (for cross-platform memory sharing)
            mapped = self._user_id_map.get(self._user_id, self._user_id)
            # If mapping failed (hashed user_id), try to recover the original from sessions.json
            if mapped == self._user_id and not self._user_id.startswith("ou_") and not self._user_id.startswith("o9cq"):
                recovered = self._recover_user_id_from_sessions()
                if recovered:
                    recovered_mapped = self._user_id_map.get(recovered, recovered)
                    if recovered_mapped != recovered:
                        logger.info(
                            "MemOS recovered original user_id=%s from sessions.json (mapped to %s), "
                            "replacing hashed user_id=%s",
                            recovered, recovered_mapped, self._user_id,
                        )
                        mapped = recovered_mapped
                        self._user_id = recovered
            self._memos_user_id = mapped
        else:
            # No user_id provided — skip initialization for non-user contexts
            # (e.g., background tasks like session compression, title generation)
            logger.debug("MemOS: skipping initialization — no user_id provided")
            self._memos_user_id = ""
            return

        # Resolve agent_id: only use if explicitly configured
        self._memos_agent_id = self._agent_id
        if self._memos_agent_id:
            logger.info("MemOS agent_id=%s", self._memos_agent_id)
        else:
            logger.info("MemOS agent_id=disabled")

        # Background thread for sync_turn (non-blocking contract)
        self._sync_thread: Optional[threading.Thread] = None
        self._prefetch_thread: Optional[threading.Thread] = None
        self._last_prefetch_result: str = ""
        self._prefetch_lock = threading.Lock()

        # Per-profile prefetch cache file (isolated per HERMES_HOME)
        self._prefetch_cache_file = os.path.join(
            self._hermes_home, "memos-cloud-prefetch.json"
        )

        logger.info(
            "MemOS Cloud initialized: user=%s platform=%s base_url=%s",
            self._memos_user_id, self._platform, self._base_url,
        )

    # ── Tool schemas ──

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "memos_search",
                    "description": (
                        "Search MemOS Cloud for relevant memories. "
                        "Use when you need to recall past conversations, user preferences, "
                        "or any information from previous sessions."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "The search query to find relevant memories.",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Max number of memories to return (default 5, max 10).",
                                "default": 5,
                            },
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "memos_forget",
                    "description": (
                        "Delete memories from MemOS Cloud. "
                        "Use when the user asks to forget something or clear memories."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Search query to find memories to delete.",
                            },
                        },
                        "required": ["query"],
                    },
                },
            },
        ]

    def handle_tool_call(
        self, tool_name: str, args: Dict[str, Any], **kwargs: Any
    ) -> str:
        if tool_name == "memos_search":
            query = args.get("query", "")
            # Check cache for deduplication (same query as prefetch)
            logger.info(
                "memos_search: query='%s', cached_query='%s', cache_hit=%s",
                query, self._last_prefetch_query, query == self._last_prefetch_query and bool(self._last_prefetch_result_cached)
            )
            if query == self._last_prefetch_query and self._last_prefetch_result_cached:
                return f"(缓存) {self._last_prefetch_result_cached}"
            return self._do_search(query, args.get("limit", 5))
        elif tool_name == "memos_forget":
            return self._do_forget(args.get("query", ""))
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    # ── Config schema for `hermes memory setup` ──

    def get_config_schema(self):
        return [
            {
                "key": "api_key",
                "description": "MemOS Cloud API Key",
                "secret": True,
                "required": True,
                "env_var": "MEMOS_API_KEY",
                "url": "https://memos-dashboard.openmem.net/apikeys",
            },
            {
                "key": "base_url",
                "description": "MemOS Cloud API Base URL (usually leave default)",
                "default": MEMOS_BASE_URL,
                "env_var": "MEMOS_BASE_URL",
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        # Non-secret config saved to JSON; secrets go to .env automatically
        pass

    # ── Optional hooks ──

    def system_prompt_block(self) -> str:
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return recalled context for the current turn.

        Uses the result from the background prefetch started in the
        PREVIOUS turn's queue_prefetch(). File-based cache survives
        AIAgent recreation and multi-process Gateway (issue #9973).
        """
        # Skip when user_id is empty (background tasks without user context)
        if not self._memos_user_id:
            return ""

        # Check file-based cross-instance cache first
        cache_entry = None
        with _prefetch_cache_lock:
            try:
                if os.path.exists(self._prefetch_cache_file):
                    with open(self._prefetch_cache_file, 'r') as f:
                        cache_entry = json.load(f)
                    # Consume-once: delete the file after reading
                    os.remove(self._prefetch_cache_file)
            except Exception:
                pass  # Cache read failure is non-fatal, just do sync search
        
        if cache_entry:
            # Update instance-level cache for tool deduplication
            self._last_prefetch_query = cache_entry["query"]
            self._last_prefetch_result_cached = cache_entry["result"]
            logger.info(
                "prefetch (cross-instance cache): query='%s', result_len=%d",
                cache_entry["query"], len(cache_entry["result"])
            )
            return cache_entry["result"]

        # Fallback: synchronous search (first turn or cache miss)
        result = self._do_search(query, limit=5)
        # Update instance-level cache for tool deduplication
        self._last_prefetch_query = query
        self._last_prefetch_result_cached = result
        logger.info(
            "prefetch (sync): query='%s', result_len=%d",
            query, len(result)
        )
        return result

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Start background search for the NEXT turn.

        Stores result in file-based cache to survive AIAgent recreation
        and multi-process Gateway (issue #9973).
        """
        # Skip when user_id is empty (background tasks without user context)
        if not self._memos_user_id:
            return

        # Cancel previous prefetch if still running
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            return  # let it finish, we'll overwrite result

        def _prefetch():
            try:
                logger.info(
                    "queue_prefetch (background): query='%s'",
                    query
                )
                memories = self._search_memos(query, limit=5)
                formatted = self._format_memories(memories)
                # Store in file-based cross-instance cache (overwrite previous)
                cache_data = {
                    "query": query,
                    "result": formatted,
                    "timestamp": time.time()
                }
                with _prefetch_cache_lock:
                    with open(self._prefetch_cache_file, 'w') as f:
                        json.dump(cache_data, f, ensure_ascii=False)
                # Also update instance-level for same-instance consumption
                with self._prefetch_lock:
                    self._last_prefetch_result = formatted
            except Exception as e:
                logger.warning("MemOS prefetch failed: %s", e)

        self._prefetch_thread = threading.Thread(target=_prefetch, daemon=True)
        self._prefetch_thread.start()

    def sync_turn(
        self, user_content: str, assistant_content: str, *, session_id: str = ""
    ) -> None:
        """Persist completed turn to MemOS Cloud. Non-blocking."""
        # Skip for non-primary contexts (subagents, cron flushes, etc.)
        if self._agent_context != "primary":
            return

        # Skip when user_id is empty (background tasks without user context)
        if not self._memos_user_id:
            return

        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=0.5)

        def _sync():
            try:
                self._add_message(user_content, assistant_content)
            except Exception as e:
                logger.warning("MemOS sync_turn failed: %s", e)

        self._sync_thread = threading.Thread(target=_sync, daemon=True)
        self._sync_thread.start()

    def on_memory_write(
        self, action: str, target: str, content: str, metadata: Any = None
    ) -> None:
        """Mirror built-in memory writes to MemOS Cloud."""
        if self._agent_context != "primary":
            return

        # Skip when user_id is empty (background tasks without user context)
        if not self._memos_user_id:
            return

        def _mirror():
            try:
                self._add_message(
                    f"[memory {action}] {target}: {content}",
                    "(system memory update)",
                )
            except Exception as e:
                logger.warning("MemOS on_memory_write failed: %s", e)

        threading.Thread(target=_mirror, daemon=True).start()

    def shutdown(self) -> None:
        """Flush pending work."""
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=2)
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=1)

    # ── Internal: MemOS Cloud API calls ──

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Token {self._api_key}",
        }

    def _add_message(self, user_content: str, assistant_content: str) -> Dict:
        """Submit conversation to MemOS for automatic processing & storage."""
        url = f"{self._base_url}/add/message"
        conversation_id = f"{self._agent_identity}-{self._platform}-{int(time.time())}"
        payload = {
            "user_id": self._memos_user_id,
            "conversation_id": conversation_id,
            "messages": [
                {"role": "user", "content": user_content},
                {"role": "assistant", "content": assistant_content},
            ],
        }
        # Only include agent_id if explicitly configured
        if self._memos_agent_id is not None:
            payload["agent_id"] = self._memos_agent_id
        resp = requests.post(url, headers=self._headers(), json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _search_memos(self, query: str, limit: int = 5) -> Dict:
        """Search MemOS for relevant memories."""
        url = f"{self._base_url}/search/memory"
        payload = {
            "query": query,
            "user_id": self._memos_user_id,
            "conversation_id": f"search-{int(time.time())}",
        }
        # Only include agent_id filter if explicitly configured
        if self._memos_agent_id is not None:
            payload["filter"] = {
                "and": [
                    {"agent_id": self._memos_agent_id},
                ],
            }
        # Log every API call for debugging
        logger.info(
            "_search_memos: query='%s', conversation_id='%s', agent_id=%s",
            query, payload["conversation_id"], self._memos_agent_id
        )
        # MemOS doesn't have a limit param; we'll trim results after
        resp = requests.post(url, headers=self._headers(), json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json()["data"]

    def _format_memories(self, data: Dict) -> str:
        """Format MemOS search results into readable text for context injection."""
        if not data:
            return ""

        lines = ["# 检索到的长期记忆", ""]

        # Facts / memory_detail_list
        memories = data.get("memory_detail_list", [])
        if memories:
            lines.append("## 事实记忆")
            for m in memories:
                key = m.get("memory_key", "")
                value = m.get("memory_value", "")
                tags = m.get("tags", [])
                cid = m.get("conversation_id", "")
                tag_str = f" [{', '.join(tags)}]" if tags else ""
                lines.append(f"- **{key}**: {value}{tag_str} (来源: {cid})")
            lines.append("")

        # Preferences / preference_detail_list
        prefs = data.get("preference_detail_list", [])
        if prefs:
            lines.append("## 用户偏好")
            for p in prefs:
                pref_type = p.get("preference_type", "")
                pref = p.get("preference", "")
                reasoning = p.get("reasoning", "")
                cid = p.get("conversation_id", "")
                type_label = "[显式偏好]" if pref_type == "explicit_preference" else "[隐式偏好]"
                lines.append(f"- {type_label} {pref}")
                if reasoning:
                    lines.append(f"  - 推理: {reasoning}")
            lines.append("")

        if not memories and not prefs:
            return ""

        return "\n".join(lines)

    def _do_search(self, query: str, limit: int = 5) -> str:
        """Execute search and return formatted result."""
        limit = min(max(1, limit), 10)
        try:
            data = self._search_memos(query, limit)
            formatted = self._format_memories(data)
            if formatted:
                return formatted
            return "未找到相关记忆。"
        except Exception as e:
            logger.error("MemOS search error: %s", e)
            return f"记忆搜索失败: {e}"

    def _do_forget(self, query: str) -> str:
        """Delete memories matching query. MemOS Cloud doesn't have a delete API yet."""
        # MemOS Cloud currently doesn't expose a delete endpoint in the public API.
        # We search first to show what would be deleted, then inform the user.
        try:
            data = self._search_memos(query, limit=10)
            memories = data.get("memory_detail_list", [])
            prefs = data.get("preference_detail_list", [])
            total = len(memories) + len(prefs)
            if total == 0:
                return f"未找到与 '{query}' 相关的记忆。"
            return (
                f"找到 {total} 条相关记忆。"
                "MemOS Cloud 暂不支持通过 API 删除记忆，"
                "请前往 https://memos-dashboard.openmem.net 手动管理。"
            )
        except Exception as e:
            return f"记忆搜索失败: {e}"

    # ── Config persistence ──

    def _load_identity_name(self) -> Optional[str]:
        """Load identity_name from profile config.yaml.
        
        Returns the readable display name (e.g., 'Lyra', 'Alice', 'Bob')
        if configured, otherwise None to fall back to profile name.
        """
        config_path = os.path.join(self._hermes_home, "config.yaml")
        try:
            if os.path.exists(config_path):
                with open(config_path) as f:
                    config = yaml.safe_load(f)
                    identity = config.get("memory", {}).get("identity_name")
                    if identity:
                        logger.info("Loaded identity_name: %s", identity)
                        return identity
        except Exception as e:
            logger.warning("Failed to load identity_name: %s", e)
        return None

    def _load_config(self) -> Dict:
        config_path = os.path.join(self._hermes_home, "memos-cloud.json")
        try:
            if os.path.exists(config_path):
                with open(config_path) as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _load_user_id_map(self) -> Dict[str, str]:
        """Load user_id mapping from config file or environment variable.
        
        This allows mapping platform-specific user IDs to a unified user ID
        for cross-platform memory sharing.
        
        Config file (~/.hermes/memos-cloud.json):
        {
            "user_id_map": {
                "ou_xxx": "user001",
                "o9cq80xV_xxx@im.wechat": "user001"
            }
        }
        
        Environment variable (takes precedence):
        MEMORY_USER_ID_MAP='{"ou_xxx":"user001","o9cq80xV_xxx":"user001"}'
        """
        # First check environment variable (takes precedence)
        env_map = os.environ.get("MEMORY_USER_ID_MAP", "")
        if env_map:
            try:
                loaded = json.loads(env_map)
                if isinstance(loaded, dict):
                    logger.info("Loaded user_id_map from environment: %d mappings", len(loaded))
                    return loaded
            except Exception as e:
                logger.warning("Failed to parse MEMORY_USER_ID_MAP: %s", e)
        
        # Then check config file
        config = self._load_config()
        file_map = config.get("user_id_map", {})
        if file_map and isinstance(file_map, dict):
            logger.info("Loaded user_id_map from config: %d mappings", len(file_map))
            return file_map
        
        return {}

    def _recover_user_id_from_sessions(self) -> Optional[str]:
        """Recover the original user_id from sessions.json when kwargs provides a hashed value.

        Reads sessions.json, finds the entry matching our session_id,
        and returns the original user_id (open_id for Feishu).

        Returns the original user_id if found, None otherwise.
        """
        sessions_path = os.path.join(self._hermes_home, "sessions", "sessions.json")
        try:
            if not os.path.exists(sessions_path):
                return None
            with open(sessions_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for entry in data.values():
                origin = entry.get("origin", {})
                if origin.get("user_id") and entry.get("session_id") == self._session_id:
                    return origin["user_id"]
        except Exception as e:
            logger.warning("Failed to recover user_id from sessions.json: %s", e)
        return None


def register(ctx) -> None:
    """Entry point for Hermes plugin discovery."""
    ctx.register_memory_provider(MemOSMemoryProvider())
