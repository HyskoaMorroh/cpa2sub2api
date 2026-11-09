"""Test fixture APIs for Python 3.9; newer unittest keeps its native implementation."""
import unittest


class CompatTestCase(unittest.TestCase):
    if not hasattr(unittest.TestCase, "enterContext"):
        def enterContext(self, cm):
            cls = type(cm)
            try:
                enter = cls.__enter__
                exit = cls.__exit__
            except AttributeError:
                raise TypeError(
                    "'%s.%s' object does not support the context manager protocol"
                    % (cls.__module__, cls.__qualname__)) from None
            result = enter(cm)
            self.addCleanup(exit, cm, None, None, None)
            return result


def write_text_lf(path, data, encoding=None):
    """Preserve explicit LF writes and Path.write_text's character return value."""
    if not isinstance(data, str):
        raise TypeError("data must be str, not %s" % data.__class__.__name__)
    with path.open("w", encoding=encoding, newline="\n") as stream:
        return stream.write(data)
