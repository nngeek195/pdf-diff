import os
from setuptools import setup, Extension
import pybind11

# Finds the pybind11 include headers automatically
ext_modules = [
    Extension(
        "kv_mechanism",
        ["kv_engine.cpp"],
        include_dirs=[pybind11.get_include()],
        language="c++",
        extra_compile_args=["-O3", "-std=c++11"] # -O3 ensures maximum speed
    ),
]

setup(
    name="kv_mechanism",
    version="1.0",
    description="Custom spatial PDF diffing engine",
    ext_modules=ext_modules,
)