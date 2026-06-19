"""Standalone smoke test: the shared oraclous_ohm package imports and exposes its core types."""


def test_core_types_importable() -> None:
    from oraclous_ohm.manifest import OHMActor, OHMManifest

    assert OHMManifest.__name__ == "OHMManifest"
    assert OHMActor.__name__ == "OHMActor"


def test_submodules_importable() -> None:
    import oraclous_ohm.canonical  # noqa: F401
    import oraclous_ohm.parse  # noqa: F401
    import oraclous_ohm.references  # noqa: F401
    import oraclous_ohm.signatures  # noqa: F401
