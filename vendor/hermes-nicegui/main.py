#!/usr/bin/env python3
"""Hermes NiceGUI entry point.

This file is also the NiceGUI ``main_file`` used by the pytest ``user``
fixture (see ``[tool.pytest.ini_options]`` in ``pyproject.toml``).
"""

from hermes_nicegui.app import main

if __name__ in {"__main__", "__mp_main__"}:
    main()
