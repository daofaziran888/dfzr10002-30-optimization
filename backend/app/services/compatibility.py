"""模型部署兼容检查与降级规则。

兼容规则（按优先级从硬到软）：

1. **输入字段必须存在**：模型某个版本声明的输入字段，如果当前数据集字段列表里
   已经不存在，判定为「字段缺失」，属于阻断级差异。这是字段漂移最典型的形态
   （例如数据集删掉 probability 后，仍带 probability 输入的版本不能继续接收请求）。
2. **数据集版本不能来自未来**：版本快照里记录的训练数据集版本，必须能在数据集
   的字段版本历史中找到，且不能新于数据集当前版本；找不到或新于当前版本都按
   阻断处理。旧于当前版本但字段仍被覆盖时只记为「数据集版本落后」警告，仍可兼容。
3. **参数配置必须与版本记录一致**：部署请求如果显式覆盖参数配置，必须与目标版本
   快照一致；不一致按阻断处理。未显式覆盖时，目标版本参数与当前生效版本参数不同
   只记为「参数配置变更」警告。
4. **字段新增不阻断**：数据集新增了模型未使用的字段时只记为警告，模型仍可兼容。

降级选择规则：当前生效版本出现阻断差异时，只从版本历史里挑选**严格早于当前
生效版本**、标记为稳定、且按上述规则兼容的版本中版本号最大的一个。降级只能
向下，不能越过稳定版本，因此重复触发不会来回切换。
"""
from __future__ import annotations

import re
import threading
from datetime import datetime
from typing import Any

from app.store import store

MODEL_MODULE = "model"
DATASET_MODULE = "dataset"
SERVICE_MODULE = "service"

DEPLOY_ACTIONS = {"部署模型", "部署", "部署模型版本", "deploy", "deploy_model"}
DEGRADE_ACTIONS = {"降级", "降级模型", "回退", "回滚", "切到稳定版本", "降级到上一稳定版本", "fallback", "rollback", "degrade"}
CHECK_ACTIONS = {"兼容检查", "检查兼容性", "兼容校验", "compatibility_check", "check_compatibility"}
RECOVER_ACTIONS = {"恢复稳定版本", "恢复", "recover"}

# 进程内部署锁：同一条模型的部署/降级串行执行。并发到达的两次降级里，
# 第一个完成状态切换后，第二个只会看到「已是稳定版本」并直接返回。
DEPLOY_LOCK = threading.RLock()


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def version_key(label: Any) -> tuple[int, ...]:
    """把 v2 / DSV-2026-02 / 任意带数字的版本号转成可比较的元组。"""
    numbers = [int(item) for item in re.findall(r"\d+", str(label or ""))]
    return tuple(numbers) if numbers else (10**9,)


