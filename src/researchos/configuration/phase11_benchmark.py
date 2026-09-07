"""Canonical fixed Phase 11 public-reference benchmark (no network access)."""

# ruff: noqa: E501

from researchos.domain.claims import ClaimEvidenceRelation
from researchos.domain.contracts import OperatingMode
from researchos.domain.evaluation import (
    DatasetProvenance,
    EvaluationCase,
    EvaluationDataset,
    ExpectedCitationConstraint,
    ExpectedClaim,
    ExpectedEvidenceConstraint,
    ExpectedPlanningTask,
    ReferenceAnnotations,
    ReferenceLevel,
    RequiredExecutionConditions,
    SourcePolicyRequirement,
    SourcePolicyRequirementKind,
)
from researchos.domain.identity import sha256_text, stable_hash
from researchos.domain.synthesis import VerificationDisposition

# Checked-in locator manifest: identities only, never copied source bodies.
_CASES = tuple(
    sorted(
        (
            (
                "p11_citation_chain",
                "As of 2025-01-01, which NASA pages identify Artemis II and name its crew?",
                ("citation_heavy", "factual", "primary_source"),
                "Locate NASA's Artemis II mission and crew pages.",
                "NASA's Artemis II mission overview identifies the mission, and NASA's crew page names its assigned astronauts.",
                "https://www.nasa.gov/mission/artemis-ii/",
            ),
            (
                "p11_complex_citation_comparison",
                "As of 2025-01-01, compare the stated purposes of the IPCC AR6 Synthesis Report and WMO State of the Global Climate report.",
                ("comparison", "citation_heavy", "multi_source"),
                "Compare the stated purposes of the IPCC and WMO reports.",
                "The IPCC AR6 Synthesis Report synthesizes assessment findings, while WMO's State of the Global Climate report summarizes observed annual climate indicators.",
                "https://www.ipcc.ch/report/ar6/syr/",
            ),
            (
                "p11_comparison_primary",
                "As of 2025-01-01, compare the stated objectives of the U.S. Inflation Reduction Act and EU Net-Zero Industry Act using enacted texts.",
                ("comparison", "multi_source", "citation_heavy"),
                "Compare objectives in the two enacted legal texts.",
                "The Inflation Reduction Act contains climate and energy incentives, while the Net-Zero Industry Act establishes measures to scale EU net-zero technology manufacturing.",
                "https://www.congress.gov/bill/117th-congress/house-bill/5376/text",
            ),
            (
                "p11_comparison_time_bounded",
                "As of 2024-12-31, compare the Federal Reserve December 2024 and December 2023 target ranges for the federal funds rate.",
                ("comparison", "time_bounded", "public_information"),
                "Read two dated Federal Reserve policy statements.",
                "The December 2024 FOMC statement set a lower target range than the December 2023 statement.",
                "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
            ),
            (
                "p11_conflict_multistep",
                "As of 2025-01-01, reconcile NASA and ESA stated launch dates for the James Webb Space Telescope.",
                ("conflict_handling", "multi_step", "multi_source"),
                "Check NASA and ESA mission pages and reconcile date formats.",
                "NASA and ESA both identify 25 December 2021 as the James Webb Space Telescope launch date; differing presentation is not a factual conflict.",
                "https://science.nasa.gov/mission/webb/",
            ),
            (
                "p11_conflicting_sources",
                "As of 2025-01-01, reconcile World Bank and IMF descriptions of their roles in international development finance.",
                ("conflict_handling", "evidence_synthesis", "citation_heavy"),
                "Distinguish the institutions' official descriptions.",
                "The World Bank and IMF describe distinct but complementary mandates: development financing and macroeconomic or financial stability support.",
                "https://www.worldbank.org/en/who-we-are",
            ),
            (
                "p11_evidence_synthesis",
                "As of 2025-01-01, summarize WHO and CDC guidance that vaccination reduces severe COVID-19 outcomes.",
                ("evidence_synthesis", "multi_source", "factual"),
                "Synthesize bounded WHO and CDC guidance with uncertainty.",
                "WHO and CDC guidance state that COVID-19 vaccination helps protect against severe disease, hospitalization, and death, while effectiveness can vary.",
                "https://www.who.int/health-topics/coronavirus/coronavirus",
            ),
            (
                "p11_multisource_factual",
                "As of 2025-01-01, what are the official SI definitions of the metre and second according to BIPM?",
                ("factual", "multi_source", "citation_heavy"),
                "Locate the BIPM SI Brochure definitions for metre and second.",
                "The second is defined using the caesium-133 hyperfine transition frequency, and the metre is defined by fixing the speed of light in vacuum.",
                "https://www.bipm.org/en/publications/si-brochure",
            ),
            (
                "p11_multistep_investigation",
                "As of 2025-01-01, identify the U.S. government branches and constitutional articles establishing each.",
                ("multi_step", "multi_source", "evidence_synthesis"),
                "Map constitutional articles to legislative, executive, and judicial branches.",
                "Articles I, II, and III of the U.S. Constitution establish the legislative, executive, and judicial branches respectively.",
                "https://constitution.congress.gov/constitution/",
            ),
            (
                "p11_primary_source_investigation",
                "As of 2025-01-01, what objective does UN Charter Article 1 state for maintaining international peace and security?",
                ("primary_source", "multi_step", "factual"),
                "Read Article 1 of the UN Charter primary text.",
                "Article 1 states that a UN purpose is to maintain international peace and security and take collective measures to prevent and remove threats to peace.",
                "https://www.un.org/en/about-us/un-charter/chapter-1",
            ),
            (
                "p11_public_evidence_synthesis",
                "As of 2025-01-01, synthesize NOAA and NASA explanations of why global mean sea level changes.",
                ("public_information", "evidence_synthesis", "citation_heavy"),
                "Synthesize the two agencies' stated mechanisms.",
                "NOAA and NASA explain that ocean warming or thermal expansion and land-ice loss are major contributors to global mean sea-level rise.",
                "https://oceanservice.noaa.gov/facts/sealevel.html",
            ),
            (
                "p11_time_bounded_public",
                "As of 2024-12-31, what did the U.S. Census Bureau report as the 2020 Census reference day?",
                ("public_information", "time_bounded", "multi_source"),
                "Locate the Census Bureau reference-day statement.",
                "The 2020 Census reference day was April 1, 2020.",
                "https://www.census.gov/programs-surveys/decennial-census/decade/2020.html",
            ),
        ),
        key=lambda item: item[0],
    )
)

