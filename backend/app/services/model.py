"""分析模型管理业务规则：版本历史、部署兼容检查、降级与状态同步。

部署/降级是带事务语义的操作：
- 执行前对整库做快照，任何异常都恢复快照，保证模型详情、部署记录、
  数据服务清单要么全部切换、要么全部保持原样。
- 同一模型的部署/降级串行执行，并记录 request_id 幂等台账；
  重复触发只执行一次，超时/失败的回滚结果也会被重试直接复用。
"""
from __future__ import annotations

import re
from typing import Any

from app.services import compatibility as compat
from app.store import store

MODULE = "model"
REQUIRED_FIELDS = ["模型编号", "模型名称", "模型类型"]
STATUS_ORDER = ["草稿", "训练中", "已部署", "已下线"]
DEPLOY_STATUS_ORDER = ["未部署", "训练中", "已部署", "已降级", "已下线"]
STATUS_ALIASES = {
    "草稿": {"未部署", "草稿"},
    "未部署": {"未部署", "草稿"},
    "训练中": {"训练中"},
    "已部署": {"已部署"},
    "已降级": {"已降级"},
    "已下线": {"已下线"},
    "归档": {"已下线"},
}

DEPLOY_ACTIONS = compat.DEPLOY_ACTIONS
DEGRADE_ACTIONS = compat.DEGRADE_ACTIONS
CHECK_ACTIONS = compat.CHECK_ACTIONS
OFFLINE_ACTIONS = {"下线模型", "下线"}
ARCHIVE_ACTIONS = {"归档模型", "归档"}


