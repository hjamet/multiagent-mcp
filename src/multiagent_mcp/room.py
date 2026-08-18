"""Multi-Agent Room manager and turn coordination."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Optional, Union

from multiagent_mcp.models import Message, Participant, TurnResult, normalize_handle


class RoomManager:
    """Manages multi-agent room participants, turn queue, and markdown transcript."""

    def __init__(self) -> None:
        self.filepath: Optional[Path] = None
        self.topic: str = ""
        self.participants: dict[str, Participant] = {}
        self.events: dict[str, asyncio.Event] = {}
        self.messages: list[Message] = []
        self.priority_scores: dict[str, int] = {}
        self.mention_seq: dict[str, int] = {}
        self.last_posted_message: Optional[Message] = None
        self.last_message_by_participant: dict[str, Message] = {}
        self.active_turn: Optional[str] = None
        self.seq_counter: int = 0
        self._lock: asyncio.Lock = asyncio.Lock()

    @property
    def turn_queue(self) -> list[str]:
        """List of active participants with priority > 0 waiting behind active_turn."""
        candidates = [
            h
            for h, score in self.priority_scores.items()
            if score > 0 and self.participants.get(h) and self.participants[h].status == "active"
        ]
        candidates.sort(
            key=lambda h: (-self.priority_scores.get(h, 0), self.mention_seq.get(h, 0))
        )
        if self.active_turn and self.active_turn in candidates:
            return [h for h in candidates if h != self.active_turn]
        return candidates

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

    def _format_last_message_callout(self) -> str:
        """Format a callout with the last message posted."""
        if not self.last_posted_message:
            return ""
        msg = self.last_posted_message
        time_str = msg.timestamp.strftime("%H:%M:%S UTC")
        clean_content = msg.content.strip().replace("\r\n", "\n")
        lines = clean_content.split("\n")
        first_lines = lines[:4]
        quoted = "\n> ".join(first_lines)
        if len(lines) > 4:
            quoted += "\n> *(...)*"

        if msg.is_private:
            recipients_str = ", ".join(msg.recipients)
            return (
                f"> [!WARNING]\n"
                f"> 🔒 **Dernier message (Privé) :** **{msg.sender}** ➔ {recipients_str} à {time_str}\n"
                f"> \n"
                f"> {quoted}\n"
            )
        else:
            return (
                f"> [!NOTE]\n"
                f"> 💬 **Dernier message :** **{msg.sender}** à {time_str}\n"
                f"> \n"
                f"> {quoted}\n"
            )

    def _format_participants_table(self) -> str:
        """Format the unified live participant & priority queue table sorted by urgency."""
        def sort_key(p_handle: str):
            p = self.participants.get(p_handle)
            status = p.status if p else "active"
            score = self.priority_scores.get(p_handle, 0)
            seq = self.mention_seq.get(p_handle, 999999)
            if status == "active" and score > 0:
                return (0, -score, seq, p_handle)
            elif status == "active":
                return (1, 0, seq, p_handle)
            else:
                return (2, 0, 0, p_handle)

        sorted_handles = sorted(self.participants.keys(), key=sort_key)

        lines = [
            "## 📊 File d'Attente & État des Participants (Temps Réel)",
            "| Participant | Statut |",
            "|---|---|",
        ]

        for h in sorted_handles:
            p = self.participants.get(h)
            status = p.status if p else "active"
            if status == "not_joined":
                status_str = "🔌 not joined yet"
            else:
                score = self.priority_scores.get(h, 0)
                if score > 0:
                    status_str = f"⏳ {score} mention{'s' if score > 1 else ''}"
                else:
                    status_str = "💤 sleeping"

            lines.append(f"| **{h}** | {status_str} |")

        return "\n".join(lines)

    def _update_file_header(self) -> None:
        """Write or refresh the markdown transcript file header and live priority table."""
        if not self.filepath:
            return

        callout = self._format_last_message_callout()
        table = self._format_participants_table()

        header = [
            "# Multi-Agent Room",
            "",
            f"- **Fichier :** `{self.filepath.as_posix()}`",
            f"- **Sujet :** {self.topic if self.topic else 'Discussion multi-agents'}",
            f"- **Initialisé le :** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ]
        if callout:
            header.append(callout)
        header.extend([
            table,
            "",
            "---",
            "",
            "## Fil de discussion",
            "",
        ])
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
        self.priority_scores.clear()
        self.mention_seq.clear()
        self.last_posted_message = None
        self.last_message_by_participant.clear()
        self.active_turn = None
        self.seq_counter = 0

        if participants:
            for handle in participants:
                canonical = normalize_handle(handle)
                self.participants[canonical] = Participant(
                    name=canonical.lstrip("@"),
                    handle=canonical,
                    status="not_joined",
                    last_read_seq_id=0,
                )
                self.events[canonical] = asyncio.Event()
                self.priority_scores[canonical] = 0
                self.mention_seq[canonical] = 0

        # Create fresh file
        if self.filepath and self.filepath.exists():
            self.filepath.unlink()
        self._update_file_header()

    async def join_room(self, handle: str, name: Optional[str] = None) -> Participant:
        """Register a participant in the room and broadcast arrival notice if >= 2 active participants."""
        canonical = normalize_handle(handle)
        disp_name = name if name else canonical.lstrip("@")

        was_active = False
        if canonical in self.participants:
            participant = self.participants[canonical]
            was_active = (participant.status == "active")
            if name:
                participant.name = disp_name
            participant.status = "active"
            if canonical not in self.events:
                self.events[canonical] = asyncio.Event()
            if canonical not in self.priority_scores:
                self.priority_scores[canonical] = 0
            if canonical not in self.mention_seq:
                self.mention_seq[canonical] = 0
        else:
            participant = Participant(
                name=disp_name,
                handle=canonical,
                status="active",
                last_read_seq_id=0,
            )
            self.participants[canonical] = participant
            self.events[canonical] = asyncio.Event()
            self.priority_scores[canonical] = 0
            self.mention_seq[canonical] = 0

        active_count = sum(1 for p in self.participants.values() if p.status == "active")

        # If participant became active and there are at least 2 active participants: broadcast arrival notice
        if not was_active and active_count >= 2:
            arrival_notice = f"{canonical} est arrivé dans la conversation"
            self.seq_counter += 1
            other_active = [
                h for h, p in self.participants.items() if p.status == "active" and h != canonical
            ]
            msg = Message(
                seq_id=self.seq_counter,
                sender="@System",
                recipients=other_active,
                content=arrival_notice,
                is_private=True,
                timestamp=datetime.now(timezone.utc),
            )
            self.messages.append(msg)

            # Append notice to markdown file
            self._append_to_file(f"> 🔔 **Système :** {arrival_notice}")

            # Wake up all currently waiting active participants with this arrival notice so they can greet each other
            for h in other_active:
                if h in self.events:
                    self.events[h].set()

        # Update file header with new participant / status
        self._update_file_header()

        return participant

    async def post_message(
        self,
        sender: str,
        content: str,
        private: Optional[Union[list[str], bool]] = False,
        is_private: Optional[Union[list[str], bool]] = None,
    ) -> Message:
        """Post a message to the room with mention validation, turn queueing, and transcript logging."""
        if is_private is not None:
            private = is_private

        canonical_sender = normalize_handle(sender)

        # Ensure sender is registered and active
        if canonical_sender not in self.participants or self.participants[canonical_sender].status != "active":
            await self.join_room(canonical_sender)

        # Extract mentions outside code blocks
        raw_mentions = self._extract_mentions(content)
        raw_mentions_lower = [rm.lower() for rm in raw_mentions]
        text_has_all = "@all" in raw_mentions_lower or "all" in raw_mentions_lower

        handle_map = {h.lower(): h for h in self.participants.keys()}
        msg_is_private = False
        valid_recipients: list[str] = []

        if isinstance(private, list) and len(private) > 0:
            msg_is_private = True
            # Reject @all in private list or text
            if any(normalize_handle(p).lower() in ("@all", "all") for p in private) or text_has_all:
                raise ValueError(
                    "Impossible de mentionner @all dans un message privé. "
                    "Seules les personnes explicitement mentionnées verront ce message "
                    "(et toutes les personnes mentionnées le verront)."
                )
            for p in private:
                p_canonical = normalize_handle(p)
                p_lower = p_canonical.lower()
                if p_lower in handle_map:
                    target_handle = handle_map[p_lower]
                    if target_handle != canonical_sender and target_handle not in valid_recipients:
                        valid_recipients.append(target_handle)

        elif private is True:
            msg_is_private = True
            if text_has_all:
                raise ValueError(
                    "Impossible de mentionner @all dans un message privé. "
                    "Seules les personnes explicitement mentionnées verront ce message "
                    "(et toutes les personnes mentionnées le verront)."
                )
            for rm in raw_mentions:
                rm_lower = rm.lower()
                if rm_lower in handle_map:
                    target_handle = handle_map[rm_lower]
                    if target_handle != canonical_sender and target_handle not in valid_recipients:
                        valid_recipients.append(target_handle)

        else:
            # Public message
            msg_is_private = False
            if text_has_all:
                for h in self.participants.keys():
                    if h != canonical_sender and h not in valid_recipients:
                        valid_recipients.append(h)
            else:
                for rm in raw_mentions:
                    rm_lower = rm.lower()
                    if rm_lower in handle_map:
                        target_handle = handle_map[rm_lower]
                        if target_handle != canonical_sender and target_handle not in valid_recipients:
                            valid_recipients.append(target_handle)

        # Rejection if 0 valid mentions / recipients
        if not valid_recipients:
            available_handles = sorted(
                [h for h in self.participants.keys() if h != canonical_sender]
            )
            if not available_handles:
                available_handles = sorted(list(self.participants.keys()))
            error_str = (
                f"Écrivez à au moins l'une des personnes suivantes : {', '.join(available_handles)}, ou @all"
            )
            raise ValueError(error_str)

        # Sequence increment
        self.seq_counter += 1
        msg = Message(
            seq_id=self.seq_counter,
            sender=canonical_sender,
            recipients=valid_recipients,
            content=content,
            is_private=msg_is_private,
            timestamp=datetime.now(timezone.utc),
        )
        self.messages.append(msg)

        # Format markdown transcript
        time_str = msg.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
        recipients_str = ", ".join(valid_recipients)
        if msg_is_private:
            clean_content = content.replace("\r\n", "\n")
            content_indented = "\n> ".join(clean_content.split("\n"))
            entry = (
                f"> [!WARNING] 🔒 Message Privé : {canonical_sender} ➔ {recipients_str} ({time_str})\n"
                f"> \n"
                f"> {content_indented}\n\n"
                f"---"
            )
        else:
            entry = (
                f"### {canonical_sender} ➔ {recipients_str} ({time_str})\n\n"
                f"{content}\n\n"
                f"---"
            )
        self._append_to_file(entry)

        # Update last message tracking
        self.last_posted_message = msg
        self.last_message_by_participant[canonical_sender] = msg

        # Enqueue deduplicated mentioned recipients (+1 score per recipient)
        for target_handle in valid_recipients:
            self.priority_scores[target_handle] = (
                self.priority_scores.get(target_handle, 0) + 1
            )
            # Only set mention_seq on first enqueue or keep earlier sequence for FIFO if priority was 0
            if target_handle not in self.mention_seq or self.priority_scores[target_handle] == 1:
                self.mention_seq[target_handle] = self.seq_counter

        # Sender has taken the floor: reset sender priority to 0
        self.priority_scores[canonical_sender] = 0

        # Elect next speaker: candidate with highest priority_score > 0
        candidates = [
            h for h, score in self.priority_scores.items() if score > 0
        ]
        if candidates:
            # Sort by -priority_score (descending), then mention_seq (FIFO)
            candidates.sort(
                key=lambda h: (-self.priority_scores.get(h, 0), self.mention_seq.get(h, 0))
            )
            next_speaker = candidates[0]
            self.active_turn = next_speaker
            if next_speaker in self.events:
                self.events[next_speaker].set()
        else:
            self.active_turn = None

        # Refresh header file on disk
        self._update_file_header()

        # If public message, wake up all active participants so waiting listeners get unread messages
        if not msg_is_private:
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
                # Do not return sender's own messages
                if m.sender == canonical:
                    continue
                if m.is_private:
                    if canonical in m.recipients:
                        unread.append(m)
                else:
                    unread.append(m)

        # Update last_read_seq_id strictly in wait_for_turn
        participant.last_read_seq_id = self.seq_counter

        # Determine status
        if self.active_turn == canonical:
            status = "your_turn"
        elif unread:
            status = "message_received"
        else:
            status = "timeout"

        notice = (
            f"Transcript: '{self.filepath}'. Interdiction formelle de consulter ce fichier sur disque."
            if self.filepath
            else None
        )
        active_list = [p.handle for p in self.participants.values() if p.status == "active"]
        return TurnResult(
            status=status,
            active_turn=self.active_turn,
            new_messages=unread,
            current_queue=list(self.turn_queue),
            active_participants=active_list,
            system_notice=notice,
        )
