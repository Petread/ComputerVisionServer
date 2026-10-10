"""Model-specific Basler GenICam settings shared by all Pylon runtimes.

The GenICam standard deliberately permits camera families to expose different
node names and units for the same physical setting.  Keep those differences in
one small, declarative registry instead of scattering model checks across the
server and Edge implementations.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BaslerGainSetting:
    """A gain node write resolved for a known camera family."""

    node_names: tuple[str, ...]
    value: float | int
    selector: str | None = None
    profile_name: str | None = None


@dataclass(frozen=True)
class _BaslerGainProfile:
    name: str
    model_names: tuple[str, ...]
    analog_gain_db_by_raw: tuple[float, ...]

    def setting_for_db(self, gain_db: float) -> BaslerGainSetting:
        raw_value = min(
            range(len(self.analog_gain_db_by_raw)),
            key=lambda raw: abs(self.analog_gain_db_by_raw[raw] - gain_db),
        )
        return BaslerGainSetting(
            node_names=("GainRaw",),
            value=raw_value,
            selector="AnalogAll",
            profile_name=self.name,
        )


# ace Classic acA1300-60g[m|c]: Basler documents the analog GainRaw steps as
# 0, 3.5, 6, and 9.5 dB.  Values in the common gain_db field are therefore
# resolved to the closest reproducible hardware step.  Higher or custom gain
# settings can be supplied explicitly as gain_raw plus features/GainSelector.
_BASLER_GAIN_PROFILES = (
    _BaslerGainProfile(
        name="ace_classic_aca1300_60g",
        model_names=("aca1300-60gm", "aca1300-60gc", "aca1300-60gmnir"),
        analog_gain_db_by_raw=(0.0, 3.5, 6.0, 9.5),
    ),
)


_BASLER_MONO_MODELS = frozenset({"aca1300-60gm", "aca1300-60gmnir"})


def basler_pixel_format(model: object, configured_pixel_format: object) -> str:
    """Resolve a safe pixel format for a known monochrome camera.

    ``BayerRG8`` is the historical channel-form default.  It is invalid for
    the monochrome ace Classic model, so only that inherited default is
    replaced; a deliberately selected format is never silently changed.
    """

    configured = str(configured_pixel_format or "").strip()
    if (
        str(model or "").strip().casefold() in _BASLER_MONO_MODELS
        and configured.casefold() == "bayerrg8"
    ):
        return "Mono8"
    return configured


def basler_gain_setting(
    model: object,
    *,
    gain_db: float | None,
    gain_raw: int | None,
) -> BaslerGainSetting | None:
    """Return the correct gain-node write for a model and configured value.

    An explicit raw value takes precedence.  Unknown models retain the
    standardized dB-node preference, while known legacy models opt into their
    documented raw-value profile.
    """

    if gain_raw is not None:
        return BaslerGainSetting(node_names=("GainRaw",), value=gain_raw)
    if gain_db is None:
        return None
    normalized_model = str(model or "").strip().casefold()
    for profile in _BASLER_GAIN_PROFILES:
        if normalized_model in profile.model_names:
            return profile.setting_for_db(gain_db)
    return BaslerGainSetting(node_names=("Gain", "GainAbs"), value=gain_db)
