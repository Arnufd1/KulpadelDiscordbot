import subprocess
import sys

from padelbot.runtime import instance_lock


def test_second_process_cannot_run_bot_and_lock_is_released_on_exit(tmp_path):
    path = tmp_path / "discord.lock"
    code = "from pathlib import Path; from padelbot.runtime import instance_lock; import sys\nwith instance_lock(Path(sys.argv[1])) as ok: print('acquired' if ok else 'busy')"
    def probe():
        return subprocess.check_output([sys.executable, "-c", code, str(path)], text=True).strip()
    with instance_lock(path) as acquired:
        assert acquired
        assert probe() == "busy"
    assert probe() == "acquired"
