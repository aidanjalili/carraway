"""Qt desktop interface.

The UI imports the core; the core never imports the UI. That direction is the
architectural rule this project is built on (docs/ARCHITECTURE.md) and is what
keeps the engine testable without a display server.
"""
