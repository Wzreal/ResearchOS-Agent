"""Trusted-code Python adapter with bounded subprocess cleanup.

The AST policy defines supported code and catches accidental misuse. It is not
an adversarial security boundary; callers must only execute trusted code.
"""

from __future__ import annotations

import ast
import asyncio
import os
import sys
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from researchos.domain.runtime import IdempotencyMode, UsageCertainty
from researchos.domain.tools import (
    AdapterMode,
    PythonExecutionRequest,
    PythonExecutionResult,
    ToolDescriptor,
    ToolError,
    ToolInvocationRequest,
    ToolInvocationResult,
    ToolInvocationStatus,
    ToolSideEffect,
    ToolUsage,
)
from researchos.interfaces.runtime import CancellationSignal
from researchos.interfaces.tools import ArtifactStore


@dataclass(frozen=True, slots=True)
class PythonSubprocessPolicy:
    allowed_import_roots: frozenset[str] = frozenset(
        {
            "collections",
            "csv",
            "datetime",
            "decimal",
            "fractions",
            "functools",
            "itertools",
            "json",
            "math",
            "random",
            "re",
            "statistics",
        }
    )
    max_output_bytes: int = 64_000
    max_artifact_count: int = 16
    max_artifact_bytes: int = 5_000_000

    def __post_init__(self) -> None:
        if self.max_output_bytes < 0:
            raise ValueError("max_output_bytes must not be negative")
        if self.max_artifact_count < 0:
            raise ValueError("max_artifact_count must not be negative")
        if self.max_artifact_bytes < 0:
            raise ValueError("max_artifact_bytes must not be negative")


class PythonSubprocessTool:
    def __init__(
        self,
        *,
        artifact_store: ArtifactStore,
        policy: PythonSubprocessPolicy | None = None,
        tool_id: str = "python",
        adapter_id: str = "local_subprocess",
    ) -> None:
        self._artifacts = artifact_store
        self._policy = policy or PythonSubprocessPolicy()
        self._descriptor = ToolDescriptor(
            tool_id=tool_id,
            capability_id="python",
            adapter_id=adapter_id,
            mode=AdapterMode.LOCAL,
            operation_version="python-subprocess-v1",
            input_type="python",
            output_type="python_result",
            side_effect=ToolSideEffect.LOCAL_ARTIFACT,
            idempotency=IdempotencyMode.NON_IDEMPOTENT,
        )

    @property
    def descriptor(self) -> ToolDescriptor:
        return self._descriptor

    async def invoke(
        self, request: ToolInvocationRequest, cancellation: CancellationSignal
    ) -> ToolInvocationResult:
        if cancellation.cancelled:
            return _terminal(
                ToolInvocationStatus.CANCELLED,
                "tool_cancelled",
                "Python execution was cancelled before dispatch",
                certainty=UsageCertainty.EXACT,
            )
        if not isinstance(request.input, PythonExecutionRequest):
            return _terminal(
                ToolInvocationStatus.FAILED,
                "invalid_tool_input",
                "Python tool input is invalid",
                certainty=UsageCertainty.EXACT,
            )
        try:
            _validate_supported_code(
                request.input.source, self._policy.allowed_import_roots
            )
        except ValueError:
            return _terminal(
                ToolInvocationStatus.FAILED,
                "unsupported_python_code",
                "Python source violates the supported-code policy",
                certainty=UsageCertainty.EXACT,
            )

        with tempfile.TemporaryDirectory(prefix="researchos-python-") as value:
            work = Path(value)
            try:
                self._prepare(work, request.input)
            except OSError:
                return _terminal(
                    ToolInvocationStatus.FAILED,
                    "python_workspace_failed",
                    "Python workspace could not be prepared",
                    certainty=UsageCertainty.EXACT,
                )
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-I",
                    "program.py",
                    cwd=work,
                    env=_minimal_environment(),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError:
                return _terminal(
                    ToolInvocationStatus.FAILED,
                    "python_subprocess_unavailable",
                    "Python subprocess could not be started",
                    certainty=UsageCertainty.EXACT,
                )
            communicate = asyncio.create_task(process.communicate())
            cancelled = asyncio.create_task(cancellation.wait())
            try:
                done, _ = await asyncio.wait(
                    {communicate, cancelled},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancelled in done and communicate not in done:
                    await _terminate(process, communicate)
                    return _terminal(
                        ToolInvocationStatus.CANCELLED,
                        "tool_cancelled",
                        "Python execution was cancelled",
                        certainty=UsageCertainty.UNKNOWN,
                    )
                cancelled.cancel()
                with suppress(asyncio.CancelledError):
                    await cancelled
                stdout, stderr = communicate.result()
            except asyncio.CancelledError:
                cancelled.cancel()
                await _terminate(process, communicate)
                raise

            stdout_value, stdout_cut = _bounded_text(
                stdout, self._policy.max_output_bytes
            )
            stderr_value, stderr_cut = _bounded_text(
                stderr, self._policy.max_output_bytes
            )
            output = PythonExecutionResult(
                exit_code=process.returncode,
                stdout=stdout_value,
                stderr=stderr_value,
                stdout_truncated=stdout_cut,
                stderr_truncated=stderr_cut,
            )
            if process.returncode != 0:
                return ToolInvocationResult(
                    status=ToolInvocationStatus.FAILED,
                    error=ToolError(
                        code="python_process_failed",
                        message="Python subprocess returned a non-zero exit code",
                    ),
                    usage=ToolUsage(tool_calls=1),
                    usage_certainty=UsageCertainty.EXACT,
                )
            try:
                artifacts = self._publish_artifacts(
                    work, request, request.input.artifact_paths
                )
            except (OSError, ValueError):
                return ToolInvocationResult(
                    status=ToolInvocationStatus.FAILED,
                    error=ToolError(
                        code="python_artifact_failed",
                        message="Python output artifact could not be published",
                    ),
                    usage=ToolUsage(tool_calls=1),
                    usage_certainty=UsageCertainty.EXACT,
                )
            return ToolInvocationResult(
                status=ToolInvocationStatus.SUCCEEDED,
                output=output,
                artifacts=artifacts,
                usage=ToolUsage(tool_calls=1),
                usage_certainty=UsageCertainty.EXACT,
            )

    @staticmethod
    def _prepare(work: Path, payload: PythonExecutionRequest) -> None:
        (work / "program.py").write_text(payload.source, encoding="utf-8")
        for item in payload.input_files:
            if item.relative_path == "program.py":
                raise OSError("reserved Python source path")
            target = work / item.relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item.content, encoding="utf-8")

    def _publish_artifacts(
        self,
        work: Path,
        request: ToolInvocationRequest,
        paths: tuple[str, ...],
    ) -> tuple:
        if len(paths) > self._policy.max_artifact_count:
            raise ValueError("declared artifact count exceeds policy")
        published = []
        for relative_path in paths:
            target = (work / relative_path).resolve()
            if not target.is_relative_to(work) or not target.is_file():
                raise ValueError("declared artifact is not a regular file")
            content = target.read_bytes()
            if len(content) > self._policy.max_artifact_bytes:
                raise ValueError("declared artifact exceeds policy")
            stored_path = f"tools/{request.tool_operation_key}/{relative_path}"
            published.append(
                self._artifacts.store_bytes(
                    run_id=request.run_id,
                    relative_path=stored_path,
                    content=content,
                    media_type="application/octet-stream",
                    producer_tool_id=self._descriptor.tool_id,
                    tool_operation_key=request.tool_operation_key,
                )
            )
        return tuple(published)


