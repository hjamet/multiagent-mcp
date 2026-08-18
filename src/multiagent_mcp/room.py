"""Multi-Agent Room manager and turn coordination."""

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Optional, Union

from multiagent_mcp.models import Message, Participant, TurnResult, normalize_handle

def get_config_dir() -> Path:
    """Get the configuration directory from environment or default."""
    env_dir = os.environ.get("MULTIAGENT_CONFIG_DIR")
    if env_dir:
        return Path(env_dir)
    return Path.home() / ".config" / "multiagent-mcp"


def get_default_state_file() -> Path:
    """Get the default fallback state file path."""
    env_file = os.environ.get("MULTIAGENT_STATE_FILE")
    if env_file:
        return Path(env_file)
    return get_config_dir() / "default_room.state.json"


def get_active_pointer_file() -> Path:
    """Get the active room pointer file path."""
    return get_config_dir() / "active_room.json"


class RoomManager:
    """Manages multi-agent room participants, turn queue, and markdown transcript."""

    def __init__(
        self,
        filepath: Optional[Union[str, Path]] = None,
        state_file: Optional[Union[str, Path]] = None,
    ) -> None:
        self.filepath: Optional[Path] = Path(filepath) if filepath else None
        self._state_file: Optional[Path] = Path(state_file) if state_file else None
        self.topic: str = ""
        self.participants: dict[str, Participant] = {}
        self.events: dict[str, asyncio.Event] = {}
        self.messages: list[Message] = []
        self.priority_scores: dict[str, int] = {}
        self.mention_seq: dict[str, int] = {}
        self.last_posted_message: Optional[Message] = None
        self.last_message_by_participant: dict[str, Message] = {}
        self.active_turn: Optional[str] = None
        self.first_speaker: Optional[str] = None
        self.seq_counter: int = 0
        self._lock: asyncio.Lock = asyncio.Lock()
        self._io_lock: asyncio.Lock = asyncio.Lock()

        if self.filepath and not self._state_file:
            self._state_file = Path(f"{self.filepath}.state.json")
        elif not self.filepath and self._state_file:
            sf_str = str(self._state_file)
            if sf_str.endswith(".state.json") and not sf_str.endswith("default_room.state.json"):
                self.filepath = Path(sf_str[:-11])

    def _get_state_file(self) -> Path:
        """Get the active state file path."""
        if self._state_file is not None:
            return self._state_file
        if self.filepath is not None:
            self._state_file = Path(f"{self.filepath}.state.json")
            return self._state_file
        pointer_file = get_active_pointer_file()
        if pointer_file.exists():
            try:
                data = json.loads(pointer_file.read_text(encoding="utf-8"))
                p_str = data.get("state_file")
                if p_str:
                    self._state_file = Path(p_str)
                    if self.filepath is None:
                        sf_str = str(self._state_file)
                        if sf_str.endswith(".state.json") and not sf_str.endswith("default_room.state.json"):
                            self.filepath = Path(sf_str[:-11])
                    return self._state_file
            except Exception:
                pass
        self._state_file = get_default_state_file()
        return self._state_file

    def _set_active_pointer(self) -> None:
        """Record active state file in global pointer file."""
        try:
            cfg = get_config_dir()
            cfg.mkdir(parents=True, exist_ok=True)
            target = self._get_state_file()
            pointer = get_active_pointer_file()
            pointer.write_text(
                json.dumps({"state_file": str(target.resolve())}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _save_state(self) -> None:
        """Atomically persist current room state to JSON file."""
        state_file = self._get_state_file()
        if self.filepath is None and self._state_file:
            sf_str = str(self._state_file)
            if sf_str.endswith(".state.json") and not sf_str.endswith("default_room.state.json"):
                self.filepath = Path(sf_str[:-11])
        try:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            filepath_str = str(self.filepath.resolve()) if self.filepath else None
            state_data = {
                "filepath": filepath_str,
                "topic": self.topic,
                "first_speaker": self.first_speaker,
                "seq_counter": self.seq_counter,
                "active_turn": self.active_turn,
                "priority_scores": self.priority_scores,
                "mention_seq": self.mention_seq,
                "participants": {
                    handle: p.model_dump(mode="json")
                    for handle, p in self.participants.items()
                },
                "messages": [
                    m.model_dump(mode="json")
                    for m in self.messages
                ],
                "last_posted_message": (
                    self.last_posted_message.model_dump(mode="json")
                    if self.last_posted_message
                    else None
                ),
            }
            json_text = json.dumps(state_data, indent=2, ensure_ascii=False)
            tmp_file = state_file.with_name(f"{state_file.name}.tmp.{os.getpid()}_{time.time_ns()}")
            tmp_file.write_text(json_text, encoding="utf-8")

            for attempt in range(10):
                try:
                    os.replace(tmp_file, state_file)
                    break
                except (PermissionError, OSError):
                    if attempt == 9:
                        try:
                            state_file.write_text(json_text, encoding="utf-8")
                            if tmp_file.exists():
                                tmp_file.unlink(missing_ok=True)
                        except Exception:
                            pass
                    else:
                        time.sleep(0.01)
        except Exception as e:
            print(f"[RoomManager] Error saving state to {state_file}: {e}", file=sys.stderr)

    def _load_state(self) -> bool:
        """Load and synchronize room state from JSON file if available."""
        state_file = self._get_state_file()
        if not state_file.exists():
            return False

        data = None
        for attempt in range(5):
            try:
                content = state_file.read_text(encoding="utf-8")
                if not content.strip():
                    time.sleep(0.02)
                    continue
                data = json.loads(content)
                break
            except (json.JSONDecodeError, OSError):
                time.sleep(0.02)
                continue

        if not data or not isinstance(data, dict):
            if state_file.exists() and state_file.stat().st_size > 0:
                raise RuntimeError(f"CRITICAL: Failed to load state file from {self._state_file}")
            return False

        if data.get("filepath"):
            self.filepath = Path(data["filepath"])
        elif self.filepath is None and self._state_file:
            sf_str = str(self._state_file)
            if sf_str.endswith(".state.json") and not sf_str.endswith("default_room.state.json"):
                self.filepath = Path(sf_str[:-11])

        self.topic = data.get("topic", "")
        self.first_speaker = data.get("first_speaker")
        self.seq_counter = data.get("seq_counter", 0)
        self.active_turn = data.get("active_turn")
        self.priority_scores = data.get("priority_scores", {})
        self.mention_seq = data.get("mention_seq", {})

        loaded_participants = {}
        for handle, p_dict in data.get("participants", {}).items():
            try:
                loaded_participants[handle] = Participant.model_validate(p_dict)
            except Exception:
                pass
        self.participants = loaded_participants

        loaded_messages = []
        for m_dict in data.get("messages", []):
            try:
                loaded_messages.append(Message.model_validate(m_dict))
            except Exception:
                pass
        self.messages = loaded_messages

        last_msg = data.get("last_posted_message")
        if last_msg:
            try:
                self.last_posted_message = Message.model_validate(last_msg)
            except Exception:
                self.last_posted_message = None
        else:
            self.last_posted_message = None

        self.last_message_by_participant = {}
        for m in self.messages:
            if m.sender != "@System":
                self.last_message_by_participant[m.sender] = m

        return True

    def _get_event(self, handle: str) -> asyncio.Event:
        """Get or recreate asyncio.Event bound to the current running event loop."""
        canonical = normalize_handle(handle)
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if canonical not in self.events:
            self.events[canonical] = asyncio.Event()
        else:
            evt = self.events[canonical]
            if evt._loop is not None and current_loop is not None and evt._loop is not current_loop:
                is_set = evt.is_set()
                new_evt = asyncio.Event()
                if is_set:
                    new_evt.set()
                self.events[canonical] = new_evt
        return self.events[canonical]

    @property
    def turn_queue(self) -> list[str]:
        """List of active participants with priority > 0 waiting behind active_turn."""
        self._load_state()
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
        stripped = re.sub(r"```[\s\S]*?```", "", content)
        stripped = re.sub(r"`[^`]*`", "", stripped)
        return stripped

    def _extract_mentions(self, content: str) -> list[str]:
        """Extract all @mentions from message content outside code blocks."""
        clean_content = self._strip_code_blocks(content)
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

        if msg.is_private:
            recipients_str = ", ".join(msg.recipients)
            quoted = "\n> > ".join(first_lines)
            if len(lines) > 4:
                quoted += "\n> > *(...)*"
            return (
                f"> [!NOTE]\n"
                f"> 🔒 **Dernier message (Privé) :** **{msg.sender}** ➔ {recipients_str} ({time_str})\n"
                f"> \n"
                f"> > {quoted}\n\n"
            )
        else:
            recipients_str = ", ".join(msg.recipients)
            quoted = "\n> ".join(first_lines)
            if len(lines) > 4:
                quoted += "\n> *(...)*"
            return (
                f"> [!NOTE]\n"
                f"> 💬 **Dernier message :** **{msg.sender}** ➔ {recipients_str} ({time_str})\n"
                f"> \n"
                f"> {quoted}\n\n"
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
        if self.filepath is None and self._state_file:
            sf_str = str(self._state_file)
            if sf_str.endswith(".state.json") and not sf_str.endswith("default_room.state.json"):
                self.filepath = Path(sf_str[:-11])

        if not self.filepath:
            return

        for attempt in range(10):
            try:
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
                break
            except (PermissionError, OSError):
                if attempt == 9:
                    print(f"[RoomManager] Error updating file header ({self.filepath})", file=sys.stderr)
                else:
                    time.sleep(0.01)

    def _append_to_file(self, formatted_entry: str) -> None:
        """Append text to the markdown transcript file."""
        if self.filepath is None and self._state_file:
            sf_str = str(self._state_file)
            if sf_str.endswith(".state.json") and not sf_str.endswith("default_room.state.json"):
                self.filepath = Path(sf_str[:-11])

        if not self.filepath:
            return

        for attempt in range(10):
            try:
                self.filepath.parent.mkdir(parents=True, exist_ok=True)
                with open(self.filepath, "a", encoding="utf-8") as f:
                    f.write(formatted_entry + "\n\n")
                break
            except (PermissionError, OSError):
                if attempt == 9:
                    print(f"[RoomManager] Error appending to transcript ({self.filepath})", file=sys.stderr)
                else:
                    time.sleep(0.01)

    def init_room(
        self,
        filepath: Optional[str] = None,
        participants: Optional[list[str]] = None,
        topic: str = "",
        first_speaker: Optional[str] = None,
        force: bool = False,
    ) -> None:
        """Initialize or reset the room, clear memory, and create markdown and state files."""
        target_path = Path(filepath) if filepath else None
        target_state = Path(f"{target_path}.state.json") if target_path else get_default_state_file()

        # Overwrite protection: check if existing file or state file has active messages
        if not force:
            has_existing_messages = False
            if target_state.exists():
                try:
                    sdata = json.loads(target_state.read_text(encoding="utf-8"))
                    if sdata.get("messages") and len(sdata["messages"]) > 0:
                        has_existing_messages = True
                except Exception:
                    pass
            if not has_existing_messages and target_path and target_path.exists():
                try:
                    t_content = target_path.read_text(encoding="utf-8")
                    if "## Fil de discussion\n" in t_content:
                        _, body = t_content.split("## Fil de discussion\n", 1)
                        if body.strip():
                            has_existing_messages = True
                except Exception:
                    pass

            if has_existing_messages:
                raise FileExistsError(
                    f"Le fichier '{filepath}' existe déjà et contient une session avec des messages. "
                    "Utilisez --force pour écraser."
                )

        self.filepath = target_path
        self._state_file = target_state
        self._set_active_pointer()

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

        norm_participants = [normalize_handle(p) for p in participants] if participants else []
        if first_speaker:
            self.first_speaker = normalize_handle(first_speaker)
        elif norm_participants:
            self.first_speaker = norm_participants[0]
        else:
            self.first_speaker = None

        if norm_participants:
            for canonical in norm_participants:
                self.participants[canonical] = Participant(
                    name=canonical.lstrip("@"),
                    handle=canonical,
                    status="not_joined",
                    last_read_seq_id=0,
                )
                self.events[canonical] = asyncio.Event()
                self.priority_scores[canonical] = 0
                self.mention_seq[canonical] = 0

        # Create fresh transcript file
        if self.filepath and self.filepath.exists():
            try:
                self.filepath.unlink()
            except Exception:
                pass

        # Remove old state file if exists
        if self._state_file and self._state_file.exists():
            try:
                self._state_file.unlink()
            except Exception:
                pass

        self._update_file_header()
        self._save_state()

    async def join_room(self, handle: str, name: Optional[str] = None) -> Participant:
        """Register a participant in the room, broadcast arrival notice, and lift all_joined barrier."""
        self._load_state()
        canonical = normalize_handle(handle)
        disp_name = name if name else canonical.lstrip("@")

        was_active = False
        if canonical in self.participants:
            participant = self.participants[canonical]
            was_active = (participant.status == "active")
            if name:
                participant.name = disp_name
            participant.status = "active"
            self._get_event(canonical)
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
            self._get_event(canonical)
            self.priority_scores[canonical] = 0
            self.mention_seq[canonical] = 0
            if self.first_speaker is None:
                self.first_speaker = canonical

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

            # Wake up all currently waiting active participants with this arrival notice
            for h in other_active:
                self._get_event(h).set()

        all_joined = len(self.participants) > 0 and all(p.status == "active" for p in self.participants.values())
        if all_joined and self.active_turn is None:
            self.active_turn = self.first_speaker
            barrier_notice = f"Tous les participants ont rejoint la conversation. La parole est à {self.first_speaker}."
            self.seq_counter += 1
            all_handles = list(self.participants.keys())
            msg = Message(
                seq_id=self.seq_counter,
                sender="@System",
                recipients=all_handles,
                content=barrier_notice,
                is_private=False,
                timestamp=datetime.now(timezone.utc),
            )
            self.messages.append(msg)

            # Append barrier notice to markdown file
            self._append_to_file(f"> 🔔 **Système :** {barrier_notice}")

            # Wake up all participants
            for h in all_handles:
                self._get_event(h).set()

        # Update file header and save state
        self._update_file_header()
        self._save_state()

        return participant

    async def post_message(
        self,
        sender: str,
        content: str,
        private: Optional[Union[list[str], bool]] = False,
        is_private: Optional[Union[list[str], bool]] = None,
    ) -> Message:
        """Post a message to the room with mention validation, turn queueing, and transcript logging."""
        self._load_state()
        if is_private is not None:
            private = is_private

        canonical_sender = normalize_handle(sender)

        # Ensure sender is registered and active
        if canonical_sender not in self.participants or self.participants[canonical_sender].status != "active":
            await self.join_room(canonical_sender)
            self._load_state()

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
                f"### 🔒 [Privé] {canonical_sender} ➔ {recipients_str} ({time_str})\n\n"
                f"> {content_indented}\n\n"
                f"---"
            )
        else:
            entry = (
                f"### 💬 {canonical_sender} ➔ {recipients_str} ({time_str})\n\n"
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
            if target_handle not in self.mention_seq or self.priority_scores[target_handle] == 1:
                self.mention_seq[target_handle] = self.seq_counter

        # Sender has taken the floor: reset sender priority to 0
        self.priority_scores[canonical_sender] = 0

        # Elect next speaker: candidate with highest priority_score > 0
        candidates = [
            h for h, score in self.priority_scores.items() if score > 0
        ]
        if candidates:
            candidates.sort(
                key=lambda h: (-self.priority_scores.get(h, 0), self.mention_seq.get(h, 0))
            )
            next_speaker = candidates[0]
            self.active_turn = next_speaker
            self._get_event(next_speaker).set()
        else:
            self.active_turn = None

        # Refresh header file on disk and save state
        self._update_file_header()
        self._save_state()

        # Wake up listeners
        if not msg_is_private:
            for h in self.participants.keys():
                self._get_event(h).set()
        else:
            for r in valid_recipients:
                self._get_event(r).set()

        return msg

    async def wait_for_turn(
        self,
        agent_id: str,
    ) -> TurnResult:
        """Wait indefinitely for turn or incoming messages for a participant and return unread messages."""
        canonical = normalize_handle(agent_id)
        self._load_state()
        if canonical not in self.participants:
            raise ValueError(f"Participant {canonical} not registered in room")

        evt = self._get_event(canonical)

        while True:
            self._load_state()
            if canonical not in self.participants:
                raise ValueError(f"Participant {canonical} not registered in room")
            participant = self.participants[canonical]

            unread = [
                m
                for m in self.messages
                if m.seq_id > participant.last_read_seq_id
                and m.sender != canonical
                and (not m.is_private or canonical in m.recipients)
            ]

            if self.active_turn == canonical or len(unread) > 0:
                break

            evt.clear()
            try:
                await asyncio.wait_for(evt.wait(), timeout=0.3)
            except asyncio.TimeoutError:
                pass

        # Final reload of state
        self._load_state()
        if canonical not in self.participants:
            raise ValueError(f"Participant {canonical} not registered in room")
        participant = self.participants[canonical]

        unread = [
            m
            for m in self.messages
            if m.seq_id > participant.last_read_seq_id
            and m.sender != canonical
            and (not m.is_private or canonical in m.recipients)
        ]

        if self.active_turn == canonical:
            status = "your_turn"
        else:
            status = "message_received"

        # Update last_read_seq_id strictly in wait_for_turn and save
        participant.last_read_seq_id = self.seq_counter
        self._save_state()

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

    def list_participants(self) -> dict:
        """List active participants, current turn, turn queue, and total messages."""
        self._load_state()
        return {
            "participants": [
                {
                    "handle": p.handle,
                    "name": p.name,
                    "status": p.status,
                    "joined_at": p.joined_at.isoformat(),
                    "last_read_seq_id": p.last_read_seq_id,
                }
                for p in self.participants.values()
            ],
            "active_participants": [
                p.handle for p in self.participants.values() if p.status == "active"
            ],
            "active_turn": self.active_turn,
            "first_speaker": self.first_speaker,
            "turn_queue": list(self.turn_queue),
            "message_count": len(self.messages),
            "topic": self.topic,
            "filepath": str(self.filepath) if self.filepath else None,
        }


