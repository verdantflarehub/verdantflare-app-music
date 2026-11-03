"""Concurrent calls must not release or scale down another call's backend."""

import threading
import unittest
from datetime import datetime, timezone

import httpx

from verdantflare_music_mcp.gpu_lease import GpuCoordinator, K8sApi, MemoryApi


def coordinator(api, holder="music3/first"):
    return GpuCoordinator(
        api=api, role="music3", holder=holder, deployment="music",
        enabled=True, idle_seconds=0,
        now=lambda: datetime(2026, 11, 3, tzinfo=timezone.utc),
    )


class ConcurrentLeaseTest(unittest.TestCase):
    def test_first_completion_keeps_second_call_protected(self):
        api = MemoryApi()
        owner = coordinator(api)
        owner.acquire_for_queue()
        owner.acquire_for_queue()
        owner.release()
        self.assertEqual(owner.in_flight, 1)
        self.assertEqual(api.get_lease(owner.lease_name)["spec"]["holderIdentity"], owner.holder)
        self.assertFalse(coordinator(api, "music3/second").try_acquire())
        owner.release()
        self.assertEqual(api.get_lease(owner.lease_name)["spec"]["holderIdentity"], "")

    def test_competing_peer_does_not_revoke_an_existing_active_call(self):
        api = MemoryApi()
        owner = coordinator(api)
        owner.acquire_for_queue()
        api.replace_lease("gpu-task-h3", owner._spec("h3/contender"), None)
        self.assertTrue(owner.try_acquire())
        self.assertEqual(api.get_lease(owner.lease_name)["spec"]["holderIdentity"], owner.holder)

    def test_renew_after_final_release_does_not_resurrect_lease(self):
        api = MemoryApi()
        owner = coordinator(api)
        owner.acquire_for_queue()
        owner.release()
        owner.renew()
        self.assertEqual(api.get_lease(owner.lease_name)["spec"]["holderIdentity"], "")

    def test_idle_old_process_cannot_scale_down_new_holder(self):
        api = MemoryApi()
        old, new = coordinator(api), coordinator(api, "music3/second")
        old.acquire_for_queue()
        old.scale_to(1)
        old.release()
        new.acquire_for_queue()
        old.maybe_scale_down()
        self.assertEqual(api.get_scale("music"), 1)

    def test_scale_down_holds_role_until_scale_write_finishes(self):
        api = MemoryApi()
        old, new = coordinator(api), coordinator(api, "music3/second")
        old.acquire_for_queue()
        old.scale_to(1)
        old.release()
        original = api.patch_scale
        admitted = []
        def patch(name, replicas):
            admitted.append(new.try_acquire())
            original(name, replicas)
        api.patch_scale = patch
        old.maybe_scale_down()
        self.assertEqual(admitted, [False])
        self.assertEqual(api.get_scale("music"), 0)
        self.assertTrue(new.try_acquire())

    def test_owner_changed_between_probe_and_write_is_not_overwritten(self):
        api = MemoryApi()
        old, new = coordinator(api), coordinator(api, "music3/second")
        original = api.get_lease
        reads = 0
        def get(name):
            nonlocal reads
            if name == old.lease_name:
                reads += 1
                if reads == 2:
                    api.replace_lease(name, new._spec(new.holder), None)
            return original(name)
        api.get_lease = get
        self.assertFalse(old.try_acquire())
        self.assertEqual(original(old.lease_name)["spec"]["holderIdentity"], new.holder)

    def test_create_conflict_never_replaces_winning_holder(self):
        api = K8sApi("test")
        calls = []
        def request(method, path, **kwargs):
            calls.append(method)
            payload = {"metadata": {"resourceVersion": "1"}, "spec": {"holderIdentity": "winner"}}
            return httpx.Response(409 if method == "POST" else 200, json=payload,
                                  request=httpx.Request(method, "http://test" + path))
        api._request = request
        self.assertEqual(api.replace_lease("gpu-task-music3", {"holderIdentity": "loser"}, None), 409)
        self.assertEqual(calls, ["POST"])

    def test_local_acquire_waits_for_idle_scale_down_to_finish(self):
        api = MemoryApi()
        owner = coordinator(api)
        owner.acquire_for_queue()
        owner.scale_to(1)
        owner.release()
        scaling, finish, admitted = threading.Event(), threading.Event(), threading.Event()
        attempting = threading.Event()
        original = api.patch_scale
        def patch(name, replicas):
            scaling.set()
            if not finish.wait(2):
                raise AssertionError("test did not release scale write")
            original(name, replicas)
        api.patch_scale = patch
        def acquire():
            attempting.set()
            owner.acquire_for_queue()
            admitted.set()
        down = threading.Thread(target=owner.maybe_scale_down)
        up = threading.Thread(target=acquire)
        down.start()
        try:
            self.assertTrue(scaling.wait(2))
            up.start()
            self.assertTrue(attempting.wait(2))
            self.assertFalse(admitted.wait(0.05))
        finally:
            finish.set()
            down.join(2)
            if up.ident is not None:
                up.join(2)
        self.assertFalse(down.is_alive())
        self.assertFalse(up.is_alive())
        self.assertTrue(admitted.is_set())


if __name__ == "__main__":
    unittest.main()
