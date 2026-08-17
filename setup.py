from setuptools import setup, find_packages

setup(
    name="power-monitor",
    version="1.1.0",
    description="Cross-platform CPU energy monitoring (Linux RAPL + Windows EMI) with SQLite logging and matplotlib graphs",
    author="Matthew Doyle",
    license="MIT",
    packages=find_packages(),
    install_requires=[
        "matplotlib>=3.7",
        "numpy>=1.24",
    ],
    entry_points={
        "console_scripts": [
            "power-monitor=power_monitor.cli:main",
            "power-monitor-collector=power_monitor.collector:main",
        ],
    },
    python_requires=">=3.10",
)
