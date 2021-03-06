import os
import sys
import subprocess
import time
import itertools
import locale

from collections import namedtuple
from threading import Thread


PY3K = sys.version_info[0] == 3

if PY3K:
    from queue import Queue, Empty
else:
    from Queue import Queue, Empty

from .killableprocess import Popen, STARTUPINFO, STARTF_USESHOWWINDOW
from .pexpect import spawn, ExceptionPexpect, TIMEOUT, EOF


def split_command_line(cmdline):
    """Split a command line into a command and its arguments according to
    the rules of the Microsoft C runtime."""
    # http://msdn.microsoft.com/en-us/library/ms880421
    s_free, s_in_quotes, s_in_escape = range(3)
    state = namedtuple('state', ('current', 'previous', 'escape_level', 'argument'))
    state.current = s_free
    state.previous = s_free
    state.argument = []
    result = []
    for c in itertools.chain(cmdline, ['EOI']):  # Mark End of Input
        if state.current == s_free:
            if c == '"':
                state.current = s_in_quotes
                state.previous = s_free
            elif c == '\\':
                state.current = s_in_escape
                state.previous = s_free
                state.escape_count = 1
            elif c in (' ', '\t', 'EOI'):
                if state.argument or state.previous != s_free:
                    result.append(''.join(state.argument))
                    del state.argument[:]
            else:
                state.argument.append(c)
        elif state.current == s_in_quotes:
            if c == '"':
                state.current = s_free
                state.previous = s_in_quotes
            elif c == '\\':
                state.current = s_in_escape
                state.previous = s_in_quotes
                state.escape_count = 1
            else:
                state.argument.append(c)
        elif state.current == s_in_escape:
            if c == '\\':
                state.escape_count += 1
            elif c == '"':
                nbs, escaped_delim = divmod(state.escape_count, 2)
                state.argument.append(nbs * '\\')
                if escaped_delim:
                    state.argument.append('"')
                    state.current = state.previous
                else:
                    if state.previous == s_in_quotes:
                        state.current = s_free
                    else:
                        state.current = s_in_quotes
                state.previous = s_in_escape
            else:
                state.argument.append(state.escape_count * '\\')
                state.argument.append(c)
                state.current = state.previous
                state.previous = s_in_escape
    if state.current != s_free:
        raise ValueError('Illegal command line.')
    return result

join_command_line = subprocess.list2cmdline


def which(command):
    path = os.environ.get('Path', '')
    path = path.split(os.pathsep)
    pathext = os.environ.get('Pathext', '.exe;.com;.bat;.cmd')
    pathext = pathext.split(os.pathsep)
    for dir in itertools.chain([''], path):
        for ext in itertools.chain([''], pathext):
            fname = os.path.join(dir, command) + ext
            if os.access(fname, os.X_OK):
                return fname


