from importlib.metadata import PackageNotFoundError, version

DISTRIBUTION = "vodafone-cli"


def package_version() -> str:
    """The installed distribution's version, or "" when running from a source tree that isn't installed."""
    try:
        return version(DISTRIBUTION)
    except PackageNotFoundError:
        return ""
