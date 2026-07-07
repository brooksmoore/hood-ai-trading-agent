#!/usr/bin/env python3
"""Convenience runner for Phase 0 plumbing demo.

From hood_agent_1/:
    python run_phase0.py
"""
import sys
from pathlib import Path

root = Path(__file__).parent.resolve()
sys.path.insert(0, str(root))

from src.phase0 import main

if __name__ == "__main__":
    main()