# Each multi-source/comparison/reconciliation case is pinned to at least two
# distinct public-source locators.  Locators are identities for offline
# evaluation constraints, not fetched benchmark content.
_SECONDARY_LOCATORS = {
    "p11_complex_citation_comparison": "https://public.wmo.int/publication-series/state-of-global-climate",
    "p11_comparison_primary": "https://eur-lex.europa.eu/eli/reg/2024/1735/oj",
    "p11_comparison_time_bounded": "https://www.federalreserve.gov/monetarypolicy/fomcpressconf20231213a.htm",
    "p11_conflict_multistep": "https://www.esa.int/Science_Exploration/Space_Science/Webb",
    "p11_conflicting_sources": "https://www.imf.org/en/About",
    "p11_evidence_synthesis": "https://www.cdc.gov/covid/vaccines/index.html",
    "p11_multisource_factual": "https://www.nist.gov/pml/owm/si-units-length",
    "p11_multistep_investigation": "https://www.archives.gov/founding-docs/constitution-transcript",
    "p11_public_evidence_synthesis": "https://science.nasa.gov/earth/explore/earths-sea-level/",
    "p11_time_bounded_public": "https://www.census.gov/programs-surveys/decennial-census/technical-documentation.html",
}


def _case(
    case_id: str,
    query: str,
    tags: tuple[str, ...],
    objective: str,
    statement: str,
    locator: str,
) -> EvaluationCase:
    if "Reference claim for" in statement or "Research p11_" in objective:
        raise ValueError("Phase 11 benchmark contains placeholder reference content")
    secondary_locator = _SECONDARY_LOCATORS.get(case_id)
    locators = (locator,) if secondary_locator is None else (locator, secondary_locator)
    locator_hashes = tuple(sha256_text(item) for item in locators)
    annotations = ReferenceAnnotations(
        expected_capability_ids=("web_search",),
        planning_tasks=(
            ExpectedPlanningTask(
                reference_task_id=f"{case_id}_task",
                objective=objective,
                normalized_objective_hash=sha256_text(objective),
                required_capability_ids=("web_search",),
            ),
        ),
        claims=(
            ExpectedClaim(
                reference_claim_id=f"{case_id}_claim",
                statement=statement,
                normalized_statement_hash=sha256_text(statement),
            ),
        ),
        evidence=tuple(
            ExpectedEvidenceConstraint(
                constraint_id=f"{case_id}_evidence_{index}",
                source_type="web",
                canonical_locator_hash=locator_hash,
                relation=ClaimEvidenceRelation.SUPPORTS,
            )
            for index, locator_hash in enumerate(locator_hashes, start=1)
        ),
        citations=tuple(
            ExpectedCitationConstraint(
                reference_claim_id=f"{case_id}_claim",
                evidence_constraint_id=f"{case_id}_evidence_{index}",
            )
            for index in range(1, len(locator_hashes) + 1)
        ),
        expected_dispositions=(VerificationDisposition.VERIFIED,),
    )
    return EvaluationCase(
        case_id=case_id,
        reference_level=ReferenceLevel.FULL_REFERENCE,
        query=query,
        required_execution_conditions=RequiredExecutionConditions(
            operating_mode=OperatingMode.REAL,
            source_policy=SourcePolicyRequirement(
                kind=SourcePolicyRequirementKind.REQUIRE_NONE
            ),
            required_allowed_capability_ids=("web_search",),
        ),
        reference_annotations=annotations,
        tags=tags,
        metadata={
            "as_of_date": "2024-12-31" if "2024-12-31" in query else "2025-01-01",
            "reference_locator": locator,
            "reference_locator_hash": locator_hashes[0],
            "reference_locators": locators,
            "reference_locator_hashes": locator_hashes,
            "reference_manifest_version": "phase11-reference-v1",
        },
    )


