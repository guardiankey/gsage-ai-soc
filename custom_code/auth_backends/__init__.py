# custom_code/auth_backends — drop custom BaseAuthProvider subclasses here.
#
# An auth backend authenticates a user's credentials and returns an AuthResult
# that the login chain uses to provision/update the local user record.
#
# Rules:
#   1. Each module must define at least one concrete BaseAuthProvider subclass.
#   2. Each class must have a unique ``name`` ClassVar (lowercase, no spaces).
#   3. The backend is auto-discovered by the AuthProviderRegistry at startup
#      via pkgutil.walk_packages.
#
# Sub-directory layout example:
#     custom_code/
#         auth_backends/
#             __init__.py          ← required
#             corporate/
#                 __init__.py      ← required for walk_packages to recurse
#                 sso_saml.py
#             example_backend.py   ← this is the example
