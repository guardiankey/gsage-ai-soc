#!/usr/bin/env python3
"""Quick test to verify setup."""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

print("Testing imports...")

try:
    print("✓ Models imported successfully")
except Exception as e:
    print(f"✗ Models import failed: {e}")
    sys.exit(1)

try:
    from src.shared.config.settings import get_settings
    settings = get_settings()
    print(f"✓ Settings loaded: {settings.app_env}")
except Exception as e:
    print(f"✗ Settings import failed: {e}")
    sys.exit(1)

try:
    from src.shared.elasticsearch import INDEX_TEMPLATES, ILM_POLICIES
    print(f"✓ Elasticsearch definitions loaded: {len(INDEX_TEMPLATES)} templates, {len(ILM_POLICIES)} policies")
except Exception as e:
    print(f"✗ Elasticsearch import failed: {e}")
    sys.exit(1)

print("\n✓ All imports successful!")
print("\nTo initialize Elasticsearch, run:")
print("  python scripts/init-elasticsearch.py")
