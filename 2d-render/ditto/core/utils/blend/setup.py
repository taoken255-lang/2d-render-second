from setuptools import setup, Extension
from Cython.Build import cythonize
import numpy as np
import os

# Get the directory of this setup.py file
blend_dir = os.path.dirname(os.path.abspath(__file__))

extensions = [
    Extension(
        name="blend",  # Just 'blend' since we're already in the package directory
        sources=[
            os.path.join(blend_dir, "blend.pyx"),
            os.path.join(blend_dir, "blend_impl.c"),
        ],
        include_dirs=[np.get_include(), blend_dir],
        extra_compile_args=["-O3", "-std=c99", "-ffast-math"],
        language="c",
    )
]

setup(
    name="blend",
    ext_modules=cythonize(
        extensions,
        compiler_directives={
            'language_level': "3",
            'embedsignature': True,
        }
    ),
)

