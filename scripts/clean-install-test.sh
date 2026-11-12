#!/bin/sh
# Compatibility entry point requested by CI; all test logic lives in Python.
exec "${PYTHON:-python3}" "$(dirname "$0")/clean_install_test.py" "$@"
