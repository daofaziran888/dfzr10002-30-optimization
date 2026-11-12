"""分析数据集业务规则：字段版本、状态流转与依赖模型兼容刷新。

数据集字段变化（提交校验/启用配置/停用配置）后，会顺着
「模型部署 -> 数据服务调用」链路刷新依赖它的模型差异与服务清单；
当新字段与现有模型版本冲突时，保持/回落到可兼容的稳定版本。
"""
from __future__ import annotations

import re
from typing import Any

from app.services import compatibility as compat
from app.store import store

MODULE = "dataset"
REQUIRED_FIELDS = ["数据集编号", "数据集名称", "来源表名"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]


def _csv(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in re.split(r"[,，;；\s]+", str(value)) if item.strip()]


class DatasetService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("数据集编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: Any) -> dict[str, Any] | None:
        dataset = self.resolve(entry_id)
        if dataset is None:
            return None
        entry = dict(dataset)
        entry["字段列表"] = compat.dataset_fields(dataset)
        entry["依赖模型"] = [
            {
                "模型编号": model.get("模型编号"),
                "生效版本": model.get("生效版本"),
                "稳定版本": model.get("稳定版本"),
                "兼容状态": (compat.current_differences(model) or {}).get("状态") or "未部署",
            }
            for model in self.dependent_models(dataset)
        ]
        return entry

    def resolve(self, entry_id: Any) -> dict[str, Any] | None:
        text = str(entry_id or "").strip()
        if not text:
            return None
        if text.isdigit():
            found = store.find(MODULE, int(text))
            if found is not None:
                return found
        return compat.find_dataset(text)

    def dependent_models(self, dataset: dict[str, Any]) -> list[dict[str, Any]]:
        code = str(dataset.get("数据集编号"))
        return [row for row in store.rows(compat.MODEL_MODULE) if str(row.get("训练数据集")) == code]

    # ------------------------------------------------------------------
    # 字段版本
    # ------------------------------------------------------------------
    def publish_field_version(self, dataset: dict[str, Any], fields: list[str]) -> dict[str, Any] | None:
        """字段真正发生变化时追加一个数据集版本，供模型版本快照按版本比对。"""
        old_fields = compat.dataset_fields(dataset)
        if fields == old_fields:
            return None
        history = dataset.setdefault("字段版本历史", [])
        current = compat.current_dataset_version(dataset)
        latest_number = max(
            [compat.version_key(item.get("版本"))[-1] for item in history]
            + [compat.version_key(current)[-1] if current else 0],
            default=0,
        )
        label = f"DSV-2026-{latest_number + 1:02d}"
        record = {
            "版本": label,
            "字段列表": list(fields),
            "新增字段": [field for field in fields if field not in old_fields],
            "移除字段": [field for field in old_fields if field not in fields],
            "时间": compat.now_text(),
        }
        history.append(record)
        dataset["字段列表"] = list(fields)
        dataset["数据集版本"] = label
        return record

    def _refresh_dependents(self, dataset: dict[str, Any]) -> list[str]:
        """字段变更后沿「模型部署 -> 数据服务调用」链路刷新。

        这里只登记差异、更新服务调用状态，不擅自切换生效版本：
        版本切换必须由部署请求、调用请求或显式降级触发，并走同一套原子事务。
        冲突时在差异记录里标出「可兼容稳定版本」，作为降级目标。
        """
        messages: list[str] = []
        for model in self.dependent_models(dataset):
            evaluation = compat.refresh_model_compatibility(model)
            if not evaluation or not evaluation.get("阻断差异"):
                continue
            stable = compat.model_stable_for_model(model, dataset)
            if stable is not None:
                messages.append(
                    f"{model.get('模型编号')} 与数据集 {dataset.get('数据集编号')} 的字段变更冲突，"
                    f"可兼容稳定版本为 {stable.get('版本')}，调用状态已更新"
                )
            else:
                messages.append(
                    f"{model.get('模型编号')} 检测到 {len(evaluation['阻断差异'])} 条阻断差异，"
                    f"且没有可兼容稳定版本，服务调用状态已更新为版本待兼容"
                )
        return messages

    # ------------------------------------------------------------------
    # 登记与动作
    # ------------------------------------------------------------------
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["字段列表"] = _csv(values.get("字段列表"))
        entry["数据集版本"] = values.get("数据集版本") or "DSV-2026-01"
        entry["稳定版本"] = entry["数据集版本"]
        entry["字段版本历史"] = [{
            "版本": entry["数据集版本"],
            "字段列表": list(entry["字段列表"]),
            "新增字段": [],
            "移除字段": [],
            "时间": compat.now_text(),
        }]
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: Any, action: str, values: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str]:
        dataset = self.resolve(entry_id)
        if dataset is None:
            return None, f"数据集 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于分析数据集可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"

        values = values or {}
        fields = values.get("字段列表", values.get("fields"))
        if fields is not None:
            new_fields = _csv(fields)
            record = self.publish_field_version(dataset, new_fields)
            notes = self._refresh_dependents(dataset)
            if record is not None:
                dataset["最近刷新"] = compat.now_text()[:10]
                dataset["是否启用"] = "是"
                dataset["abnormal"] = False
                dataset["pending"] = False
                dataset["status"] = "已启用" if dataset.get("status") in {"草稿", "待校验"} else dataset.get("status")
                message = f"数据集已{action}；字段列表已发布为 {record.get('版本')}"
                if record.get("移除字段") or record.get("新增字段"):
                    message += f"（新增 {len(record.get('新增字段') or [])} 个、移除 {len(record.get('移除字段') or [])} 个字段）"
                if notes:
                    message += "；" + "；".join(notes)
                return dataset, message

        dataset["status"] = target
        dataset["pending"] = target != STATUS_ORDER[-1]
        dataset["abnormal"] = action in NEGATIVE_ACTIONS
        notes = self._refresh_dependents(dataset) if action == "启用配置" else []
        message = f"数据集已{action}"
        if notes:
            message += "；" + "；".join(notes)
        return dataset, message
