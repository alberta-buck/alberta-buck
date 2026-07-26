# The kernels are prebuilt and staged in as package data rather than
# compiled by setuptools, so setuptools would otherwise tag the wheel
# py3-none-any -- which would install happily on the wrong platform and
# fail at import.  Declaring the distribution impure forces a
# platform-specific tag.
#
# The .so itself is abi3-py311, so one wheel per platform serves every
# CPython from 3.11 onward.  All configuration lives in pyproject.toml.

from setuptools import setup
from setuptools.dist import Distribution


class BinaryDistribution(Distribution):
    def has_ext_modules(self):
        return True

    def is_pure(self):
        return False


setup(distclass=BinaryDistribution)
