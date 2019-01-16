from __future__ import absolute_import

import contextlib
import errno
import logging
import logging.handlers
import os
import sys

from pip._vendor.six import PY2

from pip._internal.utils.compat import WINDOWS
from pip._internal.utils.misc import ensure_dir

try:
    import threading
except ImportError:
    import dummy_threading as threading  # type: ignore


try:
    from pip._vendor import colorama
# Lots of different errors can come from this, including SystemError and
# ImportError.
except Exception:
    colorama = None


_log_state = threading.local()
_log_state.indentation = 0


class BrokenStdoutLoggingError(Exception):
    """
    Raised if BrokenPipeError occurs for the stdout stream while logging.
    """
    pass


# BrokenPipeError does not exist in Python 2 and in addition manifests
# differently in Windows and non-Windows.
if WINDOWS:
    # In Windows, a broken pipe can show up as EINVAL rather than EPIPE:
    # https://bugs.python.org/issue19612
    # https://bugs.python.org/issue30418
    if PY2:
        def _is_broken_pipe_error(exc_class, exc):
            return (exc_class is IOError and
                    exc.errno in (errno.EINVAL, errno.EPIPE))
    else:
        # In Windows, a broken pipe IOError became OSError in Python 3.
        def _is_broken_pipe_error(exc_class, exc):
            return ((exc_class is BrokenPipeError) or  # noqa: F821
                    (exc_class is OSError and
                     exc.errno in (errno.EINVAL, errno.EPIPE)))
elif PY2:
    def _is_broken_pipe_error(exc_class, exc):
        return (exc_class is IOError and exc.errno == errno.EPIPE)
else:
    # Then we are handling Python 3 (non-Windows case).
    def _is_broken_pipe_error(exc_class, exc):
        """
        Return whether an exception is a broken pipe error.

        Args:
          exc_class: an exception class.
          exc: an exception instance.
        """
        return (exc_class is BrokenPipeError)  # noqa: F821


@contextlib.contextmanager
def indent_log(num=2):
    """
    A context manager which will cause the log output to be indented for any
    log messages emitted inside it.
    """
    _log_state.indentation += num
    try:
        yield
    finally:
        _log_state.indentation -= num


def get_indentation():
    return getattr(_log_state, 'indentation', 0)


class IndentingFormatter(logging.Formatter):

    def format(self, record):
        """
        Calls the standard formatter, but will indent all of the log messages
        by our current indentation level.
        """
        formatted = logging.Formatter.format(self, record)
        formatted = "".join([
            (" " * get_indentation()) + line
            for line in formatted.splitlines(True)
        ])
        return formatted


def _color_wrap(*colors):
    def wrapped(inp):
        return "".join(list(colors) + [inp, colorama.Style.RESET_ALL])
    return wrapped


class ColorizedStreamHandler(logging.StreamHandler):

    # Don't build up a list of colors if we don't have colorama
    if colorama:
        COLORS = [
            # This needs to be in order from highest logging level to lowest.
            (logging.ERROR, _color_wrap(colorama.Fore.RED)),
            (logging.WARNING, _color_wrap(colorama.Fore.YELLOW)),
        ]
    else:
        COLORS = []

    def __init__(self, stream=None, no_color=None):
        logging.StreamHandler.__init__(self, stream)
        self._no_color = no_color

        if WINDOWS and colorama:
            self.stream = colorama.AnsiToWin32(self.stream)

    def _using_stdout(self):
        """
        Return whether the handler is using sys.stdout.
        """
        if WINDOWS and colorama:
            # Then self.stream is an AnsiToWin32 object.
            return self.stream.wrapped is sys.stdout

        return self.stream is sys.stdout

    def should_color(self):
        # Don't colorize things if we do not have colorama or if told not to
        if not colorama or self._no_color:
            return False

        real_stream = (
            self.stream if not isinstance(self.stream, colorama.AnsiToWin32)
            else self.stream.wrapped
        )

        # If the stream is a tty we should color it
        if hasattr(real_stream, "isatty") and real_stream.isatty():
            return True

        # If we have an ANSI term we should color it
        if os.environ.get("TERM") == "ANSI":
            return True

        # If anything else we should not color it
        return False

    def format(self, record):
        msg = logging.StreamHandler.format(self, record)

        if self.should_color():
            for level, color in self.COLORS:
                if record.levelno >= level:
                    msg = color(msg)
                    break

        return msg

    # The logging module says handleError() can be customized.
    def handleError(self, record):
        exc_class, exc = sys.exc_info()[:2]
        # If a broken pipe occurred while calling write() or flush() on the
        # stdout stream in logging's Handler.emit(), then raise our special
        # exception so we can handle it in main() instead of logging the
        # broken pipe error and continuing.
        if (exc_class and self._using_stdout() and
                _is_broken_pipe_error(exc_class, exc)):
            raise BrokenStdoutLoggingError()

        return super(ColorizedStreamHandler, self).handleError(record)


class BetterRotatingFileHandler(logging.handlers.RotatingFileHandler):

    def _open(self):
        ensure_dir(os.path.dirname(self.baseFilename))
        return logging.handlers.RotatingFileHandler._open(self)


class MaxLevelFilter(logging.Filter):

    def __init__(self, level):
        self.level = level

    def filter(self, record):
        return record.levelno < self.level


def setup_logging(verbosity, no_color, user_log_file):
    """Configures and sets up all of the logging
    """

    # Determine the level to be logging at.
    if verbosity >= 1:
        level = "DEBUG"
    elif verbosity == -1:
        level = "WARNING"
    elif verbosity == -2:
        level = "ERROR"
    elif verbosity <= -3:
        level = "CRITICAL"
    else:
        level = "INFO"

    # The "root" logger should match the "console" level *unless* we also need
    # to log to a user log file.
    include_user_log = user_log_file is not None
    if include_user_log:
        additional_log_file = user_log_file
        root_level = "DEBUG"
    else:
        additional_log_file = "/dev/null"
        root_level = level

    # Disable any logging besides WARNING unless we have DEBUG level logging
    # enabled for vendored libraries.
    vendored_log_level = "WARNING" if level in ["INFO", "ERROR"] else "DEBUG"

    # Shorthands for clarity
    log_streams = {
        "stdout": "ext://sys.stdout",
        "stderr": "ext://sys.stderr",
    }
    handler_classes = {
        "stream": "pip._internal.utils.logging.ColorizedStreamHandler",
        "file": "pip._internal.utils.logging.BetterRotatingFileHandler",
    }

    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "exclude_warnings": {
                "()": "pip._internal.utils.logging.MaxLevelFilter",
                "level": logging.WARNING,
            },
        },
        "formatters": {
            "indent": {
                "()": IndentingFormatter,
                "format": "%(message)s",
            },
        },
        "handlers": {
            "console": {
                "level": level,
                "class": handler_classes["stream"],
                "no_color": no_color,
                "stream": log_streams["stdout"],
                "filters": ["exclude_warnings"],
                "formatter": "indent",
            },
            "console_errors": {
                "level": "WARNING",
                "class": handler_classes["stream"],
                "no_color": no_color,
                "stream": log_streams["stderr"],
                "formatter": "indent",
            },
            "user_log": {
                "level": "DEBUG",
                "class": handler_classes["file"],
                "filename": additional_log_file,
                "delay": True,
                "formatter": "indent",
            },
        },
        "root": {
            "level": root_level,
            "handlers": ["console", "console_errors"] + (
                ["user_log"] if include_user_log else []
            ),
        },
        "loggers": {
            "pip._vendor": {
                "level": vendored_log_level
            }
        },
    })
