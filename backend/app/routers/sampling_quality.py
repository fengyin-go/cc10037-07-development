"""分区抽样质量门禁 API。

把 ``partition_sampling`` 引擎的能力暴露成 HTTP 接口，覆盖：
  * 抽样计划（含版本化修订）
  * 样本批次/样本行登记（迟到、补报）
  * 整表与分层质量评估（暂算值/确认值，加权坏行率）
  * 计划阈值变更后的重算与历史快照
  * 复查明细的核验与整改流转
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.engine.partition_sampling import (
    PartitionSpec,
    QualityThresholds,
    ReviewStatus,
)
from app.engine.quality_registry import sampling_quality_service as service

router = APIRouter(prefix="/api/sampling-quality", tags=["分区抽样质量门禁"])


def _build_thresholds(body: dict[str, Any]) -> QualityThresholds:
    try:
        return QualityThresholds(
            pass_max=float(body["pass_max"]),
            fail_max=float(body["fail_max"]),
            min_table_coverage=float(body.get("min_table_coverage", 0.8)),
        )
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"缺少阈值参数: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _build_specs(items: list[dict[str, Any]]) -> list[PartitionSpec]:
    try:
        return [
            PartitionSpec(
                partition=str(item["partition"]),
                sampling_rate=float(item["sampling_rate"]),
                population_rows=int(item["population_rows"]),
                planned_sample_n=int(item["planned_sample_n"]),
                min_coverage=float(item.get("min_coverage", 0.8)),
            )
            for item in items
        ]
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"分区配置缺少字段: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/plans")
def create_plan(body: dict[str, Any]) -> dict[str, Any]:
    """创建分区抽样计划（v1）。

    body:
      plan_id, table_name,
      thresholds: {pass_max, fail_max, min_table_coverage},
      partitions: [{partition, sampling_rate, population_rows,
                    planned_sample_n, min_coverage}]
    """
    try:
        plan = service.create_plan(
            plan_id=body["plan_id"],
            table_name=body["table_name"],
            partitions=_build_specs(body.get("partitions", [])),
            thresholds=_build_thresholds(body.get("thresholds", {})),
            note=body.get("note", ""),
        )
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"缺少必填参数: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _plan_dict(plan)


@router.post("/plans/{table_name}/revise")
def revise_plan(table_name: str, body: dict[str, Any]) -> dict[str, Any]:
    """修订计划：阈值或抽样配置变更，生成新版本（旧版本保留）。"""
    try:
        plan = service.revise_plan(
            table_name,
            partitions=_build_specs(body["partitions"]) if body.get("partitions") else None,
            thresholds=_build_thresholds(body["thresholds"]) if body.get("thresholds") else None,
            note=body.get("note", ""),
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _plan_dict(plan)


@router.get("/plans/{table_name}")
def list_plans(table_name: str) -> dict[str, Any]:
    """列出一张表的全部计划版本（阈值变更可追溯）。"""
    return {
        "table_name": table_name,
        "plans": [_plan_dict(p) for p in service.list_plans(table_name)],
    }


@router.post("/batches")
def register_batch(body: dict[str, Any]) -> dict[str, Any]:
    """登记一批应到样本（批次可先于样本登记，表达样本迟到）。"""
    try:
        batch = service.register_batch(
            table_name=body["table_name"],
            batch_id=body["batch_id"],
            partition=body["partition"],
            plan_version=int(body.get("plan_version", 1)),
            expected_n=int(body["expected_n"]),
        )
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"缺少必填参数: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _batch_dict(batch)


@router.post("/samples")
def ingest_samples(body: dict[str, Any]) -> dict[str, Any]:
    """登记实际收到的样本行（迟到/补报用 is_late 标记）。

    body:
      table_name, partition, plan_version, batch_id, is_late,
      rows: [{row_id, is_bad, detail: {reason, ...}}]
    """
    try:
        stored = service.ingest_rows(
            table_name=body["table_name"],
            partition=body["partition"],
            plan_version=int(body.get("plan_version", 1)),
            batch_id=body["batch_id"],
            rows=body.get("rows", []),
            is_late=bool(body.get("is_late", False)),
        )
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"缺少必填参数: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "ingested": len(stored),
        "batch_id": body["batch_id"],
        "partition": body["partition"],
        "plan_version": int(body.get("plan_version", 1)),
    }


@router.post("/evaluate/{table_name}")
def evaluate(table_name: str) -> dict[str, Any]:
    """按当前实际覆盖重算整表与分层质量结果（同接口反复调用）。"""
    try:
        result = service.evaluate(table_name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return result.to_dict()


@router.get("/evaluate/{table_name}/snapshots")
def snapshots(table_name: str) -> dict[str, Any]:
    """历史重算快照：每次另存当时使用的抽样数与坏行数。"""
    return {"table_name": table_name, "snapshots": service.snapshots(table_name)}


@router.get("/reviews/{table_name}")
def list_reviews(table_name: str, status: str | None = None) -> dict[str, Any]:
    """复查明细：坏行来源已连到分区、批次与样本。"""
    review_status = ReviewStatus(status) if status else None
    items = service.list_reviews(table_name, review_status)
    return {
        "table_name": table_name,
        "total": len(items),
        "items": [i.to_dict() for i in items],
    }


@router.post("/reviews/{table_name}/{review_id}")
def submit_review(table_name: str, review_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """值班人员核验/提交整改：confirm / rectify / dismiss。"""
    try:
        item = service.submit_review(
            table_name,
            review_id,
            action=body.get("action", ""),
            assignee=body.get("assignee"),
            rectification=body.get("rectification"),
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "item": item.to_dict()}


@router.get("/report/{table_name}")
def quality_report(table_name: str) -> dict[str, Any]:
    """质量报表：辨别暂算值与确认值，附分层结果与复查进度。"""
    return service.quality_report(table_name)


# ---- 序列化辅助 ----


def _plan_dict(plan: Any) -> dict[str, Any]:
    return {
        "plan_id": plan.plan_id,
        "table_name": plan.table_name,
        "version": plan.version,
        "created_at": plan.created_at,
        "note": plan.note,
        "thresholds": {
            "pass_max": plan.thresholds.pass_max,
            "fail_max": plan.thresholds.fail_max,
            "min_table_coverage": plan.thresholds.min_table_coverage,
        },
        "partitions": [
            {
                "partition": p.partition,
                "sampling_rate": p.sampling_rate,
                "population_rows": p.population_rows,
                "planned_sample_n": p.planned_sample_n,
                "min_coverage": p.min_coverage,
            }
            for p in plan.partitions
        ],
    }


def _batch_dict(batch: Any) -> dict[str, Any]:
    return {
        "batch_id": batch.batch_id,
        "partition": batch.partition,
        "plan_version": batch.plan_version,
        "expected_n": batch.expected_n,
        "received_n": batch.received_n,
        "arrived": batch.arrived,
        "is_late": batch.is_late,
        "received_at": batch.received_at,
    }
