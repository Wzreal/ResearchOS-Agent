"""Bounded read-only Phase 7 evaluate-existing-artifacts coordinator."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

from researchos.application.errors import (
    CorruptEvaluationInput,
    EvaluationCancelled,
    EvaluationContractError,
    EvaluationDeadlineExceeded,
    EvaluationInputChanged,
    EvaluationModelFailure,
    EvaluationPreconditionError,
)
from researchos.application.evaluation_aggregation import aggregate_metrics
from researchos.application.evaluation_artifacts import (
    FrozenRunArtifacts,
    ReadOnlyRunArtifactReader,
)
from researchos.application.evaluation_compatibility import (
    observe_sut,
    validate_case_run,
    validate_sut_homogeneity,
)
from researchos.application.evaluation_evaluators import evaluator_bundle_hash
from researchos.application.evaluation_identity import (
    case_artifact_hash,
    case_semantic_hash,
    evaluation_artifact_hash,
    evaluation_semantic_hash,
)
from researchos.domain.contracts import canonical_json_bytes, model_sha256
from researchos.domain.evaluation import (
    ArtifactAvailability,
    CaseRunBinding,
    EvaluationCaseResult,
    EvaluationCaseStatus,
    EvaluationFailure,
    EvaluationJudgeRequest,
    EvaluationPolicy,
    EvaluationRequest,
    EvaluationRun,
    EvaluationRunStatus,
    MetricDefinitionSnapshot,
    MetricStatus,
    MetricValue,
    MetricValueKind,
    NonComputedMetricValue,
    SUTPinObservation,
    SUTPinStatus,
    SUTPinSupportKind,
    SUTPinSupportRef,
)
from researchos.domain.identity import stable_hash, stable_id
from researchos.interfaces.evaluation import (
    EvaluationArtifactStore,
    EvaluationDatasetLoader,
    EvaluationJudge,
)
from researchos.interfaces.lifecycle import Clock
from researchos.interfaces.runtime import CancellationSignal


class EvaluationHarness:
    def __init__(
        self,
        *,
        datasets: EvaluationDatasetLoader,
        artifacts: EvaluationArtifactStore,
        run_artifacts: ReadOnlyRunArtifactReader,
        clock: Clock,
        evaluators: tuple[object, ...],
        judge: EvaluationJudge | None = None,
        model_definitions: tuple[MetricDefinitionSnapshot, ...] = (),
    ) -> None:
        self._datasets = datasets
        self._artifacts = artifacts
        self._reader = run_artifacts
        self._clock = clock
        self._evaluators = evaluators
        self._judge = judge
        self._model_definitions = model_definitions

    async def evaluate_existing(
        self, request: EvaluationRequest, cancellation: CancellationSignal
    ) -> EvaluationRun:
        started = self._clock.now()
        self._check_interrupt(request, cancellation, started)
        dataset = self._datasets.load(
            request.dataset_id,
            request.dataset_version,
            expected_hash=request.dataset_hash,
        )
        if len(canonical_json_bytes(dataset)) > request.policy.max_dataset_bytes:
            raise EvaluationPreconditionError("dataset byte bound exceeded")
        if len(request.bindings) > request.policy.max_cases:
            raise EvaluationPreconditionError("case count bound exceeded")
        cases = {item.case_id: item for item in dataset.cases}
        if not set(item.case_id for item in request.bindings).issubset(cases):
            raise EvaluationPreconditionError(
                "evaluation binding references unknown case"
            )
        definitions = self._resolve_definitions(request.policy)
        if len(definitions) > request.policy.max_metrics_per_case:
            raise EvaluationPreconditionError("metric count bound exceeded")
        model_bundle_hash = (
            self._judge.model_bundle_hash
            if request.policy.enable_model_evaluators and self._judge is not None
            else None
        )
        bundle_hash = evaluator_bundle_hash(
            definitions, model_bundle_hash=model_bundle_hash
        )
        frozen_by_case: dict[str, FrozenRunArtifacts] = {}
        compatibility = {}
        observations_by_case: list[tuple[SUTPinObservation, ...]] = []
        raw_fingerprints: dict[str, str] = {}
        for binding in request.bindings:
            self._check_interrupt(request, cancellation, started)
            frozen = self._reader.freeze(
                binding.run_id,
                max_input_bytes=request.policy.max_input_bytes_per_case,
            )
            if (
                binding.expected_manifest_hash is not None
                and binding.expected_manifest_hash != frozen.manifest.manifest_hash
            ):
                raise EvaluationPreconditionError("expected run manifest hash differs")
            if (
                not frozen.corrupt
                and
                not request.policy.allow_nonterminal_runs
                and frozen.manifest.completeness.value != "terminal"
            ):
                raise EvaluationPreconditionError("nonterminal evaluation is disabled")
            frozen_by_case[binding.case_id] = frozen
            if frozen.corrupt:
                compatibility[binding.case_id] = None
                observed = ()
            else:
                compatibility[binding.case_id] = validate_case_run(
                    cases[binding.case_id], frozen
                )
                observed = self._apply_declared_absence(
                    observe_sut(frozen), binding, frozen
                )
            if request.declared_sut_observations:
                observed = tuple((*observed, *request.declared_sut_observations))
            observations_by_case.append(observed)
            raw_fingerprints[binding.run_id] = frozen.raw_fingerprint
        system = validate_sut_homogeneity(
            commit_sha=request.commit_sha,
            system_version=request.system_version,
            observations_by_case=tuple(observations_by_case),
            identity_manifest_hash=stable_hash(
                [
                    frozen_by_case[item.case_id].manifest.manifest_hash
                    for item in request.bindings
                ]
            ),
        )
        required_model_calls = (
            sum(not frozen_by_case[item.case_id].corrupt for item in request.bindings)
            if request.policy.enable_model_evaluators
            else 0
        )
        if required_model_calls > request.policy.max_model_calls:
            raise EvaluationPreconditionError("global model call bound exceeded")
        policy_hash = model_sha256(request.policy)
        eval_run_id = stable_id(
            "eval",
            [
                dataset.dataset_content_hash,
                system.system_hash,
                policy_hash,
                bundle_hash,
                [
                    [
                        item.case_id,
                        item.run_id,
                        frozen_by_case[item.case_id].manifest.manifest_hash,
                    ]
                    for item in request.bindings
                ],
            ],
        )
        model_requests: dict[str, EvaluationJudgeRequest] = {}
        if request.policy.enable_model_evaluators:
            for binding in request.bindings:
                frozen = frozen_by_case[binding.case_id]
                if frozen.corrupt:
                    continue
                assert frozen.run_state is not None
                context = {
                    "run_id": frozen.run_state.run_id,
                    "run_revision": frozen.run_state.revision,
                    "manifest_hash": frozen.manifest.manifest_hash,
                    "case_reference_level": cases[
                        binding.case_id
                    ].reference_level.value,
                }
                encoded_context = json.dumps(
                    context,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                if len(encoded_context) > request.policy.max_model_context_bytes:
                    raise EvaluationPreconditionError(
                        "model context byte bound exceeded"
                    )
                context_hash = stable_hash(context)
                model_requests[binding.case_id] = EvaluationJudgeRequest(
                    fixture_key=stable_hash(
                        [
                            eval_run_id,
                            binding.case_id,
                            "model_evaluator",
                            context_hash,
                        ]
                    ),
                    eval_run_id=eval_run_id,
                    case_id=binding.case_id,
                    evaluator_id="model_evaluator",
                    evaluator_version="1",
                    context_hash=context_hash,
                    context=context,
                    expected_metric_ids=tuple(
                        item.metric_id for item in self._model_definitions
                    ),
                )
        existing = self._artifacts.load(eval_run_id)
        if existing is not None:
            return existing

        model_calls = 0
        results: list[EvaluationCaseResult] = []
        for binding, sut_observations in zip(
            request.bindings, observations_by_case, strict=True
        ):
            self._check_interrupt(request, cancellation, started)
            case = cases[binding.case_id]
            frozen = frozen_by_case[binding.case_id]
            case_started = self._clock.now()
            metrics: list[MetricValue] = []
            failures: list[EvaluationFailure] = []
            if frozen.corrupt:
                failures.extend(
                    EvaluationFailure(
                        code=item.corruption_reason_code or "artifact_corrupt",
                        artifact_kind=item.artifact_kind,
                    )
                    for item in frozen.manifest.artifacts
                    if item.availability is ArtifactAvailability.CORRUPT
                )
                metrics.extend(
                    NonComputedMetricValue(
                        metric_id=item.metric_id,
                        definition_hash=item.definition_hash,
                        status=MetricStatus.ERROR,
                        reason_code="artifact_corrupt",
                    )
                    for item in definitions
                )
            for evaluator in self._evaluators:
                if frozen.corrupt:
                    break
                if evaluator.evaluator_id not in request.policy.enabled_evaluator_ids:
                    continue
                self._check_interrupt(request, cancellation, started)
                try:
                    produced = tuple(evaluator.evaluate(case, frozen))
                    self._validate_metrics(produced, evaluator.definitions)
                    metrics.extend(produced)
                except CorruptEvaluationInput:
                    failures.append(
                        EvaluationFailure(
                            code="artifact_semantic_corruption",
                            evaluator_id=evaluator.evaluator_id,
                        )
                    )
                    metrics = [
                        NonComputedMetricValue(
                            metric_id=item.metric_id,
                            definition_hash=item.definition_hash,
                            status=MetricStatus.ERROR,
                            reason_code="artifact_semantic_corruption",
                        )
                        for item in definitions
                    ]
                    break
                except Exception:
                    failures.append(
                        EvaluationFailure(
                            code="evaluator_failed", evaluator_id=evaluator.evaluator_id
                        )
                    )
                    metrics.extend(
                        NonComputedMetricValue(
                            metric_id=item.metric_id,
                            definition_hash=item.definition_hash,
                            status=MetricStatus.ERROR,
                            reason_code="evaluator_failed",
                        )
                        for item in evaluator.definitions
                    )
                self._check_interrupt(request, cancellation, started)
            if (
                request.policy.enable_model_evaluators
                and not frozen.corrupt
                and not any(
                    item.code == "artifact_semantic_corruption" for item in failures
                )
            ):
                if self._judge is None:
                    raise EvaluationPreconditionError(
                        "model evaluator adapter is missing"
                    )
                if model_calls >= request.policy.max_model_calls:
                    raise EvaluationPreconditionError(
                        "global model call bound exceeded"
                    )
                model_calls += 1
                judge_request = model_requests[case.case_id]
                try:
                    response = await self._invoke_model(
                        judge_request, request, cancellation, started
                    )
                    self._validate_judge_response(response, judge_request)
                    if (
                        response.raw_response_bytes
                        > request.policy.max_model_response_bytes
                        or len(canonical_json_bytes(response))
                        > request.policy.max_model_response_bytes
                    ):
                        raise EvaluationModelFailure(
                            "model response byte bound exceeded"
                        )
                    self._validate_metrics(response.metrics, self._model_definitions)
                    metrics.extend(response.metrics)
                except (EvaluationCancelled, EvaluationDeadlineExceeded):
                    raise
                except Exception:
                    self._check_interrupt(request, cancellation, started)
                    failures.append(
                        EvaluationFailure(
                            code="model_evaluator_failed",
                            evaluator_id="model_evaluator",
                        )
                    )
                    metrics.extend(
                        NonComputedMetricValue(
                            metric_id=item.metric_id,
                            definition_hash=item.definition_hash,
                            status=MetricStatus.ERROR,
                            reason_code="model_evaluator_failed",
                        )
                        for item in self._model_definitions
                    )
            self._validate_metrics(metrics, definitions)
            if len(metrics) > request.policy.max_metrics_per_case:
                raise EvaluationPreconditionError("metric count bound exceeded")
            metrics_tuple = tuple(sorted(metrics, key=lambda item: item.metric_id))
            if len({item.metric_id for item in metrics_tuple}) != len(metrics_tuple):
                raise EvaluationContractError("duplicate metric ownership")
            status = (
                EvaluationCaseStatus.FAILED
                if frozen.corrupt
                or any(item.code == "artifact_semantic_corruption" for item in failures)
                else EvaluationCaseStatus.PARTIAL
                if failures
                or any(item.status is MetricStatus.ERROR for item in metrics_tuple)
                else EvaluationCaseStatus.COMPLETED
            )
            values = dict(
                case_result_id=stable_id(
                    "evalcase",
                    [
                        eval_run_id,
                        case.case_id,
                        binding.run_id,
                        frozen.manifest.manifest_hash,
                    ],
                ),
                eval_run_id=eval_run_id,
                case_id=case.case_id,
                run_id=binding.run_id,
                input_manifest=frozen.manifest,
                compatibility=compatibility[case.case_id],
                status=status,
                metrics=metrics_tuple,
                failures=tuple(failures),
                sut_observations=sut_observations,
                started_at=case_started,
                completed_at=self._clock.now(),
            )
            provisional = EvaluationCaseResult.model_construct(
                **values, case_semantic_hash="0" * 64, artifact_content_hash="0" * 64
            )
            semantic = case_semantic_hash(provisional)
            with_semantic = provisional.model_copy(
                update={"case_semantic_hash": semantic}
            )
            result = EvaluationCaseResult(
                **values,
                case_semantic_hash=semantic,
                artifact_content_hash=case_artifact_hash(with_semantic),
            )
            if (
                len(canonical_json_bytes(result))
                > request.policy.max_case_artifact_bytes
            ):
                raise EvaluationPreconditionError("case artifact byte bound exceeded")
            results.append(result)

        aggregates = aggregate_metrics(
            tuple(item.metrics for item in results), definitions
        )
        status = (
            EvaluationRunStatus.FAILED
            if all(item.status is EvaluationCaseStatus.FAILED for item in results)
            else EvaluationRunStatus.PARTIAL
            if any(
                item.status is not EvaluationCaseStatus.COMPLETED for item in results
            )
            else EvaluationRunStatus.COMPLETED
        )
        values = dict(
            eval_run_id=eval_run_id,
            dataset_id=dataset.dataset_id,
            dataset_version=dataset.dataset_version,
            dataset_hash=dataset.dataset_content_hash,
            case_ids=tuple(item.case_id for item in request.bindings),
            system=system,
            evaluation_policy=request.policy,
            evaluation_policy_hash=policy_hash,
            metric_definitions=definitions,
            evaluator_bundle_hash=bundle_hash,
            evaluation_model_bundle_hash=model_bundle_hash,
            provenance_grade=system.provenance_grade,
            status=status,
            cases=tuple(results),
            aggregates=aggregates,
            started_at=started,
            completed_at=self._clock.now(),
        )
        provisional = EvaluationRun.model_construct(
            **values, evaluation_semantic_hash="0" * 64, artifact_content_hash="0" * 64
        )
        semantic = evaluation_semantic_hash(provisional)
        with_semantic = provisional.model_copy(
            update={"evaluation_semantic_hash": semantic}
        )
        result = EvaluationRun(
            **values,
            evaluation_semantic_hash=semantic,
            artifact_content_hash=evaluation_artifact_hash(with_semantic),
        )
        if (
            len(canonical_json_bytes(result))
            > request.policy.max_evaluation_artifact_bytes
        ):
            raise EvaluationPreconditionError("evaluation artifact byte bound exceeded")
        self._check_interrupt(request, cancellation, started)
        for binding in request.bindings:
            if self._reader.fingerprint(
                binding.run_id,
                max_input_bytes=request.policy.max_input_bytes_per_case,
            ) != raw_fingerprints[binding.run_id]:
                raise EvaluationInputChanged("run artifact changed during evaluation")
        self._artifacts.publish(result)
        return result

    def _resolve_definitions(
        self, policy: EvaluationPolicy
    ) -> tuple[MetricDefinitionSnapshot, ...]:
        available = {item.evaluator_id: item for item in self._evaluators}
        expected_ids = set(policy.enabled_evaluator_ids)
        if policy.enable_model_evaluators:
            expected_ids.remove("model_evaluator")
            if self._judge is None or not self._model_definitions:
                raise EvaluationPreconditionError(
                    "model evaluator adapter or definitions are missing"
                )
            if any(
                item.evaluator_id != "model_evaluator"
                for item in self._model_definitions
            ):
                raise EvaluationContractError("model definition owner differs")
        unknown = expected_ids - set(available)
        if unknown:
            raise EvaluationPreconditionError("enabled evaluator is unavailable")
        definitions = [
            item
            for evaluator in self._evaluators
            if evaluator.evaluator_id in policy.enabled_evaluator_ids
            for item in evaluator.definitions
        ]
        for evaluator in self._evaluators:
            if evaluator.evaluator_id not in expected_ids:
                continue
            if any(
                item.evaluator_id != evaluator.evaluator_id
                or item.evaluator_version != evaluator.evaluator_version
                for item in evaluator.definitions
            ):
                raise EvaluationContractError("evaluator definition owner differs")
        if policy.enable_model_evaluators:
            definitions.extend(self._model_definitions)
        definitions.sort(key=lambda item: item.metric_id)
        if len({item.metric_id for item in definitions}) != len(definitions):
            raise EvaluationContractError("duplicate metric definition ownership")
        return tuple(definitions)

    @staticmethod
    def _validate_metrics(
        metrics: list[MetricValue] | tuple[MetricValue, ...],
        definitions: tuple[MetricDefinitionSnapshot, ...],
    ) -> None:
        by_id = {item.metric_id: item for item in definitions}
        metric_ids = [item.metric_id for item in metrics]
        if set(metric_ids) != set(by_id) or len(metric_ids) != len(set(metric_ids)):
            raise EvaluationContractError("metric bundle is incomplete or duplicated")
        for metric in metrics:
            definition = by_id.get(metric.metric_id)
            if (
                definition is None
                or metric.definition_hash != definition.definition_hash
            ):
                raise EvaluationContractError("metric definition identity differs")
            if (
                metric.value_type is not MetricValueKind.NON_COMPUTED
                and metric.value_type is not definition.value_kind
            ):
                raise EvaluationContractError("metric value kind differs")

    def _validate_judge_response(
        self, response, request: EvaluationJudgeRequest
    ) -> None:
        if (
            response.fixture_key != request.fixture_key
            or response.evaluator_id != request.evaluator_id
            or response.evaluator_version != request.evaluator_version
            or response.model_bundle_hash != self._judge.model_bundle_hash
        ):
            raise EvaluationContractError("model evaluator response identity differs")
        expected = {
            item.metric_id: item.definition_hash for item in self._model_definitions
        }
        actual = {item.metric_id: item.definition_hash for item in response.metrics}
        if actual != expected or tuple(sorted(actual)) != tuple(
            sorted(request.expected_metric_ids)
        ):
            raise EvaluationContractError("model metric definitions differ")

    async def _invoke_model(
        self,
        model_request: EvaluationJudgeRequest,
        request: EvaluationRequest,
        cancellation: CancellationSignal,
        started: datetime,
    ):
        assert self._judge is not None
        remaining = min(
            (request.deadline - self._clock.now()).total_seconds(),
            (
                request.policy.max_duration_milliseconds
                - int((self._clock.now() - started).total_seconds() * 1000)
            )
            / 1000,
        )
        if remaining <= 0:
            raise EvaluationDeadlineExceeded("evaluation deadline exceeded")
        model_task = asyncio.create_task(
            self._judge.evaluate(model_request, cancellation)
        )
        cancel_task = asyncio.create_task(cancellation.wait())
        done, _ = await asyncio.wait(
            {model_task, cancel_task},
            timeout=remaining,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if model_task in done:
            cancel_task.cancel()
            return await model_task
        model_task.cancel()
        if cancel_task in done or cancellation.cancelled:
            raise EvaluationCancelled("evaluation was cancelled")
        cancel_task.cancel()
        raise EvaluationDeadlineExceeded("evaluation model call exceeded deadline")

    def _check_interrupt(
        self,
        request: EvaluationRequest,
        cancellation: CancellationSignal,
        started: datetime,
    ) -> None:
        if cancellation.cancelled:
            raise EvaluationCancelled("evaluation was cancelled")
        now = self._clock.now()
        if (
            now >= request.deadline
            or int((now - started).total_seconds() * 1000)
            >= request.policy.max_duration_milliseconds
        ):
            raise EvaluationDeadlineExceeded("evaluation deadline exceeded")

    @staticmethod
    def _apply_declared_absence(
        observations: tuple[SUTPinObservation, ...],
        binding: CaseRunBinding,
        frozen: FrozenRunArtifacts,
    ) -> tuple[SUTPinObservation, ...]:
        absence = set(binding.absent_by_design_pin_ids)
        result = []
        for item in observations:
            if item.pin_id not in absence:
                result.append(item)
                continue
            if item.status is SUTPinStatus.ARTIFACT_VERIFIED:
                raise EvaluationPreconditionError(
                    f"pin {item.pin_id} declared absent but artifact is present"
                )
            artifact_kind = item.supporting_ref.artifact_kind
            artifact = next(
                ref for ref in frozen.manifest.artifacts
                if ref.artifact_kind is artifact_kind
            )
            if artifact.availability is not ArtifactAvailability.ABSENT:
                raise EvaluationPreconditionError(
                    f"pin {item.pin_id} design absence is not proven"
                )
            result.append(
                SUTPinObservation(
                    pin_id=item.pin_id,
                    pin_version=item.pin_version,
                    status=SUTPinStatus.ABSENT_BY_DESIGN,
                    reason_code="explicit_arm_absence",
                    supporting_ref=SUTPinSupportRef(
                        kind=SUTPinSupportKind.ABSENCE_DECLARATION,
                        input_manifest_hash=item.supporting_ref.input_manifest_hash,
                        artifact_kind=item.supporting_ref.artifact_kind,
                    ),
                )
            )
        unknown = absence - {item.pin_id for item in observations}
        if unknown:
            raise EvaluationPreconditionError("absent-by-design pin is unknown")
        return tuple(result)