def latest_version(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not records:
        return None
    return max(records, key=lambda item: version_key(item.get("版本")))


def latest_stable(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    stable = [item for item in records if item.get("稳定")]
    return latest_version(stable)


def find_model_by_code(code: str) -> dict[str, Any] | None:
    for row in store.rows(MODEL_MODULE):
        if str(row.get("模型编号")) == str(code):
            return row
    return None


def find_dataset(identifier: Any) -> dict[str, Any] | None:
    text = str(identifier or "").strip()
    if not text:
        return None
    for row in store.rows(DATASET_MODULE):
        if str(row.get("数据集编号")) == text:
            return row
    for row in store.rows(DATASET_MODULE):
        if str(row.get("id")) == text:
            return row
    for row in store.rows(DATASET_MODULE):
        if str(row.get("数据集名称")) == text:
            return row
    return None


def dataset_for_model(model: dict[str, Any]) -> dict[str, Any] | None:
    return find_dataset(model.get("训练数据集"))


def dataset_fields(dataset: dict[str, Any]) -> list[str]:
    raw = dataset.get("字段列表") or []
    if isinstance(raw, str):
        raw = [item.strip() for item in re.split(r"[,，;；\s]+", raw) if item.strip()]
    return [str(item) for item in raw]


def dataset_version_map(dataset: dict[str, Any]) -> dict[str, dict[str, Any]]:
    records = dataset.get("字段版本历史") or []
    return {str(item.get("版本")): item for item in records}


def current_dataset_version(dataset: dict[str, Any]) -> str:
    version = dataset.get("数据集版本")
    if version:
        return str(version)
    records = dataset.get("字段版本历史") or []
    return str((records[-1] if records else {}).get("版本") or "")


def dataset_version_position(dataset: dict[str, Any], version: Any) -> int | None:
    records = dataset.get("字段版本历史") or []
    for index, item in enumerate(records):
        if str(item.get("版本")) == str(version):
            return index
    return None


def normalized_params(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(key): value[key] for key in value}
    return {}


def params_difference(left: dict[str, Any], right: dict[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for key in sorted(set(left) | set(right)):
        if left.get(key) != right.get(key):
            changes.append({"参数": key, "版本记录值": left.get(key), "请求值": right.get(key)})
    return changes


def model_records(model: dict[str, Any]) -> list[dict[str, Any]]:
    records = model.get("版本历史")
    if not records:
        label = str(model.get("模型版本") or "v1")
        records = [{
            "版本": label,
            "输入字段": list(model.get("输入字段") or []),
            "参数配置": normalized_params(model.get("参数配置")),
            "数据集版本": current_dataset_version(dataset_for_model(model) or {}),
            "稳定": True,
            "发布时间": "",
        }]
        model["版本历史"] = records
    return records


def find_version_record(model: dict[str, Any], label: Any) -> dict[str, Any] | None:
    for record in model_records(model):
        if str(record.get("版本")) == str(label):
            return record
    return None


def evaluate_version(
    model: dict[str, Any],
    dataset: dict[str, Any] | None,
    record: dict[str, Any],
    *,
    params_override: Any = None,
    fields_override: Any = None,
    comparison_version: Any = None,
) -> dict[str, Any]:
    """按兼容规则评估一个模型版本，返回差异清单与可兼容结论。"""
    version = str(record.get("版本"))
    dataset_code = str((dataset or {}).get("数据集编号") or model.get("训练数据集") or "")
    dataset_version = str(record.get("数据集版本") or "")
    current_version = current_dataset_version(dataset or {})
    fields = dataset_fields(dataset or {})
    required = [str(item) for item in (record.get("输入字段") or [])]
    missing = [field for field in required if field not in fields]
    extra = [field for field in fields if field not in required]
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    if dataset is None:
        issues.append({
            "类型": "数据集缺失",
            "字段": "",
            "说明": f"训练数据集 {model.get('训练数据集')} 不存在，无法校验字段",
            "严重级别": "阻断",
        })

    for field in missing:
        issues.append({
            "类型": "字段缺失",
            "字段": field,
            "说明": f"当前数据集 {dataset_code} 已没有字段 {field}，版本 {version} 的输入无法补齐",
            "严重级别": "阻断",
            "模型版本": version,
            "数据集版本": current_version or dataset_version,
        })

    position = dataset_version_position(dataset or {}, dataset_version)
    current_position = dataset_version_position(dataset or {}, current_version)
    if dataset is not None and position is None:
        issues.append({
            "类型": "数据集版本",
            "字段": "",
            "说明": f"版本 {version} 记录的数据集版本 {dataset_version or '未记录'} 不在数据集 {dataset_code} 的字段版本历史中",
            "严重级别": "阻断",
            "模型版本": version,
            "数据集版本": dataset_version,
        })
    elif position is not None and current_position is not None and position > current_position:
        issues.append({
            "类型": "数据集版本",
            "字段": "",
            "说明": f"版本 {version} 依赖的数据集版本 {dataset_version} 新于当前数据集版本 {current_version}，不能回退部署",
            "严重级别": "阻断",
            "模型版本": version,
            "数据集版本": dataset_version,
        })
    elif position is not None and current_position is not None and position < current_position:
        warnings.append({
            "类型": "数据集版本落后",
            "字段": "",
            "说明": f"版本 {version} 基于 {dataset_version}，当前数据集已到 {current_version}；字段仍被覆盖，可按兼容稳定版本使用",
            "严重级别": "警告",
            "模型版本": version,
            "数据集版本": dataset_version,
        })

    for field in extra:
        warnings.append({
            "类型": "字段新增",
            "字段": field,
            "说明": f"当前数据集新增字段 {field}，版本 {version} 未使用，不影响兼容",
            "严重级别": "警告",
            "模型版本": version,
            "数据集版本": current_version,
        })

    record_params = normalized_params(record.get("参数配置"))
    override_params = normalized_params(params_override)
    if override_params:
        changes = params_difference(record_params, override_params)
        for change in changes:
            issues.append({
                "类型": "参数配置",
                "字段": change["参数"],
                "说明": f"部署请求参数 {change['参数']}={change['请求值']} 与版本记录值 {change['版本记录值']} 不一致",
                "严重级别": "阻断",
                "模型版本": version,
                "数据集版本": dataset_version,
            })

    compare_label = str(comparison_version or model.get("生效版本") or "")
    compare_record = find_version_record(model, compare_label) if compare_label else None
    if compare_record is not None and normalized_params(compare_record.get("参数配置")) != record_params:
        changes = params_difference(normalized_params(compare_record.get("参数配置")), record_params)
        warnings.append({
            "类型": "参数配置变更",
            "字段": "、".join(change["参数"] for change in changes),
            "说明": f"版本 {version} 的参数配置与当前生效版本 {compare_label} 不同",
            "严重级别": "警告",
            "模型版本": version,
            "数据集版本": dataset_version,
        })

    if fields_override is not None:
        requested = [str(item) for item in _as_list(fields_override)]
        if set(requested) != set(required):
            issues.append({
                "类型": "输入字段",
                "字段": "、".join(sorted(set(requested) ^ set(required))),
                "说明": f"部署请求声明的输入字段与版本 {version} 记录不一致",
                "严重级别": "阻断",
                "模型版本": version,
                "数据集版本": dataset_version,
            })

    differences = issues + warnings
    return {
        "版本": version,
        "模型编号": model.get("模型编号"),
        "训练数据集": model.get("训练数据集"),
        "数据集版本": dataset_version,
        "当前数据集版本": current_version,
        "当前数据集字段": fields,
        "输入字段": required,
        "缺失字段": missing,
        "新增字段": extra,
        "差异": differences,
        "阻断差异": issues,
        "警告差异": warnings,
        "兼容": not issues,
        "状态": "兼容" if not issues else ("阻断" if issues else ""),
    }


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[,，;；\s]+", value) if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def compatible_stable_version(
    model: dict[str, Any],
    dataset: dict[str, Any] | None,
    *,
    before_version: Any,
) -> dict[str, Any] | None:
    """在稳定版本里挑出可兼容且严格早于 before_version 的最高版本。

    只向下寻找，保证降级不会越过稳定版本；重复触发也不会切换到别的版本。
    """
    candidates = [
        record for record in model_records(model)
        if record.get("稳定") and version_key(record.get("版本")) < version_key(before_version)
    ]
    for record in sorted(candidates, key=lambda item: version_key(item.get("版本")), reverse=True):
        result = evaluate_version(model, dataset, record)
        if result["兼容"]:
            return record
    return None


def model_stable_for_model(model: dict[str, Any], dataset: dict[str, Any] | None) -> dict[str, Any] | None:
    """冲突场景下模型可用的稳定版本：优先「上一稳定版本」，其次向下的最高兼容稳定版本。"""
    label = model.get("上一稳定版本") or model.get("稳定版本")
    if label:
        record = find_version_record(model, label)
        if record is not None and record.get("稳定") and evaluate_version(model, dataset, record)["兼容"]:
            return record
    effective = model.get("生效版本")
    if not effective:
        return None
    return compatible_stable_version(model, dataset, before_version=effective)


def highest_compatible_stable(model: dict[str, Any], dataset: dict[str, Any] | None) -> dict[str, Any] | None:
    """版本历史里兼容结论为真的最高稳定版本（用于报告展示可回退目标）。"""
    candidates = [
        record for record in model_records(model)
        if record.get("稳定") and evaluate_version(model, dataset, record)["兼容"]
    ]
    return latest_version(candidates)


def current_differences(model: dict[str, Any]) -> dict[str, Any] | None:
    """当前生效版本相对数据集的实时兼容状态；未部署时返回 None。"""
    effective = model.get("生效版本")
    if not effective:
        return None
    record = find_version_record(model, effective)
    if record is None:
        return {
            "版本": str(effective),
            "兼容": False,
            "状态": "阻断",
            "差异": [{"类型": "版本记录缺失", "字段": "", "说明": f"生效版本 {effective} 不在版本历史中", "严重级别": "阻断"}],
            "阻断差异": [{"类型": "版本记录缺失", "字段": "", "说明": f"生效版本 {effective} 不在版本历史中", "严重级别": "阻断"}],
            "警告差异": [],
            "缺失字段": [],
            "新增字段": [],
        }
    return evaluate_version(model, dataset_for_model(model), record)


def next_difference_id(models: list[dict[str, Any]] | None = None) -> str:
    models = models if models is not None else store.rows(MODEL_MODULE)
    biggest = 0
    for row in models:
        for diff in row.get("兼容差异") or []:
            match = re.search(r"DIF-(\d+)", str(diff.get("差异编号") or ""))
            if match:
                biggest = max(biggest, int(match.group(1)))
    return f"DIF-{biggest + 1:04d}"


def _pending_fingerprint(diff: dict[str, Any]) -> tuple[str, str, str, str]:
    """同一个版本、同一类型、同一字段只保留一条差异记录，重复检测只更新状态。"""
    return (
        str(diff.get("模型版本") or ""),
        str(diff.get("类型") or ""),
        str(diff.get("字段") or ""),
        str(diff.get("数据集版本") or ""),
    )


def record_differences(
    model: dict[str, Any],
    evaluation: dict[str, Any],
    *,
    status: str = "待处理",
    compatible_version: str | None = None,
) -> list[dict[str, Any]]:
    """把阻断差异写入模型历史，按「版本+类型+字段+说明」去重。"""
    existing = model.setdefault("兼容差异", [])
    # 按「模型版本+类型+字段+数据集版本」去重：重复检测只更新同一条记录的状态，
    # 不会因为恢复说明变化而反复新增条目；恢复后仍保留原记录供事后查看。
    known = {_pending_fingerprint(item): item for item in existing}
    added: list[dict[str, Any]] = []
    dataset = dataset_for_model(model) or {}
    for diff in evaluation.get("阻断差异") or []:
        entry = dict(diff)
        entry.setdefault("模型编号", model.get("模型编号"))
        entry.setdefault("模型版本", evaluation.get("版本"))
        entry.setdefault("数据集编号", dataset.get("数据集编号"))
        entry.setdefault("数据集版本", evaluation.get("当前数据集版本"))
        fingerprint = _pending_fingerprint(entry)
        if fingerprint in known:
            prior = known[fingerprint]
            prior.update({
                "说明": entry.get("说明", prior.get("说明")),
                "严重级别": entry.get("严重级别", "阻断"),
                "状态": status,
                "检测时间": now_text(),
                "可兼容版本": compatible_version,
            })
            if status == "待处理":
                prior.pop("恢复时间", None)
            continue
        entry["差异编号"] = next_difference_id()
        entry["状态"] = status
        entry["检测时间"] = now_text()
        entry["可兼容版本"] = compatible_version
        existing.append(entry)
        known[fingerprint] = entry
        added.append(entry)
    return added


def resolve_differences(
    model: dict[str, Any],
    *,
    status: str = "已恢复",
    note: str = "",
    effective_version: str | None = None,
) -> int:
    """保留历史差异，只处理仍在待处理状态的记录。

    如果指定了 effective_version，则只把该版本的差异标为已兼容/已恢复；
    被放弃版本（如无法兼容的 v3）的差异保留为「已降级」，仍可在差异列表查看。
    """
    count = 0
    for diff in model.get("兼容差异") or []:
        if diff.get("状态") not in {"待处理", "阻断", "已降级"}:
            continue
        if effective_version and str(diff.get("模型版本")) != str(effective_version):
            if diff.get("状态") == "待处理":
                diff["状态"] = "已降级"
                diff["恢复时间"] = now_text()
                diff["说明"] = f"{diff.get('说明', '')}；已通过切换版本规避".strip("；")
            continue
        diff["状态"] = status
        diff["恢复时间"] = now_text()
        if note:
            diff["说明"] = f"{diff.get('说明', '')}；{note}".strip("；")
        count += 1
    return count


def next_deploy_id() -> str:
    biggest = 0
    for row in store.rows(MODEL_MODULE):
        for record in row.get("部署记录") or []:
            match = re.search(r"DEP-(\d+)", str(record.get("部署编号") or ""))
            if match:
                biggest = max(biggest, int(match.group(1)))
    return f"DEP-{biggest + 1:04d}"


def append_deploy_record(
    model: dict[str, Any],
    *,
    version: str,
    effective_version: str | None,
    action: str,
    result: str,
    dataset: dict[str, Any] | None,
    note: str = "",
) -> dict[str, Any]:
    record = {
        "部署编号": next_deploy_id(),
        "模型编号": model.get("模型编号"),
        "版本": version,
        "生效版本": effective_version,
        "动作": action,
        "结果": result,
        "数据集编号": (dataset or {}).get("数据集编号") or model.get("训练数据集"),
        "数据集版本": (dataset or {}).get("数据集版本") or "",
        "时间": now_text(),
        "操作人": "值班管理员",
        "说明": note,
    }
    model.setdefault("部署记录", []).append(record)
    return record


def linked_services(model: dict[str, Any]) -> list[dict[str, Any]]:
    code = str(model.get("模型编号") or "")
    return [row for row in store.rows(SERVICE_MODULE) if str(row.get("关联模型") or "") == code]


def derive_compat_status(model: dict[str, Any], evaluation: dict[str, Any] | None = None) -> str:
    if not model.get("生效版本"):
        if str(model.get("部署状态")) == "已下线" or str(model.get("status")) == "已下线":
            return "已下线"
        return "未部署"
    result = evaluation if evaluation is not None else current_differences(model)
    if result is None:
        return "未部署"
    return "待处理" if result.get("阻断差异") else "兼容"


def derive_call_status(model: dict[str, Any]) -> str:
    if str(model.get("status") or "") == "已下线" or model.get("部署状态") == "已下线":
        return "已停用"
    if model.get("部署状态") == "已降级":
        return "已降级"
    effective = model.get("生效版本")
    if not effective:
        return model.get("服务调用状态") or "未调用"
    # 只有「当前生效版本」的待处理差异才影响调用；其它版本（例如被阻止部署的候选版本）
    # 的差异只保留在差异清单里，不能把正常服务误标成待兼容。
    if any(
        diff.get("状态") == "待处理" and str(diff.get("模型版本")) == str(effective)
        for diff in model.get("兼容差异") or []
    ):
        return "版本待兼容"
    return "正常"


def sync_services(model: dict[str, Any], *, call_status: str | None = None) -> list[str]:
    """把模型当前生效版本同步到数据服务清单与调用状态。"""
    services = linked_services(model)
    history_labels = [str(record.get("版本")) for record in model_records(model)]
    effective = model.get("生效版本")
    status = call_status or derive_call_status(model)
    for service in services:
        service["生效模型版本"] = effective
        service["模型版本记录"] = history_labels
        service.pop("模型版本", None)
        service["服务调用状态"] = status
    model["关联数据服务"] = sorted({str(service.get("服务编号")) for service in services if service.get("服务编号")})
    model["服务调用状态"] = status
    # 生效版本存在时，模型详情的兼容状态按生效版本实时口径刷新；
    # 已下线/未部署的模型保留其生命周期状态，避免被覆盖成「未部署」。
    if model.get("生效版本"):
        model["兼容状态"] = derive_compat_status(model)
    return model["关联数据服务"]


def refresh_model_compatibility(model: dict[str, Any]) -> dict[str, Any] | None:
    """数据集字段变更或模型状态变化后重新计算差异，并同步服务调用状态。"""
    evaluation = current_differences(model)
    if evaluation is None:
        return None
    if evaluation.get("阻断差异"):
        stable = model_stable_for_model(model, dataset_for_model(model))
        record_differences(model, evaluation, status="待处理", compatible_version=(stable or {}).get("版本"))
    else:
        resolve_differences(model, note="重新校验已兼容")
    model["兼容状态"] = "待处理" if evaluation.get("阻断差异") else "兼容"
    sync_services(model)
    return evaluation


def apply_version(
    model: dict[str, Any],
    record: dict[str, Any],
    *,
    action: str,
    result: str,
    note: str = "",
    deploy_status: str = "已部署",
    sync: bool = True,
) -> dict[str, Any]:
    """把某个版本设为生效版本，并保证模型详情、部署记录、服务清单一致。"""
    version = str(record.get("版本"))
    model["生效版本"] = version
    # 模型版本保留「最新构建版本」，生效版本单独记录；模型详情与服务清单都读生效版本。
    latest = latest_version(model_records(model))
    model["模型版本"] = str((latest or record).get("版本"))
    model["部署状态"] = deploy_status
    model["status"] = "已下线" if deploy_status == "已下线" else "已部署"
    model["pending"] = False
    model["abnormal"] = deploy_status in {"已降级", "已下线"}
    model["版本历史"] = model_records(model)
    stable = latest_stable(model_records(model))
    if stable is not None:
        model["稳定版本"] = str(stable.get("版本"))
    dataset = dataset_for_model(model)
    append_deploy_record(
        model,
        version=version,
        effective_version=version,
        action=action,
        result=result,
        dataset=dataset,
        note=note,
    )
    if sync:
        sync_services(model)
    return model


def make_report(model: dict[str, Any], *, keyword: str | None = None, status: str | None = None, version: str | None = None) -> dict[str, Any]:
    """兼容差异报告：当前差异 + 历史差异 + 各版本可兼容结论。"""
    dataset = dataset_for_model(model)
    effective_record = find_version_record(model, model.get("生效版本")) if model.get("生效版本") else None
    current = evaluate_version(model, dataset, effective_record) if effective_record else None
    # 报告里的「可兼容稳定版本」= 版本历史中兼容结论为真的最高稳定版本，
    # 不要求严格早于生效版本；具体降级目标仍由模型记录的「上一稳定版本」约束。
    stable = highest_compatible_stable(model, dataset)
    version_checks: list[dict[str, Any]] = []
    for record in model_records(model):
        check = evaluate_version(model, dataset, record)
        version_checks.append({
            "版本": check["版本"],
            "兼容": check["兼容"],
            "状态": check["状态"],
            "缺失字段": check["缺失字段"],
            "差异数量": len(check["差异"]),
            "稳定": bool(record.get("稳定")),
        })
    history = list(model.get("兼容差异") or [])
    current_items = [
        {
            **item,
            "模型编号": model.get("模型编号"),
            "状态": "待处理" if item.get("严重级别") == "阻断" else "警告",
            "检测时间": now_text(),
        }
        for item in ((current or {}).get("差异") or [])
    ]
    if status:
        current_items = [item for item in current_items if str(item.get("状态")) == status]
        history = [item for item in history if str(item.get("状态")) == status]
    if keyword and keyword not in str(model.get("模型编号") or ""):
        current_items = []
        history = []
    if version:
        current_items = [item for item in current_items if str(item.get("模型版本")) == version]
        history = [item for item in history if str(item.get("模型版本")) == version]
    return {
        "模型编号": model.get("模型编号"),
        "模型名称": model.get("模型名称"),
        "生效版本": model.get("生效版本"),
        "稳定版本": (stable or {}).get("版本") or model.get("稳定版本"),
        "部署状态": model.get("部署状态"),
        "服务调用状态": model.get("服务调用状态"),
        "数据集编号": (dataset or {}).get("数据集编号"),
        "数据集版本": current_dataset_version(dataset or {}),
        "当前数据集字段": dataset_fields(dataset or {}),
        "兼容": bool((current or {}).get("兼容", True)) if current else True,
        "兼容状态": derive_compat_status(model, current) if current else "未部署",
        "字段差异": current_items,
        "差异": current_items,
        "兼容差异": current_items,
        "历史差异": history,
        "版本检查": version_checks,
        "可兼容稳定版本": (stable or {}).get("版本"),
        "查询条件": {"keyword": keyword, "status": status, "version": version},
        "items": current_items,
        "total": len(current_items),
    }


def snapshot_store() -> dict[str, list[dict[str, Any]]]:
    return store.snapshot()


def restore_store(snapshot: dict[str, list[dict[str, Any]]]) -> None:
    store.restore(snapshot)

