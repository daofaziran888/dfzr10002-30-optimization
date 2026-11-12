"""部署兼容与降级规则的回归测试（标准库 unittest，不依赖 httpx）。

运行：cd backend && python -m unittest discover -s tests -v
"""
from __future__ import annotations

import copy
import unittest

from app.services import compatibility as compat
from app.services.dataset import DatasetService
from app.services.model import ModelService
from app.services.service import ServiceService
from app.store import store

# 模块导入时已经加载好种子数据，这里留一份基线，每个用例前整体恢复。
BASELINE = store.snapshot()


class ModelCompatibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        store.restore(BASELINE)
        self.models = ModelService()
        self.datasets = DatasetService()
        self.services = ServiceService()

    def _drop_probability(self) -> None:
        dataset, message = self.datasets.run_action(
            "DATA-0003",
            "启用配置",
            {"字段列表": ["user_id", "amount", "channel", "event_time"]},
        )
        self.assertIsNotNone(dataset)
        self.assertIn("DSV-2026-04", message)

    def test_missing_field_is_reported_and_deploy_blocked(self) -> None:
        self._drop_probability()
        model, message, ok = self.models.deploy(
            self.models.resolve("MODEL-003"), {"版本": "v3"},
        )
        self.assertFalse(ok)
        self.assertIn("字段 probability", message)
        self.assertIn("v2", message)
        report = self.models.compatibility_report("MODEL-003")
        self.assertFalse(report["兼容"])
        self.assertEqual(report["可兼容稳定版本"], "v2")
        self.assertNotIn("probability", report["当前数据集字段"])
        self.assertEqual(report["字段差异"][0]["字段"], "probability")

    def test_old_request_rejected_or_switched_to_stable(self) -> None:
        self._drop_probability()
        entry, message, ok = self.models.invoke(
            "MODEL-003",
            {"请求字段": ["user_id", "amount", "channel", "probability"], "request_id": "req-1"},
        )
        self.assertFalse(ok)
        self.assertEqual(entry["生效版本"], "v3")

        entry, message, ok = self.models.invoke(
            "MODEL-003",
            {
                "请求字段": ["user_id", "amount", "channel", "probability"],
                "mode": "fallback",
                "request_id": "req-2",
            },
        )
        self.assertTrue(ok)
        self.assertEqual(entry["生效版本"], "v2")
        self.assertEqual(entry["部署状态"], "已降级")
        self.assertIn("v2", message)
        # 模型详情与服务清单显示同一生效版本
        service = self.services.get_entry("SERV-0003")
        self.assertEqual(service["生效模型版本"], "v2")
        self.assertEqual(service["服务调用状态"], "已降级")

    def test_repeated_fallback_does_not_switch_again(self) -> None:
        self._drop_probability()
        first, _, ok = self.models.run_action("MODEL-003", "降级模型", {"request_id": "fb-1"})
        self.assertTrue(ok)
        self.assertEqual(first["生效版本"], "v2")

        before = len(self.models.deployment_records("MODEL-003"))
        second, _, _ = self.models.run_action("MODEL-003", "降级模型", {"request_id": "fb-2"})
        self.assertEqual(second["生效版本"], "v2")
        self.assertEqual(len(self.models.deployment_records("MODEL-003")), before)

        retry, _, _ = self.models.run_action("MODEL-003", "降级模型", {"request_id": "fb-1"})
        self.assertEqual(retry["生效版本"], "v2")

    def test_timeout_rolls_back_and_retry_keeps_original(self) -> None:
        self._drop_probability()
        _, message, ok = self.models.deploy(self.models.resolve("MODEL-003"), {"版本": "v2"})
        self.assertTrue(ok)
        # 先让字段恢复，使降级在业务上可行，但模拟超时
        self.datasets.run_action(
            "DATA-0003", "启用配置",
            {"字段列表": ["user_id", "amount", "channel", "probability", "event_time"]},
        )
        _, message, ok = self.models.deploy(self.models.resolve("MODEL-003"), {"版本": "v3"})
        self.assertTrue(ok)
        before = self.models.resolve("MODEL-003")["生效版本"]

        entry, message, ok = self.models.run_action(
            "MODEL-003", "降级模型", {"request_id": "to-1", "simulate_timeout": True},
        )
        self.assertFalse(ok)
        self.assertEqual(entry["生效版本"], before)
        self.assertIn("回滚", message)

        entry, message, ok = self.models.run_action("MODEL-003", "降级模型", {"request_id": "to-1"})
        self.assertFalse(ok)
        self.assertEqual(entry["生效版本"], before)
        self.assertEqual(self.services.get_entry("SERV-0003")["生效模型版本"], before)

    def test_params_mismatch_blocks_deploy(self) -> None:
        self._drop_probability()
        _, message, ok = self.models.deploy(
            self.models.resolve("MODEL-003"),
            {"版本": "v2", "参数配置": {"threshold": 0.9, "batch_size": 64}},
        )
        self.assertFalse(ok)
        self.assertIn("threshold", message)

    def test_dataset_version_published_and_dependents_refreshed(self) -> None:
        dataset, message = self.datasets.run_action(
            "DATA-0003", "启用配置",
            {"字段列表": ["user_id", "amount", "channel", "event_time"]},
        )
        versions = [item["版本"] for item in dataset["字段版本历史"]]
        self.assertIn("DSV-2026-04", versions)
        self.assertIn("MODEL-003", message)
        self.assertEqual(self.models.get_entry("MODEL-003")["兼容状态"], "待处理")
        self.assertEqual(self.services.get_entry("SERV-0004")["服务调用状态"], "版本待兼容")
        # 差异保存为历史，恢复字段后仍能查到
        diffs, total = self.models.list_differences(keyword="MODEL-003")
        self.assertGreaterEqual(total, 1)

    def test_list_search_filter_and_version_order(self) -> None:
        items, total = self.models.list_entries(keyword="MODEL-003")
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["模型编号"], "MODEL-003")

        items, total = self.models.list_entries(status="已部署")
        self.assertEqual(total, 3)
        self.assertTrue(all(item["部署状态"] == "已部署" for item in items))

        items, total = self.models.list_entries(version="v3")
        self.assertEqual(total, 1)

        page1, total = self.models.list_entries(page=1, size=3)
        page2, _ = self.models.list_entries(page=2, size=3)
        self.assertEqual(total, 8)
        ids1 = {item["id"] for item in page1}
        ids2 = {item["id"] for item in page2}
        self.assertFalse(ids1 & ids2)
        self.assertIn(3, ids1 | ids2)

    def test_offline_syncs_services(self) -> None:
        entry, _, ok = self.models.run_action("MODEL-004", "下线模型")
        self.assertTrue(ok)
        self.assertEqual(entry["部署状态"], "已下线")
        self.assertEqual(self.services.get_entry("SERV-0002")["服务调用状态"], "已停用")

    def test_store_rollback_is_deep(self) -> None:
        snapshot = store.snapshot()
        self.models.resolve("MODEL-003")["版本历史"].append({"版本": "v9"})
        store.restore(snapshot)
        self.assertNotIn("v9", [item["版本"] for item in self.models.resolve("MODEL-003")["版本历史"]])
        self.assertEqual(compat.MODEL_MODULE, "model")


if __name__ == "__main__":
    unittest.main()
