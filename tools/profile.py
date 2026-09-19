# SPDX-License-Identifier: GPL-2.0-only
"""Declarative game profiles.

A profile is JSON so a new game can be automated without writing Python: it
names the disk images the game asks for and the screen-to-keystroke rules that
answer its prompts. Paths are resolved relative to the profile's `disk_dir`,
which itself is resolved relative to the profile file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from engine import Action, Rule
from hires import HiresTextReader, TextBox


class ProfileError(ValueError):
    """Raised when a profile file is malformed."""


class Profile:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        try:
            data: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ProfileError(f"{self.path}: {exc}") from exc

        self.name: str = data.get("name", self.path.stem)
        self.description: str = data.get("description", "")
        self.poll_frames: int = int(data.get("poll_frames", 12))
        idle = data.get("idle") or {}
        self.idle_after: int = int(idle.get("after_polls", 0))
        self._idle_spec = idle.get("do", [])

        disk_dir = data.get("disk_dir", ".")
        self.disk_dir = (self.path.parent / disk_dir).resolve()
        self.disks: dict[str, Path] = {
            key: (self.disk_dir / value).resolve()
            for key, value in data.get("disks", {}).items()
        }
        self.boot_disk: str | None = data.get("boot_disk")
        self.rules: list[Rule] = [self._make_rule(r) for r in data.get("rules", [])]

        # Games that draw their own font need the hi-res decoder rather than
        # the hardware text page; a profile says so by giving a "screen" block.
        screen = data.get("screen") or {}
        self.font_base: int | None = (
            int(screen["font_base"], 0) if isinstance(screen.get("font_base"), str)
            else screen.get("font_base")
        )
        self.font_height: int = int(screen.get("font_height", 8))
        # Which memory image the font table is read out of. Only matters for a
        # font above $D000, where the language card gives the two images
        # different tables; `find_font.py` says which one it found the font in.
        self.font_view: str = screen.get("font_view", "main")
        self.text_boxes: dict[str, TextBox] = {
            name: TextBox(**box) for name, box in (screen.get("boxes") or {}).items()
        }

    @property
    def idle_do(self) -> list[Action]:
        return [self._make_action(a) for a in self._idle_spec]

    def make_reader(self, client):
        """Builds the screen reader this profile needs, or None for text mode."""
        if self.font_base is None or not self.text_boxes:
            return None
        return HiresTextReader(client, self.font_base, self.text_boxes,
                               height=self.font_height, view=self.font_view)

    # -- construction ------------------------------------------------------

    def _resolve_disk(self, key: str) -> str:
        if key not in self.disks:
            raise ProfileError(
                f"{self.path}: rule refers to unknown disk '{key}'; "
                f"known disks: {sorted(self.disks)}"
            )
        return str(self.disks[key])

    def _make_action(self, spec: dict[str, Any]) -> Action:
        if "type" in spec:
            return Action.type(str(spec["type"]))
        if "key" in spec:
            value = spec["key"]
            code = ord(value.upper()) if isinstance(value, str) and len(value) == 1 \
                else int(value)
            return Action.key(code)
        if "wait" in spec:
            return Action.wait(int(spec["wait"]))
        if "insert" in spec:
            drive, disk = spec["insert"]
            return Action.insert(int(drive), self._resolve_disk(str(disk)))
        if "stop" in spec:
            return Action.stop(str(spec["stop"]))
        raise ProfileError(f"{self.path}: unrecognised action {spec!r}")

    def _make_rule(self, spec: dict[str, Any]) -> Rule:
        if "when" not in spec:
            raise ProfileError(f"{self.path}: rule is missing 'when': {spec!r}")
        return Rule(
            name=spec.get("name", spec["when"][:32]),
            when=spec["when"],
            do=[self._make_action(a) for a in spec.get("do", [])],
            max_consecutive=int(spec.get("max_consecutive", 8)),
            settle_frames=int(spec.get("settle_frames", 6)),
        )

    # -- reporting ---------------------------------------------------------

    def missing_disks(self) -> dict[str, Path]:
        """Disk images the profile names but that are not on disk yet."""
        return {key: p for key, p in self.disks.items() if not p.is_file()}

    def describe(self) -> str:
        lines = [f"profile: {self.name}"]
        if self.description:
            lines.append(f"  {self.description}")
        lines.append(f"  disk_dir: {self.disk_dir}")
        for key, path in sorted(self.disks.items()):
            mark = "ok " if path.is_file() else "MISSING"
            lines.append(f"  disk {key:<12} [{mark}] {path}")
        lines.append(f"  rules: {len(self.rules)}")
        for rule in self.rules:
            lines.append(f"    - {rule.name}: /{rule.when}/")
        return "\n".join(lines)
