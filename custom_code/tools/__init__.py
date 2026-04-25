# custom_code/tools — drop custom BaseTool subclasses here.
#
# Rules:
#   1. Each module must define at least one concrete BaseTool subclass.
#   2. Sub-directories ARE supported, but MUST contain an __init__.py so
#      pkgutil.walk_packages can recurse into them.
#      Example: tools/network/__init__.py  +  tools/network/my_tool.py
#   3. A YAML file with the same stem as the module (e.g. my_tool.yaml) may
#      be placed alongside the .py file to supply config_defaults for that
#      tool.  Values in the class definition always take precedence over YAML.
#
# See custom_code/tools/example_tool.py for a minimal working example.