def _validate_supported_code(source: str, allowed: frozenset[str]) -> None:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError("invalid Python syntax") from exc
    for node in ast.walk(tree):
        names: tuple[str, ...] = ()
        if isinstance(node, ast.Import):
            names = tuple(item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module is None:
                raise ValueError("relative imports are unsupported")
            names = (node.module,)
        for name in names:
            if name.split(".", 1)[0] not in allowed:
                raise ValueError("import is outside supported-code policy")


async def _terminate(
    process: asyncio.subprocess.Process,
    communicate: asyncio.Task[tuple[bytes, bytes]],
) -> None:
    if process.returncode is None:
        process.terminate()
    try:
        await asyncio.wait_for(communicate, timeout=1.0)
    except TimeoutError:
        if process.returncode is None:
            process.kill()
        await communicate


def _bounded_text(value: bytes, limit: int) -> tuple[str, bool]:
    truncated = len(value) > limit
    return value[:limit].decode("utf-8", errors="replace"), truncated


def _minimal_environment() -> dict[str, str]:
    environment = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    for name in ("SYSTEMROOT", "WINDIR"):
        if name in os.environ:
            environment[name] = os.environ[name]
    return environment


def _terminal(
    status: ToolInvocationStatus,
    code: str,
    message: str,
    *,
    certainty: UsageCertainty,
) -> ToolInvocationResult:
    usage = ToolUsage(tool_calls=0) if certainty is not UsageCertainty.UNKNOWN else None
    return ToolInvocationResult(
        status=status,
        error=ToolError(code=code, message=message),
        usage=usage,
        usage_certainty=certainty,
    )
