"""分区抽样质量门禁场景测试。

复现需求中的完整业务剧本：

  同一张表两个分区按不同概率抽样（地区小分区 50%，大分区 10%）；
  大分区的一批样本迟到 → 复查人员先拿到局部风险，但整表只能给暂算值；
  补报小分区、补齐大分区 → 同一质量接口按实际覆盖重算，最终给出确认值；
  阈值变更 → 受影响分区重算，并另存当时使用的抽样数与坏行数；
  全程核对分层抽样配置、加权坏行率、诊断结果与复查明细。

运行：
  cd backend && python3 -I -m unittest tests.test_partition_sampling -v
"""
from __future__ import annotations

import unittest

from app.engine.partition_sampling import (
    DiagnosticCode,
    EvalStatus,
    PartitionSpec,
    PartitionSamplingCalculator,
    QualityThresholds,
    QualityTier,
    ReviewStatus,
    SamplingQualityService,
)

TABLE = "dw.dwd_order_wide"
SMALL = "region=west"   # 地区小分区
LARGE = "region=east"   # 大分区


def make_rows(prefix: str, n: int, bad_indexes: set[int]) -> list[dict]:
    """构造 n 条样本，bad_indexes 中的序号为坏行。"""
    return [
        {"row_id": f"{prefix}-{i:03d}", "is_bad": i in bad_indexes,
         "detail": {"reason": "NULL/空值"} if i in bad_indexes else {}}
        for i in range(n)
    ]


