import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


class FakeOmniScheduler:
    def get_new_batch_prefill(self, running_batch):
        del running_batch
        self.parent_queue_rids = [req.rid for req in self.waiting_queue]
        self.parent_prefill_budget = self.max_prefill_tokens
        return "prefill-plan"


class SchedulerPatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        omni_module = types.ModuleType("sglang_omni.scheduling.omni_scheduler")
        omni_module.OmniScheduler = FakeOmniScheduler
        sys.modules["sglang_omni"] = types.ModuleType("sglang_omni")
        sys.modules["sglang_omni.scheduling"] = types.ModuleType(
            "sglang_omni.scheduling"
        )
        sys.modules["sglang_omni.scheduling.omni_scheduler"] = omni_module

        package = types.ModuleType("music3_patch")
        package.__path__ = []
        sys.modules["music3_patch"] = package
        request_builder = types.ModuleType("music3_patch.sglang_request_builder")
        request_builder.cfg_uncond_rid = lambda rid: f"{rid}-cfg"
        request_builder.is_cfg_uncond_rid = lambda rid: rid.endswith("-cfg")
        sys.modules["music3_patch.sglang_request_builder"] = request_builder

        scheduler_path = Path(__file__).parents[1] / "patches" / "scheduler.py"
        spec = importlib.util.spec_from_file_location(
            "music3_patch.scheduler", scheduler_path
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        cls.scheduler_class = module.MiniMaxMusic3Scheduler

    def test_first_cfg_pair_can_exceed_default_prefill_budget(self):
        scheduler = self.scheduler_class.__new__(self.scheduler_class)
        scheduler.max_prefill_tokens = 2048
        scheduler.waiting_queue = [
            SimpleNamespace(rid="conditioned", origin_input_ids=range(1145)),
            SimpleNamespace(rid="unconditioned", origin_input_ids=range(1145)),
            SimpleNamespace(rid="next-conditioned", origin_input_ids=range(100)),
            SimpleNamespace(rid="next-unconditioned", origin_input_ids=range(100)),
        ]
        scheduler.get_num_allocatable_reqs = lambda running: 4

        result = scheduler.get_new_batch_prefill(SimpleNamespace(reqs=[]))

        self.assertEqual(result, "prefill-plan")
        self.assertEqual(
            scheduler.parent_queue_rids, ["conditioned", "unconditioned"]
        )
        self.assertEqual(scheduler.parent_prefill_budget, 2290)
        self.assertEqual(scheduler.max_prefill_tokens, 2048)
        self.assertEqual(
            [req.rid for req in scheduler.waiting_queue],
            [
                "conditioned",
                "unconditioned",
                "next-conditioned",
                "next-unconditioned",
            ],
        )


if __name__ == "__main__":
    unittest.main()
