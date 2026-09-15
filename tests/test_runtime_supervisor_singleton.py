from pathlib import Path
import pytest
from aos.runtime_supervisor import SupervisorAlreadyRunning,SupervisorSingleton


def test_supervisor_singleton_rejects_second_owner_and_releases(tmp_path:Path):
    first=SupervisorSingleton(tmp_path,"AOS.Test.RuntimeSupervisor.Singleton")
    second=SupervisorSingleton(tmp_path,"AOS.Test.RuntimeSupervisor.Singleton")
    with first:
        with pytest.raises(SupervisorAlreadyRunning):
            with second:
                pass
    with SupervisorSingleton(tmp_path,"AOS.Test.RuntimeSupervisor.Singleton"):
        pass