class PartitionSamplingScenarioTest(unittest.TestCase):
    def setUp(self) -> None:
        # 单调递增的固定时钟，便于断言快照时间字段存在且稳定
        counter = {"n": 0}

        def clock() -> str:
            counter["n"] += 1
            minute, second = divmod(counter["n"], 60)
            return f"2026-11-21T{8 + minute // 60:02d}:{minute % 60:02d}:{second:02d}"

        self.svc = SamplingQualityService(clock=clock)

        # 明示阈值：加权坏行率 <=2% PASS，(2%,5%] WARN，>5% FAIL
        self.svc.create_plan(
            plan_id="PLAN-ORDER-001",
            table_name=TABLE,
            thresholds=QualityThresholds(pass_max=0.02, fail_max=0.05,
                                         min_table_coverage=0.8),
            partitions=[
                # 小分区：总体 1000 行，50% 抽样 → 计划 500 个样本
                PartitionSpec(SMALL, sampling_rate=0.5, population_rows=1000,
                              planned_sample_n=500, min_coverage=0.8),
                # 大分区：总体 5000 行，10% 抽样 → 计划 500 个样本
                PartitionSpec(LARGE, sampling_rate=0.1, population_rows=5000,
                              planned_sample_n=500, min_coverage=0.8),
            ],
            note="两层不等比例抽样",
        )

    # ------------------------------------------------------------------
    # 阶段 1：只收到小分区一批样本（含坏行），大分区整批未到
    # ------------------------------------------------------------------
    def test_01_partial_small_partition_is_provisional(self) -> None:
        # 两个批次先登记：大分区批次承诺 500 个样本但一个都没到
        self.svc.register_batch(TABLE, "B-S1", SMALL, 1, expected_n=300)
        self.svc.register_batch(TABLE, "B-L1", LARGE, 1, expected_n=500)

        # 小分区先到 300/500，其中 18 个坏行 → 原始坏行率 6%
        self.svc.ingest_rows(TABLE, SMALL, 1, "B-S1",
                             make_rows("s1", 300, set(range(18))))

        result = self.svc.evaluate(TABLE)

        # 整表只能是暂算值：大分区无样本、小分区覆盖也不够
        self.assertEqual(result.status, EvalStatus.PROVISIONAL)
        # 当前只有小分区数据，加权坏行率 6% > fail 阈值 5% → FAIL（但是暂算值）
        self.assertEqual(result.tier, QualityTier.FAIL)

        small = next(p for p in result.partitions if p.partition == SMALL)
        large = next(p for p in result.partitions if p.partition == LARGE)

        # 小分区：覆盖率 60% < 80%，暂算；加权坏行率与原始一致（单分区等概率）
        self.assertEqual(small.status, EvalStatus.PROVISIONAL)
        self.assertAlmostEqual(small.coverage, 0.6)
        self.assertAlmostEqual(small.weighted_bad_rate, 0.06)
        self.assertEqual(small.bad_sample_n, 18)
        self.assertEqual(small.received_sample_n, 300)
        self.assertTrue(
            any(d.code == DiagnosticCode.PARTITION_UNDER_COVERAGE
                for d in small.diagnostics)
        )

        # 大分区：无样本，不按通过算，给 NO_DATA 与缺样本诊断
        self.assertEqual(large.tier, QualityTier.NO_DATA)
        self.assertEqual(large.status, EvalStatus.PROVISIONAL)
        self.assertEqual(large.received_sample_n, 0)
        self.assertEqual(large.weighted_bad_rate, 0.0)
        self.assertTrue(
            any(d.code == DiagnosticCode.PARTITION_NO_SAMPLE
                for d in result.diagnostics if d.partition == LARGE)
        )

        # 迟到批次诊断挂在整表上
        self.assertTrue(
            any(d.code == DiagnosticCode.PARTITION_LATE_BATCH
                and d.partition == LARGE for d in result.diagnostics)
        )

        # 局部风险已可见：复查明细已为 18 个坏行建单
        reviews = self.svc.list_reviews(TABLE)
        self.assertEqual(len(reviews), 18)
        self.assertTrue(all(r.status == ReviewStatus.PENDING for r in reviews))
        self.assertTrue(all(r.partition == SMALL for r in reviews))
        # 坏行来源连到分区和样本
        first = reviews[0]
        self.assertEqual(first.batch_id, "B-S1")
        self.assertEqual(first.plan_version, 1)
        self.assertEqual(first.reason, "NULL/空值")

    # ------------------------------------------------------------------
    # 阶段 2：小分区补报到齐（覆盖率 100%），大分区仍缺失 → 仍暂算
    # ------------------------------------------------------------------
    def test_02_small_partition_completed_large_still_missing(self) -> None:
        self.svc.register_batch(TABLE, "B-S1", SMALL, 1, expected_n=300)
        self.svc.register_batch(TABLE, "B-S2", SMALL, 1, expected_n=200)
        self.svc.register_batch(TABLE, "B-L1", LARGE, 1, expected_n=500)
        self.svc.ingest_rows(TABLE, SMALL, 1, "B-S1",
                             make_rows("s1", 300, set(range(18))))

        first = self.svc.evaluate(TABLE)
        self.assertEqual(first.status, EvalStatus.PROVISIONAL)

        # 补报小分区剩余 200 个样本（无坏行），标记为迟到补报
        self.svc.ingest_rows(TABLE, SMALL, 1, "B-S2",
                             make_rows("s2", 200, set()), is_late=True)
        result = self.svc.evaluate(TABLE)

        small = next(p for p in result.partitions if p.partition == SMALL)
        # 小分区覆盖达标，分层结论可以确认；但整表仍因大分区缺失而暂算
        self.assertEqual(small.status, EvalStatus.CONFIRMED)
        self.assertAlmostEqual(small.coverage, 1.0)
        self.assertEqual(small.received_sample_n, 500)
        self.assertEqual(small.bad_sample_n, 18)
        self.assertAlmostEqual(small.weighted_bad_rate, 18 / 500)
        self.assertEqual(result.status, EvalStatus.PROVISIONAL)

        # 报表能辨别暂算值
        report = self.svc.quality_report(TABLE)
        self.assertEqual(report["result_kind"], EvalStatus.PROVISIONAL.value)
        self.assertTrue(
            any(d["code"] == DiagnosticCode.PARTITION_NO_SAMPLE.value
                for d in report["diagnostics"] if d["partition"] == LARGE)
        )

    # ------------------------------------------------------------------
    # 阶段 3：大分区样本部分到达（40%），整表仍暂算
    # ------------------------------------------------------------------
    def test_03_large_partition_partial_arrival(self) -> None:
        self._prepare_full_arrival(partial_large=True)
        result = self.svc.evaluate(TABLE)

        large = next(p for p in result.partitions if p.partition == LARGE)
        self.assertEqual(large.received_sample_n, 200)
        self.assertEqual(large.status, EvalStatus.PROVISIONAL)
        self.assertAlmostEqual(large.coverage, 0.4)
        self.assertEqual(result.status, EvalStatus.PROVISIONAL)
        self.assertTrue(
            any(d.code == DiagnosticCode.PARTITION_UNDER_COVERAGE
                and d.partition == LARGE for d in result.diagnostics)
        )

    # ------------------------------------------------------------------
    # 阶段 4：大分区补齐 → 整表确认，加权坏行率区别于朴素合并
    # ------------------------------------------------------------------
    def test_04_full_coverage_confirmed_weighted_rate(self) -> None:
        self._prepare_full_arrival(partial_large=False)
        result = self.svc.evaluate(TABLE)

        self.assertEqual(result.status, EvalStatus.CONFIRMED)
        small = next(p for p in result.partitions if p.partition == SMALL)
        large = next(p for p in result.partitions if p.partition == LARGE)
        self.assertEqual(small.status, EvalStatus.CONFIRMED)
        self.assertEqual(large.status, EvalStatus.CONFIRMED)

        # 小分区坏行率 18/500=3.6%；大分区 50/500=10%
        self.assertAlmostEqual(small.weighted_bad_rate, 0.036)
        self.assertAlmostEqual(large.weighted_bad_rate, 0.10)

        # 加权整表坏行率：按总体规模加权
        # 估计坏行量 = 1000*0.036 + 5000*0.10 = 36 + 500 = 536；总体 6000
        expected_weighted = 536 / 6000
        self.assertAlmostEqual(result.weighted_bad_rate, expected_weighted, places=6)
        # 朴素合并（坏样本数/总样本数）= 68/1000 = 6.8%，与加权值明显不同
        self.assertAlmostEqual(result.naive_bad_rate, 68 / 1000)
        self.assertNotAlmostEqual(result.weighted_bad_rate, result.naive_bad_rate)

        # 加权后 8.93% > fail 阈值 5% → FAIL
        self.assertEqual(result.tier, QualityTier.FAIL)
        self.assertAlmostEqual(result.table_coverage, 1.0)

        # 报表转为确认值
        report = self.svc.quality_report(TABLE)
        self.assertEqual(report["result_kind"], EvalStatus.CONFIRMED.value)
        self.assertEqual(report["tier"], QualityTier.FAIL.value)

        # 所有缺失类诊断已消除（迟到补报诊断以 resolved 形式留痕）
        active_codes = {
            d.code for d in result.diagnostics if not d.resolved
        }
        self.assertNotIn(DiagnosticCode.PARTITION_NO_SAMPLE, active_codes)
        self.assertNotIn(DiagnosticCode.PARTITION_UNDER_COVERAGE, active_codes)
        self.assertNotIn(DiagnosticCode.TABLE_UNDER_COVERAGE, active_codes)

    # ------------------------------------------------------------------
    # 阶段 5：阈值变更 → 重算，快照另存当时抽样数/坏行数
    # ------------------------------------------------------------------
    def test_05_threshold_revision_recalculates_and_keeps_snapshots(self) -> None:
        self._prepare_full_arrival(partial_large=False)
        before = self.svc.evaluate(TABLE)
        self.assertEqual(before.tier, QualityTier.FAIL)

        # 阈值收紧：fail_max 从 5% 降到 3%（FAIL 边界更低，结论仍 FAIL，
        # 但快照需记录新阈值）；再测一次放宽到 12% 会变 PASS
        self.svc.revise_plan(
            TABLE,
            thresholds=QualityThresholds(pass_max=0.01, fail_max=0.03,
                                         min_table_coverage=0.8),
            note="坏行率红线收紧到3%",
        )
        tightened = self.svc.evaluate(TABLE)
        self.assertEqual(tightened.plan_version, 2)
        self.assertEqual(tightened.tier, QualityTier.FAIL)
        # 受影响分区全部按新阈值重算，且快照保存当时使用的抽样数/坏行数
        for p in tightened.partitions:
            snap = p.calc_snapshot
            self.assertEqual(snap["plan_version"], 2)
            self.assertIn("used_sample_n", snap)
            self.assertIn("used_bad_n", snap)
            if p.partition == SMALL:
                self.assertEqual(snap["used_sample_n"], 500)
                self.assertEqual(snap["used_bad_n"], 18)
            else:
                self.assertEqual(snap["used_sample_n"], 500)
                self.assertEqual(snap["used_bad_n"], 50)

        # 放宽阈值 → 同一批数据重算后变 PASS，但坏行率本身不变
        self.svc.revise_plan(
            TABLE,
            thresholds=QualityThresholds(pass_max=0.12, fail_max=0.15,
                                         min_table_coverage=0.8),
            note="整改观察期临时放宽",
        )
        relaxed = self.svc.evaluate(TABLE)
        self.assertEqual(relaxed.plan_version, 3)
        self.assertEqual(relaxed.tier, QualityTier.PASS)
        self.assertAlmostEqual(relaxed.weighted_bad_rate,
                               tightened.weighted_bad_rate)

        # 历史快照完整保留（v1、v2、v3 各一次）
        snaps = self.svc.snapshots(TABLE)
        self.assertEqual([s["plan_version"] for s in snaps], [1, 2, 3])
        v1 = snaps[0]
        self.assertEqual(v1["calc_snapshot"]["thresholds"]["fail_max"], 0.05)
        self.assertEqual(snaps[1]["calc_snapshot"]["thresholds"]["fail_max"], 0.03)
        self.assertEqual(snaps[2]["calc_snapshot"]["thresholds"]["fail_max"], 0.15)
        # 旧计划版本仍可查
        self.assertEqual(self.svc.plan_version(TABLE, 1).thresholds.fail_max, 0.05)

    # ------------------------------------------------------------------
    # 阶段 6：复查明细核验与整改流转
    # ------------------------------------------------------------------
    def test_06_review_flow(self) -> None:
        self._prepare_full_arrival(partial_large=False)
        self.svc.evaluate(TABLE)

        reviews = self.svc.list_reviews(TABLE)
        self.assertEqual(len(reviews), 68)  # 18（小分区）+ 50（大分区）

        # 值班人员核验第一条：确认坏行
        first = reviews[0]
        confirmed = self.svc.submit_review(
            TABLE, first.review_id, "confirm", assignee="oncall-zhang")
        self.assertEqual(confirmed.status, ReviewStatus.CONFIRMED)
        self.assertEqual(confirmed.assignee, "oncall-zhang")

        # 非法流转拦截：PENDING 才能 confirm，CONFIRMED 不能再 confirm
        with self.assertRaises(ValueError):
            self.svc.submit_review(TABLE, first.review_id, "confirm")

        # 提交整改并复核通过
        rectified = self.svc.submit_review(
            TABLE, first.review_id, "rectify",
            rectification="回填 order_id 并重跑分区 ETL")
        self.assertEqual(rectified.status, ReviewStatus.RECTIFIED)
        self.assertIn("重跑", rectified.rectification)

        # 第二条判定误报驳回
        second = reviews[1]
        dismissed = self.svc.submit_review(TABLE, second.review_id, "dismiss")
        self.assertEqual(dismissed.status, ReviewStatus.DISMISSED)

        # 按状态筛选与报表复查进度
        pending = self.svc.list_reviews(TABLE, ReviewStatus.PENDING)
        self.assertEqual(len(pending), 66)
        progress = self.svc.quality_report(TABLE)["review_progress"]
        self.assertEqual(progress["RECTIFIED"], 1)
        self.assertEqual(progress["DISMISSED"], 1)
        self.assertEqual(progress["PENDING"], 66)

    # ------------------------------------------------------------------
    # 阶段 7：补报样本携带旧计划版本，按对应版本抽样率计权
    # ------------------------------------------------------------------
    def test_07_late_rows_use_their_own_plan_version_weight(self) -> None:
        # v1：小分区抽样率 50%
        self.svc.register_batch(TABLE, "B-S1", SMALL, 1, expected_n=500)
        self.svc.register_batch(TABLE, "B-L1", LARGE, 1, expected_n=500)
        self.svc.ingest_rows(TABLE, SMALL, 1, "B-S1",
                             make_rows("s1", 500, set(range(18))))
        self.svc.ingest_rows(TABLE, LARGE, 1, "B-L1",
                             make_rows("l1", 500, set(range(50))))
        self.svc.evaluate(TABLE)

        # v2：小分区抽样率收紧到 25%（计划抽样数变为 250）
        self.svc.revise_plan(
            TABLE,
            partitions=[
                PartitionSpec(SMALL, sampling_rate=0.25, population_rows=1000,
                              planned_sample_n=250, min_coverage=0.8),
                PartitionSpec(LARGE, sampling_rate=0.1, population_rows=5000,
                              planned_sample_n=500, min_coverage=0.8),
            ],
            note="小分区抽样率调整",
        )
        # 迟到的补报样本仍按 v1 的 50% 抽取 → 权重应为 2.0 而非 4.0
        late = self.svc.ingest_rows(
            TABLE, SMALL, 1, "B-S1",
            [{"row_id": "s1-late-001", "is_bad": True,
              "detail": {"reason": "补报坏行"}}],
            is_late=True,
        )
        self.assertAlmostEqual(late[0].weight, 1 / 0.5)

        result = self.svc.evaluate(TABLE)
        # 版本不一致诊断要出现，但样本不被丢弃
        self.assertTrue(
            any(d.code == DiagnosticCode.PLAN_VERSION_MISMATCH
                and d.partition == SMALL for d in result.diagnostics)
        )
        small = next(p for p in result.partitions if p.partition == SMALL)
        self.assertEqual(small.received_sample_n, 501)
        self.assertEqual(small.bad_sample_n, 19)

    # ------------------------------------------------------------------
    # 纯计算单元：阈值分层与计权系数
    # ------------------------------------------------------------------
    def test_08_threshold_classification_and_weight(self) -> None:
        th = QualityThresholds(pass_max=0.02, fail_max=0.05)
        self.assertEqual(th.classify(0.01), QualityTier.PASS)
        self.assertEqual(th.classify(0.02), QualityTier.PASS)   # 闭区间
        self.assertEqual(th.classify(0.0201), QualityTier.WARN)
        self.assertEqual(th.classify(0.05), QualityTier.WARN)
        self.assertEqual(th.classify(0.0501), QualityTier.FAIL)

        self.assertAlmostEqual(
            PartitionSamplingCalculator.row_weight(0.1), 10.0)
        self.assertAlmostEqual(
            PartitionSamplingCalculator.row_weight(0.5), 2.0)

        with self.assertRaises(ValueError):
            QualityThresholds(pass_max=0.1, fail_max=0.01)  # 倒挂
        with self.assertRaises(ValueError):
            PartitionSpec("p", sampling_rate=0.0, population_rows=10,
                          planned_sample_n=5)

    # ------------------------------------------------------------------
    # 检测规则全部通过（0 坏行），但覆盖不足时整表仍只能是暂算值
    # ------------------------------------------------------------------
    def test_09_passing_rules_but_insufficient_coverage_stays_provisional(self) -> None:
        self.svc.register_batch(TABLE, "B-S1", SMALL, 1, expected_n=500)
        self.svc.register_batch(TABLE, "B-L1", LARGE, 1, expected_n=500)
        # 小分区样本全合格，大分区一个没到
        self.svc.ingest_rows(TABLE, SMALL, 1, "B-S1",
                             make_rows("s1", 300, set()))
        result = self.svc.evaluate(TABLE)

        # 坏行率为 0，分层是 PASS，但覆盖不够 → 仍是暂算值，不能确认放行
        small = next(p for p in result.partitions if p.partition == SMALL)
        self.assertEqual(small.tier, QualityTier.PASS)
        self.assertEqual(small.status, EvalStatus.PROVISIONAL)
        self.assertEqual(result.tier, QualityTier.PASS)
        self.assertEqual(result.status, EvalStatus.PROVISIONAL)

        # 补齐两分区后（仍 0 坏行）才允许确认
        self.svc.ingest_rows(TABLE, SMALL, 1, "B-S1",
                             make_rows("s2", 200, set()), is_late=True)
        self.svc.ingest_rows(TABLE, LARGE, 1, "B-L1",
                             make_rows("l1", 500, set()), is_late=True)
        full = self.svc.evaluate(TABLE)
        self.assertEqual(full.tier, QualityTier.PASS)
        self.assertEqual(full.status, EvalStatus.CONFIRMED)

    # ------------------------------------------------------------------
    # 质量执行汇总跨模块读取分层结果与复查进度
    # ------------------------------------------------------------------
    def test_10_validation_run_service_reads_layered_summary(self) -> None:
        from app.services.validation_run import ValidationRunService

        self._prepare_full_arrival(partial_large=False)
        self.svc.evaluate(TABLE)
        # 先处理一条复查单，验证汇总里的复查进度
        reviews = self.svc.list_reviews(TABLE)
        self.svc.submit_review(TABLE, reviews[0].review_id, "confirm",
                               assignee="oncall-li")

        # 让共享注册点指向本次测试构造的服务实例
        from app.engine import quality_registry
        original = quality_registry.sampling_quality_service
        quality_registry.sampling_quality_service = self.svc
        try:
            summary = ValidationRunService().layered_quality_summary(TABLE)
        finally:
            quality_registry.sampling_quality_service = original

        self.assertEqual(summary["source"], "partition_sampling")
        self.assertEqual(summary["result_kind"], EvalStatus.CONFIRMED.value)
        self.assertEqual(summary["result_kind_text"], "确认值")
        self.assertEqual(summary["review_total"], 68)
        self.assertEqual(summary["review_pending"], 67)
        self.assertEqual(len(summary["layered_partitions"]), 2)
        by_partition = {p["partition"]: p for p in summary["layered_partitions"]}
        self.assertAlmostEqual(
            by_partition[LARGE]["weighted_bad_rate"], 0.10)
        self.assertEqual(
            by_partition[SMALL]["review_progress"]["已确认"], 1)

        # 没有抽样计划的表回退旧口径，不抛异常
        fallback = ValidationRunService().layered_quality_summary("no.such.table")
        self.assertEqual(fallback["source"], "legacy")

    # ------------------------------------------------------------------
    # 重复建计划必须显式拒绝，防止版本历史被静默覆盖
    # ------------------------------------------------------------------
    def test_11_duplicate_plan_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.svc.create_plan(
                plan_id="PLAN-DUP",
                table_name=TABLE,
                thresholds=QualityThresholds(0.02, 0.05),
                partitions=[
                    PartitionSpec(SMALL, 0.5, 1000, 500),
                    PartitionSpec(LARGE, 0.1, 5000, 500),
                ],
            )
        # 原计划仍是 v1，未被覆盖
        self.assertEqual(self.svc.current_plan(TABLE).version, 1)

    # ---- 剧本夹具：两分区完整到达（可切换大分区是否只到一部分） ----
    def _prepare_full_arrival(self, *, partial_large: bool) -> None:
        self.svc.register_batch(TABLE, "B-S1", SMALL, 1, expected_n=300)
        self.svc.register_batch(TABLE, "B-S2", SMALL, 1, expected_n=200)
        self.svc.register_batch(TABLE, "B-L1", LARGE, 1, expected_n=500)

        # 小分区：300 + 200 两批，共 18 坏行（坏行率 3.6%）
        self.svc.ingest_rows(TABLE, SMALL, 1, "B-S1",
                             make_rows("s1", 300, set(range(18))))
        self.svc.ingest_rows(TABLE, SMALL, 1, "B-S2",
                             make_rows("s2", 200, set()), is_late=True)

        # 大分区：先到 200（含 20 坏行），补齐时再到 300（含 30 坏行），共 10%
        self.svc.ingest_rows(TABLE, LARGE, 1, "B-L1",
                             make_rows("l1", 200, set(range(20))))
        if not partial_large:
            self.svc.ingest_rows(
                TABLE, LARGE, 1, "B-L1",
                make_rows("l2", 300, {20 + i for i in range(30)}),
                is_late=True,
            )


if __name__ == "__main__":
    unittest.main()
