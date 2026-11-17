"""Extensible bounded provider registry; a probe never transmits RF."""
from __future__ import annotations

from collections.abc import Iterable

from .config import RadioConfig
from .providers import RadioProvider, default_providers
from .types import ProviderResult, RadioDevice


class RadioDiscovery:
    def __init__(self, config: RadioConfig | None = None,
                 providers: Iterable[RadioProvider] | None = None):
        self.config = config or RadioConfig()
        self.providers = tuple(providers) if providers is not None else default_providers(self.config)
        names = [p.name for p in self.providers]
        if len(set(names)) != len(names):
            raise ValueError("Radio providers must have distinct names")

    def scan(self) -> tuple[ProviderResult, ...]:
        if not self.config.enabled:
            return ()
        results = []
        remaining = self.config.maxDevices
        for provider in self.providers:
            if provider.name in self.config.excludedProviders:
                continue
            try:
                found = provider.scan()
                if not isinstance(found, list):
                    raise TypeError("Radio provider must return a list")
                if len(found) > remaining:
                    # Do not treat an incomplete inventory as proof of unplug.
                    raise OverflowError("Radio discovery exceeded configured capacity")
                if any(not isinstance(d, RadioDevice) or d.provider != provider.name for d in found):
                    raise ValueError("Radio provider supplied an invalid device")
                if len({d.id for d in found}) != len(found):
                    raise ValueError("Radio provider supplied duplicate device identities")
                results.append(ProviderResult(provider.name, "complete", tuple(found)))
                remaining -= len(found)
            except Exception as exc:
                # Record only the exception class; firmware strings/serials might
                # contain secrets and must not enter the shared ledger as errors.
                results.append(ProviderResult(provider.name, "error", errorType=type(exc).__name__))
        return tuple(results)
