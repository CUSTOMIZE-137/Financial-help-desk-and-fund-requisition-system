#!/usr/bin/env python
"""Command-line entry point for Django management commands."""

import os
import sys

def main():
    """Load project settings and dispatch the requested management command."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "financial_helpdesk.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
