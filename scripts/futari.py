#!/usr/bin/env python3
"""Compatibility name for the public user-supplied-game build launcher.

All supported flags are forwarded unchanged to build_from_game.py. This entry
point has no repository-local SDK, game-data, save, or runtime paths.
"""
from build_from_game import main


if __name__ == "__main__":
    raise SystemExit(main())
