"""Test package root.

Makes every test module importable by its package-qualified name (e.g.
``tests.test_catalog``) so pytest's prepend import
mode does not collide on the ``test_catalog`` basename shared by the runtime tests and catalog tests.
"""
