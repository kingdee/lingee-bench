"""Startup-time loading of an instance config and its business packs.

Reads YAML, runs the validation steps and produces the read-only LoadedInstance.
Runs only at startup or build time, never while serving tool calls.
"""
