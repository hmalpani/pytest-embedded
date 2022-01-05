import errno
import logging
import os
import subprocess
import sys
import threading
from io import TextIOWrapper
from typing import AnyStr, BinaryIO, List, Union

import pexpect.fdpexpect
from pexpect import EOF, TIMEOUT
from pexpect.utils import poll_ignore_interrupts, select_ignore_interrupts

from .utils import to_bytes, to_str


class PexpectProcess(pexpect.fdpexpect.fdspawn):
    """
    Use a temp file to gather multiple inputs into one output, and do `pexpect.expect()` from one place.
    """

    def __init__(self, pexpect_fr: BinaryIO, pexpect_fw: BinaryIO, count: int = 1, total: int = 1, **kwargs):
        self._count = count
        self._total = total

        if self._total > 1:
            self.source = f'dut-{self._count}'
        else:
            self.source = None

        super().__init__(pexpect_fr, **kwargs)

        self._fr = pexpect_fr
        self._fw = pexpect_fw

    def send(self, s: AnyStr) -> int:  # noqa
        """
        Write to the pexpect process and log.

        Args:
            s: bytes or str

        Returns:
            number of written bytes.
        """
        s = self._coerce_send_string(s)
        self._log(s, 'send')

        # for pytest logging
        for line in to_str(s).replace('\r', '\n').split('\n'):
            if line.strip():
                if self.source:
                    line = f'[{self.source}]' + line
                logging.info(line)

        # write the bytes into the pexpect process
        b = self._encoder.encode(s, final=False)
        try:
            written = self._fw.write(b)
            self._fw.flush()
        except ValueError:  # write to closed file. since this function would be run in daemon thread, would happen
            return 0

        return written

    def write(self, s: AnyStr) -> None:
        self.send(s)

    def read_nonblocking(self, size=1, timeout=-1) -> bytes:
        """
        Since we're using real file stream, here we only raise an EOF error only when the file stream has been closed.
        This could solve the `os.read()` blocked issue.

        Args:
            size: most read bytes
            timeout: timeout

        Returns:
            read bytes
        """
        try:
            if os.name == 'posix':
                if timeout == -1:
                    timeout = self.timeout
                rlist = [self.child_fd]
                wlist = []
                xlist = []
                if self.use_poll:
                    rlist = poll_ignore_interrupts(rlist, timeout)
                else:
                    rlist, wlist, xlist = select_ignore_interrupts(rlist, wlist, xlist, timeout)
                if self.child_fd not in rlist:
                    raise TIMEOUT('Timeout exceeded.')

            s = os.read(self.child_fd, size)
        except OSError as err:
            if err.args[0] == errno.EIO:  # Linux-style EOF
                pass
            if err.args[0] == errno.EBADF:  # Bad file descriptor
                raise EOF('Bad File Descriptor')
            raise

        s = self._decoder.decode(s, final=False)
        self._log(s, 'read')
        return s

    def terminate(self, force=False):
        """
        Close the temporary file streams
        """
        try:
            self._fr.close()
            self._fw.close()
        except:  # noqa
            pass


class DuplicateStdout(TextIOWrapper):
    """
    A context manager to redirect `sys.stdout` to `pexpect_proc`.

    Use pytest logging functionality to log to cli or file by setting `log_cli` or `log_file` related attributes.
    These attributes could be set at the same time.

    Warning:
        - Within this context manager, the `print()` would be redirected to `self.write()`.
        All the `args` and `kwargs` passed to `print()` would be ignored and might not work as expected.

        - The context manager replacement of `sys.stdout` is NOT thread-safe. DO NOT use it in a thread.
    """

    def __init__(self, pexpect_proc: PexpectProcess):  # noqa
        """
        Args:
            pexpect_proc: `PexpectProcess` instance
        """
        # DO NOT call super().__init__(), use TextIOWrapper as parent class only for types and functions
        self.pexpect_proc = pexpect_proc

        self.stdout = None

    def __enter__(self):
        if self.stdout is None:
            self.stdout = sys.stdout
            sys.stdout = self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def __del__(self):
        self.close()

    def write(self, data: bytes) -> None:
        """
        Call `pexpect_proc.write()` instead of `sys.stdout.write()`
        """
        if not data:
            return

        self.pexpect_proc.write(data)
        sys.stdout = self  # logging info would modify the sys.stdout again, re-assigning here

    def flush(self) -> None:
        """
        Don't need to flush anymore since the `flush` method would be called inside `pexpect_proc`.
        """
        pass

    def close(self) -> None:
        """
        Stop redirecting `sys.stdout`.
        """
        if self.stdout is not None:
            sys.stdout = self.stdout
            self.stdout = None

    def isatty(self) -> bool:
        """
        Returns:
            True since it has `write()`.
        """
        return True


def live_print_call(*args, **kwargs):
    """`
    live print the `subprocess.Popen` process. Use this function when redirecting `sys.stdout` to enable
    live-logging and logging to file simultaneously.

    Note:
        This function behaves the same as `subprocess.call()`, it would block your current process.
    """
    default_kwargs = {
        'stdout': subprocess.PIPE,
        'stderr': subprocess.STDOUT,
    }
    default_kwargs.update(kwargs)

    process = subprocess.Popen(*args, **default_kwargs)
    while process.poll() is None:
        print(to_str(process.stdout.read()))


class DuplicateStdoutMixin:
    """
    A mixin class which provides function `create_forward_io_thread` to create a forward io thread.

    Note:
        `_forward_io()` should be implemented in subclasses, the function should be something like:

        ```python
        def _forward_io(self, pexpect_proc: PexpectProcess) -> None:
            pexpect_proc.write(...)
        ```
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._forward_io_thread = None

    def create_forward_io_thread(self, pexpect_proc: PexpectProcess) -> None:
        """
        Create a forward io daemon thread if it doesn't exist.

        Args:
            pexpect_proc: `PexpectProcess` instance
        """
        if self._forward_io_thread:
            return

        self._forward_io_thread = threading.Thread(target=self._forward_io, args=(pexpect_proc,), daemon=True)
        self._forward_io_thread.start()

    def _forward_io(self, pexpect_proc: PexpectProcess) -> None:
        raise NotImplementedError('should be implemented by subclasses')


class DuplicateStdoutPopen(DuplicateStdoutMixin, subprocess.Popen):
    """
    `subprocess.Popen` with `DuplicateStdoutMixin` mixed with default popen kwargs.
    """

    POPEN_KWARGS = {
        'bufsize': 0,
        'stdin': subprocess.PIPE,
        'stdout': subprocess.PIPE,
        'stderr': subprocess.STDOUT,
        'shell': True,
    }

    def __init__(self, cmd: Union[str, List[str]], **kwargs):
        kwargs.update(self.POPEN_KWARGS)
        super().__init__(cmd, **kwargs)

    def send(self, s: AnyStr) -> None:
        """
        Write to `stdin` via `stdin.write`.

        If the input is `str`, will encode to `bytes` and add a b'\\n' automatically in the end.

        if the input is `bytes`, will pass this directly.

        Args:
            s: bytes or str
        """
        self.stdin.write(to_bytes(s, '\n'))

    def _forward_io(self, pexpect_proc: PexpectProcess) -> None:
        while self.poll() is None:
            pexpect_proc.write(self.stdout.read())
