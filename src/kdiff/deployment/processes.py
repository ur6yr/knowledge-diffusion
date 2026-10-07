"""Shared process cleanup for the owned service launchers."""

import signal


def restore_signal_handlers(handlers):
    for signum, handler in handlers.items():
        # Python can report None for a handler installed outside Python. It
        # cannot reinstall that handler, so use the OS default in that case.
        signal.signal(signum, signal.SIG_DFL if handler is None else handler)
