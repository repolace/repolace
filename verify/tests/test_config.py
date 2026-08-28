"""The sandbox configuration defaults.

Thin, but the memory/swap assertion is not decoration: unequal values silently
disable the memory cap, and the failure mode is a container that swaps instead
of being killed.
"""

from verify.config import DockerConfig, DockerLimits


class TestDefaults:
    def test_swap_equals_memory_so_the_cap_cannot_be_escaped(self):
        limits = DockerLimits()

        assert limits.memory_swap == limits.memory

    def test_gvisor_is_off_by_default(self):
        """It is untested until runsc is actually installed; opting in is deliberate."""
        assert DockerConfig().runtime is None

    def test_the_container_does_not_run_as_root(self):
        assert DockerConfig().user == "65534:65534"

    def test_logs_are_capped(self):
        """The daemon's json log lives on the host disk and is unbounded by default."""
        assert DockerLimits().log_max_size.endswith(("k", "m", "g"))
