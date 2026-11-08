"""ACSM / Adobe-DRM ingestion pack for Research Engine.

This module intentionally imports nothing. Core discovers the plugin by reading
wheel metadata and ``plugin.yaml`` without importing the package (constitution
§V); importing anything here would make discovery execute plugin code.
"""
