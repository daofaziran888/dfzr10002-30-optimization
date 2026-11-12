"""数据服务发布业务规则：状态流转、字段校验、生效模型版本同步与筛选口径。

数据服务清单不单独维护「模型版本」这个副本：查询时按关联模型实时计算
生效版本、模型版本记录与调用状态，避免模型降级后服务清单还停留在旧版本。
"""
from __future__ import annotations

from typing import Any

from app.services import compatibility as compat
from app.store import store

MODULE = "service"
REQUIRED_FIELDS = ["服务编号", "服务名称", "输出数据集"]
STATUS_ORDER = ["草稿", "待审批", "已发布", "已停用"]
ACTION_RULES = {"提交审批": "待审批", "发布服务": "已发布", "停用服务": "已停用"}
NEGATIVE_ACTIONS = ["停用服务"]


class ServiceService:
    def _model(self, row: dict[str, Any]) -> dict[str, Any] | None:
        code = str(row.get("关联模型") or "")
        return compat.find_model_by_code(code) if code else None

    def _decorate(self, row: dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        model = self._model(row)
        if model is not None:
            history = compat.model_records(model)
            item["关联模型"] = model.get("模型编号")
            item["生效模型版本"] = model.get("生效版本")
            item["稳定模型版本"] = model.get("稳定版本")
            item["模型版本记录"] = [str(record.get("版本")) for record in history]
            # 调用清单里的「模型版本」就是服务当前生效的版本，和模型详情保持同一口径；
            # 最新构建版本另存为「最新模型版本」。
            item["模型版本"] = model.get("生效版本")
            item["最新模型版本"] = model.get("模型版本")
            item["模型部署状态"] = model.get("部署状态")
            item["服务调用状态"] = "已停用" if row.get("status") == "已停用" else compat.derive_call_status(model)
        else:
            item.setdefault("生效模型版本", None)
            item.setdefault("模型版本记录", [])
            item.setdefault("服务调用状态", "未关联模型")
        return item

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        version: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        items = [self._decorate(row) for row in rows]
        if keyword:
            items = [row for row in items if keyword in str(row.get("服务编号", ""))]
        if status:
            items = [
                row for row in items
                if str(row.get("status")) == status or str(row.get("服务调用状态")) == status
            ]
        if version:
            items = [row for row in items if str(row.get("生效模型版本")) == str(version)]
        total = len(items)
        start = max(page - 1, 0) * size
        return items[start:start + size], total

    def get_entry(self, entry_id: Any) -> dict[str, Any] | None:
        row = self.resolve(entry_id)
        if row is None:
            return None
        return self._decorate(row)

    def resolve(self, entry_id: Any) -> dict[str, Any] | None:
        text = str(entry_id or "").strip()
        if not text:
            return None
        if text.isdigit():
            found = store.find(MODULE, int(text))
            if found is not None:
                return found
        for row in store.rows(MODULE):
            if str(row.get("服务编号")) == text:
                return row
        return None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["关联模型"] = values.get("关联模型")
        model = self._model(entry)
        entry["生效模型版本"] = model.get("生效版本") if model else None
        entry["模型版本记录"] = [str(record.get("版本")) for record in compat.model_records(model)] if model else []
        entry["服务调用状态"] = compat.derive_call_status(model) if model else "未关联模型"
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: Any, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = self.resolve(entry_id)
        if entry is None:
            return None, f"数据服务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据服务发布可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        if action == "停用服务":
            entry["服务调用状态"] = "已停用"
        elif action == "发布服务":
            model = self._model(entry)
            entry["服务调用状态"] = compat.derive_call_status(model) if model else entry.get("服务调用状态", "未关联模型")
        return self._decorate(entry), f"数据服务已{action}"
