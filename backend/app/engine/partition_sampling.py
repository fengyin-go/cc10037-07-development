"""分区抽样质量门禁引擎。

背景
----
旧版 ``validation_executor`` 只按"实际收到的行"计算坏行比例：
  * 不区分各分区不同的抽样概率，大分区样本占比天然偏大，直接合并会失真；
  * 没有覆盖度概念，某分区样本一批迟到时仍可能给出"整表通过"的错误结论；
  * 计划阈值调整后无法追溯当时使用的抽样数与坏行数。

本引擎引入两层判定：

1. 分层（分区）质量结果
   每个分区按自己的抽样概率给样本行计权，计算 **加权坏行率**，
   再用计划中明示的阈值规则给出 PASS / WARN / FAIL 分层结论；
   覆盖率不足时只产出"暂算值"，不得确认。
2. 整表质量结果
   按各分区总体规模（权重池）汇总加权坏行率，
   只有所有必需分区都达到各自最低覆盖率、且整表覆盖率达标，
   才允许输出"确认值"，否则一律标记为暂算值并附诊断。

关键规则（与需求逐条对应）：

* 没收到的样本 **不按通过算** —— 只按实际收到的行计权，缺失样本进覆盖率诊断；
* 已收到的行用其 **对应抽样版本** 计权（补报批次携带计划版本号）；
* 计划阈值变更后重算受影响分区，并 **另存当时使用的抽样数和坏行数** 快照；
* 补报小分区后同一质量接口按实际覆盖重算，报表区分暂算值与确认值。

只用标准库，纯函数式计算 + 一个内存仓库，便于单测与 FastAPI 接入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


# ---------------------------------------------------------------------------
# 枚举与基础数据结构
# ---------------------------------------------------------------------------


class QualityTier(str, Enum):
    """分层质量结论，阈值规则在抽样计划中明示。"""

    PASS = "PASS"   # 加权坏行率 <= pass 阈值
    WARN = "WARN"   # pass 阈值 < 坏行率 <= fail 阈值
    FAIL = "FAIL"   # 坏行率 > fail 阈值
    NO_DATA = "NO_DATA"  # 该分区一个样本都没收到，无法计算


class EvalStatus(str, Enum):
    """结果的确认状态：覆盖够不够，决定能不能确认整表/分区结论。"""

    PROVISIONAL = "PROVISIONAL"  # 暂算值：覆盖不足或有样本迟到，结论不可作为放行依据
    CONFIRMED = "CONFIRMED"      # 确认值：覆盖达标，结论可用于放行


class DiagnosticCode(str, Enum):
    PARTITION_NO_SAMPLE = "PARTITION_NO_SAMPLE"       # 必需分区一个样本都没到
    PARTITION_UNDER_COVERAGE = "PARTITION_UNDER_COVERAGE"  # 分区覆盖率低于最低要求
    TABLE_UNDER_COVERAGE = "TABLE_UNDER_COVERAGE"     # 整表覆盖率低于最低要求
    PARTITION_LATE_BATCH = "PARTITION_LATE_BATCH"     # 存在迟到/未到齐的样本批次
    PLAN_VERSION_MISMATCH = "PLAN_VERSION_MISMATCH"   # 样本携带的计划版本与当前版本不一致
    PARTITION_NOT_IN_PLAN = "PARTITION_NOT_IN_PLAN"   # 收到了计划外分区的样本


class ReviewStatus(str, Enum):
    """复查明细的整改流转状态。"""

    PENDING = "PENDING"         # 待值班人员核验
    CONFIRMED = "CONFIRMED"     # 核验确认是坏行，待整改
    RECTIFIED = "RECTIFIED"     # 已提交整改并复核通过
    DISMISSED = "DISMISSED"     # 核验后判定误报，驳回


@dataclass
class PartitionSpec:
    """抽样计划中的一个分区配置（一个计划版本内每个分区一条）。

    ``sampling_rate`` 是该分区的抽样概率（0~1），两个分区可以不同。
    样本行的计权系数 = 1 / sampling_rate，即该样本代表的总体行数。
    """

    partition: str
    sampling_rate: float
    population_rows: int          # 该分区总体行数（整表汇总时的权重池依据）
    planned_sample_n: int         # 计划抽样数（覆盖率分母），通常 = population * rate 后取整
    min_coverage: float = 0.8     # 该分区允许确认结论的最低覆盖率（实际收到/计划抽样数）

    def __post_init__(self) -> None:
        if not 0.0 < self.sampling_rate <= 1.0:
            raise ValueError(f"分区 {self.partition} 抽样概率必须在 (0, 1] 内")
        if self.population_rows <= 0:
            raise ValueError(f"分区 {self.partition} 总体行数必须为正")
        if self.planned_sample_n <= 0:
            raise ValueError(f"分区 {self.partition} 计划抽样数必须为正")
        if not 0.0 < self.min_coverage <= 1.0:
            raise ValueError(f"分区 {self.partition} 最低覆盖率必须在 (0, 1] 内")


@dataclass
class QualityThresholds:
    """明示的阈值规则：分层结论完全由这两个阈值决定。"""

    pass_max: float               # 加权坏行率 <= 该值 → PASS
    fail_max: float               # pass_max < 坏行率 <= fail_max → WARN，> fail_max → FAIL
    min_table_coverage: float = 0.8  # 整表确认所需的最低加权覆盖率

    def __post_init__(self) -> None:
        if not 0.0 <= self.pass_max <= self.fail_max <= 1.0:
            raise ValueError("阈值需满足 0 <= pass_max <= fail_max <= 1")
        if not 0.0 < self.min_table_coverage <= 1.0:
            raise ValueError("整表最低覆盖率必须在 (0, 1] 内")

    def classify(self, bad_rate: float) -> QualityTier:
        if bad_rate <= self.pass_max:
            return QualityTier.PASS
        if bad_rate <= self.fail_max:
            return QualityTier.WARN
        return QualityTier.FAIL


@dataclass
class SamplingPlan:
    """一个版本的分区抽样计划。

    阈值或抽样配置变更时不覆盖旧版本，而是新增版本，
    历史批次仍按其携带的 ``plan_version`` 找到当时的抽样概率与阈值。
    """

    plan_id: str
    table_name: str
    version: int
    partitions: list[PartitionSpec]
    thresholds: QualityThresholds
    created_at: str
    note: str = ""

    def spec(self, partition: str) -> PartitionSpec | None:
        for p in self.partitions:
            if p.partition == partition:
                return p
        return None


@dataclass
class SampleRow:
    """一条实际收到的样本行。

    ``is_bad`` 由上游检测规则（ValidationEngine 等）判定后写入；
    本引擎不重新做行级规则检测，只负责抽样计权与覆盖度判定。
    """

    row_id: str
    partition: str
    is_bad: bool
    plan_version: int                 # 该样本按哪个计划版本抽取（补报也携带当时版本）
    batch_id: str
    received_at: str
    detail: dict[str, Any] = field(default_factory=dict)  # 坏行原因、字段值等溯源信息
    weight: float = 0.0               # 计权系数，入库时按计划版本回填


@dataclass
class SampleBatch:
    """一批样本的到达登记，用于表达"迟到/补报"。"""

    batch_id: str
    partition: str
    plan_version: int
    expected_n: int                   # 该批承诺的样本数
    received_n: int = 0               # 实际到达数
    arrived: bool = False             # 整批是否到齐
    received_at: str | None = None
    is_late: bool = False             # 是否为迟到/补报批次


# ---------------------------------------------------------------------------
# 计算结果结构
# ---------------------------------------------------------------------------


@dataclass
class Diagnostic:
    code: DiagnosticCode
    partition: str | None
    message: str
    detail: dict[str, Any] = field(default_factory=dict)
    resolved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "partition": self.partition,
            "message": self.message,
            "detail": self.detail,
            "resolved": self.resolved,
        }


@dataclass
class PartitionQuality:
    """分层（分区）质量结果。"""

    partition: str
    plan_version: int
    sampling_rate: float
    planned_sample_n: int
    received_sample_n: int
    bad_sample_n: int
    coverage: float                          # 收到样本数 / 计划抽样数（封顶 1.0 展示）
    weighted_total: float                    # 收到样本的计权总量（代表已覆盖的总体行数）
    weighted_bad: float                      # 坏行计权总量
    weighted_bad_rate: float                 # 加权坏行率
    naive_bad_rate: float                    # 不计权的原始坏行比例，用于对照展示
    tier: QualityTier
    status: EvalStatus
    expected_population: int
    diagnostics: list[Diagnostic] = field(default_factory=list)
    # 重算追溯：本次结果实际使用的抽样数与坏行数（需求要求另存）
    calc_snapshot: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "partition": self.partition,
            "plan_version": self.plan_version,
            "sampling_rate": self.sampling_rate,
            "planned_sample_n": self.planned_sample_n,
            "received_sample_n": self.received_sample_n,
            "bad_sample_n": self.bad_sample_n,
            "coverage": round(self.coverage, 6),
            "weighted_total": round(self.weighted_total, 4),
            "weighted_bad": round(self.weighted_bad, 4),
            "weighted_bad_rate": round(self.weighted_bad_rate, 6),
            "naive_bad_rate": round(self.naive_bad_rate, 6),
            "tier": self.tier.value,
            "status": self.status.value,
            "expected_population": self.expected_population,
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "calc_snapshot": self.calc_snapshot,
        }


@dataclass
class TableQuality:
    """整表质量结果。"""

    table_name: str
    plan_id: str
    plan_version: int
    weighted_bad_rate: float
    naive_bad_rate: float
    tier: QualityTier
    status: EvalStatus
    table_coverage: float                   # 加权覆盖率：已覆盖总体 / 全表总体
    received_sample_n: int
    bad_sample_n: int
    partitions: list[PartitionQuality]
    diagnostics: list[Diagnostic] = field(default_factory=list)
    computed_at: str = ""
    calc_snapshot: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "table_name": self.table_name,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "weighted_bad_rate": round(self.weighted_bad_rate, 6),
            "naive_bad_rate": round(self.naive_bad_rate, 6),
            "tier": self.tier.value,
            "status": self.status.value,
            "table_coverage": round(self.table_coverage, 6),
            "received_sample_n": self.received_sample_n,
            "bad_sample_n": self.bad_sample_n,
            "partitions": [p.to_dict() for p in self.partitions],
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "computed_at": self.computed_at,
            "calc_snapshot": self.calc_snapshot,
        }


# ---------------------------------------------------------------------------
# 核心计算引擎（纯计算，无状态，方便单测）
# ---------------------------------------------------------------------------


class PartitionSamplingCalculator:
    """加权坏行率与覆盖度的纯计算逻辑。"""

    @staticmethod
    def row_weight(sampling_rate: float) -> float:
        """样本计权系数：抽样概率的倒数。"""
        return 1.0 / sampling_rate

    @staticmethod
    def evaluate_partition(
        spec: PartitionSpec,
        plan_version: int,
        rows: list[SampleRow],
        thresholds: QualityThresholds,
        now: str,
    ) -> PartitionQuality:
        """计算单个分区的分层质量结果。

        已收到的行全部按其所属计划版本的抽样概率计权（调用方负责保证
        传入的 rows 已回填 weight）；没收到的样本不参与分子分母，
        只通过覆盖率体现缺口。
        """
        diagnostics: list[Diagnostic] = []
        received = len(rows)
        bad_n = sum(1 for r in rows if r.is_bad)
        coverage = received / spec.planned_sample_n

        weighted_total = sum(r.weight for r in rows)
        weighted_bad = sum(r.weight for r in rows if r.is_bad)

        if received == 0:
            tier = QualityTier.NO_DATA
            bad_rate = 0.0
            naive = 0.0
            diagnostics.append(Diagnostic(
                DiagnosticCode.PARTITION_NO_SAMPLE, spec.partition,
                f"分区 {spec.partition} 尚未收到任何样本，坏行率无法计算",
                {"planned_sample_n": spec.planned_sample_n},
            ))
        else:
            bad_rate = weighted_bad / weighted_total if weighted_total else 0.0
            naive = bad_n / received
            tier = thresholds.classify(bad_rate)

        # 覆盖度判定：覆盖率按计划抽样数衡量，超过 100%（补报超额）不判缺
        if coverage < spec.min_coverage:
            status = EvalStatus.PROVISIONAL
            if received > 0:
                diagnostics.append(Diagnostic(
                    DiagnosticCode.PARTITION_UNDER_COVERAGE, spec.partition,
                    (f"分区 {spec.partition} 覆盖率 {coverage:.1%} "
                     f"低于最低要求 {spec.min_coverage:.1%}，结论为暂算值"),
                    {"coverage": round(coverage, 6),
                     "min_coverage": spec.min_coverage,
                     "missing_sample_n": max(spec.planned_sample_n - received, 0)},
                ))
        else:
            status = EvalStatus.CONFIRMED

        snapshot = {
            "plan_version": plan_version,
            "partition": spec.partition,
            "sampling_rate": spec.sampling_rate,
            "planned_sample_n": spec.planned_sample_n,
            "used_sample_n": received,          # 当时实际使用的抽样数
            "used_bad_n": bad_n,                # 当时实际使用的坏行数
            "weighted_total": round(weighted_total, 4),
            "weighted_bad": round(weighted_bad, 4),
            "computed_at": now,
        }

        return PartitionQuality(
            partition=spec.partition,
            plan_version=plan_version,
            sampling_rate=spec.sampling_rate,
            planned_sample_n=spec.planned_sample_n,
            received_sample_n=received,
            bad_sample_n=bad_n,
            coverage=min(coverage, 1.0),
            weighted_total=weighted_total,
            weighted_bad=weighted_bad,
            weighted_bad_rate=bad_rate,
            naive_bad_rate=naive,
            tier=tier,
            status=status,
            expected_population=spec.population_rows,
            diagnostics=diagnostics,
            calc_snapshot=snapshot,
        )

    @staticmethod
    def evaluate_table(
        plan: SamplingPlan,
        partition_rows: dict[str, list[SampleRow]],
        extra_batches: list[SampleBatch] | None = None,
        now: str = "",
    ) -> TableQuality:
        """汇总整表质量结果。

        分区之间按总体规模加权：每个分区的加权坏行率已经是对该分区
        总体的无偏估计，整表率 = Σ(分区坏行估计量) / Σ(分区总体量)。
        已收到样本覆盖到的总体量作为整表覆盖率分子，没收到的样本不按通过算。
        """
        partition_results: list[PartitionQuality] = []
        diagnostics: list[Diagnostic] = []

        for spec in plan.partitions:
            rows = partition_rows.get(spec.partition, [])
            # 版本一致性检查：样本携带的版本应当能在计划历史中找到，
            # 与当前版本不同只提示，不拒绝（补报的旧版本样本仍按旧权计）。
            for r in rows:
                if r.plan_version != plan.version:
                    diagnostics.append(Diagnostic(
                        DiagnosticCode.PLAN_VERSION_MISMATCH, spec.partition,
                        (f"分区 {spec.partition} 存在计划 v{r.plan_version} 的样本，"
                         f"当前计划为 v{plan.version}，仍按样本所属版本计权"),
                        {"sample_row_id": r.row_id, "sample_version": r.plan_version,
                         "current_version": plan.version},
                    ))
                    break
            pq = PartitionSamplingCalculator.evaluate_partition(
                spec, plan.version, rows, plan.thresholds, now,
            )
            partition_results.append(pq)
            diagnostics.extend(pq.diagnostics)

        # 计划外分区
        planned_names = {p.partition for p in plan.partitions}
        for name in partition_rows:
            if name not in planned_names:
                diagnostics.append(Diagnostic(
                    DiagnosticCode.PARTITION_NOT_IN_PLAN, name,
                    f"收到计划外分区 {name} 的样本，已忽略不计入整表",
                ))

        # 迟到/未到齐批次诊断
        for batch in extra_batches or []:
            if not batch.arrived:
                diagnostics.append(Diagnostic(
                    DiagnosticCode.PARTITION_LATE_BATCH, batch.partition,
                    (f"分区 {batch.partition} 批次 {batch.batch_id} 尚未到齐 "
                     f"({batch.received_n}/{batch.expected_n})，缺失样本不按通过计算"),
                    {"batch_id": batch.batch_id, "expected_n": batch.expected_n,
                     "received_n": batch.received_n},
                ))
            elif batch.is_late:
                diagnostics.append(Diagnostic(
                    DiagnosticCode.PARTITION_LATE_BATCH, batch.partition,
                    f"分区 {batch.partition} 批次 {batch.batch_id} 为迟到补报，已纳入重算",
                    {"batch_id": batch.batch_id, "is_late": True},
                    resolved=True,
                ))

        # ---- 整表加权汇总 ----
        total_population = sum(p.population_rows for p in plan.partitions)
        covered_population = sum(p.weighted_total for p in partition_results)
        est_bad_population = sum(p.weighted_bad for p in partition_results)
        table_coverage = covered_population / total_population if total_population else 0.0
        weighted_bad_rate = (
            est_bad_population / covered_population if covered_population else 0.0
        )

        total_received = sum(p.received_sample_n for p in partition_results)
        total_bad = sum(p.bad_sample_n for p in partition_results)
        naive_bad_rate = total_bad / total_received if total_received else 0.0

        any_data = any(p.received_sample_n > 0 for p in partition_results)
        tier = plan.thresholds.classify(weighted_bad_rate) if any_data else QualityTier.NO_DATA

        all_confirmed = all(
            p.status == EvalStatus.CONFIRMED for p in partition_results
        )
        if all_confirmed and table_coverage >= plan.thresholds.min_table_coverage:
            status = EvalStatus.CONFIRMED
        else:
            status = EvalStatus.PROVISIONAL
            if table_coverage < plan.thresholds.min_table_coverage and any_data:
                diagnostics.append(Diagnostic(
                    DiagnosticCode.TABLE_UNDER_COVERAGE, None,
                    (f"整表加权覆盖率 {table_coverage:.1%} 低于最低要求 "
                     f"{plan.thresholds.min_table_coverage:.1%}，整表结论为暂算值"),
                    {"table_coverage": round(table_coverage, 6),
                     "min_table_coverage": plan.thresholds.min_table_coverage},
                ))

        snapshot = {
            "plan_id": plan.plan_id,
            "plan_version": plan.version,
            "thresholds": {
                "pass_max": plan.thresholds.pass_max,
                "fail_max": plan.thresholds.fail_max,
                "min_table_coverage": plan.thresholds.min_table_coverage,
            },
            "partitions": [p.calc_snapshot for p in partition_results],
            "used_sample_n": total_received,
            "used_bad_n": total_bad,
            "computed_at": now,
        }

        return TableQuality(
            table_name=plan.table_name,
            plan_id=plan.plan_id,
            plan_version=plan.version,
            weighted_bad_rate=weighted_bad_rate,
            naive_bad_rate=naive_bad_rate,
            tier=tier,
            status=status,
            table_coverage=table_coverage,
            received_sample_n=total_received,
            bad_sample_n=total_bad,
            partitions=partition_results,
            diagnostics=diagnostics,
            computed_at=now,
            calc_snapshot=snapshot,
        )


# ---------------------------------------------------------------------------
# 有状态的应用服务：计划版本、样本登记、重算快照、复查明细
# ---------------------------------------------------------------------------


@dataclass
class ReviewItem:
    """一条坏行复查明细，把坏行来源连到对应分区和样本。"""

    review_id: str
    table_name: str
    partition: str
    row_id: str
    batch_id: str
    plan_version: int
    reason: str
    sample_detail: dict[str, Any]
    weight: float
    status: ReviewStatus = ReviewStatus.PENDING
    assignee: str | None = None
    rectification: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "table_name": self.table_name,
            "partition": self.partition,
            "row_id": self.row_id,
            "batch_id": self.batch_id,
            "plan_version": self.plan_version,
            "reason": self.reason,
            "sample_detail": self.sample_detail,
            "weight": round(self.weight, 4),
            "status": self.status.value,
            "assignee": self.assignee,
            "rectification": self.rectification,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class SamplingQualityService:
    """分区抽样质量门禁的内存仓库 + 业务编排。

    职责：
      * 管理抽样计划的多个版本（阈值/抽样变更 → 新版本，不覆盖旧版本）；
      * 登记样本批次与样本行，按样本所属计划版本回填计权；
      * 每次重算保存分层结果快照，可区分暂算值/确认值；
      * 从坏行生成复查明细，支持值班人员核验与整改提交。
    """

    def __init__(self, clock: Callable[[], str] | None = None) -> None:
        self._clock = clock or _iso_now
        self._plans: dict[str, list[SamplingPlan]] = {}       # table -> 版本列表（升序）
        self._rows: dict[str, list[SampleRow]] = {}           # table -> 样本行
        self._batches: dict[str, list[SampleBatch]] = {}      # table -> 批次
        self._snapshots: dict[str, list[TableQuality]] = {}   # table -> 历史整表结果
        self._reviews: dict[str, list[ReviewItem]] = {}       # table -> 复查明细
        self._review_seq = 0

    # ---- 计划管理 ----

    def create_plan(
        self,
        plan_id: str,
        table_name: str,
        partitions: list[PartitionSpec],
        thresholds: QualityThresholds,
        note: str = "",
    ) -> SamplingPlan:
        if table_name in self._plans:
            raise ValueError(f"表 {table_name} 已存在抽样计划，请用 revise_plan 修订新版本")
        plan = SamplingPlan(
            plan_id=plan_id,
            table_name=table_name,
            version=1,
            partitions=partitions,
            thresholds=thresholds,
            created_at=self._clock(),
            note=note,
        )
        self._plans[table_name] = [plan]
        self._rows.setdefault(table_name, [])
        self._batches.setdefault(table_name, [])
        self._snapshots.setdefault(table_name, [])
        self._reviews.setdefault(table_name, [])
        return plan

    def revise_plan(
        self,
        table_name: str,
        *,
        partitions: list[PartitionSpec] | None = None,
        thresholds: QualityThresholds | None = None,
        note: str = "",
    ) -> SamplingPlan:
        """阈值或抽样配置变更：新增一个计划版本，旧版本保留可追溯。"""
        history = self._plans.get(table_name)
        if not history:
            raise KeyError(f"表 {table_name} 尚无抽样计划")
        old = history[-1]
        new = SamplingPlan(
            plan_id=old.plan_id,
            table_name=table_name,
            version=old.version + 1,
            partitions=partitions or [
                PartitionSpec(
                    partition=p.partition,
                    sampling_rate=p.sampling_rate,
                    population_rows=p.population_rows,
                    planned_sample_n=p.planned_sample_n,
                    min_coverage=p.min_coverage,
                )
                for p in old.partitions
            ],
            thresholds=thresholds or QualityThresholds(
                pass_max=old.thresholds.pass_max,
                fail_max=old.thresholds.fail_max,
                min_table_coverage=old.thresholds.min_table_coverage,
            ),
            created_at=self._clock(),
            note=note,
        )
        history.append(new)
        return new

    def current_plan(self, table_name: str) -> SamplingPlan:
        history = self._plans.get(table_name)
        if not history:
            raise KeyError(f"表 {table_name} 尚无抽样计划")
        return history[-1]

    def plan_version(self, table_name: str, version: int) -> SamplingPlan | None:
        for plan in self._plans.get(table_name, []):
            if plan.version == version:
                return plan
        return None

    def list_plans(self, table_name: str) -> list[SamplingPlan]:
        return list(self._plans.get(table_name, []))

    def _spec_for_version(self, table_name: str, version: int, partition: str) -> PartitionSpec | None:
        plan = self.plan_version(table_name, version)
        return plan.spec(partition) if plan else None

    # ---- 批次与样本登记 ----

    def register_batch(
        self, table_name: str, batch_id: str, partition: str,
        plan_version: int, expected_n: int,
    ) -> SampleBatch:
        """登记一批应到样本（批次先于样本登记，用于表达"还没到"）。"""
        if self._spec_for_version(table_name, plan_version, partition) is None:
            raise ValueError(
                f"批次 {batch_id} 引用的表 {table_name} v{plan_version} "
                f"分区 {partition} 不存在"
            )
        batch = SampleBatch(
            batch_id=batch_id, partition=partition, plan_version=plan_version,
            expected_n=expected_n,
        )
        self._batches.setdefault(table_name, []).append(batch)
        return batch

    def ingest_rows(
        self,
        table_name: str,
        partition: str,
        plan_version: int,
        batch_id: str,
        rows: list[dict[str, Any]],
        *,
        is_late: bool = False,
    ) -> list[SampleRow]:
        """登记实际收到的样本行，并按样本所属计划版本回填计权系数。

        每行 dict 至少包含 ``row_id`` 与 ``is_bad``；
        没收到的样本根本不会出现在这里，因此天然"不按通过算"。
        """
        spec = self._spec_for_version(table_name, plan_version, partition)
        if spec is None:
            raise ValueError(
                f"表 {table_name} v{plan_version} 分区 {partition} 不在抽样计划中"
            )
        weight = PartitionSamplingCalculator.row_weight(spec.sampling_rate)
        stored: list[SampleRow] = []
        for raw in rows:
            row = SampleRow(
                row_id=str(raw["row_id"]),
                partition=partition,
                is_bad=bool(raw["is_bad"]),
                plan_version=plan_version,
                batch_id=batch_id,
                received_at=self._clock(),
                detail=raw.get("detail", {}),
                weight=weight,
            )
            stored.append(row)
        self._rows.setdefault(table_name, []).extend(stored)

        # 同步批次到齐状态
        batch = self._find_batch(table_name, batch_id)
        if batch is not None:
            batch.received_n += len(stored)
            if batch.received_n >= batch.expected_n:
                batch.arrived = True
                batch.received_at = self._clock()
                batch.is_late = is_late
        return stored

    def mark_batch_late(self, table_name: str, batch_id: str) -> None:
        batch = self._find_batch(table_name, batch_id)
        if batch is not None:
            batch.is_late = True

    def _find_batch(self, table_name: str, batch_id: str) -> SampleBatch | None:
        for b in self._batches.get(table_name, []):
            if b.batch_id == batch_id:
                return b
        return None

    def _rows_by_partition(self, table_name: str) -> dict[str, list[SampleRow]]:
        grouped: dict[str, list[SampleRow]] = {}
        for row in self._rows.get(table_name, []):
            grouped.setdefault(row.partition, []).append(row)
        return grouped

    # ---- 评估与重算 ----

    def evaluate(self, table_name: str) -> TableQuality:
        """按当前计划版本重算整表与分层质量，并保存快照。

        同一质量接口在补报/阈值变更后反复调用：每次都基于当前实际覆盖
        重新计算，历史快照保留当时使用的抽样数和坏行数。
        """
        plan = self.current_plan(table_name)
        result = PartitionSamplingCalculator.evaluate_table(
            plan,
            self._rows_by_partition(table_name),
            extra_batches=list(self._batches.get(table_name, [])),
            now=self._clock(),
        )
        self._snapshots.setdefault(table_name, []).append(result)
        self._sync_review_items(table_name, result)
        return result

    def snapshots(self, table_name: str) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self._snapshots.get(table_name, [])]

    def latest_snapshot(self, table_name: str) -> TableQuality | None:
        history = self._snapshots.get(table_name, [])
        return history[-1] if history else None

    # ---- 复查明细 ----

    def _sync_review_items(self, table_name: str, result: TableQuality) -> None:
        """坏行来源连到对应分区和样本：为新坏行生成复查明细。"""
        existing = {r.row_id for r in self._reviews.get(table_name, [])}
        for row in self._rows.get(table_name, []):
            if not row.is_bad or row.row_id in existing:
                continue
            self._review_seq += 1
            item = ReviewItem(
                review_id=f"REV-{self._review_seq:04d}",
                table_name=table_name,
                partition=row.partition,
                row_id=row.row_id,
                batch_id=row.batch_id,
                plan_version=row.plan_version,
                reason=str(row.detail.get("reason", "检测规则命中")),
                sample_detail=row.detail,
                weight=row.weight,
                created_at=self._clock(),
                updated_at=self._clock(),
            )
            self._reviews[table_name].append(item)

    def list_reviews(
        self, table_name: str, status: ReviewStatus | None = None
    ) -> list[ReviewItem]:
        items = self._reviews.get(table_name, [])
        if status is not None:
            items = [i for i in items if i.status == status]
        return list(items)

    def submit_review(
        self,
        table_name: str,
        review_id: str,
        action: str,
        *,
        assignee: str | None = None,
        rectification: str | None = None,
    ) -> ReviewItem:
        """值班人员核验/提交整改。

        action:
          confirm   —— 核验确认坏行（PENDING → CONFIRMED）
          rectify   —— 提交整改并复核通过（CONFIRMED → RECTIFIED）
          dismiss   —— 判定误报驳回（PENDING/CONFIRMED → DISMISSED）
        """
        item = next(
            (i for i in self._reviews.get(table_name, []) if i.review_id == review_id),
            None,
        )
        if item is None:
            raise KeyError(f"复查单 {review_id} 不存在")
        flow = {
            "confirm": {ReviewStatus.PENDING: ReviewStatus.CONFIRMED},
            "rectify": {ReviewStatus.CONFIRMED: ReviewStatus.RECTIFIED},
            "dismiss": {
                ReviewStatus.PENDING: ReviewStatus.DISMISSED,
                ReviewStatus.CONFIRMED: ReviewStatus.DISMISSED,
            },
        }
        allowed = flow.get(action)
        if not allowed or item.status not in allowed:
            raise ValueError(
                f"复查单 {review_id} 当前状态 {item.status.value} 不支持动作 {action}"
            )
        item.status = allowed[item.status]
        item.assignee = assignee or item.assignee
        if rectification is not None:
            item.rectification = rectification
        item.updated_at = self._clock()
        return item

    # ---- 报表 ----

    def quality_report(self, table_name: str) -> dict[str, Any]:
        """给报表层使用：明示暂算值/确认值、分层结果、复查进度。"""
        latest = self.latest_snapshot(table_name)
        if latest is None:
            return {"table_name": table_name, "has_result": False}
        reviews = self._reviews.get(table_name, [])
        review_progress = {
            status.value: sum(1 for i in reviews if i.status == status)
            for status in ReviewStatus
        }
        return {
            "has_result": True,
            "table_name": table_name,
            "plan_version": latest.plan_version,
            "result_kind": latest.status.value,       # 暂算值 / 确认值
            "tier": latest.tier.value,
            "weighted_bad_rate": round(latest.weighted_bad_rate, 6),
            "naive_bad_rate": round(latest.naive_bad_rate, 6),
            "table_coverage": round(latest.table_coverage, 6),
            "partition_results": [p.to_dict() for p in latest.partitions],
            "diagnostics": [d.to_dict() for d in latest.diagnostics],
            "review_progress": review_progress,
            "snapshot_count": len(self._snapshots.get(table_name, [])),
            "computed_at": latest.computed_at,
        }


def _iso_now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")
