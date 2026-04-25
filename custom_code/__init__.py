# custom_code — gSage AI extensibility package
#
# This directory is mounted as a Docker bind volume into the MCP server
# container at /app/custom_code and is importable as ``custom_code.*``.
#
# Sub-packages:
#   tools/     — Custom BaseTool subclasses, auto-discovered at startup.
#   providers/ — Custom BasePermissionProvider implementations.
#
# Since /app is the working directory + PYTHONPATH, no sys.path manipulation
# is needed.  Just place valid Python packages here and they will be found.