class ModelService:
    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def resolve(self, entry_id: Any) -> dict[str, Any] | None:
        """支持用数值 id 或模型编号（MODEL-003）定位同一条记录。"""
        text = str(entry_id or "").strip()
        if text.isdigit():
            return store.find(MODULE, int(text))
        return compat.find_model_by_code(text)

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        version: str | None = None,
        order: str | None = None,
        sort: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(MODULE))
        if keyword:
            keyword = keyword.strip()
            rows = [row for row in rows if keyword in str(row.get("模型编号", ""))]
        if status:
            allowed = STATUS_ALIASES.get(status.strip(), {status.strip()})
            rows = [
                row for row in rows
                if str(row.get("部署状态")) in allowed or str(row.get("status")) in allowed
            ]
        if version:
            rows = [
                row for row in rows
                if str(row.get("模型版本")) == str(version) or str(row.get("生效版本")) == str(version)
            ]
        direction = (order or "asc").lower()
        if direction not in {"asc", "desc"}:
            direction = "asc"
        reverse = direction == "desc"
        if sort in {"version", "模型版本", "版本"} or version:
            rows.sort(key=lambda row: compat.version_key(row.get("模型版本")), reverse=reverse)
        else:
            rows.sort(key=lambda row: int(row.get("id", 0) or 0), reverse=reverse)
        total = len(rows)
        start = max(page - 1, 0) * size
        page_rows = [self._list_item(row) for row in rows[start:start + size]]
        return page_rows, total

    def _list_item(self, model: dict[str, Any]) -> dict[str, Any]:
        item = dict(model)
        evaluation = compat.current_differences(model)
        item["兼容状态"] = compat.derive_compat_status(model, evaluation)
        item["当前差异数量"] = len((evaluation or {}).get("阻断差异") or [])
        item["缺失字段"] = (evaluation or {}).get("缺失字段") or []
        item["服务调用状态"] = compat.derive_call_status(model)
        return item

    def get_entry(self, entry_id: Any) -> dict[str, Any] | None:
        model = self.resolve(entry_id)
        if model is None:
            return None
        entry = dict(model)
        evaluation = compat.current_differences(model)
        entry["兼容状态"] = compat.derive_compat_status(model, evaluation)
        entry["字段差异"] = (evaluation or {}).get("差异") or []
        entry["兼容报告"] = compat.make_report(model)
        entry["服务调用状态"] = compat.derive_call_status(model)
        return entry

    def compatibility_report(
        self,
        entry_id: Any,
        *,
        keyword: str | None = None,
        status: str | None = None,
        version: str | None = None,
    ) -> dict[str, Any] | None:
        model = self.resolve(entry_id)
        if model is None:
            return None
        return compat.make_report(model, keyword=keyword, status=status, version=version)

    def deployment_records(self, entry_id: Any) -> list[dict[str, Any]] | None:
        model = self.resolve(entry_id)
        if model is None:
            return None
        return list(model.get("部署记录") or [])

    def list_differences(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        version: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """把各模型的兼容差异汇总成一张清单，筛选条件与模型列表保持一致。"""
        rows: list[dict[str, Any]] = []
        for model in store.rows(MODULE):
            if keyword and keyword.strip() not in str(model.get("模型编号", "")):
                continue
            for diff in model.get("兼容差异") or []:
                item = dict(diff)
                item.setdefault("模型编号", model.get("模型编号"))
                item["模型名称"] = model.get("模型名称")
                item["生效版本"] = model.get("生效版本")
                item["部署状态"] = model.get("部署状态")
                item["服务调用状态"] = compat.derive_call_status(model)
                if status and str(item.get("状态")) != status:
                    continue
                if version and str(item.get("模型版本")) != str(version):
                    continue
                rows.append(item)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    # ------------------------------------------------------------------
    # 登记
    # ------------------------------------------------------------------
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["输入字段"] = compat._as_list(values.get("输入字段")) if values.get("输入字段") is not None else []
        entry["参数配置"] = compat.normalized_params(values.get("参数配置"))
        entry["训练数据集"] = values.get("训练数据集")
        entry["模型版本"] = values.get("模型版本") or "v1"
        entry["部署状态"] = "未部署"
        entry["status"] = STATUS_ORDER[0]
        entry["生效版本"] = None
        entry["上一稳定版本"] = None
        entry["稳定版本"] = None
        entry["pending"] = True
        entry["abnormal"] = False
        entry["版本历史"] = [{
            "版本": entry["模型版本"],
            "输入字段": list(entry["输入字段"]),
            "参数配置": dict(entry["参数配置"]),
            "数据集版本": compat.current_dataset_version(compat.dataset_for_model(entry) or {}),
            "稳定": False,
            "发布时间": compat.now_text(),
            "说明": "登记草稿",
        }]
        entry["部署记录"] = []
        entry["兼容差异"] = []
        entry["部署操作"] = []
        entry["调用记录"] = []
        entry["服务调用状态"] = "未调用"
        entry["关联数据服务"] = []
        rows.append(entry)
        return entry, []

    # ------------------------------------------------------------------
    # 动作入口
    # ------------------------------------------------------------------
    def run_action(
        self,
        entry_id: Any,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str, bool]:
        values = values or {}
        model = self.resolve(entry_id)
        if model is None:
            return None, f"分析模型 {entry_id} 不存在或已归档", False
        text = str(action or "").strip()

        # 部署/降级/兼容检查/恢复涉及版本与多条记录，统一串行；读类动作不加锁。
        if text in DEPLOY_ACTIONS or text in DEGRADE_ACTIONS or text in CHECK_ACTIONS or text in {"恢复稳定版本", "恢复", "恢复版本", "recover"}:
            with compat.DEPLOY_LOCK:
                return self._dispatch_action(entry_id, text, values)
        return self._dispatch_action(entry_id, text, values, model=model)

    def _dispatch_action(
        self,
        entry_id: Any,
        text: str,
        values: dict[str, Any],
        *,
        model: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str, bool]:
        model = model or self.resolve(entry_id)
        if model is None:
            return None, f"分析模型 {entry_id} 不存在或已归档", False
        if text in DEPLOY_ACTIONS:
            return self.deploy(model, values)
        if text in DEGRADE_ACTIONS or text in {"降级到上一稳定版本"}:
            return self.fallback(model, values)
        if text in CHECK_ACTIONS:
            return self.check(model, values)
        if text in OFFLINE_ACTIONS:
            return self.offline(model, values)
        if text in ARCHIVE_ACTIONS:
            return self.offline(model, values, archive=True)
        if text in {"恢复稳定版本", "恢复", "恢复版本", "recover"}:
            return self.recover(model, values)
        return None, f"动作「{text}」不属于分析模型管理可执行范围", False

    # ------------------------------------------------------------------
    # 部署
    # ------------------------------------------------------------------
    def deploy(self, model: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str, bool]:
        requested = values.get("版本") or values.get("version")
        target = self._target_version(model, requested)
        if target is None:
            return model, f"模型 {model.get('模型编号')} 没有可部署的版本记录", False

        params_override = values.get("参数配置", values.get("params"))
        fields_override = values.get("输入字段", values.get("fields"))
        dataset = compat.dataset_for_model(model)
        evaluation = compat.evaluate_version(
            model,
            dataset,
            target,
            params_override=params_override,
            fields_override=fields_override,
        )
        request_id = self._request_id(values)

        if not evaluation["兼容"]:
            stable = self._fallback_target(model, dataset, before=target.get("版本"))
            compat.record_differences(
                model,
                evaluation,
                status="待处理",
                compatible_version=(stable or {}).get("版本") or model.get("上一稳定版本"),
            )
            compat.append_deploy_record(
                model,
                version=str(target.get("版本")),
                effective_version=model.get("生效版本"),
                action="部署模型",
                result="阻断",
                dataset=dataset,
                note=self._diff_text(evaluation["阻断差异"]),
            )
            model["兼容状态"] = compat.derive_compat_status(model)
            compat.sync_services(model)
            message = (
                f"部署前兼容检查未通过：{self._diff_text(evaluation['阻断差异'])}"
                f"；已阻止版本 {target.get('版本')} 部署"
            )
            if stable is not None:
                message += f"，可兼容稳定版本为 {stable.get('版本')}"
            model["pending"] = True
            return model, message, False

        # 兼容：在快照保护下切换生效版本
        snapshot = compat.snapshot_store()
        try:
            self._maybe_raise(values)
            compat.resolve_differences(
                model, status="已兼容", note="重新部署兼容版本", effective_version=str(target.get("版本"))
            )
            model["兼容状态"] = "兼容"
            applied = compat.apply_version(
                model,
                target,
                action="部署模型",
                result="成功",
                note=str(values.get("remark") or ""),
            )
            model.pop("降级尝试", None)
            self._remember_operation(model, request_id, "成功", target.get("版本"), model.get("生效版本"), action="部署模型")
            message = f"模型 {model.get('模型编号')} 已部署版本 {target.get('版本')}，生效版本已同步到模型详情与服务调用清单"
            return applied, message, True
        except _SimulatedFailure as error:
            compat.restore_store(snapshot)
            fresh = self.resolve(model.get("模型编号")) or model
            if request_id:
                self._remember_operation(fresh, request_id, error.status, target.get("版本"), fresh.get("生效版本"), str(error), action="部署模型")
            return fresh, str(error), False
        except Exception as error:  # 兜底：任何异常都不留下半切换状态
            compat.restore_store(snapshot)
            fresh = self.resolve(model.get("模型编号")) or model
            if request_id:
                self._remember_operation(fresh, request_id, "失败", target.get("版本"), fresh.get("生效版本"), str(error))
            return fresh, f"部署失败，已原子回滚到原版本：{error}", False

    # ------------------------------------------------------------------
    # 降级
    # ------------------------------------------------------------------
    def fallback(self, model: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str, bool]:
        request_id = self._request_id(values)
        dataset = compat.dataset_for_model(model)
        original = model.get("生效版本")

        # 超时后的重试：上一次降级没完成切换、已在原版本回滚；再次到达时
        # 无论是否带同一个请求编号，都只确认「仍在原版本」，不重复切换。
        # 该守卫只消费一次，之后的新降级请求仍可正常执行。
        marker = model.get("降级尝试")
        if (
            marker is not None
            and not marker.get("已重试")
            and marker.get("结果") == "超时"
            and (
                not request_id
                or str(marker.get("请求编号") or "") == request_id
            )
        ):
            marker["已重试"] = True
            marker["重试时间"] = compat.now_text()
            return (
                model,
                f"上次降级请求超时，重试已回到原版本 {marker.get('原版本') or original}，不重复切换",
                False,
            )

        if request_id:
            previous = self._find_operation(model, request_id)
            if previous is not None:
                if previous.get("状态") == "成功":
                    return model, f"同一降级请求已执行过，保持生效版本 {original}，不重复切换", True
                return (
                    model,
                    f"同一降级请求上次结果为「{previous.get('状态')}」，已回到原版本 {original}，本次不再切换",
                    False,
                )

        if not original:
            return model, f"模型 {model.get('模型编号')} 当前没有生效版本，无法降级", False

        current_record = compat.find_version_record(model, original)
        current_evaluation = (
            compat.evaluate_version(model, dataset, current_record) if current_record else None
        )
        # 已经处于可兼容的稳定版本：重复触发只做幂等确认，不再向下切换。
        if (
            current_record is not None
            and current_record.get("稳定")
            and current_evaluation is not None
            and current_evaluation["兼容"]
        ):
            return model, f"模型已处于可兼容稳定版本 {original}，重复降级触发未执行切换", True

        target = self._fallback_target(model, dataset, before=original)
        if target is None:
            evaluation = current_evaluation or compat.evaluate_version(
                model, dataset, current_record or {"版本": original, "输入字段": []}
            )
            compat.record_differences(model, evaluation, status="待处理")
            compat.sync_services(model)
            return (
                model,
                f"模型 {model.get('模型编号')} 没有可兼容的上一稳定版本，降级未执行，保持原版本 {original}",
                False,
            )

        snapshot = compat.snapshot_store()
        try:
            self._maybe_raise(values)
            target_eval = compat.evaluate_version(model, dataset, target)
            if not target_eval["兼容"]:
                raise _SimulatedFailure("失败", f"稳定版本 {target.get('版本')} 与当前数据集不兼容，降级失败")
            compat.resolve_differences(
                model, status="已降级", note=f"降级到稳定版本 {target.get('版本')}", effective_version=str(target.get("版本"))
            )
            applied = compat.apply_version(
                model,
                target,
                action="降级模型",
                result="成功",
                note=f"由 {original} 降级到上一稳定版本 {target.get('版本')}",
                deploy_status="已降级",
                sync=False,
            )
            compat.sync_services(model, call_status="已降级")
            model.pop("降级尝试", None)
            self._remember_operation(model, request_id, "成功", target.get("版本"), target.get("版本"), action="降级模型")
            model["兼容状态"] = "兼容"
            return (
                applied,
                f"模型 {model.get('模型编号')} 已从 {original} 降级到上一稳定版本 {target.get('版本')}，"
                f"模型详情与服务调用清单均显示 {target.get('版本')}",
                True,
            )
        except _SimulatedFailure as error:
            compat.restore_store(snapshot)
            fresh = self.resolve(model.get("模型编号")) or model
            if error.status == "超时":
                self._mark_fallback_attempt(fresh, original, target.get("版本"), request_id, "超时")
            if request_id:
                self._remember_operation(fresh, request_id, error.status, target.get("版本"), fresh.get("生效版本"), str(error))
            return fresh, f"{error}，已原子回滚到原版本 {fresh.get('生效版本') or original}", False
        except Exception as error:
            compat.restore_store(snapshot)
            fresh = self.resolve(model.get("模型编号")) or model
            self._mark_fallback_attempt(fresh, original, target.get("版本"), request_id, "失败")
            if request_id:
                self._remember_operation(fresh, request_id, "失败", target.get("版本"), fresh.get("生效版本"), str(error))
            return fresh, f"降级失败，已原子回滚到原版本 {fresh.get('生效版本') or original}：{error}", False

    # ------------------------------------------------------------------
    # 请求调用（预测/数据服务调用）
    # ------------------------------------------------------------------
    def invoke(self, entry_id: Any, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str, bool]:
        with compat.DEPLOY_LOCK:
            return self._invoke(entry_id, values)

    def _invoke(self, entry_id: Any, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str, bool]:
        model = self.resolve(entry_id)
        if model is None:
            return None, f"分析模型 {entry_id} 不存在或已归档", False
        if not model.get("生效版本") or str(model.get("部署状态")) in {"已下线", "未部署"}:
            return model, f"模型 {model.get('模型编号')} 当前没有可接收请求的生效版本", False

        dataset = compat.dataset_for_model(model)
        effective = model.get("生效版本")
        record = compat.find_version_record(model, effective)
        request_fields = values.get("请求字段", values.get("fields", values.get("input_fields")))
        request_id = self._request_id(values)
        mode = str(values.get("mode") or values.get("降级策略") or "reject").strip().lower()
        if mode in {"fallback", "degrade", "降级", "auto"}:
            mode = "fallback"
        else:
            mode = "reject"

        if request_id:
            previous = self._find_operation(model, request_id, action="调用请求")
            if previous is not None:
                effective_now = model.get("生效版本")
                return (
                    model,
                    f"同一调用请求已处理过（{previous.get('状态')}），当前生效版本 {effective_now}，未重复切换",
                    previous.get("状态") == "成功",
                )

        evaluation = self._evaluate_request(model, dataset, record, request_fields)
        if evaluation["兼容"]:
            self._record_call(model, request_fields, effective, "成功", "请求字段与生效版本兼容")
            model["服务调用状态"] = "正常"
            compat.sync_services(model, call_status="正常")
            return model, f"请求已按模型版本 {effective} 处理", True

        # 请求不兼容：记录差异；按策略拒绝或降级到上一稳定版本
        compat.record_differences(
            model,
            evaluation,
            status="待处理",
            compatible_version=model.get("上一稳定版本"),
        )
        model["兼容状态"] = "待处理"
        blocked = self._diff_text(evaluation["阻断差异"])
        if mode == "reject":
            self._record_call(model, request_fields, effective, "拒绝", blocked)
            compat.sync_services(model, call_status="版本待兼容")
            return (
                model,
                f"请求被拒绝：{blocked}；生效版本 {effective} 已不再接受该字段请求",
                False,
            )

        target = self._fallback_target(model, dataset, before=effective)
        if target is None:
            self._record_call(model, request_fields, effective, "拒绝", blocked)
            compat.sync_services(model, call_status="版本待兼容")
            return model, f"请求无法处理且没有可兼容稳定版本：{blocked}", False

        snapshot = compat.snapshot_store()
        try:
            self._maybe_raise(values, prefix="降级请求")
            target_eval = compat.evaluate_version(model, dataset, target)
            if not target_eval["兼容"]:
                raise _SimulatedFailure("失败", f"上一稳定版本 {target.get('版本')} 不兼容，无法降级")
            request_eval = self._evaluate_request(model, dataset, target, request_fields)
            if not request_eval["兼容"]:
                raise _SimulatedFailure("失败", f"请求字段与稳定版本 {target.get('版本')} 仍不兼容，降级失败")
            applied = compat.apply_version(
                model,
                target,
                action="降级模型",
                result="成功",
                note=f"请求触发降级：由 {effective} 切到上一稳定版本 {target.get('版本')}",
                deploy_status="已降级",
                sync=False,
            )
            compat.record_differences(model, evaluation, status="已降级", compatible_version=target.get("版本"))
            compat.resolve_differences(
                model, status="已降级", note=f"请求已切换到稳定版本 {target.get('版本')}", effective_version=str(target.get("版本"))
            )
            compat.sync_services(model, call_status="已降级")
            self._record_call(model, request_fields, target.get("版本"), "已降级", f"由 {effective} 切到 {target.get('版本')}")
            self._remember_operation(model, request_id, "成功", target.get("版本"), target.get("版本"), action="调用请求")
            return (
                applied,
                f"请求已从 {effective} 切换到上一稳定版本 {target.get('版本')}，模型详情与服务调用清单均显示该版本",
                True,
            )
        except _SimulatedFailure as error:
            compat.restore_store(snapshot)
            fresh = self.resolve(model.get("模型编号")) or model
            if request_id:
                self._remember_operation(fresh, request_id, error.status, target.get("版本"), fresh.get("生效版本"), str(error), action="调用请求")
            return fresh, f"{error}，已原子回滚到原版本 {fresh.get('生效版本') or effective}", False
        except Exception as error:
            compat.restore_store(snapshot)
            fresh = self.resolve(model.get("模型编号")) or model
            if request_id:
                self._remember_operation(fresh, request_id, "失败", target.get("版本"), fresh.get("生效版本"), str(error), action="调用请求")
            return fresh, f"降级失败，已原子回滚到原版本 {fresh.get('生效版本') or effective}：{error}", False

    def _evaluate_request(
        self,
        model: dict[str, Any],
        dataset: dict[str, Any] | None,
        record: dict[str, Any] | None,
        request_fields: Any,
    ) -> dict[str, Any]:
        """请求级别兼容检查：字段既要在数据集里，也要满足版本输入要求。"""
        if record is None:
            return {
                "版本": str(model.get("生效版本") or ""),
                "兼容": False,
                "状态": "阻断",
                "差异": [{"类型": "版本记录缺失", "字段": "", "说明": "生效版本不在版本历史中", "严重级别": "阻断"}],
                "阻断差异": [{"类型": "版本记录缺失", "字段": "", "说明": "生效版本不在版本历史中", "严重级别": "阻断"}],
                "警告差异": [],
                "缺失字段": [],
                "新增字段": [],
            }
        base = compat.evaluate_version(model, dataset, record)
        issues = list(base.get("阻断差异") or [])
        warnings = list(base.get("警告差异") or [])
        requested = compat._as_list(request_fields) if request_fields is not None else None
        available = compat.dataset_fields(dataset or {})
        required = [str(item) for item in (record.get("输入字段") or [])]

        if requested is not None:
            for field in requested:
                if field in available:
                    continue
                if field in required:
                    issues.append({
                        "类型": "请求字段缺失",
                        "字段": field,
                        "说明": f"当前数据集 {model.get('训练数据集')} 已没有请求字段 {field}，预测结果会为空，拒绝该请求",
                        "严重级别": "阻断",
                        "模型版本": record.get("版本"),
                        "数据集版本": base.get("当前数据集版本"),
                    })
                else:
                    warnings.append({
                        "类型": "请求字段已移除",
                        "字段": field,
                        "说明": f"数据集已移除字段 {field}，版本 {record.get('版本')} 不使用该字段，可按兼容稳定版本继续处理",
                        "严重级别": "警告",
                        "模型版本": record.get("版本"),
                        "数据集版本": base.get("当前数据集版本"),
                    })
            for field in required:
                if field not in requested:
                    issues.append({
                        "类型": "请求字段不足",
                        "字段": field,
                        "说明": f"版本 {record.get('版本')} 需要输入字段 {field}，请求未提供，预测结果会为空",
                        "严重级别": "阻断",
                        "模型版本": record.get("版本"),
                        "数据集版本": base.get("当前数据集版本"),
                    })
            for field in requested:
                if field in available and field not in required:
                    warnings.append({
                        "类型": "请求字段未使用",
                        "字段": field,
                        "说明": f"请求携带字段 {field}，版本 {record.get('版本')} 未使用，可忽略",
                        "严重级别": "警告",
                        "模型版本": record.get("版本"),
                        "数据集版本": base.get("当前数据集版本"),
                    })

        result = dict(base)
        result["差异"] = issues + warnings
        result["阻断差异"] = issues
        result["警告差异"] = warnings
        result["兼容"] = not issues
        result["状态"] = "兼容" if not issues else "阻断"
        return result

    # ------------------------------------------------------------------
    # 其它动作
    # ------------------------------------------------------------------
    def check(self, model: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str, bool]:
        dataset = compat.dataset_for_model(model)
        target = self._target_version(model, values.get("版本") or values.get("version"))
        evaluation = compat.evaluate_version(model, dataset, target or {"版本": model.get("生效版本"), "输入字段": []})
        stable = self._fallback_target(model, dataset, before=(target or {}).get("版本") or model.get("生效版本") or "")
        if evaluation["兼容"]:
            compat.resolve_differences(
                model, status="已兼容", note="兼容检查通过", effective_version=str(model.get("生效版本") or evaluation.get("版本") or "")
            )
            model["兼容状态"] = "兼容"
            compat.sync_services(model)
            return model, f"版本 {evaluation['版本']} 与数据集 {dataset.get('数据集编号') if dataset else ''} 兼容，无需降级", True
        compat.record_differences(
            model,
            evaluation,
            status="待处理",
            compatible_version=(stable or {}).get("版本"),
        )
        model["兼容状态"] = "待处理"
        compat.sync_services(model)
        message = f"兼容检查发现阻断差异：{self._diff_text(evaluation['阻断差异'])}"
        if stable:
            message += f"；可降级到稳定版本 {stable.get('版本')}"
        return model, message, False

    def offline(self, model: dict[str, Any], values: dict[str, Any], *, archive: bool = False) -> tuple[dict[str, Any] | None, str, bool]:
        snapshot = compat.snapshot_store()
        try:
            original = model.get("生效版本")
            model["生效版本"] = None
            model["模型版本"] = str(model.get("模型版本") or "")
            model["部署状态"] = "已下线"
            model["status"] = "已下线"
            model["pending"] = False
            model["abnormal"] = True
            model["兼容状态"] = "已下线"
            compat.append_deploy_record(
                model,
                version=str(original or model.get("模型版本") or ""),
                effective_version=None,
                action="归档模型" if archive else "下线模型",
                result="成功",
                dataset=compat.dataset_for_model(model),
                note="停止接收调用请求",
            )
            compat.sync_services(model, call_status="已停用")
            return model, ("分析模型已归档，服务调用状态已同步为已停用" if archive else "分析模型已下线，服务调用状态已同步为已停用"), True
        except Exception as error:  # pragma: no cover - 防御性
            compat.restore_store(snapshot)
            fresh = self.resolve(model.get("模型编号")) or model
            return fresh, f"下线失败，已原子回滚：{error}", False

    def recover(self, model: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str, bool]:
        """字段恢复后重新校验：可兼容则恢复稳定状态，差异保留为历史。"""
        dataset = compat.dataset_for_model(model)
        latest = compat.latest_version(compat.model_records(model))
        if latest is None:
            return model, "模型没有版本记录", False
        evaluation = compat.evaluate_version(model, dataset, latest)
        if evaluation["兼容"]:
            resolved = compat.resolve_differences(model, note="数据集字段恢复后重新兼容")
            model["部署状态"] = "已降级" if model.get("部署状态") == "已降级" else model.get("部署状态")
            model["兼容状态"] = "兼容"
            compat.sync_services(model)
            return (
                model,
                f"字段已恢复，版本 {latest.get('版本')} 重新兼容；已归档 {resolved} 条历史差异，可继续查看字段差异",
                True,
            )
        compat.record_differences(model, evaluation, status="待处理", compatible_version=model.get("上一稳定版本"))
        model["兼容状态"] = "待处理"
        compat.sync_services(model)
        return model, f"字段尚未恢复，版本 {latest.get('版本')} 仍有阻断差异：{self._diff_text(evaluation['阻断差异'])}", False

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _target_version(self, model: dict[str, Any], requested: Any) -> dict[str, Any] | None:
        records = compat.model_records(model)
        if requested:
            return compat.find_version_record(model, requested)
        label = model.get("模型版本")
        return compat.find_version_record(model, label) or compat.latest_version(records)

    def _fallback_target(
        self,
        model: dict[str, Any],
        dataset: dict[str, Any] | None,
        *,
        before: Any,
    ) -> dict[str, Any] | None:
        """严格只回到模型记录的「上一稳定版本」，不越过稳定版本继续向下。"""
        label = model.get("上一稳定版本")
        if label:
            record = compat.find_version_record(model, label)
            if (
                record is not None
                and record.get("稳定")
                and compat.version_key(record.get("版本")) < compat.version_key(before)
                and compat.evaluate_version(model, dataset, record)["兼容"]
            ):
                return record
            return None
        return compat.model_stable_for_model(model, dataset)

    @staticmethod
    def _diff_text(differences: list[dict[str, Any]]) -> str:
        parts = []
        for diff in differences:
            field = f"字段 {diff.get('字段')} " if diff.get("字段") else ""
            parts.append(f"{diff.get('类型')}：{field}{diff.get('说明')}")
        return "；".join(parts) if parts else "存在不兼容差异"

    @staticmethod
    def _mark_fallback_attempt(
        model: dict[str, Any],
        original: Any,
        target: Any,
        request_id: str | None,
        result: str,
    ) -> None:
        """记录未完成的降级尝试：下一次到达时按「重试回原版本」处理。"""
        model["降级尝试"] = {
            "请求编号": request_id,
            "原版本": original,
            "目标版本": target,
            "结果": result,
            "时间": compat.now_text(),
            "已重试": False,
        }

    @staticmethod
    def _record_call(
        model: dict[str, Any],
        request_fields: Any,
        version: Any,
        status: str,
        note: str,
    ) -> None:
        model.setdefault("调用记录", []).append({
            "时间": compat.now_text(),
            "模型版本": str(version or ""),
            "请求字段": compat._as_list(request_fields) if request_fields is not None else [],
            "结果": status,
            "说明": note,
        })

    @staticmethod
    def _request_id(values: dict[str, Any]) -> str | None:
        raw = values.get("request_id") or values.get("请求编号") or values.get("operation_id")
        return str(raw).strip() if raw else None

    def _find_operation(self, model: dict[str, Any], request_id: str, *, action: str | None = None) -> dict[str, Any] | None:
        for item in model.get("部署操作") or []:
            if str(item.get("请求编号")) != request_id:
                continue
            if action and str(item.get("动作")) != action:
                continue
            return item
        return None

    def _remember_operation(
        self,
        model: dict[str, Any],
        request_id: str | None,
        status: str,
        target_version: Any,
        effective_version: Any,
        note: str = "",
        *,
        action: str = "部署模型",
    ) -> None:
        if not request_id:
            return
        operations = model.setdefault("部署操作", [])
        for item in operations:
            if str(item.get("请求编号")) == request_id:
                item.update({"状态": status, "生效版本": effective_version, "结果说明": note, "时间": compat.now_text()})
                return
        operations.append({
            "请求编号": request_id,
            "动作": "调用请求" if action == "调用" else action,
            "目标版本": str(target_version),
            "生效版本": effective_version,
            "状态": status,
            "结果说明": note,
            "时间": compat.now_text(),
        })

    @staticmethod
    def _maybe_raise(values: dict[str, Any], *, prefix: str = "") -> None:
        """为联调/测试提供可控失败：超时与失败都走同一条原子回滚路径。"""
        if values.get("simulate_timeout") or values.get("timeout"):
            raise _SimulatedFailure("超时", f"{prefix}处理超时，未完成版本切换")
        if values.get("simulate_failure") or values.get("force_failure") or values.get("simulate_error"):
            raise _SimulatedFailure("失败", f"{prefix}执行失败，版本切换未完成")


class _SimulatedFailure(Exception):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status

