"""Errors and standard exit codes shared by CLI, guardrails, craft, and daemon handlers (L0).

Exit-code contract:
  - ``0`` (``EXIT_OK``): success
  - ``1`` (``EXIT_REQUEST_ERROR``): invalid arguments, guardrail block, patch trap, or build state error
  - ``5`` (``EXIT_DAEMON_ERROR``): daemon, engine, or upstream fault
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .game_profile import GAME_ID

EXIT_OK: int = 0
EXIT_REQUEST_ERROR: int = 1
EXIT_DAEMON_ERROR: int = 5
DAEMON_FAILURE_EXIT_CODE: int = EXIT_DAEMON_ERROR


class DaemonRequestError(RuntimeError):
    """Raised when the PoB2 daemon rejects a request due to invalid arguments or build state."""

    exit_code: int = EXIT_REQUEST_ERROR


class InternalBugError(DaemonRequestError):
    """Raised when an internal Python programming error (AttributeError/TypeError/KeyError) occurs."""

    error_kind: str = "internal_error"
    exit_code: int = EXIT_REQUEST_ERROR


class BootstrapMissingError(RuntimeError):
    """Raised when no local PoB2 engine exists and bootstrap failed or is offline."""

    exit_code: int = EXIT_REQUEST_ERROR

    def __init__(self, message: str, update_status: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.update_status = update_status or {}


class CrossGameHallucinationError(DaemonRequestError):
    """Raised when a PoE1-only input/mechanic is sent to ``pob2`` (or vice versa).

    Contract:
      - Always exits CLI with code ``1`` (never ``5`` daemon crash).
      - Carries ``errorKind = "cross_game_hallucination"`` and ``"game": "poe2"`` in JSON output.
    """

    error_kind: str = "cross_game_hallucination"

    def __init__(
        self,
        *,
        game: str = GAME_ID,
        detected_game: str = "poe1",
        trigger: str,
        error: str,
        remediation: str,
        equivalent_command: str | None = None,
        rule_id: str | None = None,
    ) -> None:
        self.game = game
        self.detected_game = detected_game
        self.trigger = trigger
        self.error_message = error
        self.remediation = remediation
        self.equivalent_command = equivalent_command
        self.rule_id = rule_id
        super().__init__(self.format_human())

    def _cli_name(self) -> str:
        return "pob2" if self.game == "poe2" else "pob"

    def format_human(self) -> str:
        prefix = f"[{self.game.upper()}-CROSS-GAME-BLOCK]"
        parts = [
            f"{prefix} Detected {self.detected_game.upper()} input in `{self._cli_name()}` "
            f"(trigger: {self.trigger!r}): {self.error_message}",
            f"Remediation: {self.remediation}",
        ]
        if self.equivalent_command:
            parts.append(f"Equivalent command: `{self.equivalent_command}`")
        return "\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "game": self.game,
            "ok": False,
            "errorKind": self.error_kind,
            "detectedGame": self.detected_game,
            "trigger": self.trigger,
            "error": self.error_message,
            "remediation": self.remediation,
            "equivalentCommand": self.equivalent_command,
        }
        if self.rule_id:
            payload["ruleId"] = self.rule_id
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, default_game: str = GAME_ID) -> CrossGameHallucinationError:
        game = str(data.get("game") or default_game)
        detected = str(data.get("detectedGame") or ("poe1" if game == "poe2" else "poe2"))
        return cls(
            game=game,
            detected_game=detected,
            trigger=str(data.get("trigger") or ""),
            error=str(data.get("error") or "Cross-game input blocked."),
            remediation=str(data.get("remediation") or ""),
            equivalent_command=data.get("equivalentCommand"),
            rule_id=data.get("ruleId"),
        )


class LegacyPatchTrapError(DaemonRequestError):
    """Raised when a removed PoE2 legacy mechanic (e.g. 0.4 Homogenising Omen, 0.5 Recombinator) is requested."""

    error_kind: str = "legacy_patch_trap"

    def __init__(
        self,
        *,
        trigger: str,
        patch_version: str,
        error: str,
        remediation: str,
        equivalent_command: str | None = None,
        rule_id: str | None = None,
        game: str = GAME_ID,
    ) -> None:
        self.game = game
        self.trigger = trigger
        self.patch_version = patch_version
        self.error_message = error
        self.remediation = remediation
        self.equivalent_command = equivalent_command
        self.rule_id = rule_id
        super().__init__(self.format_human())

    def format_human(self) -> str:
        prefix = f"[{self.game.upper()}-PATCH-BLOCK]"
        parts = [
            f"{prefix} Removed in PoE2 {self.patch_version} (trigger: {self.trigger!r}): {self.error_message}",
            f"Remediation: {self.remediation}",
        ]
        if self.equivalent_command:
            parts.append(f"Suggested command: `{self.equivalent_command}`")
        return "\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "game": self.game,
            "ok": False,
            "errorKind": self.error_kind,
            "patchVersion": self.patch_version,
            "trigger": self.trigger,
            "error": self.error_message,
            "remediation": self.remediation,
            "equivalentCommand": self.equivalent_command,
        }
        if self.rule_id:
            payload["ruleId"] = self.rule_id
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, default_game: str = GAME_ID) -> LegacyPatchTrapError:
        return cls(
            game=str(data.get("game") or default_game),
            trigger=str(data.get("trigger") or ""),
            patch_version=str(data.get("patchVersion") or "0.5.5"),
            error=str(data.get("error") or "Removed legacy PoE2 mechanic."),
            remediation=str(data.get("remediation") or ""),
            equivalent_command=data.get("equivalentCommand"),
            rule_id=data.get("ruleId"),
        )


@dataclass(frozen=True)
class HallucinationWarning:
    """Non-blocking false-friend, legacy-patch, or CN mistranslation warning attached to valid queries."""

    rule_id: str
    game: str
    category: str
    trigger: str
    summary: str
    poe1_behavior: str
    poe2_behavior: str
    canonical_term: str | None = None
    patch_version: str | None = None
    remediation: str | None = None
    source_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "game": self.game,
            "ruleId": self.rule_id,
            "category": self.category,
            "trigger": self.trigger,
            "warning": self.summary,
            "poe1Behavior": self.poe1_behavior,
            "poe2Behavior": self.poe2_behavior,
        }
        if self.canonical_term:
            out["canonicalTerm"] = self.canonical_term
        if self.patch_version:
            out["patchVersion"] = self.patch_version
        if self.remediation:
            out["remediation"] = self.remediation
        if self.source_ref:
            out["sourceRef"] = self.source_ref
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, default_game: str = GAME_ID) -> HallucinationWarning:
        return cls(
            rule_id=str(data.get("ruleId") or ""),
            game=str(data.get("game") or default_game),
            category=str(data.get("category") or "false_friend"),
            trigger=str(data.get("trigger") or ""),
            summary=str(data.get("warning") or ""),
            poe1_behavior=str(data.get("poe1Behavior") or ""),
            poe2_behavior=str(data.get("poe2Behavior") or ""),
            canonical_term=data.get("canonicalTerm"),
            patch_version=data.get("patchVersion"),
            remediation=data.get("remediation"),
            source_ref=data.get("sourceRef"),
        )

    def format_stderr(self) -> str:
        if self.category == "legacy_patch":
            tag = f"[{self.game.upper()}-PATCH-WARN]"
        elif self.game == "poe2":
            tag = "[POE2-TRAP-WARN]"
        else:
            tag = "[POE1-TRAP-WARN]"
        canon = f" (canonical: {self.canonical_term})" if self.canonical_term else ""
        return f"{tag} `{self.trigger}`{canon}: {self.summary}"

    def format_markdown(self) -> str:
        return f"> {self.format_stderr()}"
