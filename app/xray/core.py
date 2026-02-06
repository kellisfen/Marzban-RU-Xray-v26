import atexit
import os
import re
import subprocess
import threading
from collections import deque
from contextlib import contextmanager

from app import logger
from app.xray.config import XRayConfig

DEBUG = os.environ.get("DEBUG", "false").lower() == "true"


class XRayCore:
    def __init__(self,
                 executable_path: str = None,
                 assets_path: str = "/usr/share/xray"):
        self.executable_path = executable_path or os.environ.get('XRAY_EXECUTABLE_PATH', "/usr/bin/xray")
        self.version = self.get_version()
        self.process = None
        self.restarting = False

        self._logs_buffer = deque(maxlen=100)
        self._temp_log_buffers = {}
        self._on_start_funcs = []
        self._on_stop_funcs = []
        self._env = {
            "XRAY_LOCATION_ASSET": assets_path
        }

        atexit.register(lambda: self.stop() if self.started else None)

    def get_version(self):
        try:
            cmd = [self.executable_path, "version"]
            output = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode('utf-8')
            m = re.search(r'Xray (\d+\.\d+\.\d+)', output)
            if m:
                return m.group(1)
            return "unknown"
        except Exception as e:
            logger.error(f"Error getting Xray version: {e}")
            return "error"

    def get_x25519(self, private_key: str = None):
        cmd = [self.executable_path, "x25519"]
        if private_key:
            cmd.extend(['-i', private_key])
        try:
            output = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode('utf-8')
            m_priv = re.search(r'Private\s*key:\s*(\S+)', output, re.IGNORECASE)
            m_pub = re.search(r'(?:Password|Public\s*key):\s*(\S+)', output, re.IGNORECASE)
            if m_priv and m_pub:
                return {
                    "private_key": m_priv.group(1),
                    "public_key": m_pub.group(1)
                }
        except Exception as e:
            logger.error(f"DEBUG: Error running x25519: {e}")
        return None

    def __capture_process_logs(self):
        def capture_and_debug_log():
            while self.process:
                output = self.process.stdout.readline()
                if output:
                    output = output.strip()
                    self._logs_buffer.append(output)
                    for buf in list(self._temp_log_buffers.values()):
                        buf.append(output)
                    logger.debug(output)

                elif not self.process or self.process.poll() is not None:
                    break

        def capture_only():
            while self.process:
                output = self.process.stdout.readline()
                if output:
                    output = output.strip()
                    self._logs_buffer.append(output)
                    for buf in list(self._temp_log_buffers.values()):
                        buf.append(output)

                elif not self.process or self.process.poll() is not None:
                    break

        if DEBUG:
            threading.Thread(target=capture_and_debug_log).start() 
        else:
            threading.Thread(target=capture_only).start()

    @contextmanager
    def get_logs(self):
        buf = deque(self._logs_buffer, maxlen=100)
        buf_id = id(buf)
        try:
            self._temp_log_buffers[buf_id] = buf
            yield buf
        finally:
            del self._temp_log_buffers[buf_id]
            del buf

    @property
    def started(self):
        if not self.process:
            return False

        if self.process.poll() is None:
            return True

        return False

    def start(self, config: XRayConfig):
        if self.started is True:
            raise RuntimeError("Xray is started already")

        if config.get('log', {}).get('logLevel') in ('none', 'error'):
            config['log']['logLevel'] = 'warning'

        cmd = [
            self.executable_path,
            "run",
            '-config',
            'stdin:'
        ]
        self.process = subprocess.Popen(
            cmd,
            env=self._env,
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
            universal_newlines=True
        )
        self.process.stdin.write(config.to_json())
        self.process.stdin.flush()
        self.process.stdin.close()
        logger.warning(f"Xray core {self.version} started")        

        self.__capture_process_logs()

        # execute on start functions
        for func in self._on_start_funcs:
            threading.Thread(target=func).start()

    def stop(self):
        if not self.started:
            return

        self.process.terminate()
        self.process = None
        logger.warning("Xray core stopped")

        # execute on stop functions
        for func in self._on_stop_funcs:
            threading.Thread(target=func).start()

    def restart(self, config: XRayConfig):
        if self.restarting is True:
            return

        try:
            self.restarting = True
            logger.warning("Restarting Xray core...")
            self.stop()
            self.start(config)
        finally:
            self.restarting = False

    def on_start(self, func: callable):
        self._on_start_funcs.append(func)
        return func

    def on_stop(self, func: callable):
        self._on_stop_funcs.append(func)
        return func