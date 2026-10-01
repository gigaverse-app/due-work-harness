"""Legacy imports, isolated from the canonical pytest_obligation implementation.

Aliases resolve lazily so importing this package never eagerly imports optional
framework adapters. Each old submodule points to the canonical module object:
contexts, registries, monkeypatches and class identities cannot diverge.
"""

import importlib
import importlib.abc
import importlib.util
import sys
from collections.abc import Sequence
from importlib.machinery import ModuleSpec
from types import CodeType, ModuleType

import pytest_obligation as _canonical
from pytest_obligation import *  # noqa: F403
from pytest_obligation import ObligationContract as DueWorkContract
from pytest_obligation import ObligationContractDesignError as DueWorkContractDesignError
from pytest_obligation import __version__ as __version__

__all__ = [*_canonical.__all__, "DueWorkContract", "DueWorkContractDesignError"]


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, canonical_name: str) -> None:
        self.canonical_name = canonical_name
        self.canonical_spec: ModuleSpec | None = None

    def create_module(self, spec: ModuleSpec) -> ModuleType:
        module = importlib.import_module(self.canonical_name)
        self.canonical_spec = module.__spec__
        # Old pickle/import paths resolve to actual renamed classes, not subclasses.
        for legacy_name, canonical_name in (
            ("DueWorkContract", "ObligationContract"),
            ("DueWorkContractDesignError", "ObligationContractDesignError"),
        ):
            if canonical_name in module.__dict__:
                module.__dict__[legacy_name] = module.__dict__[canonical_name]
        return module

    def exec_module(self, module: ModuleType) -> None:
        # Import machinery overwrites __spec__ with the alias spec. Restore the
        # canonical identity so relative imports, introspection and reload stay sound.
        assert self.canonical_spec is not None
        module.__spec__ = self.canonical_spec

    def get_code(self, fullname: str) -> CodeType | None:
        # Preserve `python -m due_work_harness.coverage.cli` as well as imports.
        spec = importlib.util.find_spec(self.canonical_name)
        assert spec is not None and isinstance(spec.loader, importlib.abc.InspectLoader)
        return spec.loader.get_code(self.canonical_name)


class _AliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(
        self, fullname: str, path: Sequence[str] | None = None, target: ModuleType | None = None
    ) -> ModuleSpec | None:
        if not fullname.startswith("due_work_harness."):
            return None
        canonical_name = "pytest_obligation" + fullname.removeprefix("due_work_harness")
        spec = importlib.util.find_spec(canonical_name)
        if spec is None:
            return None
        return importlib.util.spec_from_loader(
            fullname, _AliasLoader(canonical_name), is_package=spec.submodule_search_locations is not None
        )


sys.meta_path.insert(0, _AliasFinder())