def build_phase11_real_benchmark_v1() -> EvaluationDataset:
    """Return the exact offline benchmark definition without provider access."""
    cases = tuple(_case(*item) for item in _CASES)
    values = dict(
        dataset_id="phase11_real_benchmark",
        dataset_version="1",
        provenance=DatasetProvenance(
            license_id="researchos_internal",
            source_uri_hash=stable_hash("phase11-reference-manifest-v1"),
            curator_id="researchos",
            provenance_version="1",
        ),
        cases=cases,
    )
    provisional = EvaluationDataset.model_construct(
        **values, dataset_content_hash="0" * 64
    )
    return EvaluationDataset(
        **values,
        dataset_content_hash=stable_hash(
            provisional.model_dump(mode="json", exclude={"dataset_content_hash"})
        ),
    )


def build_phase11_ablation_subset_v1(*, comparison_id: str) -> EvaluationDataset:
    selections = {
        "max_rounds_1_vs_2": (
            "p11_citation_chain",
            "p11_conflict_multistep",
            "p11_multisource_factual",
            "p11_time_bounded_public",
        ),
        "search_browser_vs_search_only": (
            "p11_comparison_primary",
            "p11_evidence_synthesis",
            "p11_multistep_investigation",
            "p11_primary_source_investigation",
        ),
    }
    try:
        ids = selections[comparison_id]
    except KeyError as exc:
        raise ValueError("unknown Phase 11 ablation comparison") from exc
    benchmark = build_phase11_real_benchmark_v1()
    values = dict(
        dataset_id=f"phase11_{comparison_id}",
        dataset_version="1",
        provenance=benchmark.provenance,
        cases=tuple(
            {item.case_id: item for item in benchmark.cases}[item] for item in ids
        ),
    )
    provisional = EvaluationDataset.model_construct(
        **values, dataset_content_hash="0" * 64
    )
    return EvaluationDataset(
        **values,
        dataset_content_hash=stable_hash(
            provisional.model_dump(mode="json", exclude={"dataset_content_hash"})
        ),
    )
