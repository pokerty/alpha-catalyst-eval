"""Compatibility entry point for the alpha-catalyst-eval project."""

import ideaengine as _engine


def __getattr__(name):
    return getattr(_engine, name)


if __name__ == "__main__":
    _engine.main()
