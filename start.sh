#!/bin/bash
# Used by the Docker image: apply a configured update, then start the server.
uv run update.py && uv run -m Backend