class winspawn(spawn):
    """This is the main class interface for Pexpect. Use this class to start
    and control child applications."""
    def __init__(self, command, args=[], timeout=30, maxread=2000, searchwindowsize=None,
                 logfile=None, cwd=None, env=None, encoding='utf-8'):
        self.child_output_queue = Queue()
        super(winspawn, self).__init__(command, args, timeout=timeout, maxread=maxread,
                                       searchwindowsize=searchwindowsize, logfile=logfile,
                                       cwd=cwd, env=env, encoding=encoding)

    def _spawn(self, command, args=None):
        """Start the child process. If args is empty, command will be parsed
        according to the rules of the MS C runtime, and args will be set to
        the parsed args."""
        if args:
            args = args[:]  # copy
            args.insert(0, command)
        else:
            args = split_command_line(command)
            command = args[0]

        self.command = command
        self.args = args
        executable = which(self.command)
        if executable is None:
            raise ExceptionPexpect('Executable not found: %s' % self.command)
        args = join_command_line(self.args)

        # Create the pipes
        startupinfo = STARTUPINFO()
        startupinfo.dwFlags |= STARTF_USESHOWWINDOW
        startupinfo.wShowWindow |= 1  # SW_SHOWNORMAL

        if not PY3K:  # Python 2.x, Popen cannot handle unicode path and args correctly
            encoding = locale.getpreferredencoding()
            if isinstance(executable, unicode):
                executable = executable.encode(encoding)
            if isinstance(args, unicode):
                args = args.encode(encoding)
        self.popen = Popen(args,
                           executable=executable,
                           startupinfo=startupinfo,
                           creationflags=0x8000000,  # CREATE_NO_WINDOW
                           bufsize=1,
                           cwd=self.cwd,
                           env=self.env,
                           stderr=subprocess.STDOUT,
                           stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE)

        # Start up the I/O threads
        self.pid = self.popen.pid
        self.child_fd = self.popen.stdin.fileno()  # for pexpect

        self.terminated = False
        self.closed = False

        self.stdout_handle = self.popen.stdout
        self.stdout_reader = Thread(target=self._child_reader, args=(self.child_output_queue,))
        self.stdout_reader.daemon = True
        self.stdout_reader.start()

    def _child_reader(self, q):
        while True:
            try:
                data = self.stdout_handle.read(1)
                if len(data) == 0:
                    break
                q.put(data)
            except:
                break

    def close(self, force=True):   # File-like object.
        if not self.closed:
            time.sleep(self.delayafterclose)  # Give kernel time to update process status.
            if self.isalive():
                if not self.terminate(force):
                    raise ExceptionPexpect('close() could not terminate the child using terminate()')
            self.child_fd = -1
            self.closed = True

    def waitnoecho(self, timeout=-1):
        raise NotImplementedError()

    def getecho(self):
        raise NotImplementedError()

    def setecho(self, state):
        raise NotImplementedError()

    def sendeof(self):
        # CTRL-Z
        char = chr(26)
        self.send(char)

    def sendintr(self):
        # platform does not define VINTR so assume CTRL-C
        char = chr(3)
        self.send(char)

    def terminate(self, force=False):
        if not self.isalive():
            return True
        try:
            self.kill(0)
        except:
            # I think there are kernel timing issues that sometimes cause
            # this to happen. I think isalive() reports True, but the
            # process is dead to the kernel.
            # Make one last attempt to see if the kernel is up to date.
            time.sleep(self.delayafterterminate)
            if not self.isalive():
                return True
            else:
                return False

    def wait(self):
        if not self.isalive():
            raise ExceptionPexpect('Cannot wait for dead child process.')
        self.exitstatus = self.popen.wait()
        self.stdout_handle = None
        self.terminated = True
        return self.exitstatus

    def isalive(self):
        if self.terminated:
            return False

        exitstatus = self.popen.poll()
        if exitstatus is None:
            return True

        self.exitstatus = exitstatus
        self.terminated = True
        return False

    def kill(self, sig):
        if self.isalive():
            self.popen.kill()
            self.stdout_handle = None

    def read_nonblocking(self, size=1, timeout=-1):
        if self.closed:
            raise ValueError('I/O operation on closed file in read_nonblocking().')

        if timeout == -1:
            timeout = self.timeout

        if not self.isalive():
            self.flag_eof = True
            raise EOF('End Of File (EOF) in read_nonblocking(). Braindead platform.')

        q = self.child_output_queue

        # Check first byte timeout
        try:
            s = q.get(True, timeout)
        except Empty:
            if not self.isalive():
                self.flag_eof = True
                raise EOF('End of File (EOF) in read_nonblocking(). Very pokey platform.')
            else:
                raise TIMEOUT('Timeout exceeded in read_nonblocking().')

        if len(s) < size:
            while True:
                try:
                    s += q.get_nowait()
                except Empty:
                    break
                if len(s) == size:
                    break

        s2 = self._cast_buffer_type(s)
        if self.logfile is not None:
            self.logfile.write(s2)
            self.logfile.flush()
        if self.logfile_read is not None:
            self.logfile_read.write(s2)
            self.logfile_read.flush()
        return s2

    def getwinsize(self):
        raise NotImplementedError()

    def setwinsize(self, r, c):
        raise NotImplementedError()

    def interact(self, escape_character=b'\x1d', input_filter=None, output_filter=None):
        raise NotImplementedError()
