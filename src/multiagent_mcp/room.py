"""Multi-Agent Room manager and turn coordination."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Optional

from multiagent_mcp.models import Message, Participant, TurnResult, normalize_handle


class RoomManager:
    """Manages multi-agent room participants, turn queue, and markdown transcript."""

    def __init__(self) -> None:
        self.filepath: Optional[Path] = None
        self.topic: str = ""
        self.participants: dict[str, Participant] = {}
        self.events: dict[str, asyncio.Event] = {}
        self.messages: list[Message] = []
        self.turn_queue: list[str] = []
        self.active_turn: Optional[str] = None
        self.seq_counter: int = 0
        self._lock: asyncio.Lock = asyncio.Lock()

    def _strip_code_blocks(self, content: str) -> str:
        """Strip fenced and inline markdown code blocks to avoid false @mentions."""
        # Strip fenced code blocks ```...```
        stripped = re.sub(r"```[\s\S]*?```", "", content)
        # Strip inline code `...`
        stripped = re.sub(r"`[^`]*`", "", stripped)
        return stripped

    def _extract_mentions(self, content: str) -> list[str]:
        """Extract all @mentions from message content outside code blocks."""
        clean_content = self._strip_code_blocks(content)
        # Match @word or @handle (letters, digits, underscores)
        matches = re.findall(r"@([a-zA-Z0-9_-]+)", clean_content)
        return [normalize_handle(m) for m in matches]

    def _format_participants_table(self) -> str:
        """Format the participants table for markdown file."""
        lines = [
            "## Participants",
            "| Handle | Nom | Statut | Rejoint le |",
            "|---|---|---|---|",
        ]
        for p in self.participants.values():
            joined_str = p.joined_at.strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"| {p.handle} | {p.name} | {p.status} | {joined_str} |")
        return "\n".join(lines)

    def _update_file_header(self) -> None:
        """Write or refresh the markdown transcript file header and participant table."""
        if not self.filepath:
            return

        header = [
            "# Multi-Agent Room",
            "",
            f"- **Fichier :** `{self.filepath.as_posix()}`",
            f"- **Sujet :** {self.topic if self.topic else 'Discussion multi-agents'}",
            f"- **Initialisé le :** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            self._format_participants_table(),
            "",
            "---",
            "",
            "## Fil de discussion",
            "",
        ]
        header_text = "\n".join(header)

        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        if self.filepath.exists():
            current_text = self.filepath.read_text(encoding="utf-8")
            marker = "## Fil de discussion\n"
            if marker in current_text:
                _, body = current_text.split(marker, 1)
                body = body.lstrip("\n")
                if body:
                    self.filepath.write_text(header_text + "\n" + body, encoding="utf-8")
                    return
        self.filepath.write_text(header_text, encoding="utf-8")

    def _append_to_file(self, formatted_entry: str) -> None:
        """Append text to the markdown transcript file."""
        if not self.filepath:
            return
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(self.filepath, "a", encoding="utf-8") as f:
            f.write(formatted_entry + "\n\n")

    def init_room(
        self,
        filepath: str,
        participants: Optional[list[str]] = None,
        topic: str = "",
    ) -> None:
        """Initialize or reset the room, clear memory, and create markdown file."""
        self.filepath = Path(filepath) if filepath else None
        self.topic = topic
        self.participants.clear()
        self.events.clear()
        self.messages.clear()
        self.turn_queue.clear()
        self.active_turn = None
        self.seq_counter = 0

        if participants:
            for handle in participants:
                canonical = normalize_handle(handle)
                self.participants[canonical] = Participant(
                    name=canonical.lstrip("@"),
                    handle=canonical,
                    status="active",
                    last_read_seq_id=0,
                )
                self.events[canonical] = asyncio.Event()

        # Create fresh file
        if self.filepath and self.filepath.exists():
            self.filepath.unlink()
        self._update_file_header()

    async def join_room(self, handle: str, name: Optional[str] = None) -> Participant:
        """Register a participant in the room and broadcast arrival notice if >= 2 participants."""
        canonical = normalize_handle(handle)
        disp_name = name if name else canonical.lstrip("@")

        if canonical in self.participants:
            participant = self.participants[canonical]
            participant.status = "active"
            if canonical not in self.events:
                self.events[canonical] = asyncio.Event()
            return participant

        participant = Participant(
            name=disp_name,
            handle=canonical,
            status="active",
            last_read_seq_id=self.seq_counter,
        )
        self.participants[canonical] = participant
        self.events[canonical] = asyncio.Event()

        # Update file header with new participant
        self._update_file_header()

        # If this is participant >= 2: broadcast arrival notice
        if len(self.participants) >= 2:
            arrival_notice = f"{canonical} est arrivé dans la conversation"
            self.seq_counter += 1
            all_handles = list(self.participants.keys())
            msg = Message(
                seq_id=self.seq_counter,
                sender="@System",
                recipients=all_handles,
                content=arrival_notice,
                is_private=False,
                timestamp=datetime.now(timezone.utc),
            )
            self.messages.append(msg)

            # Newly joined participant has acknowledged their own arrival
            participant.last_read_seq_id = self.seq_counter

            # Append notice to markdown file
            self._append_to_file(f"> 🔔 **Système :** {arrival_notice}")

            # Wake up all currently waiting participants with this arrival notice so they can greet each other
            for h, evt in self.events.items():
                if h != canonical:
                    evt.set()

        return participant

    async def post_message(
        self,
        sender: str,
        content: str,
        is_private: bool = False,
    ) -> Message:
        """Post a message to the room with mention validation, turn queueing, and transcript logging."""
        canonical_sender = normalize_handle(sender)

        # Ensure sender is registered
        if canonical_sender not in self.participants:
            await self.join_room(canonical_sender)

        # Extract mentions outside code blocks
        raw_mentions = self._extract_mentions(content)

        # Map lowercase to canonical handle for active participants
        handle_map = {h.lower(): h for h in self.participants.keys()}

        valid_recipients: list[str] = []
        for rm in raw_mentions:
            rm_lower = rm.lower()
            if rm_lower in handle_map:
                target_handle = handle_map[rm_lower]
                if target_handle != canonical_sender and target_handle not in valid_recipients:
                    valid_recipients.append(target_handle)

        # Rejection if 0 valid mentions
        if not valid_recipients:
            available_handles = sorted(
                [h for h in self.participants.keys() if h != canonical_sender]
            )
            if not available_handles:
                available_handles = sorted(list(self.participants.keys()))
            error_str = (
                f"Écrivez à au moins l'une des personnes suivantes : {', '.join(available_handles)}"
            )
            raise ValueError(error_str)

        # Sequence increment
        self.seq_counter += 1
        msg = Message(
            seq_id=self.seq_counter,
            sender=canonical_sender,
            recipients=valid_recipients,
            content=content,
            is_private=is_private,
            timestamp=datetime.now(timezone.utc),
        )
        self.messages.append(msg)

        # Sender has read their own message
        self.participants[canonical_sender].last_read_seq_id = self.seq_counter

        # Format markdown transcript
        time_str = msg.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
        recipients_str = ", ".join(valid_recipients)
        if is_private:
            entry = (
                f"### 🔒 [Message Privé] {canonical_sender} ➔ {recipients_str} ({time_str})\n\n"
                f"{content}\n\n"
                f"---"
            )
        else:
            entry = (
                f"### {canonical_sender} ➔ {recipients_str} ({time_str})\n\n"
                f"{content}\n\n"
                f"---"
            )
        self._append_to_file(entry)

        # Enqueue deduplicated mentioned agents (+1 score/entry per mentioned agent)
        for target_handle in valid_recipients:
            self.turn_queue.append(target_handle)

        # Pop next speaker from turn queue
        if self.turn_queue:
            next_speaker = self.turn_queue.pop(0)
            self.active_turn = next_speaker
            if next_speaker in self.events:
                self.events[next_speaker].set()

        # If public message, wake up all participants so waiting listeners get unread messages
        if not is_private:
            for h, evt in self.events.items():
                evt.set()
        else:
            # Wake up private recipients
            for r in valid_recipients:
                if r in self.events:
                    self.events[r].set()

        return msg

    async def wait_for_turn(
        self,
        agent_id: str,
        timeout_seconds: float = 45.0,
    ) -> TurnResult:
        """Wait for turn or incoming messages for a participant and return unread messages."""
        canonical = normalize_handle(agent_id)
        if canonical not in self.participants:
            raise ValueError(f"Participant {canonical} not registered in room")

        participant = self.participants[canonical]
        if canonical not in self.events:
            self.events[canonical] = asyncio.Event()

        evt = self.events[canonical]

        # If it's already their turn, no wait needed
        if self.active_turn != canonical:
            evt.clear()
            try:
                await asyncio.wait_for(evt.wait(), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                pass
            finally:
                evt.clear()

        # Collect unread messages (seq_id > last_read_seq_id)
        unread: list[Message] = []
        for m in self.messages:
            if m.seq_id > participant.last_read_seq_id:
                if m.is_private:
                    if canonical in m.recipients or m.sender == canonical:
                        unread.append(m)
                else:
                    unread.append(m)

        # Update last_read_seq_id
        if unread:
            participant.last_read_seq_id = max(m.seq_id for m in unread)
        elif self.messages:
            # If there are messages but none visible/unread to this participant, catch up seq
            participant.last_read_seq_id = self.seq_counter

        # Determine status
        if self.active_turn == canonical:
            status = "your_turn"
        elif unread:
            status = "message_received"
        else:
            status = "timeout"

        return TurnResult(
            status=status,
            active_turn=self.active_turn,
            new_messages=unread,
            current_queue=list(self.turn_queue),
            active_participants=list(self.participants.keys()),
            system_notice=None,
        )
