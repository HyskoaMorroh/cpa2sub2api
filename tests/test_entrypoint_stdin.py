"""Automatic entrypoints finish even when Docker keeps stdin open."""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SHELL = shutil.which('sh')


class EntrypointStdinTests(unittest.TestCase):
    def test_automatic_shared_run_once_disconnects_stdin(self):
        source = (ROOT / 'entrypoint.sh').read_text(encoding='utf8')
        automatic = next(line for line in source.splitlines() if 'ASSUME_YES=1 python' in line)
        self.assertRegex(automatic, r'ASSUME_YES=1 python[^\n]+<\s*/dev/null')
        self.assertIn('run_once || log', source)

    def fixture(self, directory, interval='0', exit_code='0'):
        app = Path(directory)
        (app / 'entrypoint.sh').write_text((ROOT / 'entrypoint.sh').read_text(encoding='utf8'), encoding='utf8')
        (app / 'config.yaml').write_text('claude-api-key: []\n', encoding='utf8')
        (app / '一键导入.py').write_text('# intercepted by the local test launcher\n', encoding='utf8')
        binary = app / 'bin'
        binary.mkdir()
        launcher = binary / 'python'
        launcher.write_text('#!/bin/sh\nIFS= read -r unexpected && exit 42\n'
                            'printf "eof\\n" >> "$ENTRYPOINT_TEST_LOG"\n'
                            'exit "$ENTRYPOINT_TEST_EXIT"\n', encoding='utf8')
        launcher.chmod(0o755)
        self.assertTrue(os.access(launcher, os.X_OK), 'fixture requires an executable temporary filesystem')
        env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ.get('PATH', ''),
                   SUB2API_BASE_URL='http://admin.invalid', SUB2API_ADMIN_KEY='synthetic-only',
                   RUN_INTERVAL_SECONDS=interval, ENTRYPOINT_TEST_LOG=str(app / 'calls'),
                   ENTRYPOINT_TEST_EXIT=exit_code)
        env.pop('RUN_ONCE', None)
        return app, env

    def stop(self, process):
        if process.stdin:
            process.stdin.close()
            process.stdin = None
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        process.communicate(timeout=5)

    @unittest.skipUnless(os.name == 'posix' and SHELL, 'POSIX entrypoint executes in the Linux runtime')
    def test_one_shot_held_open_stdin_finishes_and_preserves_failure_status(self):
        for exit_code in ('0', '7'):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as directory:
                app, env = self.fixture(directory, exit_code=exit_code)
                self.assertEqual(subprocess.run([SHELL, '-n', str(app / 'entrypoint.sh')]).returncode, 0)
                process = subprocess.Popen([SHELL, str(app / 'entrypoint.sh')], env=env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
                try:
                    # Keep the parent write-end open: Docker OpenStdin=true/Tty=false.
                    self.assertEqual(process.wait(timeout=2), int(exit_code))
                    self.assertEqual((app / 'calls').read_text(), 'eof\n')
                finally:
                    self.stop(process)

    @unittest.skipUnless(os.name == 'posix' and SHELL, 'POSIX entrypoint executes in the Linux runtime')
    def test_loop_held_open_stdin_runs_multiple_cycles(self):
        with tempfile.TemporaryDirectory() as directory:
            app, env = self.fixture(directory, interval='1')
            process = subprocess.Popen([SHELL, str(app / 'entrypoint.sh')], env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            try:
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    count = len((app / 'calls').read_text().splitlines()) if (app / 'calls').exists() else 0
                    if count >= 2:
                        break
                    time.sleep(.05)
                self.assertGreaterEqual(count, 2)
                self.assertIsNone(process.poll())
            finally:
                self.stop(process)


if __name__ == '__main__':
    unittest.main()
