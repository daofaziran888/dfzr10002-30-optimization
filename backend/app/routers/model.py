"""分析模型管理接口：维护分析模型，覆盖部署、降级、兼容检查与调用状态查询。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.model import ModelService

router = APIRouter(prefix="/api/model", tags=["分析模型管理"])

service = ModelService()

LIST_FIELDS = ["模型编号", "模型名称", "模型类型", "输入字段", "参数配置", "训练数据集", "模型版本", "部署状态"]
STATUSES = ["草稿", "训练中", "已部署", "已下线"]


def _check_size(size: int) -> None:
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出分析模型管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "model", "total": total, "items": items}


@router.get("/differences", response_model=PageResult[dict])
def list_differences(
    keyword: str | None = Query(default=None, description="按模型编号检索"),
    status: str | None = Query(default=None, description="待处理、已恢复、已降级"),
    version: str | None = Query(default=None, description="按模型版本定位"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """兼容差异清单；筛选条件与模型列表保持一致。"""
    _check_size(size)
    items, total = service.list_differences(keyword=keyword, status=status, version=version, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按模型编号检索"),
    status: str | None = Query(default=None, description="草稿、训练中、已部署、已下线，也支持部署状态"),
    version: str | None = Query(default=None, description="按模型版本定位"),
    order: str | None = Query(default="asc", description="翻页方向：asc 或 desc"),
    sort: str | None = Query(default="id", description="排序字段：id（默认，按登记顺序）或 version（按版本先后）"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按模型编号、状态与版本定位分析模型列表；翻页后仍可定位到同一模型。"""
    _check_size(size)
    items, total = service.list_entries(
        keyword=keyword, status=status, version=version, order=order, sort=sort, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: str) -> dict:
    """读取单条分析模型明细（支持数值 id 或 MODEL-003 这类模型编号）。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"分析模型 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/compatibility")
def compatibility(
    entry_id: str,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    version: str | None = Query(default=None),
) -> dict[str, Any]:
    """字段差异与版本兼容报告：包含当前差异、历史差异和各版本检查结论。"""
    report = service.compatibility_report(entry_id, keyword=keyword, status=status, version=version)
    if report is None:
        raise HTTPException(status_code=404, detail=f"分析模型 {entry_id} 不存在或已归档")
    return report


@router.get("/{entry_id}/deployments")
def deployments(entry_id: str) -> dict[str, Any]:
    """部署记录：查看部署、降级、下线历史，与当前生效版本对齐。"""
    records = service.deployment_records(entry_id)
    if records is None:
        raise HTTPException(status_code=404, detail=f"分析模型 {entry_id} 不存在或已归档")
    model = service.resolve(entry_id) or {}
    return {
        "模型编号": model.get("模型编号"),
        "生效版本": model.get("生效版本"),
        "稳定版本": model.get("稳定版本"),
        "当前模型版本": model.get("模型版本"),
        "部署状态": model.get("部署状态"),
        "items": records,
        "total": len(records),
    }


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条分析模型，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="分析模型已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: str, payload: EntryPayload) -> ActionResult:
    """部署、降级、兼容检查、下线与归档；不兼容时阻止切换并返回可兼容稳定版本。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message, ok = service.run_action(entry_id, action, payload.values)
    return ActionResult(ok=ok, message=message, entry=entry)


@router.post("/{entry_id}/deploy", response_model=ActionResult)
def deploy(entry_id: str, payload: EntryPayload) -> ActionResult:
    """部署前的兼容检查入口：字段、参数配置、数据集版本不一致时阻止或提示降级。"""
    entry, message, ok = service.run_action(entry_id, "部署模型", payload.values)
    return ActionResult(ok=ok, message=message, entry=entry)


@router.post("/{entry_id}/fallback", response_model=ActionResult)
def fallback(entry_id: str, payload: EntryPayload) -> ActionResult:
    """降级到上一稳定版本；重复请求与超时重试都不会重复切换。"""
    entry, message, ok = service.run_action(entry_id, "降级模型", payload.values)
    return ActionResult(ok=ok, message=message, entry=entry)


@router.post("/{entry_id}/invoke", response_model=ActionResult)
def invoke(entry_id: str, payload: EntryPayload) -> ActionResult:
    """模拟数据服务调用：旧字段请求被拒绝，或在允许时切到上一稳定版本。"""
    entry, message, ok = service.invoke(entry_id, payload.values)
    return ActionResult(ok=ok, message=message, entry=entry)
