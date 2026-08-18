# multiagent-mcp

Multi-Agent MCP Room for collaborative LLM dialogues, turn coordination, and live markdown transcript tracking.

## Architecture

- **`models.py`**: Pydantic models for participants (`Participant`), messages (`Message`), and turn results (`TurnResult`).
- **`room.py`**: `RoomManager` handling multi-participant room initialization, handle normalization, markdown code block stripping, turn-queueing (+1 score per unique mention), arrival broadcasts, private messaging, and unread sequence tracking.

## Features implemented in Chantier 1

1. **`pyproject.toml`**: Dependency configuration (`mcp[cli]`, `pydantic>=2.7.0`, `starlette>=0.38.0`, `uvicorn>=0.30.0`, `rich>=13.7.0`).
2. **`models.py`**:
   - `Participant`: `id`, `name`, `handle` (canonical e.g. `@Alice`), `status`, `last_read_seq_id`, `joined_at`.
   - `Message`: `id`, `seq_id`, `sender`, `recipients`, `content`, `is_private`, `timestamp`.
   - `TurnResult`: `status`, `active_turn`, `new_messages`, `current_queue`, `active_participants`, `system_notice`.
3. **`room.py`**:
   - `RoomManager.init_room(filepath, participants, topic)`: Clears state, creates markdown room transcript with header and participant table.
   - `RoomManager.join_room(handle, name)`: Registers participant, broadcasts arrival notice when `>= 2` participants, wakes waiting participants.
   - `RoomManager.post_message(sender, content, is_private)`:
     - Strips fenced and inline code blocks before detecting `@mentions`.
     - Validates and resolves mentions against active participants.
     - Rejects with exact error message if 0 valid mentions.
     - Deduplicates mentions (+1 queue score per unique recipient).
     - Appends public/private message to markdown transcript.
     - Pops next speaker from turn queue and sets their `asyncio.Event`.
   - `RoomManager.wait_for_turn(agent_id, timeout_seconds)`:
     - Returns only unread messages (`seq_id > last_read_seq_id`).
     - Supports turn detection, message delivery wakeups, and timeouts.
